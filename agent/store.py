"""SQLite execution cache and private chat/memory. ArkivStore supplies task authority."""

import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4


class Store:
    backend = "sqlite"

    def flush_task(self, task_id):
        """Local mode has no remote checkpoint; Arkiv mode overrides this barrier."""

    async def checkpoint_barrier(self, task_id):
        """Wait for any user-signed checkpoint before continuing task execution."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
                instruction TEXT NOT NULL, done_when TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued', wake_at REAL, failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status,wake_at);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, task_id TEXT,
                kind TEXT NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS events_owner ON events(user_id,task_id,id);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS messages_scope ON messages(scope,id);
            CREATE TABLE IF NOT EXISTS memories (
                user_id TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
                source_event INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(user_id,key));
            CREATE TABLE IF NOT EXISTS effects (
                key TEXT PRIMARY KEY, user_id TEXT NOT NULL, task_id TEXT, tool TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS wallets (
                user_id TEXT PRIMARY KEY, address TEXT NOT NULL, kind TEXT NOT NULL, secret TEXT);
            CREATE TABLE IF NOT EXISTS transfers (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, task_id TEXT NOT NULL, data TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def recover(self):
        with self.db:
            self.db.execute("UPDATE tasks SET status='queued' WHERE status='running'")
            self.db.execute("UPDATE effects SET status='unknown' WHERE status='pending'")

    def event(self, user_id: str, kind: str, data: dict, *, task_id=None, source="agent") -> int:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO events(user_id,task_id,kind,source,data,at) VALUES(?,?,?,?,?,?)",
                (user_id, task_id, kind, source, json.dumps(data, ensure_ascii=False), time.time()),
            )
        return cursor.lastrowid

    def events(self, user_id: str, *, task_id=None, after=0, kind=None, limit=100) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE user_id=? AND id>?", [user_id, after]
        if task_id is not None:
            sql += " AND task_id=?"
            args.append(task_id)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        rows = self.db.execute(sql + " ORDER BY id LIMIT ?", [*args, limit]).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def message(self, scope: str, message: dict):
        with self.db:
            self.db.execute("INSERT INTO messages(scope,data) VALUES(?,?)", (scope, json.dumps(message)))

    def messages(self, scope: str) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT data FROM messages WHERE scope=? ORDER BY id", (scope,))
        ]

    def notice(self, user_id: str, text: str, task_id=None) -> int:
        return self.event(user_id, "chat_message", {"role": "assistant", "content": text}, task_id=task_id)

    def create_task(self, user_id: str, title: str, instruction: str, done_when: str) -> dict:
        task_id, now = uuid4().hex, time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,user_id,title,instruction,done_when,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, user_id, title, instruction, done_when, now, now),
            )
        self.event(user_id, "task_created", {"instruction": instruction}, task_id=task_id, source="user")
        return self.task(user_id, task_id)

    def task(self, user_id: str, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
        if row is None:
            raise ValueError("No such task for this user")
        return dict(row)

    def tasks(self, user_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC", (user_id,))
        ]

    def update_task(self, user_id: str, task_id: str, **changes):
        self.task(user_id, task_id)
        if changes.keys() - {"status", "notes", "wake_at", "instruction", "failures", "last_error"}:
            raise ValueError("Unknown task field")
        changes["updated_at"] = time.time()
        with self.db:
            self.db.execute(
                "UPDATE tasks SET " + ",".join(f"{name}=?" for name in changes) + " WHERE id=? AND user_id=?",
                [*changes.values(), task_id, user_id],
            )

    def claim(self) -> dict | None:
        now = time.time()
        with self.db:
            row = self.db.execute(
                """UPDATE tasks SET status='running', updated_at=? WHERE id=(
                SELECT id FROM tasks WHERE status IN ('queued','waiting') AND (wake_at IS NULL OR wake_at<=?)
                ORDER BY updated_at LIMIT 1) RETURNING *""",
                (now, now),
            ).fetchone()
        return dict(row) if row else None

    def remember(self, user_id: str, key: str, content: str, event_id: int):
        row = self.db.execute("SELECT source,user_id FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None or row["source"] != "user" or row["user_id"] != user_id:
            raise ValueError("Memory must cite this user's own message")
        with self.db:
            self.db.execute(
                "INSERT INTO memories VALUES(?,?,?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET "
                "content=excluded.content,source_event=excluded.source_event,updated_at=excluded.updated_at",
                (user_id, key, content, event_id, time.time()),
            )

    def memories(self, user_id: str, query="") -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM memories WHERE user_id=? AND "
                "(content LIKE ? OR key LIKE ?) ORDER BY updated_at DESC LIMIT 80",
                (user_id, f"%{query}%", f"%{query}%"),
            )
        ]

    def forget(self, user_id: str, key: str):
        with self.db:
            self.db.execute("DELETE FROM memories WHERE user_id=? AND key=?", (user_id, key))

    def effect(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM effects WHERE key=?", (key,)).fetchone()
        return {**dict(row), "result": json.loads(row["result"]) if row["result"] else None} if row else None

    def effect_start(self, key: str, user_id: str, task_id: str | None, tool: str):
        with self.db:
            self.db.execute(
                "INSERT INTO effects VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "status='pending',updated_at=excluded.updated_at",
                (key, user_id, task_id, tool, "pending", None, time.time()),
            )

    def effect_finish(self, key: str, status: str, result: dict):
        with self.db:
            self.db.execute(
                "UPDATE effects SET status=?,result=?,updated_at=? WHERE key=?",
                (status, json.dumps(result), time.time(), key),
            )

    def evidence(self, user_id: str, task_id: str, event_ids: list[int]) -> bool:
        if not event_ids:
            return False
        for event_id in event_ids:
            row = self.db.execute(
                "SELECT * FROM events WHERE id=? AND user_id=? AND task_id=?", (event_id, user_id, task_id)
            ).fetchone()
            if row is None:
                return False
            data = json.loads(row["data"])
            if not (
                row["kind"] == "task_reply"
                or (row["kind"] == "tool_result" and data.get("evidence") and data.get("ok"))
            ):
                return False
        return True
