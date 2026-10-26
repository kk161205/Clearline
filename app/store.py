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
"""


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
