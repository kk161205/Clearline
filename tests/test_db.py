"""The database layer on SQLite and, with TEST_DATABASE_URL, on Postgres: migrations, every
table's store methods, the dialect bits (JSON fields, string aggregation, upserts, ids,
booleans, float precision) and /healthz. No network unless TEST_DATABASE_URL is set."""

import sqlite3
import time

import pytest

from app.db import MIGRATIONS, TABLES, Database
from app.store import TimelineStore
from tests import pg
from tests.support import db_target, make_client, query


@pytest.fixture
def store(tmp_path):
    return TimelineStore(db_target(tmp_path))


def test_migrations_run_once_and_record_the_version(tmp_path, store):
    latest = MIGRATIONS[-1][0]
    assert query(store.db.target, "SELECT version FROM schema_version") == [{"version": latest}]
    TimelineStore(db_target(tmp_path))  # a second start changes nothing
    store.db.migrate()
    assert store.db.count("schema_version") == 1
    for table in TABLES:
        assert store.db.table_exists(table), table
        assert store.db.count(table) == 0
    assert not store.db.table_exists("no_such_table")


@pytest.mark.skipif(bool(pg.TEST_DATABASE_URL), reason="an old SQLite file only exists on SQLite")
def test_an_existing_sqlite_file_from_before_versions_keeps_its_data(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:  # the schema as it was, with data, no schema_version
        for statement in filter(str.strip, MIGRATIONS[0][1].split(";")):
            conn.execute(statement)
        conn.execute("INSERT INTO events (order_id, kind, payload, created_at) VALUES ('O1', 'order_created', '{}', 1.5)")
    store = TimelineStore(str(path))
    assert store.timeline("O1") == [{"kind": "order_created", "payload": {}, "created_at": 1.5}]
    assert store.db.count("schema_version") == 1


def test_events_orders_and_json_lookups(store):
    store.add("O1", "order_created", {"amount": "10.00"})
    store.add("O1", "payment_captured", {"capture_id": "CAP-1"})
    store.add("O2", "dispute_created", {"dispute_id": "D-1", "update_time": "2026-01-01T00:00:00Z"})
    store.add("O1", "dispute_updated", {"dispute_id": "D-1", "update_time": "2026-01-02T00:00:00Z"})
    orders = store.orders()
    assert [o["order_id"] for o in orders] == ["O1", "O2"]  # newest activity first
    assert orders[0]["kinds"] == ["order_created", "payment_captured", "dispute_updated"]  # in order
    assert store.find_order_by_capture("CAP-1") == "O1" and store.find_order_by_capture("nope") is None
    assert [e["order_id"] for e in store.dispute_events("D-1")] == ["O2", "O1"]
    assert store.latest_dispute_state("D-1")["kind"] == "dispute_updated"
    assert set(store.all_events()) == {"O1", "O2"}
    since = store.events_since("payment_captured", 0)
    assert since[0]["payload"] == {"capture_id": "CAP-1"}
    assert store.events_since("payment_captured", time.time() + 60) == []


def test_times_keep_full_precision(store):
    before = time.time()
    store.add("O1", "order_created", {})
    created = store.timeline("O1")[0]["created_at"]
    assert isinstance(created, float) and abs(created - before) < 5  # 8-byte floats, not 4
    store.update_user(store.create_user("p@example.com", "h", 1_793_000_000.123456), locked_until=1_793_000_000.654321)
    assert store.user_by_email("p@example.com")["locked_until"] == 1_793_000_000.654321


def test_upserts_and_dedupe(store):
    assert store.mark_webhook_processed("WH-1") is True
    assert store.mark_webhook_processed("WH-1") is False  # PayPal retry: ignored
    store.set_buyer_summary("O1", "k1", "first", "gemini", "m")
    store.set_buyer_summary("O1", "k2", "second", "groq", "m")
    assert store.buyer_summary("O1")["text"] == "second" and store.buyer_summary("O9") is None
    assert store.ai_state()["active_provider"] == "gemini"  # the default
    store.set_ai_provider("groq", "quota")
    store.set_ai_provider("groq", "again")
    assert store.ai_state()["reason"] == "again" and store.db.count("ai_state") == 1
    store.set_setting("policy", {"enabled": False})
    store.set_setting("policy", {"enabled": True})
    assert store.get_setting("policy") == {"enabled": True} and store.get_setting("x") is None
    store.set_insights_layout("7", {"a": 1})
    store.set_insights_layout("7", {"a": 2})
    assert store.insights_layout("7")["state"] == {"a": 2}
    store.delete_insights_layout("7")
    assert store.insights_layout("7") is None


def test_users_sessions_and_keys(store):
    first = store.create_user("a@example.com", "hash-a", 1.0)
    second = store.create_user("b@example.com", "hash-b", 2.0)
    assert second == first + 1 and store.count_users() == 2
    with pytest.raises(Exception):  # unique email
        store.create_user("a@example.com", "x", 3.0)
    store.update_user(first, failed_logins=3, disabled=1)
    user = store.user_by_id(first)
    assert user["failed_logins"] == 3 and user["disabled"] == 1 and user["email"] == "a@example.com"
    assert [u["id"] for u in store.list_users()] == [first, second]
    assert "password_hash" not in store.list_users()[0]

    store.create_session("s1", second, 10.0, 100.0)
    store.create_session("s2", second, 10.0, 100.0)
    store.touch_session("s1", 20.0)
    assert store.session("s1")["last_seen_at"] == 20.0
    assert store.delete_user_sessions(second, keep="s1") == 1 and store.session("s2") is None
    assert store.delete_expired_sessions(now=50.0, idle_seconds=25.0) == 1  # s1 idle since 20
    store.create_session("s3", second, 10.0, 100.0)
    store.delete_session("s3")
    assert store.session("s3") is None

    key_id = store.create_api_key(second, "agent", "h1", "cl_live_abcd", 5.0)
    assert store.use_api_key("h1", 6.0) == key_id and store.api_keys(second)[0]["last_used_at"] == 6.0
    assert store.use_api_key("nope", 6.0) is None and store.active_api_keys() == 1
    assert store.revoke_api_key(first, key_id, 7.0) is False  # someone else's key
    assert store.revoke_api_key(second, key_id, 7.0) is True and store.use_api_key("h1", 8.0) is None
    disabled_key = store.create_api_key(first, "x", "h2", "cl_live_efgh", 5.0)
    assert disabled_key and store.use_api_key("h2", 9.0) is None  # owner is disabled


def test_the_first_account_takes_the_old_shared_layout(store):
    store.set_insights_layout("seller", {"from": "before accounts"})
    user_id = store.create_user("first@example.com", "h", 1.0)
    assert store.insights_layout(str(user_id))["state"] == {"from": "before accounts"}
    assert store.insights_layout("seller") is None


def test_agent_call_log_is_trimmed(store):
    for i in range(6):
        store.log_agent_call("buyer", f"tool{i}", "ok", None, keep=3)
    assert [c["tool"] for c in store.agent_calls()] == ["tool5", "tool4", "tool3"]


def test_database_targets():
    for scheme in ("postgresql", "postgres"):  # URLs assembled so no real-looking one is in the repo
        assert Database(f"{scheme}:" + "//u:p@h/db").postgres
    assert not Database("clearline.db").postgres and not Database(r"C:\data\clearline.db").postgres
    lite = Database("x.db")
    assert lite.json_text("payload", "capture_id") == "json_extract(payload, '$.capture_id')"
    with pytest.raises(AssertionError):
        lite.json_text("payload", "x'; DROP TABLE events; --")


# ---- /healthz ----------------------------------------------------------------------------

def test_healthz_is_public_and_says_only_ok(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    res = client.get("/healthz")
    assert res.status_code == 200 and res.json() == {"ok": True, "db": "ok"}
    assert res.headers["cache-control"] == "no-store"
    assert client.head("/healthz").status_code in (200, 405)


def test_healthz_is_503_when_the_database_is_down(tmp_path, monkeypatch, caplog):
    client, settings = make_client(tmp_path, login=False)

    def down(self):
        raise ConnectionError(f"could not connect to {settings.database_target}")

    monkeypatch.setattr(TimelineStore, "ping", down)
    res = client.get("/healthz")
    assert res.status_code == 503 and res.json() == {"ok": False, "db": "down"}
    assert settings.database_target not in res.text and settings.database_target not in caplog.text
