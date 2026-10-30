"""The case timeline: every event that happens to a sale, in order.

Day 1 records order created / captured / tracking added. Later days add
invoice, hold, dispute and buyer-question events to the same table, which is
what lets one screen show the whole life of a payment.
"""

import json
import time

from .db import Database


DISPUTE_KINDS = ("dispute_created", "dispute_updated", "dispute_resolved")


def latest_state(states: list[dict]) -> dict | None:
    """The dispute event with the newest PayPal update_time (webhooks arrive out of order, so
    the last inserted row isn't necessarily the current state). Ties go to the newest row."""
    if not states:
        return None
    # max() keeps the first of equal keys, so reverse to prefer the newest row on ties.
    return max(reversed(states), key=lambda e: e["payload"].get("update_time") or "")


class TimelineStore:
    """`target` is a SQLite file path or a Postgres URL (see app/db.py)."""

    def __init__(self, target: str):
        self.db = Database(target)
        self.db.migrate()

    def ping(self) -> None:
        self.db.ping()

    def add(self, order_id: str, kind: str, payload: dict) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO events (order_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (order_id, kind, json.dumps(payload), time.time()),
            )

    def timeline(self, order_id: str) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT kind, payload, created_at FROM events WHERE order_id = ? ORDER BY id",
                (order_id,),
            ).fetchall()
        return [{"kind": r["kind"], "payload": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]

    def orders(self) -> list[dict]:
        """Latest state of every order, newest first (for the seller page)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT order_id, MAX(created_at) AS updated_at,
                           {self.db.group_concat('kind', 'id')} AS kinds
                    FROM events GROUP BY order_id ORDER BY updated_at DESC"""
            ).fetchall()
        return [{"order_id": r["order_id"], "updated_at": r["updated_at"],
                 "kinds": r["kinds"].split(",")} for r in rows]

    def all_events(self) -> dict[str, list[dict]]:
        """Every order's timeline in one query (for the dashboard), oldest event first."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT order_id, kind, payload, created_at FROM events ORDER BY id").fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["order_id"], []).append(
                {"kind": r["kind"], "payload": json.loads(r["payload"]), "created_at": r["created_at"]})
        return out

    def find_order_by_capture(self, capture_id: str) -> str | None:
        """Order whose payment_captured event has this capture ID, if any."""
        with self.db.connect() as conn:
            row = conn.execute(
                f"""SELECT order_id FROM events
                    WHERE kind = 'payment_captured' AND {self.db.json_text('payload', 'capture_id')} = ?
                    ORDER BY id DESC LIMIT 1""",
                (capture_id,),
            ).fetchone()
        return row["order_id"] if row else None

    def mark_webhook_processed(self, event_id: str) -> bool:
        """Remember a webhook event ID. False if we've already seen it (PayPal retries)."""
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO processed_webhooks (event_id, received_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (event_id, time.time()),
            )
        return cur.rowcount == 1

    # ---- Disputes (Day 3) ---------------------------------------------------

    def dispute_events(self, dispute_id: str) -> list[dict]:
        """Every event (any order, any kind) whose payload names this dispute, oldest first."""
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT order_id, kind, payload, created_at FROM events
                    WHERE {self.db.json_text('payload', 'dispute_id')} = ? ORDER BY id""",
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
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM buyer_summaries WHERE order_id = ?",
                               (order_id,)).fetchone()
        return dict(row) if row else None

    def set_buyer_summary(self, order_id: str, state_key: str, text: str, provider: str,
                          model: str) -> None:
        """Cache the AI summary for one order state (replaces the previous one)."""
        with self.db.connect() as conn:
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
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT active_provider, switched_at, reason FROM ai_state WHERE id = 1").fetchone()
        if not row:
            return {"active_provider": "gemini", "switched_at": None, "reason": None}
        return dict(row)

    def set_ai_provider(self, provider: str, reason: str) -> None:
        """Persist the active AI provider so the choice survives restarts."""
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO ai_state (id, active_provider, switched_at, reason) VALUES (1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET active_provider = excluded.active_provider,
                       switched_at = excluded.switched_at, reason = excluded.reason""",
                (provider, time.time(), reason),
            )

    # ---- Seller settings and AI-agent activity (Day 6) ----------------------

    def get_setting(self, key: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: dict) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                       updated_at = excluded.updated_at""",
                (key, json.dumps(value), time.time()))

    def events_since(self, kind: str, since: float) -> list[dict]:
        """Every event of one kind (any order) created at or after `since`."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT order_id, payload, created_at FROM events
                   WHERE kind = ? AND created_at >= ? ORDER BY id""", (kind, since)).fetchall()
        return [{"order_id": r["order_id"], "payload": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]

    # ---- Seller accounts: users, sessions, agent API keys ------------------------
    # Plain SQL and the same column types as everything else (INTEGER, TEXT, REAL epoch
    # seconds); `disabled` is 0/1 on both databases.

    _USER_COLUMNS = ("id, email, password_hash, created_at, last_login_at, failed_logins, "
                     "locked_until, disabled")

    def count_users(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def create_user(self, email: str, password_hash: str, now: float) -> int:
        """A new user (email already lowercased). The first account also takes over the Insights
        layout saved before accounts existed (key "seller")."""
        with self.db.transaction() as conn:
            first = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
            user_id = conn.execute("INSERT INTO users (email, password_hash, created_at, failed_logins, disabled) "
                                   "VALUES (?, ?, ?, 0, 0) RETURNING id", (email, password_hash, now)).fetchone()["id"]
            if first:
                conn.execute("UPDATE insights_layouts SET user_key = ? WHERE user_key = 'seller'",
                             (str(user_id),))
        return user_id

    def _user(self, where: str, value) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(f"SELECT {self._USER_COLUMNS} FROM users WHERE {where} = ?", (value,)).fetchone()
        return dict(row) if row else None

    def user_by_email(self, email: str) -> dict | None:
        return self._user("email", email)

    def user_by_id(self, user_id: int) -> dict | None:
        return self._user("id", user_id)

    def list_users(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, email, created_at, last_login_at, disabled FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_user(self, user_id: int, **fields) -> None:
        allowed = {"password_hash", "last_login_at", "failed_logins", "locked_until", "disabled"}
        assert fields and set(fields) <= allowed, fields
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db.connect() as conn:
            conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*fields.values(), user_id))

    def create_session(self, session_id: str, user_id: int, now: float, expires_at: float) -> None:
        with self.db.connect() as conn:
            conn.execute("INSERT INTO sessions (id, user_id, created_at, last_seen_at, expires_at) "
                         "VALUES (?, ?, ?, ?, ?)", (session_id, user_id, now, now, expires_at))

    def session(self, session_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT id, user_id, created_at, last_seen_at, expires_at FROM sessions "
                               "WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def touch_session(self, session_id: str, now: float) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, session_id))

    def delete_session(self, session_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def delete_user_sessions(self, user_id: int, keep: str | None = None) -> int:
        with self.db.connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, keep or ""))
        return cur.rowcount

    def delete_expired_sessions(self, now: float, idle_seconds: float) -> int:
        with self.db.connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR last_seen_at <= ?",
                               (now, now - idle_seconds))
        return cur.rowcount

    def create_api_key(self, user_id: int, name: str, key_hash: str, prefix: str, now: float) -> int:
        with self.db.connect() as conn:
            return conn.execute("INSERT INTO api_keys (user_id, name, key_hash, prefix, created_at) "
                                "VALUES (?, ?, ?, ?, ?) RETURNING id",
                                (user_id, name, key_hash, prefix, now)).fetchone()["id"]

    def api_keys(self, user_id: int) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, name, prefix, created_at, last_used_at, revoked_at FROM api_keys "
                                "WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def revoke_api_key(self, user_id: int, key_id: int, now: float) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? "
                               "AND revoked_at IS NULL", (now, key_id, user_id))
        return cur.rowcount == 1

    def use_api_key(self, key_hash: str, now: float) -> int | None:
        """The key's id if it exists and isn't revoked (and its owner isn't disabled), noting
        when it was last used; else None."""
        with self.db.connect() as conn:
            row = conn.execute("UPDATE api_keys SET last_used_at = ? WHERE key_hash = ? AND revoked_at IS NULL "
                               "AND user_id IN (SELECT id FROM users WHERE disabled = 0) RETURNING id",
                               (now, key_hash)).fetchone()
        return row["id"] if row else None

    def active_api_keys(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM api_keys WHERE revoked_at IS NULL").fetchone()["n"]

    # ---- Insights layouts (Day 8): the seller's own AG Studio report ------------

    def insights_layout(self, user_key: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT state, updated_at FROM insights_layouts WHERE user_key = ?",
                               (user_key,)).fetchone()
        return {"state": json.loads(row["state"]), "updated_at": row["updated_at"]} if row else None

    def set_insights_layout(self, user_key: str, state: dict) -> float:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO insights_layouts (user_key, state, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(user_key) DO UPDATE SET state = excluded.state,
                                                       updated_at = excluded.updated_at""",
                (user_key, json.dumps(state), now))
        return now

    def delete_insights_layout(self, user_key: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM insights_layouts WHERE user_key = ?", (user_key,))

    def log_agent_call(self, role: str, tool: str, outcome: str, order_ref: str | None,
                       keep: int = 500) -> None:
        """One MCP tool call: tool name, role, a short outcome and the last 6 characters of
        the order ID. Never arguments, messages, links or tokens."""
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO agent_calls (at, role, tool, outcome, order_ref) "
                         "VALUES (?, ?, ?, ?, ?)", (time.time(), role, tool, outcome, order_ref))
            conn.execute("DELETE FROM agent_calls WHERE id <= "
                         "(SELECT MAX(id) FROM agent_calls) - ?", (keep,))

    def agent_calls(self, limit: int = 20) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT at, role, tool, outcome, order_ref FROM agent_calls "
                                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
