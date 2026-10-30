"""python -m app.migrate_sqlite: SQLite file -> DATABASE_URL. The target is another SQLite file
by default, or with TEST_DATABASE_URL a fresh Postgres schema. No network otherwise."""

import pytest

from app.accounts import Accounts
from app.config import Settings
from app.db import TABLES, Database
from app.migrate_sqlite import main as migrate
from app.store import TimelineStore
from tests.support import db_target


def source_db(tmp_path) -> str:
    """A SQLite file with rows in every table, like a used clearline.db."""
    path = str(tmp_path / "clearline.db")
    store = TimelineStore(path)
    store.add("O1", "order_created", {"amount": "120.00"})
    store.add("O1", "payment_captured", {"capture_id": "CAP-1"})
    store.add("O2", "order_created", {"amount": "5.00"})
    store.mark_webhook_processed("WH-1")
    store.set_buyer_summary("O1", "k", "Your order shipped.", "gemini", "m")
    store.set_ai_provider("groq", "quota")
    store.set_setting("policy", {"enabled": False})
    user_id = Accounts(store).create_user("owner@example.com", "an old enough passphrase")
    store.create_session("sid", user_id, 1.0, 2.0)
    store.create_api_key(user_id, "agent", "hash", "cl_live_abcd", 1.0)
    store.set_insights_layout(str(user_id), {"pages": []})
    store.log_agent_call("buyer", "get_order_status", "ok", "ORDER1")
    return path


def target_settings(tmp_path, url: str | None) -> Settings:
    (tmp_path / "target").mkdir(exist_ok=True)
    return Settings(_env_file=None, paypal_client_id="x", paypal_client_secret="y",
                    database_path=str(tmp_path / "unused.db"), database_url=url)


def target_of(tmp_path) -> str:
    (tmp_path / "target").mkdir(exist_ok=True)
    return db_target(tmp_path / "target")


EXPECTED = {"events": 3, "processed_webhooks": 1, "buyer_summaries": 1, "ai_state": 1, "app_settings": 1,
            "users": 1, "sessions": 1, "api_keys": 1, "insights_layouts": 1, "agent_calls": 1}


def counts_in(output: str, title: str) -> dict[str, int]:
    block = output.split(title, 1)[1].split("total", 1)[0]
    return {line.split()[0]: int(line.split()[1]) for line in block.strip().splitlines()}


def test_dry_run_shows_counts_and_writes_nothing(tmp_path, capsys):
    source, target = source_db(tmp_path), target_of(tmp_path)
    assert migrate(["--from", source, "--dry-run"], settings=target_settings(tmp_path, target)) == 0
    out = capsys.readouterr().out
    assert counts_in(out, "Source clearline.db:") == EXPECTED
    assert "Target: empty (no Clearline tables yet)" in out and "Dry run: nothing written." in out
    assert target not in out
    assert not any(Database(target).table_exists(t) for t in TABLES)  # not even the schema


def test_copy_moves_every_row_and_ids_continue(tmp_path, capsys):
    source, target = source_db(tmp_path), target_of(tmp_path)
    assert migrate(["--from", source], settings=target_settings(tmp_path, target)) == 0
    out = capsys.readouterr().out
    assert counts_in(out, "Copied:") == EXPECTED and "Done." in out and target not in out

    copied = TimelineStore(target)
    old = TimelineStore(source)
    assert copied.all_events() == old.all_events()
    assert copied.find_order_by_capture("CAP-1") == "O1"
    assert copied.ai_state()["active_provider"] == "groq" and copied.get_setting("policy") == {"enabled": False}
    user, outcome = Accounts(copied).authenticate("owner@example.com", "an old enough passphrase")
    assert outcome == "ok"  # the password hash came across intact
    assert copied.use_api_key("hash", 3.0) is not None
    assert copied.insights_layout(str(user["id"]))["state"] == {"pages": []}
    # New rows get ids after the copied ones (identity counters moved on Postgres).
    assert copied.create_user("next@example.com", "h", 5.0) == user["id"] + 1
    copied.add("O3", "order_created", {})
    assert len(copied.all_events()) == 3


def test_a_target_with_data_is_refused_unless_forced(tmp_path, capsys):
    source, target = source_db(tmp_path), target_of(tmp_path)
    settings = target_settings(tmp_path, target)
    TimelineStore(target).add("KEEP", "order_created", {})
    with pytest.raises(SystemExit, match="already has data"):
        migrate(["--from", source], settings=settings)
    assert "Target already has rows:" in capsys.readouterr().out
    assert list(TimelineStore(target).all_events()) == ["KEEP"]  # untouched

    assert migrate(["--from", source, "--dry-run"], settings=settings) == 0  # dry run still fine
    assert migrate(["--from", source, "--force"], settings=settings) == 0
    assert set(TimelineStore(target).all_events()) == {"O1", "O2"}  # replaced


def test_needs_a_target_and_a_source(tmp_path):
    with pytest.raises(SystemExit, match="DATABASE_URL is not set"):
        migrate(["--from", source_db(tmp_path)], settings=target_settings(tmp_path, None))
    with pytest.raises(SystemExit, match="No SQLite file"):
        migrate(["--from", str(tmp_path / "missing.db")], settings=target_settings(tmp_path, target_of(tmp_path)))


def test_an_unreachable_target_never_prints_its_url(tmp_path, capsys):
    url = "postgresql:" + "//someone:secret-pw@127.0.0.1:1/nothing?connect_timeout=1"  # nothing listens
    with pytest.raises(SystemExit) as exit_info:
        migrate(["--from", source_db(tmp_path), "--dry-run"], settings=target_settings(tmp_path, url))
    captured = capsys.readouterr()
    message = str(exit_info.value) + captured.out + captured.err
    assert "Couldn't reach the target database" in message
    assert "secret-pw" not in message and "127.0.0.1" not in message
