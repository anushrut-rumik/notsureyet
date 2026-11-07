"""Arkiv is authoritative; SQLite is an execution cache, never a fallback."""

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values

from agent.store import Store
from agent.task_state import TERMINAL, public_state, valid_stored_public_state
from agent.vault import Vault


class ArkivError(RuntimeError):
    pass


class ArkivSignatureRequired(ArkivError):
    """A checkpoint is prepared locally, but is not stored on Arkiv yet."""


def initialize(config):
    vault = Vault(config.data_dir)
    return {
        "network": "Rabdi",
        "chain_id": 7794095,
        "writer": "Selected from each chat's active wallet; use /wallet create or /wallet connect",
        "backup": [str(vault.path), str(config.data_dir / "wallet-*.json"), str(config.data_dir / "arkiv-scopes.json")],
        "funding": "Send Rabdi tGLM to your active wallet address. No separate task writer is created.",
    }


class Bridge:
    def __init__(self, config, vault):
        self.config, self.vault = config, vault
        self.path = config.data_dir / "arkiv-scopes.json"
        self.registry = json.loads(self.path.read_text()) if self.path.exists() else {"scopes": {}, "tasks": {}}
        self.signature_link = None
        # Existing application-owned records remain readable. New tasks never use this signer.
        self.instance = hmac.new(vault.key, b"ira-instance-v1", hashlib.sha256).hexdigest()
        legacy = config.data_dir / "arkiv-writer.json"
        self.legacy = (
            {
                "writer": json.loads(legacy.read_text())["address"],
                "instance": self.instance,
                "user_id": None,
                "kind": "legacy",
            }
            if legacy.exists()
            else None
        )
        self.script = Path(__file__).resolve().parent.parent / "integrations" / "arkiv.mjs"
        self.lock = open(config.data_dir / "arkiv.lock", "a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ArkivError("Another process holds this Arkiv writer. Stop it before starting this agent.") from None

    def _save(self):
        self.save_json(self.path, self.registry)

    @staticmethod
    def save_json(path, value):
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def active(self, user, source=None):
        name = hashlib.sha256(user.encode()).hexdigest()
        preference = self.config.data_dir / f"arkiv-wallet-{name}.json"
        if source is None:
            source = json.loads(preference.read_text())["source"] if preference.exists() else "active"
        choices = {"active": ("metamask", "wallet"), "agent": ("wallet",), "connected": ("metamask",)}
        if source not in choices:
            raise ArkivError("Choose active, agent, or connected for the Arkiv storage wallet")
        for prefix in choices[source]:
            path = self.config.data_dir / f"{prefix}-{name}.json"
            if path.exists():
                wallet = json.loads(path.read_text())
                if wallet["user_id"] != user:
                    raise ArkivError("Wallet identity does not match this chat")
                if source == "agent" and wallet["kind"] != "local":
                    raise ArkivError("An agent-created signing wallet is required for automatic storage")
                return wallet
        raise ArkivError("Create or connect a wallet in this chat before saving tasks.")

    def select_wallet(self, user, source):
        wallet = self.active(user, source)
        name = hashlib.sha256(user.encode()).hexdigest()
        self.save_json(self.config.data_dir / f"arkiv-wallet-{name}.json", {"source": source})
        scope = self.scope(user)
        if scope["writer"].lower() != wallet["address"].lower():
            raise ArkivError("Storage wallet selection did not match the requested wallet")
        return scope

    def scope(self, user):
        wallet = self.active(user)
        identity = f"{user}:{wallet['address'].lower()}"
        instance = hmac.new(self.vault.key, identity.encode(), hashlib.sha256).hexdigest()
        scope = {"user_id": user, "writer": wallet["address"], "instance": instance, "kind": wallet["kind"]}
        if self.registry["scopes"].get(instance) != scope:
            self.registry["scopes"][instance] = scope
            self._save()
        return scope

    def scope_for_task(self, user, task):
        current = self.scope(user)
        instance = self.registry["tasks"].get(task)
        if instance and instance != current["instance"]:
            raise ArkivError("This task belongs to a previously selected wallet. Reconnect that wallet to continue it.")
        if not instance:
            self.registry["tasks"][task] = current["instance"]
            self._save()
        return current

    def task_active(self, user, task):
        try:
            return self.registry["tasks"].get(task) in {None, self.scope(user)["instance"]}
        except ArkivError:
            return False

    def scopes(self):
        for prefix in ("wallet", "metamask"):
            for path in self.config.data_dir.glob(f"{prefix}-*.json"):
                self.scope(json.loads(path.read_text())["user_id"])
        return [s for s in self.registry["scopes"].values() if s["kind"] != "legacy"] + (
            [self.legacy] if self.legacy else []
        )

    def journal_path(self, scope):
        if scope["kind"] == "legacy":
            return self.config.data_dir / "arkiv-send.json"
        return self.config.data_dir / f"arkiv-send-{scope['writer'].lower()}.json"

    def journal(self, scope):
        path = self.journal_path(scope)
        return json.loads(path.read_text()) if path.exists() else None

    def pending(self, user):
        scope = self.scope(user)
        journal = self.journal(scope)
        if journal and journal.get("state") != "verified" and journal.get("instance") == scope["instance"]:
            return journal
        return None

    def payment_reserved(self, user):
        from agent.rabdi import payment_reserved

        return payment_reserved(self.config, self.scope(user)["writer"])

    def pending_rows(self):
        rows = []
        seen = set()
        for scope in self.scopes():
            journal = self.journal(scope)
            if (
                journal
                and journal.get("state", "").startswith("external_")
                and journal.get("instance") == scope["instance"]
                and scope["instance"] not in seen
            ):
                seen.add(scope["instance"])
                rows.append(
                    {
                        **journal,
                        "revision": journal["revision"],
                        "pending": True,
                        "user_id": scope["user_id"],
                        "writer": scope["writer"],
                    }
                )
        return rows

    def blocked_tasks(self):
        return {row["task"] for row in self.pending_rows()}

    def signature_required(self, user):
        link = self.signature_link(user) if self.signature_link else "Request /arkiv in WhatsApp for a signing link."
        raise ArkivSignatureRequired(
            "Your task checkpoint is waiting for your MetaMask approval on Rabdi. "
            "It has not been stored on Arkiv yet. Work is paused.\n" + link
        )

    def _invoke(self, scope, command, **args):
        from agent.arkiv_custody import activity_reserved, signer_lock

        if command in {"read", "funding"}:
            return self._invoke_unlocked(scope, command, **args)
        try:
            with signer_lock(self.config, scope["writer"]):
                if activity_reserved(self.config, scope["writer"]):
                    raise ArkivError("An activity-storage transaction needs reconciliation before another Rabdi write.")
                return self._invoke_unlocked(scope, command, **args)
        except ValueError as exc:
            raise ArkivError(str(exc)) from None

    def _invoke_unlocked(self, scope, command, **args):
        if command in {"write", "prepare_external", "claim_external"}:
            from agent.rabdi import payment_reserved

            if payment_reserved(self.config, scope["writer"]):
                raise ArkivError("A Rabdi payment is awaiting its receipt. Storage writes wait for that wallet nonce.")
        request = {
            "command": command,
            "writer": scope["writer"],
            "instance": scope["instance"],
            "access_key": (
                os.environ.get("AGENT_ARKIV_ACCESS_KEY")
                or dotenv_values(self.config.root / ".env").get("AGENT_ARKIV_ACCESS_KEY")
                or self.config.env.get("AGENT_ARKIV_ACCESS_KEY")
                or ""
            ),
            "journal": str(self.journal_path(scope)),
            "max_fee_wei": str(self.config.env.get("AGENT_ARKIV_MAX_FEE_WEI") or "10000000000000000"),
            **args,
        }
        if command == "write":
            wallet = self.active(scope["user_id"])
            if wallet["kind"] != "local" or wallet["address"].lower() != scope["writer"].lower():
                raise ArkivError("The selected wallet cannot sign this checkpoint automatically")
            request["private_key"] = self.vault.open(wallet["secret"], f"wallet:{scope['user_id']}")
            if not request["private_key"].startswith("0x"):
                request["private_key"] = "0x" + request["private_key"]
        try:
            result = subprocess.run(
                ["node", str(self.script)],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=150,
                check=False,
            )
            parsed = json.loads(result.stdout)
            if not parsed["ok"]:
                raise ArkivError(parsed["error"])
            return parsed["result"]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
            raise ArkivError(
                "Arkiv bridge unavailable. Run npm ci; reconcile the send journal before retrying."
            ) from None

    def call(self, command, **args):
        user = args.pop("user_id", None)
        if command in {"read", "reconcile"}:
            scopes = self.scopes()
            if user is not None:
                # A clean observer needs the decryption key and trusted scope descriptor,
                # not a wallet signing key. Writes still require active()/scope_for_task().
                if not any(s["user_id"] == user for s in scopes):
                    self.scope(user)
                scopes = [s for s in self.scopes() if s["user_id"] in {None, user}]
            rows = []
            for scope in scopes:
                if command == "reconcile":
                    journal = self.journal(scope)
                    if (
                        not journal
                        or journal.get("state") == "verified"
                        or (journal.get("state", "").startswith("external_") and not journal.get("hash"))
                    ):
                        continue  # Nothing was submitted; no network request or signing is necessary.
                result = self._invoke(scope, command, **args)
                rows.extend(
                    {**row, "instance": scope["instance"], "writer": scope["writer"], "user_id": scope["user_id"]}
                    for row in result.get("rows", [])
                )
            return {"rows": rows} if command == "read" else {"reconciled": True}
        scope = self.scope_for_task(user, args["task"])
        if scope["kind"] == "watch":
            raise ArkivError("A watch-only address cannot sign Arkiv writes. Connect MetaMask or create a wallet.")
        if scope["kind"] == "metamask":
            return self._invoke(scope, "prepare_external", **args)
        return self._invoke(scope, command, **args)

    def status(self, user):
        scope = self.scope(user)
        return {**scope, **self._invoke(scope, "funding"), "pending": bool(self.pending(user))}

    def claim_external(self, user):
        scope = self.scope(user)
        if not self.pending(user):
            raise ArkivError("No Arkiv checkpoint is waiting for this wallet")
        return self._invoke(scope, "claim_external")

    def attach_external(self, user, tx_hash):
        return self._invoke(self.scope(user), "attach_external", tx_hash=tx_hash)

    def close(self):
        self.lock.close()


class ArkivStore(Store):
    backend = "arkiv"

    def __init__(self, path, config, bridge=None):
        super().__init__(path)
        self.vault = Vault(config.data_dir)
        self.bridge = bridge or Bridge(config, self.vault)
        self.revisions = {}
        self.digests = {}
        self.available = False
        self.receipts = {}
        self.pending_writes = {}
        self.failed_tasks = set()
        self.next_checkpoint_poll = 0
        self.staged_checkpoints = set()

    def close(self):
        self.bridge.close()
        super().close()

    def _rows(self, task=None, user_id=None, include_pending=False):
        rows = self.bridge.call(
            "read", **({"task": task} if task else {}), **({"user_id": user_id} if user_id is not None else {})
        )["rows"]
        if include_pending and isinstance(self.bridge, Bridge):
            rows += self.bridge.pending_rows()
        latest = {}
        for row in rows:
            task_id, revision = row["task"], int(row["revision"])
            prior = latest.get(task_id)
            if prior and revision == int(prior["revision"]) and row["encrypted"] != prior["encrypted"]:
                raise ArkivError("Conflicting task revisions; recovery stopped")
            # The SDK resolves v2 heads. Legacy/fake bridges still need terminal-aware resolution.
            if prior:
                previous = self.vault.open(
                    prior["encrypted"], f"{prior.get('instance', self.bridge.instance)}:{task_id}:{prior['revision']}"
                )
                candidate = self.vault.open(
                    row["encrypted"], f"{row.get('instance', self.bridge.instance)}:{task_id}:{row['revision']}"
                )
                if previous["task"]["status"] in TERMINAL and candidate["task"]["status"] not in TERMINAL:
                    continue
                if candidate["task"]["status"] in TERMINAL and previous["task"]["status"] not in TERMINAL:
                    latest[task_id] = row
                    continue
            if prior is None or revision > int(prior["revision"]):
                latest[task_id] = row
        decoded = []
        for row in latest.values():
            instance = row.get("instance", self.bridge.instance)
            data = self.vault.open(row["encrypted"], f"{instance}:{row['task']}:{row['revision']}")
            expected_fields = {column[1] for column in self.db.execute("PRAGMA table_info(tasks)")}
            if (
                data.get("version") not in {1, 2}
                or data["task"]["id"] != row["task"]
                or set(data["task"]) != expected_fields
            ):
                raise ArkivError("Invalid task checkpoint; recovery stopped")
            if data.get("version") == 2 and (
                not valid_stored_public_state(data) or row.get("public_state") != data["public_state"]
            ):
                raise ArkivError("Public task state differs from encrypted checkpoint")
            if row.get("user_id") is not None and data["task"]["user_id"] != row["user_id"]:
                raise ArkivError("Checkpoint belongs to a different chat")
            if isinstance(self.bridge, Bridge):
                bound = self.bridge.registry["tasks"].get(row["task"])
                if bound and bound != instance:
                    raise ArkivError("Task wallet changed unexpectedly; recovery stopped")
                # Legacy records retain their identity and cannot silently move to a new wallet.
                self.bridge.registry["tasks"][row["task"]] = instance
            data["arkiv"] = {k: v for k, v in row.items() if k != "encrypted"}
            decoded.append(data)
        return decoded

    def tasks(self, user_id, *, status=None, protocol=None):
        # Every user-visible list performs a real Arkiv query. Do not return the local cache on failure.
        return sorted(
            [
                {**item["task"], "arkiv": item["arkiv"]}
                for item in self._rows(user_id=user_id)
                if item["task"]["user_id"] == user_id
                and (status is None or item["task"]["status"] == status)
                and (protocol is None or public_state(item)["protocol"] == protocol)
            ],
            key=lambda task: task["created_at"],
            reverse=True,
        )

    def progress(self, user_id, task_id):
        for item in self._rows(task=task_id, user_id=user_id):
            if item["task"]["user_id"] == user_id:
                return {
                    "task": item["task"],
                    "public_state": public_state(item),
                    "events": item["events"],
                    "arkiv": item["arkiv"],
                }
        raise ValueError("No stored task for this user")

    def recover(self):
        self.available = False
        self.bridge.call("reconcile")
        snapshots = self._rows(include_pending=True)
        self.revisions.clear()
        self.digests.clear()
        self.failed_tasks.clear()
        if isinstance(self.bridge, Bridge):
            self.bridge._save()
        # Keep a private copy of the pre-Arkiv database. Legacy tasks need explicit migration.
        backup = Path(self.db.execute("PRAGMA database_list").fetchone()[2]).parent / "pre-arkiv.sqlite3"
        if not backup.exists():
            with sqlite3.connect(backup) as destination:
                self.db.backup(destination)
            backup.chmod(0o600)
        with self.db:
            # This execution mirror is reconstructed, including uncertain transfer/effect references.
            for table in ("tasks", "effects", "transfers"):
                self.db.execute(f"DELETE FROM {table}")
            self.db.execute("DELETE FROM messages WHERE scope LIKE 'task:%'")
            self.db.execute("DELETE FROM events WHERE task_id IS NOT NULL")
            for item in snapshots:
                task = item["task"]
                fields = list(task)
                self.db.execute(
                    f"INSERT INTO tasks({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                    list(task.values()),
                )
                for event in item["events"]:
                    self.db.execute(
                        "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                        (
                            event["id"],
                            event["user_id"],
                            event["task_id"],
                            event["kind"],
                            event["source"],
                            json.dumps(event["data"]),
                            event["at"],
                        ),
                    )
                for message in item["messages"]:
                    self.db.execute(
                        "INSERT INTO messages(scope,data) VALUES(?,?)", (f"task:{task['id']}", json.dumps(message))
                    )
                for effect in item["effects"]:
                    self.db.execute(
                        "INSERT INTO effects VALUES(?,?,?,?,?,?,?)",
                        (
                            effect["key"],
                            effect["user_id"],
                            effect["task_id"],
                            effect["tool"],
                            effect["status"],
                            json.dumps(effect["result"]) if effect["result"] is not None else None,
                            effect["updated_at"],
                        ),
                    )
                for transfer in item.get("transfers", []):
                    self.db.execute(
                        "INSERT INTO transfers VALUES(?,?,?,?)",
                        (transfer["id"], task["user_id"], task["id"], json.dumps(transfer)),
                    )
                self.revisions[task["id"]] = int(item["arkiv"]["revision"])
                self.receipts[task["id"]] = item["arkiv"]
                original = {k: v for k, v in item.items() if k != "arkiv"}
                if not item["arkiv"].get("pending"):
                    self.digests[task["id"]] = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
        super().recover()
        # A restored submitted intent is tracked by hash, never handed back to a payment planner.
        with self.db:
            self.db.execute("""UPDATE tasks SET status='needs_input',wake_at=NULL
                WHERE id IN (SELECT task_id FROM transfers
                    WHERE json_extract(data,'$.status') IN
                    ('awaiting_approval','awaiting_signature','submitting','submitted',
                     'unknown','in_transit','allowance_confirmed'))
                AND status IN ('queued','running','waiting')""")
        self.available = True
        # A paused payment has a separate durable admission journal. Restoring its transient
        # execution status must not create a new storage approval before the payment review.
        payment_tasks = set()
        for path in self.bridge.config.data_dir.glob("payment-*.json") if isinstance(self.bridge, Bridge) else []:
            if not re.fullmatch(r"payment-[a-f0-9]{32}\.json", path.name):
                continue
            transfer_id = path.stem.removeprefix("payment-")
            envelope = json.loads(path.read_text())
            payment = self.vault.open(envelope["encrypted"], f"payment:{transfer_id}")
            if payment["status"] in {
                "awaiting_approval",
                "awaiting_signature",
                "submitting",
                "submitted",
                "unknown",
                "in_transit",
            }:
                payment_tasks.add(payment["task_id"])
        # Running tasks and pending effects were normalized; persist before any work resumes.
        for item in snapshots:
            if item["task"]["id"] in payment_tasks:
                continue
            if not item["arkiv"].get("pending"):
                if isinstance(self.bridge, Bridge) and not self.bridge.task_active(
                    item["task"]["user_id"], item["task"]["id"]
                ):
                    continue
                try:
                    self.flush_task(item["task"]["id"])
                except ArkivSignatureRequired:
                    pass  # A recovered MetaMask task must wait for its next checkpoint approval.

    def _snapshot(self, task_id):
        task = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            return None
        task = dict(task)
        effects = [
            self.effect(row[0]) for row in self.db.execute("SELECT key FROM effects WHERE task_id=?", (task_id,))
        ]
        snapshot = {
            "version": 2,
            "public_state_version": 2,
            "task": task,
            "events": self.events(task["user_id"], task_id=task_id, limit=100000),
            "messages": self.messages(f"task:{task_id}"),
            "effects": effects,
            "transfers": [
                json.loads(row[0]) for row in self.db.execute("SELECT data FROM transfers WHERE task_id=?", (task_id,))
            ],
        }
        snapshot["public_state"] = public_state(snapshot)
        return snapshot

    @contextmanager
    def stage_checkpoint(self, task_id):
        """Stage unsigned preparation locally; callers must still cross the normal signing barrier."""
        already_staged = task_id in self.staged_checkpoints
        self.staged_checkpoints.add(task_id)
        try:
            yield
        finally:
            if not already_staged:
                self.staged_checkpoints.discard(task_id)

    def flush_task(self, task_id, force=False):
        if task_id in self.staged_checkpoints:
            return
        if not self.available:
            raise ArkivError("Arkiv recovery has not completed; work is stopped")
        snapshot = self._snapshot(task_id)
        if snapshot is None:
            return
        if isinstance(self.bridge, Bridge) and self.bridge.payment_reserved(snapshot["task"]["user_id"]):
            return  # The payment admission journal protects this gap until the receipt is reconciled.
        data = json.dumps(snapshot, sort_keys=True)
        if len(data.encode()) > 85000:
            raise ArkivError("Task checkpoint exceeds 85 KB; stop and archive before continuing")
        digest = hashlib.sha256(data.encode()).hexdigest()
        if not force and self.digests.get(task_id) == digest:
            return
        revision = max(time.time_ns() // 1000, self.revisions.get(task_id, 0) + 1)
        instance = self.bridge.instance
        if isinstance(self.bridge, Bridge):
            # Reuse an existing prepared checkpoint; never replace an uncertain wallet request.
            pending = self.bridge.pending(snapshot["task"]["user_id"])
            if pending:
                return  # Frozen pending payload; async barriers wait before further execution.
            instance = self.bridge.scope_for_task(snapshot["task"]["user_id"], task_id)["instance"]
        encrypted = self.vault.seal(snapshot, f"{instance}:{task_id}:{revision}")
        try:
            receipt = self.bridge.call(
                "write",
                user_id=snapshot["task"]["user_id"],
                task=task_id,
                revision=str(revision),
                encrypted=encrypted,
                public_state=snapshot["public_state"],
                previous_revision=str(self.revisions.get(task_id, 0)),
            )
        except ArkivSignatureRequired:
            raise
        except Exception:
            if isinstance(self.bridge, Bridge):
                self.failed_tasks.add(task_id)
            else:
                self.available = False
            raise
        if receipt.get("pending"):
            self.pending_writes[task_id] = (revision, digest)
            self.notify_signature(snapshot["task"]["user_id"], task_id)
            return
        self.revisions[task_id], self.digests[task_id], self.receipts[task_id] = revision, digest, receipt

    def notify_signature(self, user, task_id):
        if not isinstance(self.bridge, Bridge) or not self.bridge.signature_link:
            return
        journal = self.bridge.pending(user)
        if journal and not journal.get("notified"):
            self.notice(
                user,
                "Approve saving task progress to Arkiv on Rabdi with your connected wallet. "
                "The checkpoint is not stored yet. Work waits for confirmation.\n" + self.bridge.signature_link(user),
            )
            journal["notified"] = True
            self.bridge.save_json(self.bridge.journal_path(self.bridge.scope(user)), journal)

    def accept_checkpoint(self, user):
        scope = self.bridge.scope(user)
        journal = self.bridge.journal(scope)
        if not journal or journal["state"] != "verified" or journal.get("instance") != scope["instance"]:
            return
        snapshot = self.vault.open(journal["encrypted"], f"{scope['instance']}:{journal['task']}:{journal['revision']}")
        task_id = journal["task"]
        self.revisions[task_id] = int(journal["revision"])
        self.digests[task_id] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        self.receipts[task_id] = journal["result"]
        self.pending_writes.pop(task_id, None)

    def reconcile_checkpoints(self, *, force=False):
        """Check already-admitted MetaMask storage hashes. Never signs, broadcasts, or prepares a retry."""
        if not self.available or not isinstance(self.bridge, Bridge):
            return []
        if not force and time.monotonic() < self.next_checkpoint_poll:
            return []
        self.next_checkpoint_poll = time.monotonic() + 15
        recovered = []
        for pending in self.bridge.pending_rows():
            if not pending.get("hash") or pending.get("state") not in {"external_claimed", "external_submitted"}:
                continue
            user = pending["user_id"]
            if not self.bridge.task_active(user, pending["task"]):
                continue
            scope = self.bridge.scope(user)
            if scope["instance"] != pending["instance"]:
                continue
            try:
                # reconcile only verifies the saved receipt and exact entity readback in the SDK bridge.
                self.bridge._invoke(scope, "reconcile")
                journal = self.bridge.journal(scope)
                if (
                    journal
                    and journal.get("state") == "verified"
                    and journal.get("hash") == pending["hash"]
                    and journal.get("operation") == pending["operation"]
                ):
                    self.accept_checkpoint(user)
                    recovered.append(pending["task"])
            except ArkivError:
                pass  # Pending, reverted or unavailable is never permission to submit another write.
        return recovered

    async def checkpoint_barrier(self, task_id):
        if not isinstance(self.bridge, Bridge):
            return
        task = self.db.execute("SELECT user_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return
        user = task["user_id"]
        if task_id in self.failed_tasks:
            raise ArkivError(
                "This task has an unconfirmed storage failure. Restore Arkiv and recover before continuing."
            )
        while True:
            while self.bridge.pending(user) or self.bridge.payment_reserved(user):
                self.reconcile_checkpoints()
                if not self.bridge.payment_reserved(user):
                    self.notify_signature(user, task_id)
                await asyncio.sleep(0.5)
            self.accept_checkpoint(user)
            self.flush_task(task_id)
            if not self.bridge.pending(user):
                return

    def maintain(self):
        """Refresh unfinished records weekly while running, before their 30-day expiry."""
        if not self.available:
            return
        for row in self.db.execute(
            "SELECT id,user_id FROM tasks WHERE status NOT IN ('completed','cancelled')"
        ).fetchall():
            if row["id"] not in self.revisions:
                continue  # An unsigned local draft has no on-chain record to renew.
            if isinstance(self.bridge, Bridge) and not self.bridge.task_active(row["user_id"], row["id"]):
                continue
            if isinstance(self.bridge, Bridge) and self.bridge.payment_reserved(row["user_id"]):
                continue
            if time.time() - self.revisions.get(row["id"], 0) / 1_000_000 > 7 * 86400:
                self.flush_task(row["id"], force=True)

    def create_task(self, *args, **kwargs):
        if not self.available:
            raise ArkivError("Arkiv is unavailable; recover before creating work")
        if isinstance(self.bridge, Bridge):
            user = args[0] if args else kwargs["user_id"]
            scope = self.bridge.scope(user)
            if scope["kind"] == "watch":
                raise ArkivError("Connect MetaMask or create an agent wallet before saving tasks.")
            if self.bridge.pending(user):
                self.bridge.signature_required(user)
        task = super().create_task(*args, **kwargs)
        self.flush_task(task["id"])
        return task

    def update_task(self, user_id, task_id, **changes):
        super().update_task(user_id, task_id, **changes)
        self.flush_task(task_id)

    def claim(self):
        if not self.available:
            return None
        if isinstance(self.bridge, Bridge):
            blocked = self.bridge.blocked_tasks()
            eligible = [
                row["id"]
                for row in self.db.execute(
                    "SELECT id,user_id FROM tasks WHERE status IN ('queued','waiting') "
                    "AND (wake_at IS NULL OR wake_at<=?) "
                    "ORDER BY updated_at",
                    (time.time(),),
                )
                if row["id"] in self.digests
                and row["id"] not in blocked
                and row["id"] not in self.failed_tasks
                and self.bridge.task_active(row["user_id"], row["id"])
                and not self.bridge.payment_reserved(row["user_id"])
            ]
            if not eligible:
                return None
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (eligible[0],)).fetchone()
            task = dict(row)
            with self.db:
                self.db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (time.time(), task["id"]))
            task = self.task(task["user_id"], task["id"])
        else:
            task = super().claim()
        if task:
            try:
                self.flush_task(task["id"])
            except ArkivSignatureRequired:
                return None
        return task

    def effect_start(self, key, user_id, task_id, tool):
        super().effect_start(key, user_id, task_id, tool)
        if task_id:
            self.flush_task(task_id)

    def effect_finish(self, key, status, result):
        super().effect_finish(key, status, result)
        effect = self.effect(key)
        if effect and effect["task_id"]:
            self.flush_task(effect["task_id"])


def open_store(config):
    if config.task_backend == "sqlite":
        return Store(config.db_path)
    return ArkivStore(config.db_path, config)
