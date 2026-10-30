"""The case timeline: every event that happens to a sale, in order.

Day 1 records order created / captured / tracking added. Later days add
invoice, hold, dispute and buyer-question events to the same table, which is
what lets one screen show the whole life of a payment.
"""

import json
import sqlite3
import time
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id);
CREATE TABLE IF NOT EXISTS processed_webhooks (
    event_id TEXT PRIMARY KEY,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS buyer_summaries (
    order_id TEXT PRIMARY KEY,
    state_key TEXT NOT NULL,
    text TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active_provider TEXT NOT NULL,
    switched_at REAL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_login_at REAL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until REAL,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS insights_layouts (
    user_key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    role TEXT NOT NULL,
    tool TEXT NOT NULL,
    outcome TEXT NOT NULL,
    order_ref TEXT
);
"""

DISPUTE_KINDS = ("dispute_created", "dispute_updated", "dispute_resolved")


def latest_state(states: list[dict]) -> dict | None:
    """The dispute event with the newest PayPal update_time (webhooks arrive out of order, so
    the last inserted row isn't necessarily the current state). Ties go to the newest row."""
    if not states:
        return None
    # max() keeps the first of equal keys, so reverse to prefer the newest row on ties.
    return max(reversed(states), key=lambda e: e["payload"].get("update_time") or "")


class TimelineStore:
    def __init__(self, path: str):
        self.path = path
        with closing(self._conn()) as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, order_id: str, kind: str, payload: dict) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO events (order_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (order_id, kind, json.dumps(payload), time.time()),
            )

    def timeline(self, order_id: str) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT kind, payload, created_at FROM events WHERE order_id = ? ORDER BY id",
                (order_id,),
            ).fetchall()
        return [{"kind": r["kind"], "payload": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]

    def orders(self) -> list[dict]:
        """Latest state of every order, newest first (for the seller page)."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                """SELECT order_id, MAX(created_at) AS updated_at,
                          GROUP_CONCAT(kind) AS kinds
                   FROM events GROUP BY order_id ORDER BY updated_at DESC"""
            ).fetchall()
        return [{"order_id": r["order_id"], "updated_at": r["updated_at"],
                 "kinds": r["kinds"].split(",")} for r in rows]

    def all_events(self) -> dict[str, list[dict]]:
        """Every order's timeline in one query (for the dashboard), oldest event first."""
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT order_id, kind, payload, created_at FROM events ORDER BY id").fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["order_id"], []).append(
                {"kind": r["kind"], "payload": json.loads(r["payload"]), "created_at": r["created_at"]})
        return out

    def find_order_by_capture(self, capture_id: str) -> str | None:
        """Order whose payment_captured event has this capture ID, if any."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                """SELECT order_id FROM events
                   WHERE kind = 'payment_captured' AND json_extract(payload, '$.capture_id') = ?
                   ORDER BY id DESC LIMIT 1""",
                (capture_id,),
            ).fetchone()
        return row["order_id"] if row else None

    def mark_webhook_processed(self, event_id: str) -> bool:
        """Remember a webhook event ID. False if we've already seen it (PayPal retries)."""
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_webhooks (event_id, received_at) VALUES (?, ?)",
                (event_id, time.time()),
            )
        return cur.rowcount == 1

    # ---- Disputes (Day 3) ---------------------------------------------------

    def dispute_events(self, dispute_id: str) -> list[dict]:
        """Every event (any order, any kind) whose payload names this dispute, oldest first."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                """SELECT order_id, kind, payload, created_at FROM events
                   WHERE json_extract(payload, '$.dispute_id') = ? ORDER BY id""",
                (dispute_id,),
            ).fetchall()
        return [{"order_id": r["order_id"], "kind": r["kind"], "payload": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]

    def latest_dispute_state(self, dispute_id: str) -> dict | None:
        """The stored dispute event with the newest PayPal update_time (webhooks arrive out of
        order, so the last inserted row isn't necessarily the current state)."""
        return latest_state([e for e in self.dispute_events(dispute_id) if e["kind"] in DISPUTE_KINDS])

    # ---- Buyer page summaries (Day 4) ----------------------------------------

    def buyer_summary(self, order_id: str) -> dict | None:
        """The cached AI summary for an order's buyer page, if any."""
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM buyer_summaries WHERE order_id = ?",
                               (order_id,)).fetchone()
        return dict(row) if row else None

    def set_buyer_summary(self, order_id: str, state_key: str, text: str, provider: str,
                          model: str) -> None:
        """Cache the AI summary for one order state (replaces the previous one)."""
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO buyer_summaries (order_id, state_key, text, provider, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET state_key = excluded.state_key,
                       text = excluded.text, provider = excluded.provider,
                       model = excluded.model, created_at = excluded.created_at""",
                (order_id, state_key, text, provider, model, time.time()))

    # ---- AI provider state (Day 3) -----------------------------------------

    def ai_state(self) -> dict:
        """Which AI provider is active. Defaults to gemini until something switches it."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT active_provider, switched_at, reason FROM ai_state WHERE id = 1").fetchone()
        if not row:
            return {"active_provider": "gemini", "switched_at": None, "reason": None}
        return dict(row)

    def set_ai_provider(self, provider: str, reason: str) -> None:
        """Persist the active AI provider so the choice survives restarts."""
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO ai_state (id, active_provider, switched_at, reason) VALUES (1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET active_provider = excluded.active_provider,
                       switched_at = excluded.switched_at, reason = excluded.reason""",
                (provider, time.time(), reason),
            )

    # ---- Seller settings and AI-agent activity (Day 6) ----------------------

    def get_setting(self, key: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: dict) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                       updated_at = excluded.updated_at""",
                (key, json.dumps(value), time.time()))

    def events_since(self, kind: str, since: float) -> list[dict]:
        """Every event of one kind (any order) created at or after `since`."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                """SELECT order_id, payload, created_at FROM events
                   WHERE kind = ? AND created_at >= ? ORDER BY id""", (kind, since)).fetchall()
        return [{"order_id": r["order_id"], "payload": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]

    # ---- Seller accounts: users, sessions, agent API keys ------------------------
    # Plain SQL and the same column types as everything else (INTEGER, TEXT, REAL epoch
    # seconds), so the planned Postgres move copies these tables like the rest.

    _USER_COLUMNS = ("id, email, password_hash, created_at, last_login_at, failed_logins, "
                     "locked_until, disabled")

    def count_users(self) -> int:
        with closing(self._conn()) as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, email: str, password_hash: str, now: float) -> int:
        """A new user (email already lowercased). The first account also takes over the Insights
        layout saved before accounts existed (key "seller")."""
        with closing(self._conn()) as conn, conn:
            first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            cur = conn.execute("INSERT INTO users (email, password_hash, created_at, failed_logins, disabled) "
                               "VALUES (?, ?, ?, 0, 0)", (email, password_hash, now))
            user_id = cur.lastrowid
            if first:
                conn.execute("UPDATE insights_layouts SET user_key = ? WHERE user_key = 'seller'",
                             (str(user_id),))
        return user_id

    def _user(self, where: str, value) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute(f"SELECT {self._USER_COLUMNS} FROM users WHERE {where} = ?", (value,)).fetchone()
        return dict(row) if row else None

    def user_by_email(self, email: str) -> dict | None:
        return self._user("email", email)

    def user_by_id(self, user_id: int) -> dict | None:
        return self._user("id", user_id)

    def list_users(self) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT id, email, created_at, last_login_at, disabled FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_user(self, user_id: int, **fields) -> None:
        allowed = {"password_hash", "last_login_at", "failed_logins", "locked_until", "disabled"}
        assert fields and set(fields) <= allowed, fields
        sets = ", ".join(f"{k} = ?" for k in fields)
        with closing(self._conn()) as conn, conn:
            conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*fields.values(), user_id))

    def create_session(self, session_id: str, user_id: int, now: float, expires_at: float) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute("INSERT INTO sessions (id, user_id, created_at, last_seen_at, expires_at) "
                         "VALUES (?, ?, ?, ?, ?)", (session_id, user_id, now, now, expires_at))

    def session(self, session_id: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT id, user_id, created_at, last_seen_at, expires_at FROM sessions "
                               "WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def touch_session(self, session_id: str, now: float) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, session_id))

    def delete_session(self, session_id: str) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def delete_user_sessions(self, user_id: int, keep: str | None = None) -> int:
        with closing(self._conn()) as conn, conn:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, keep or ""))
        return cur.rowcount

    def delete_expired_sessions(self, now: float, idle_seconds: float) -> int:
        with closing(self._conn()) as conn, conn:
            cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR last_seen_at <= ?",
                               (now, now - idle_seconds))
        return cur.rowcount

    def create_api_key(self, user_id: int, name: str, key_hash: str, prefix: str, now: float) -> int:
        with closing(self._conn()) as conn, conn:
            cur = conn.execute("INSERT INTO api_keys (user_id, name, key_hash, prefix, created_at) "
                               "VALUES (?, ?, ?, ?, ?)", (user_id, name, key_hash, prefix, now))
        return cur.lastrowid

    def api_keys(self, user_id: int) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT id, name, prefix, created_at, last_used_at, revoked_at FROM api_keys "
                                "WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def revoke_api_key(self, user_id: int, key_id: int, now: float) -> bool:
        with closing(self._conn()) as conn, conn:
            cur = conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? "
                               "AND revoked_at IS NULL", (now, key_id, user_id))
        return cur.rowcount == 1

    def use_api_key(self, key_hash: str, now: float) -> int | None:
        """The key's id if it exists and isn't revoked (and its owner isn't disabled), noting
        when it was last used; else None."""
        with closing(self._conn()) as conn, conn:
            row = conn.execute("SELECT k.id FROM api_keys k JOIN users u ON u.id = k.user_id "
                               "WHERE k.key_hash = ? AND k.revoked_at IS NULL AND u.disabled = 0",
                               (key_hash,)).fetchone()
            if row:
                conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, row["id"]))
        return row["id"] if row else None

    def active_api_keys(self) -> int:
        with closing(self._conn()) as conn:
            return conn.execute("SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL").fetchone()[0]

    # ---- Insights layouts (Day 8): the seller's own AG Studio report ------------

    def insights_layout(self, user_key: str) -> dict | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT state, updated_at FROM insights_layouts WHERE user_key = ?",
                               (user_key,)).fetchone()
        return {"state": json.loads(row["state"]), "updated_at": row["updated_at"]} if row else None

    def set_insights_layout(self, user_key: str, state: dict) -> float:
        now = time.time()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO insights_layouts (user_key, state, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(user_key) DO UPDATE SET state = excluded.state,
                                                       updated_at = excluded.updated_at""",
                (user_key, json.dumps(state), now))
        return now

    def delete_insights_layout(self, user_key: str) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute("DELETE FROM insights_layouts WHERE user_key = ?", (user_key,))

    def log_agent_call(self, role: str, tool: str, outcome: str, order_ref: str | None,
                       keep: int = 500) -> None:
        """One MCP tool call: tool name, role, a short outcome and the last 6 characters of
        the order ID. Never arguments, messages, links or tokens."""
        with closing(self._conn()) as conn, conn:
            conn.execute("INSERT INTO agent_calls (at, role, tool, outcome, order_ref) "
                         "VALUES (?, ?, ?, ?, ?)", (time.time(), role, tool, outcome, order_ref))
            conn.execute("DELETE FROM agent_calls WHERE id <= "
                         "(SELECT MAX(id) FROM agent_calls) - ?", (keep,))

    def agent_calls(self, limit: int = 20) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT at, role, tool, outcome, order_ref FROM agent_calls "
                                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
