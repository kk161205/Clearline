"""Copy Clearline's data from a SQLite file into the database at DATABASE_URL (Postgres).

    python -m app.migrate_sqlite --from clearline.db --dry-run   # counts only, writes nothing
    python -m app.migrate_sqlite --from clearline.db             # the copy
    python -m app.migrate_sqlite --from clearline.db --force     # target has rows: replace them

Every table is copied in one transaction (all or nothing), ids included, and identity counters
are moved past the copied ids. A target that already has rows is refused unless --force,
which deletes the target's rows in Clearline's tables first. Prints row counts per table;
never the database URL.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from .config import Settings
from .db import IDENTITY_TABLES, TABLES, Database, quiet_driver_logs
from .store import TimelineStore

BATCH = 500


def source_counts(src: sqlite3.Connection) -> dict[str, int]:
    present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES if t in present}


def target_counts(db: Database) -> dict[str, int]:
    return {t: db.count(t) for t in TABLES if db.table_exists(t)}


def print_counts(title: str, counts: dict[str, int]) -> None:
    print(title)
    for table in TABLES:
        if table in counts:
            print(f"  {table:<20} {counts[table]:>8}")
    print(f"  {'total':<20} {sum(counts.values()):>8}")


def copy(src: sqlite3.Connection, db: Database, tables: list[str], *, replace: bool) -> None:
    with db.transaction() as conn:  # all or nothing
        if replace:
            for table in reversed(TABLES):
                conn.execute(f"DELETE FROM {table}")
        for table in tables:
            cursor = src.execute(f"SELECT * FROM {table} LIMIT 0")
            source_cols = [d[0] for d in cursor.description]
            target_cols = {d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description}
            cols = [c for c in source_cols if c in target_cols]
            insert = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid")
            while batch := rows.fetchmany(BATCH):
                conn.executemany(insert, [tuple(r) for r in batch])
        if db.postgres:  # next generated id goes after the copied ones
            for table in IDENTITY_TABLES:
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                             f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrate_sqlite",
                                     description="Copy Clearline's SQLite data into DATABASE_URL.")
    parser.add_argument("--from", dest="source", required=True, help="the SQLite file, e.g. clearline.db")
    parser.add_argument("--dry-run", action="store_true", help="show the counts; write nothing")
    parser.add_argument("--force", action="store_true", help="replace rows already in the target")
    args = parser.parse_args(argv)

    quiet_driver_logs()
    settings = settings or Settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set: there's nowhere to copy to.")
    source = Path(args.source)
    if not source.is_file():
        raise SystemExit(f"No SQLite file at {source}.")
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)  # read-only
    try:
        counts = source_counts(src)
        print_counts(f"Source {source.name}:", counts)
        db = Database(settings.database_url)
        try:
            existing = target_counts(db)
        except Exception as err:  # never print the driver's message: it can name the host
            raise SystemExit(f"Couldn't reach the target database ({type(err).__name__}).")
        filled = {t: n for t, n in existing.items() if n}
        if filled:
            print_counts("Target already has rows:", filled)
        else:
            print("Target: empty" + ("" if existing else " (no Clearline tables yet)"))

        if args.dry_run:
            print("Dry run: nothing written.")
            return 0
        if filled and not args.force:
            raise SystemExit("The target already has data, so nothing was copied. "
                             "Use --force to replace it.")
        TimelineStore(settings.database_url)  # creates or updates the schema
        copy(src, db, list(counts), replace=bool(filled))
        copied = target_counts(db)
        print_counts("Copied:", {t: copied[t] for t in counts})
        mismatched = [t for t in counts if copied[t] != counts[t]]
        if mismatched:
            raise SystemExit(f"Row counts differ for: {', '.join(mismatched)}")
        print("Done.")
        return 0
    finally:
        src.close()


if __name__ == "__main__":
    sys.exit(main())
