"""Encrypted, independent lending audit mirror. No CRE execution or payment tools.

The collector reads saved state and journals. SQLite triggers retain every new
state/transaction revision, including revisions between observer polls. Only the
separate publisher talks to Arkiv. A failed upload never rolls back lending.
"""

import argparse
import base64
import fcntl
import hashlib
import hmac
import html
import json
import os
import sqlite3
import subprocess
import time
import zlib
from contextlib import contextmanager
from pathlib import Path

from agent.arkiv import Bridge
from agent.arkiv_custody import signer_lock
from agent.config import Config
from agent.vault import Vault


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def scrub(value):
    """Keep activity facts, excluding chat identity, signing material and raw calls."""
    hidden = {
        "user_id",
        "userId",
        "secret",
        "private_key",
        "key",
        "raw",
        "data",
        "signature",
        "ownerSignature",
        "report",
        "approvalEvent",
        "startRequestEvent",
        "approvalId",
        "approvalCode",
        "inputPath",
        "channel",
        "chat",
        "message_id",
    }
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in hidden}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


@contextmanager
def connect(path, *, readonly=False):
    db = sqlite3.connect(f"file:{Path(path).resolve()}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=0.1)
    db.row_factory = sqlite3.Row
    if readonly:
        db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


def install_state_capture(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS lending_activity_states(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,sealed TEXT NOT NULL,at REAL NOT NULL);
        CREATE TRIGGER IF NOT EXISTS lending_activity_insert AFTER INSERT ON lending_private BEGIN
            INSERT INTO lending_activity_states(user_id,sealed,at) VALUES(NEW.user_id,NEW.sealed,unixepoch('subsec'));
        END;
        CREATE TRIGGER IF NOT EXISTS lending_activity_update AFTER UPDATE OF sealed ON lending_private BEGIN
            INSERT INTO lending_activity_states(user_id,sealed,at) VALUES(NEW.user_id,NEW.sealed,unixepoch('subsec'));
        END;
    """)


def install_transaction_capture(db, table):
    if table not in {"txs", "actions"}:
        raise ValueError("Unknown transaction history")
    columns = "id,hash,state,intent" + (",chain,receipt" if table == "txs" else "")
    names = columns.split(",")
    fields = ",".join(f"'{n}',NEW.{n}" for n in names)
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS lending_activity_transactions(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,record TEXT NOT NULL,at REAL NOT NULL);
        CREATE TRIGGER IF NOT EXISTS lending_activity_tx_insert AFTER INSERT ON {table} BEGIN
            INSERT INTO lending_activity_transactions(record,at) VALUES(json_object({fields}),unixepoch('subsec'));
        END;
        CREATE TRIGGER IF NOT EXISTS lending_activity_tx_update AFTER UPDATE ON {table} BEGIN
            INSERT INTO lending_activity_transactions(record,at) VALUES(json_object({fields}),unixepoch('subsec'));
        END;
    """)


class ActivityLog:
    def __init__(self, config):
        self.config = config
        if not (config.data_dir / "vault.key").exists():
            raise ValueError("Existing encryption key required; restore its backup, do not replace it.")
        self.vault = Vault(config.data_dir)
        self.directory = config.data_dir / "lending-activity"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "activity.sqlite", timeout=1)
        self.db.row_factory = sqlite3.Row
        (self.directory / "activity.sqlite").chmod(0o600)
        self.db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,writer TEXT NOT NULL,scope TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS activity(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,source TEXT NOT NULL,
                event TEXT UNIQUE NOT NULL,scope TEXT NOT NULL,encrypted TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',entity_key TEXT,transaction_hash TEXT,expires_at_block TEXT,
                UNIQUE(user_id,source));
            CREATE TABLE IF NOT EXISTS cursors(source TEXT PRIMARY KEY,seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,user_id TEXT NOT NULL,events TEXT NOT NULL,state TEXT NOT NULL,
                plan_hash TEXT,maximum_cost TEXT,error TEXT);
        """)

    def close(self):
        self.db.close()

    def opaque(self, value):
        return hmac.new(self.vault.key, ("lending-activity-v1:" + value).encode(), hashlib.sha256).hexdigest()

    def seal_content(self, content, scope, event):
        packed = base64.b64encode(zlib.compress(canonical(content).encode(), level=6)).decode()
        return self.vault.seal(
            {"encoding": "zlib-json-v1", "body": packed}, f"lending-activity:7794095:{scope}:{event}"
        )

    def open_content(self, encrypted, scope, event):
        value = self.vault.open(encrypted, f"lending-activity:7794095:{scope}:{event}")
        if value.get("encoding") != "zlib-json-v1":
            return value  # Original uncompressed publications remain readable.
        try:
            inflater = zlib.decompressobj()
            raw = inflater.decompress(base64.b64decode(value["body"], validate=True), 100001)
            if len(raw) > 100000 or not inflater.eof or inflater.unconsumed_tail or inflater.unused_data:
                raise ValueError
            return json.loads(raw)
        except Exception:
            raise ValueError("Invalid compressed activity; no plaintext fallback.") from None

    def compact_pending(self):
        # Never rewrite ciphertext already bound into any prepared/submitted plan.
        admitted = {row["event"] for (job,) in self.db.execute("SELECT events FROM jobs") for row in json.loads(job)}
        rows = self.db.execute("SELECT event,scope,encrypted FROM activity WHERE state='pending'").fetchall()
        with self.db:
            for row in rows:
                if row["event"] in admitted:
                    continue
                value = self.vault.open(row["encrypted"], f"lending-activity:7794095:{row['scope']}:{row['event']}")
                if value.get("encoding") == "zlib-json-v1":
                    continue
                encrypted = self.seal_content(value, row["scope"], row["event"])
                self.db.execute(
                    "UPDATE activity SET encrypted=? WHERE event=? AND state='pending'", (encrypted, row["event"])
                )

    def register(self, user, writer):
        scope = self.opaque(user + ":" + writer.lower())
        old = self.db.execute("SELECT * FROM users WHERE user_id=?", (user,)).fetchone()
        if old and old["writer"].lower() != writer.lower():
            raise ValueError("Activity wallet changed; retain the original publisher binding.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users VALUES(?,?,?)", (user, writer, scope))
        return scope

    def append(self, user, source, kind, value, at=None):
        if self.cursor("captured:" + user + ":" + source):
            return
        if self.db.execute("SELECT 1 FROM activity WHERE user_id=? AND source=?", (user, source)).fetchone():
            return
        row = self.db.execute("SELECT scope FROM users WHERE user_id=?", (user,)).fetchone()
        if not row:
            raise ValueError("Unregistered activity owner")
        scope = row[0]
        payload = {"version": 1, "kind": kind, "recorded_at": at, "activity": scrub(value)}
        # Chunk unusually large snapshots before encryption; no activity is silently truncated.
        text = canonical(payload)
        chunks = [text[i : i + 20000] for i in range(0, len(text), 20000)]
        with self.db:
            for index, chunk in enumerate(chunks):
                identity = source if len(chunks) == 1 else f"{source}:part:{index}"
                event = self.opaque(scope + ":" + identity)
                content = (
                    payload
                    if len(chunks) == 1
                    else {
                        "version": 1,
                        "kind": "activity_chunk",
                        "part": index,
                        "parts": len(chunks),
                        "group": self.opaque(scope + ":" + source),
                        "text": chunk,
                    }
                )
                sealed = self.seal_content(content, scope, event)
                self.db.execute(
                    "INSERT OR IGNORE INTO activity(user_id,source,event,scope,encrypted) VALUES(?,?,?,?,?)",
                    (user, identity, event, scope, sealed),
                )
            # Multi-part source marker prevents re-encryption of already collected snapshots.
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,1)", ("captured:" + user + ":" + source,))

    def cursor(self, source):
        row = self.db.execute("SELECT seq FROM cursors WHERE source=?", (source,)).fetchone()
        return row[0] if row else 0

    def advance(self, source, seq):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (source, seq))

    def collect(self):
        # Short database snapshots only. No source connection is held during Arkiv requests.
        with connect(self.config.db_path) as source:
            install_state_capture(source)
            users = source.execute(
                "SELECT l.user_id,w.address FROM lending_private l JOIN wallets w "
                "ON w.user_id=l.user_id WHERE w.kind='local'"
            ).fetchall()
            for user, writer in users:
                self.register(user, writer)
                initial = source.execute("SELECT sealed FROM lending_private WHERE user_id=?", (user,)).fetchone()
                if not self.cursor("baseline:" + user):
                    value = self.vault.open(initial[0], "lending:" + user)
                    self.append(user, "baseline", "management_snapshot_at_integration", value, time.time())
                    self.advance("baseline:" + user, 1)
            rows = source.execute(
                "SELECT * FROM lending_activity_states WHERE seq>? ORDER BY seq LIMIT 200", (self.cursor("states"),)
            ).fetchall()
            for row in rows:
                if self.db.execute("SELECT 1 FROM users WHERE user_id=?", (row["user_id"],)).fetchone():
                    self.append(
                        row["user_id"],
                        f"state:{row['seq']}",
                        "management_state",
                        self.vault.open(row["sealed"], "lending:" + row["user_id"]),
                        row["at"],
                    )
                self.advance("states", row["seq"])
            events = source.execute(
                "SELECT * FROM events WHERE id>? AND kind='lending_update' ORDER BY id LIMIT 200",
                (self.cursor("events"),),
            ).fetchall()
            for row in events:
                if self.db.execute("SELECT 1 FROM users WHERE user_id=?", (row["user_id"],)).fetchone():
                    self.append(
                        row["user_id"], f"event:{row['id']}", "lending_update", json.loads(row["data"]), row["at"]
                    )
                self.advance("events", row["id"])
        for user, _ in users:
            name = hashlib.sha256(user.encode()).hexdigest()
            live = self.config.data_dir / "lending-live" / name
            operations = live / "operations.sqlite"
            if operations.exists():
                with connect(operations, readonly=True) as db:
                    cursor = "operations:" + user
                    rows = db.execute(
                        "SELECT h.seq,h.id,h.state,h.record,o.immutable FROM history h "
                        "JOIN operations o ON h.id=o.id WHERE h.seq>? ORDER BY h.seq LIMIT 200",
                        (self.cursor(cursor),),
                    ).fetchall()
                for row in rows:
                    immutable = json.loads(row["immutable"])
                    details = json.loads(row["record"])
                    self.append(
                        user,
                        f"operation:{row['seq']}",
                        "operation_stage",
                        {
                            "operation_id": row["id"],
                            "state": row["state"],
                            "details": details,
                            "instruction": immutable.get("instruction"),
                        },
                        details.get("updatedAt") or details.get("createdAt"),
                    )
                    self.advance(cursor, row["seq"])
            for label, path, table in [
                ("wallet", live / "transactions.sqlite", "txs"),
                ("setup", self.config.data_dir / "lending-setup" / name / "setup.sqlite", "actions"),
            ]:
                if not path.exists():
                    continue
                with connect(path) as db:
                    install_transaction_capture(db, table)
                    fields = "id,hash,state,intent" + (",chain,receipt" if table == "txs" else "")
                    baseline = db.execute(f"SELECT {fields} FROM {table}").fetchall()
                    cursor = label + ":" + user
                    rows = db.execute(
                        "SELECT * FROM lending_activity_transactions WHERE seq>? ORDER BY seq LIMIT 200",
                        (self.cursor(cursor),),
                    ).fetchall()
                for row in baseline:
                    self.append(
                        user,
                        f"{label}:initial:{row['id']}",
                        label + "_transaction_snapshot",
                        self.transaction(dict(row)),
                    )
                for row in rows:
                    self.append(
                        user,
                        f"{label}:revision:{row['seq']}",
                        label + "_transaction",
                        self.transaction(json.loads(row["record"])),
                        row["at"],
                    )
                    self.advance(cursor, row["seq"])
        self.compact_pending()
        return self.summary()

    @staticmethod
    def transaction(row):
        intent = json.loads(row.pop("intent"))
        # A successful relay receipt alone does not prove a token transfer/deposit.
        calldata = intent.pop("data", "")
        row["function_selector"] = calldata[:10] if calldata else None
        row["intent"] = intent
        if row.get("receipt"):
            row["receipt"] = json.loads(row["receipt"])
        row["evidence"] = "saved application transaction status, not an independent destination-deposit proof"
        return row

    def invoke(self, payload):
        request = {"access_key": self.config.env.get("AGENT_ARKIV_ACCESS_KEY") or "", **payload}
        env = {**os.environ, "DEBUG": ""}
        try:
            result = subprocess.run(
                ["node", str(self.config.root / "integrations/lending-activity.mjs")],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=180,
                cwd=self.config.root,
                env=env,
                check=False,
            )
            parsed = json.loads(result.stdout)
            if not parsed["ok"]:
                raise ValueError("Activity storage is pending; reconcile its saved journal.")
            return parsed["result"]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
            raise ValueError("Activity storage is unavailable; saved history remains queued.") from None

    def sync(self, user):
        from agent.rabdi import payment_reserved

        scope = self.db.execute("SELECT * FROM users WHERE user_id=?", (user,)).fetchone()
        if not scope:
            raise ValueError("No captured lending history for this user")
        writer = scope["writer"]
        with signer_lock(self.config, writer, wait=True):
            task_journal = self.config.data_dir / f"arkiv-send-{writer.lower()}.json"
            if task_journal.exists() and json.loads(task_journal.read_text()).get("state") != "verified":
                return {"state": "waiting_for_task_storage"}
            if payment_reserved(self.config, writer):
                return {"state": "waiting_for_rabdi_payment"}
            job = self.db.execute(
                "SELECT * FROM jobs WHERE user_id=? AND state!='verified' ORDER BY rowid LIMIT 1", (user,)
            ).fetchone()
            if not job:
                rows = self.db.execute(
                    "SELECT event,scope,encrypted FROM activity WHERE user_id=? AND state='pending' "
                    "ORDER BY seq LIMIT 20",
                    (user,),
                ).fetchall()
                if not rows:
                    return {"state": "caught_up"}
                # Small events share a batch; large snapshots keep the exact gas
                # estimate comfortably inside the existing per-write fee cap.
                bounded, size = [], 0
                for row in rows:
                    if bounded and size + len(row["encrypted"]) > 40000:
                        break
                    bounded.append(row)
                    size += len(row["encrypted"])
                rows = bounded
                identity = hashlib.sha256(canonical([r["event"] for r in rows]).encode()).hexdigest()
                with self.db:
                    self.db.execute(
                        "INSERT INTO jobs VALUES(?,?,?,'created',NULL,NULL,NULL)",
                        (identity, user, json.dumps([dict(r) for r in rows])),
                    )
                job = self.db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            reservation = self.directory / f"reservation-{writer.lower()}.json"
            with connect(self.config.db_path, readonly=True) as db:
                wallet = db.execute("SELECT address,kind,secret FROM wallets WHERE user_id=?", (user,)).fetchone()
            if not wallet or wallet["kind"] != "local" or wallet["address"].lower() != writer.lower():
                raise ValueError("Original authorized activity wallet is unavailable")
            signing_key = "0x" + self.vault.open(wallet["secret"], "wallet:" + user).removeprefix("0x")
            directory = self.directory / "jobs" / job["id"]
            payload = {"writer": writer, "private_key": signing_key, "directory": str(directory)}
            try:
                prepared_path = directory / "prepared.json"
                prepared = json.loads(prepared_path.read_text()) if prepared_path.exists() else None
                if not job["plan_hash"]:
                    prepared = prepared or self.invoke(
                        {
                            **payload,
                            "command": "prepare",
                            "job_id": job["id"],
                            "rows": json.loads(job["events"]),
                            "max_fee_wei": self.config.env.get("AGENT_ARKIV_MAX_FEE_WEI") or "10000000000000000",
                        }
                    )
                    with self.db:
                        self.db.execute(
                            "UPDATE jobs SET state='prepared',plan_hash=?,maximum_cost=? WHERE id=?",
                            (prepared["planSha256"], prepared["maximumCost"], job["id"]),
                        )
                plan_hash = job["plan_hash"] or prepared["planSha256"]
                Bridge.save_json(reservation, {"state": "pending", "job": job["id"]})
                result = self.invoke({**payload, "command": "publish", "plan_hash": plan_hash})
                if result["state"] != "complete":
                    return result
                with self.db:
                    for row in result["rows"]:
                        self.db.execute(
                            "UPDATE activity SET state='verified',entity_key=?,transaction_hash=?,expires_at_block=? "
                            "WHERE user_id=? AND event=?",
                            (row["entity_key"], row["transaction_hash"], row["expires_at_block"], user, row["event"]),
                        )
                    self.db.execute("UPDATE jobs SET state='verified',error=NULL WHERE id=?", (job["id"],))
                Bridge.save_json(reservation, {"state": "verified", "job": job["id"]})
                return {"state": "verified", "records": len(result["rows"])}
            except Exception:
                with self.db:
                    self.db.execute("UPDATE jobs SET error='storage_pending' WHERE id=?", (job["id"],))
                raise
            finally:
                payload.pop("private_key", None)
                signing_key = None

    def summary(self, user=None):
        clause, args = (" WHERE user_id=?", (user,)) if user else ("", ())
        counts = dict(self.db.execute("SELECT state,count(*) FROM activity" + clause + " GROUP BY state", args))
        last = self.db.execute(
            "SELECT entity_key,transaction_hash FROM activity"
            + (" WHERE user_id=? AND" if user else " WHERE")
            + " state='verified' ORDER BY seq DESC LIMIT 1",
            args,
        ).fetchone()
        return {
            "network": "Arkiv Rabdi",
            "encrypted": True,
            "captured": sum(counts.values()),
            "verified": counts.get("verified", 0),
            "pending": counts.get("pending", 0),
            "latest": dict(last) if last else None,
            "retention_days": 30,
            "scope": "lending state revisions, updates, operation stages, setup and wallet transactions",
        }

    def local_history(self, user, limit=30, before_sequence=None):
        before = " AND seq<?" if before_sequence is not None else ""
        params = [user, *([int(before_sequence)] if before_sequence is not None else []), max(1, min(int(limit), 200))]
        rows = self.db.execute(
            "SELECT * FROM activity WHERE user_id=?" + before + " ORDER BY seq DESC LIMIT ?", params
        ).fetchall()
        return [
            {
                "sequence": r["seq"],
                "storage": r["state"],
                "entity_key": r["entity_key"],
                "transaction_hash": r["transaction_hash"],
                "details": self.open_content(r["encrypted"], r["scope"], r["event"]),
            }
            for r in rows
        ]

    def remote_history(self, user, limit=30, before_sequence=None):
        scope = self.db.execute("SELECT * FROM users WHERE user_id=?", (user,)).fetchone()
        if not scope:
            raise ValueError("No activity publisher bound to this user")
        expected = None
        if limit is not None:
            before = " AND seq<?" if before_sequence is not None else ""
            params = [
                user,
                *([int(before_sequence)] if before_sequence is not None else []),
                max(1, min(int(limit), 200)),
            ]
            expected = dict(
                self.db.execute(
                    "SELECT event,seq FROM activity WHERE user_id=? AND state='verified'"
                    + before
                    + " ORDER BY seq DESC LIMIT ?",
                    params,
                )
            )
            if not expected:
                return []
        result, after = [], None
        for _ in range(100):
            page = self.invoke(
                {
                    "command": "read",
                    "writer": scope["writer"],
                    "scope": scope["scope"],
                    "after": after,
                    **({"events": list(expected)} if expected is not None else {}),
                }
            )["rows"]
            for row in page:
                details = self.open_content(row["encrypted"], scope["scope"], row["event"])
                if details.get("version") != 1 or "kind" not in details:
                    raise ValueError("Invalid decrypted activity record")
                result.append({"entity_key": row["entity_key"], "event": row["event"], "details": details})
            if len(page) < 100:
                if expected is not None:
                    if {r["event"] for r in result} != set(expected):
                        raise ValueError("Some previously stored records are absent or expired on Arkiv.")
                    return sorted(result, key=lambda r: expected[r["event"]], reverse=True)
                return result
            after = page[-1]["entity_key"]
        raise ValueError("Activity page limit reached; refine the history query")

    def render(self):
        sections = []
        for (user,) in self.db.execute("SELECT user_id FROM users"):
            summary = self.summary(user)
            sections.append(f"<h2>{summary['verified']} verified on Arkiv · {summary['pending']} pending</h2>")
            records, before = [], None
            while True:
                page = self.local_history(user, 200, before)
                records.extend(page)
                if len(page) < 200:
                    break
                before = page[-1]["sequence"]
            groups = {}
            for item in records:
                part = item["details"]
                if part["kind"] == "activity_chunk":
                    groups.setdefault(part["group"], []).append(part)
            restored = {}
            for group, parts in groups.items():
                parts.sort(key=lambda p: p["part"])
                if [p["part"] for p in parts] == list(range(parts[0]["parts"])):
                    restored[group] = json.loads("".join(p["text"] for p in parts))
            shown = set()
            for item in records:
                details = item["details"]
                part = details if details["kind"] == "activity_chunk" else None
                details = restored.get(part["group"], details) if part else details
                activity = details.get("activity", {})
                label = activity.get("state") or activity.get("event") or (activity.get("plan") or {}).get("status")
                label = f" · {label}" if label else ""
                if part:
                    label += f" · encrypted part {part['part'] + 1}/{part['parts']}"
                    if part["group"] in shown:
                        details = {"note": "Another encrypted part of the same activity shown above."}
                    shown.add(part["group"])
                link = (
                    f"<a href='https://rabdi.explorer.arkiv.network/tx/{item['transaction_hash']}'>"
                    "Arkiv transaction</a>"
                    if item["transaction_hash"]
                    else "waiting to sync"
                )
                sections.append(
                    f"<details><summary>#{item['sequence']} · "
                    f"{html.escape(details.get('kind', 'activity_continuation') + label)} · "
                    f"{item['storage']} · {link}</summary>"
                    f"<pre>{html.escape(json.dumps(details, indent=2))}</pre></details>"
                )
        target = self.directory / "timeline.html"
        # The report contains decrypted details, so restrict access before writing.
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
        target.write_text(
            "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=10>"
            "<title>Encrypted lending activity</title><style>body{max-width:1050px;margin:40px auto;"
            "font:16px system-ui;background:#f6f5f1;color:#222}details{padding:14px;border-bottom:1px solid #ccc}"
            "summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}</style>"
            "<h1>Lending activity</h1><p>Decrypted locally. Activity details stored on Arkiv are encrypted. "
            "Storage confirmation does not change the recorded execution outcome. "
            "All captured records are listed below.</p>" + "".join(sections)
        )
        target.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "sync", "watch", "status", "read"])
    parser.add_argument("--user")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--before-sequence", type=int)
    args = parser.parse_args()
    log = ActivityLog(Config())
    try:
        if args.command == "status":
            print(json.dumps(log.summary(args.user)))
            return
        if args.command == "read":
            if not args.user:
                parser.error("--user is required")
            read = log.remote_history if args.remote else log.local_history
            print(json.dumps(read(args.user, args.limit, args.before_sequence), indent=2))
            return
        with (log.directory / "worker.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = None
            while True:
                uploaded = False
                try:
                    log.collect()
                    if args.command != "collect":
                        users = (
                            [args.user] if args.user else [r[0] for r in log.db.execute("SELECT user_id FROM users")]
                        )
                        for user in users:
                            uploaded = log.sync(user).get("state") == "verified" or uploaded
                    log.render()
                    current = log.summary()
                except Exception:
                    current = {"state": "sync_pending", **log.summary()}
                    if args.command != "watch":
                        print(json.dumps(current), flush=True)
                        raise SystemExit(1) from None
                if current != previous:
                    print(json.dumps(current), flush=True)
                    previous = current
                if args.command != "watch":
                    break
                time.sleep(0.2 if uploaded and current["pending"] else 5)
    finally:
        log.close()


if __name__ == "__main__":
    main()
