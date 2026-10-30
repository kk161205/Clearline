"""Postgres for the tests: set TEST_DATABASE_URL and every test database is a fresh schema there.

Each test gets its own schema (cl_test_<random>), created on first use and dropped (with only
what the test made in it) when the test ends, so nothing else in that database is touched.
The schema is picked with search_path in the connection options, which needs a direct
connection: a Neon "-pooler" host is swapped for its direct host.
"""

import os
import secrets
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from app.db import close_pool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
_targets: dict[str, tuple[str, str]] = {}  # key (the test's tmp dir) -> (schema, url)


def _direct(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=parts.netloc.replace("-pooler.", ".")))


def _with_schema(url: str, schema: str) -> str:
    parts = urlsplit(_direct(url))
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-c search_path={schema}"
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def _admin():
    import psycopg

    return psycopg.connect(_direct(TEST_DATABASE_URL), autocommit=True, connect_timeout=15)


def target(key) -> str:
    """The Postgres URL of this test's own schema (created on first use)."""
    key = str(key)
    if key not in _targets:
        schema = f"cl_test_{secrets.token_hex(6)}"
        with _admin() as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
        _targets[key] = (schema, _with_schema(TEST_DATABASE_URL, schema))
    return _targets[key][1]


def cleanup() -> None:
    """Drop every schema this test created (and close its connections)."""
    if not _targets:
        return
    made = list(_targets.values())
    _targets.clear()
    for _, url in made:
        close_pool(url)
    with _admin() as conn:
        for schema, _ in made:
            assert schema.startswith("cl_test_")
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
