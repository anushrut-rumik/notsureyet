import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent.arkiv_custody import signer_lock
from agent.lending_activity import ActivityLog, install_state_capture, install_transaction_capture
from agent.vault import Vault


@pytest.fixture
def activity(tmp_path):
    data = tmp_path / ".local"
    vault = Vault(data)
    db_path = data / "agent.sqlite3"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE lending_private(user_id TEXT PRIMARY KEY,sealed TEXT);
        CREATE TABLE wallets(user_id TEXT,address TEXT,kind TEXT,secret TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY,user_id TEXT,kind TEXT,data TEXT,at REAL);
    """)
    writer = "0x" + "11" * 20
    db.execute("INSERT INTO wallets VALUES('alice',?,'local','not-used')", (writer,))
    db.execute(
        "INSERT INTO lending_private VALUES('alice',?)", (vault.seal({"plan": {"status": "active"}}, "lending:alice"),)
    )
    db.commit()
    config = SimpleNamespace(data_dir=data, db_path=db_path, root=tmp_path, env={})
    log = ActivityLog(config)
    yield log, db, vault, writer
    log.close()
    db.close()


def test_every_state_revision_retained_even_between_polls(activity):
    log, db, vault, _ = activity
    log.collect()
    for status in ["awaiting_deposit", "active", "paused", "withdrawing", "withdrawn"]:
        db.execute(
            "UPDATE lending_private SET sealed=? WHERE user_id='alice'",
            (vault.seal({"plan": {"status": status, "userId": "private-chat"}}, "lending:alice"),),
        )
        db.commit()
    log.collect()
    assert log.summary()["captured"] == 6
    records = log.local_history("alice")
    assert [r["details"]["activity"]["plan"]["status"] for r in records[:5]] == [
        "withdrawn",
        "withdrawing",
        "paused",
        "active",
        "awaiting_deposit",
    ]
    assert "private-chat" not in json.dumps(records)
    before = list(log.db.execute("SELECT event,encrypted FROM activity"))
    log.collect()
    assert [tuple(r) for r in before] == [tuple(r) for r in log.db.execute("SELECT event,encrypted FROM activity")]


def test_encryption_context_and_user_isolation(activity):
    log, _, vault, writer = activity
    log.register("alice", writer)
    log.append("alice", "example", "decision", {"amount": "800000000000000000", "secret": "DO-NOT-STORE"})
    row = log.db.execute("SELECT * FROM activity").fetchone()
    assert "800000000000000000" not in row["encrypted"]
    assert log.local_history("bob") == []
    assert log.local_history("alice")[0]["details"]["activity"] == {"amount": "800000000000000000"}
    with pytest.raises(ValueError):
        vault.open(row["encrypted"], "wrong-context")
    with pytest.raises(ValueError):
        vault.open(row["encrypted"][:-4] + "AAAA", f"lending-activity:7794095:{row['scope']}:{row['event']}")


def test_transaction_capture_retains_all_stages_and_excludes_raw(activity):
    log, _, _, _ = activity
    log.collect()
    directory = log.config.data_dir / "lending-live" / hashlib.sha256(b"alice").hexdigest()
    directory.mkdir(parents=True)
    db = sqlite3.connect(directory / "transactions.sqlite")
    db.executescript("CREATE TABLE txs(id TEXT,hash TEXT,state TEXT,intent TEXT,raw TEXT,chain TEXT,receipt TEXT)")
    install_transaction_capture(db, "txs")
    db.execute(
        "INSERT INTO txs VALUES('deposit','0x123','unknown',?,'SECRET_RAW','11155111',NULL)",
        (json.dumps({"data": "0x12345678PRIVATE", "to": "pool"}),),
    )
    db.execute("UPDATE txs SET state='confirmed',receipt=?", (json.dumps({"confirmation": "mined"}),))
    db.commit()
    log.collect()
    records = log.local_history("alice")
    assert len(records) == 4
    text = json.dumps(records)
    assert "SECRET_RAW" not in text and "PRIVATE" not in text
    assert '"confirmation": "mined"' in text
    assert {r["details"]["activity"]["state"] for r in records if "state" in r["details"]["activity"]} == {
        "unknown",
        "confirmed",
    }
    db.close()


def test_storage_outage_does_not_block_lending_state_updates(activity, monkeypatch):
    log, db, vault, _ = activity
    log.collect()

    def unavailable(*_):
        raise ValueError("storage unavailable")

    monkeypatch.setattr(log, "sync", unavailable)
    with pytest.raises(ValueError):
        log.sync("alice")
    db.execute("UPDATE lending_private SET sealed=?", (vault.seal({"plan": {"status": "withdrawn"}}, "lending:alice"),))
    db.commit()
    log.collect()
    assert log.local_history("alice")[0]["details"]["activity"]["plan"]["status"] == "withdrawn"
    assert log.summary()["pending"] == 2


def test_signer_custody_refuses_parallel_storage_but_releases_on_exit(activity):
    log, _, _, writer = activity
    with signer_lock(log.config, writer):
        with pytest.raises(ValueError, match="busy"), signer_lock(log.config, writer):
            pass
    with signer_lock(log.config, writer):
        pass


def test_capture_install_is_idempotent_and_transactions_roll_back(activity):
    log, db, vault, _ = activity
    install_state_capture(db)
    install_state_capture(db)
    db.execute("UPDATE lending_private SET sealed=?", (vault.seal({"plan": None}, "lending:alice"),))
    db.rollback()
    assert db.execute("SELECT count(*) FROM lending_activity_states").fetchone()[0] == 0


def test_large_activity_is_encrypted_without_truncation(activity):
    log, _, _, writer = activity
    log.register("alice", writer)
    value = {"detail": "a" * 60000}
    log.append("alice", "large", "snapshot", value)
    rows = log.local_history("alice")
    assert len(rows) == 4
    parts = sorted((r["details"] for r in rows), key=lambda d: d["part"])
    assert json.loads("".join(p["text"] for p in parts))["activity"] == value
    log.append("alice", "large", "snapshot", value)
    assert log.summary()["captured"] == 4


def test_compaction_preserves_admitted_ciphertext_and_exact_content(activity):
    log, _, _, writer = activity
    scope = log.register("alice", writer)
    log.append("alice", "first", "decision", {"status": "hold"})
    row = dict(log.db.execute("SELECT * FROM activity").fetchone())
    original = log.open_content(row["encrypted"], scope, row["event"])
    old = log.vault.seal(original, f"lending-activity:7794095:{scope}:{row['event']}")
    with log.db:
        log.db.execute("UPDATE activity SET encrypted=?", (old,))
        log.db.execute(
            "INSERT INTO jobs VALUES('job','alice',?,'prepared','hash','100',NULL)",
            (json.dumps([{"event": row["event"]}]),),
        )
    log.compact_pending()
    assert log.db.execute("SELECT encrypted FROM activity").fetchone()[0] == old
    assert log.open_content(old, scope, row["event"]) == original
