"""Manage seller accounts from the command line, against the same database as the app.

    python -m app.users create --email you@example.com      # asks for the password twice
    python -m app.users reset-password --email you@example.com
    python -m app.users disable --email you@example.com
    python -m app.users enable --email you@example.com
    python -m app.users list

    # Non-interactive (scripts, a host's shell): the password is the first line of stdin.
    python -m app.users create --email you@example.com --password-stdin < password.txt

Passwords are typed at the prompt (getpass) or read from stdin, never passed as arguments,
and never printed. Resetting a password or disabling an account signs that account out
everywhere. Uses DATABASE_URL (Postgres) if set, else the SQLite file, like the app.
"""

import argparse
import getpass
import sys
from datetime import datetime, timezone

from .accounts import Accounts, normalise_email, password_problem
from .config import Settings
from .db import quiet_driver_logs
from .store import TimelineStore


def ask_password(email: str) -> str:
    for _ in range(3):
        first = getpass.getpass("New password (12-128 characters): ")
        second = getpass.getpass("Repeat it: ")
        if first != second:
            print("The passwords don't match. Try again.")
            continue
        problem = password_problem(first, email)
        if problem:
            print(problem)
            continue
        return first
    raise SystemExit("No password set.")


def read_password_stdin(email: str) -> str:
    """The first line of stdin (without its line ending), checked against the policy."""
    password = sys.stdin.readline().rstrip("\r\n")
    problem = password_problem(password, email)
    if problem:
        raise SystemExit(problem)
    return password


def when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "never"


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.users", description="Manage Clearline seller accounts.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "reset-password", "disable", "enable"):
        command = sub.add_parser(name)
        command.add_argument("--email", required=True)
        if name in ("create", "reset-password"):
            command.add_argument("--password-stdin", action="store_true",
                                 help="read the password from the first line of stdin instead of prompting")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    quiet_driver_logs()
    settings = settings or Settings()
    store = TimelineStore(settings.database_target)
    accounts = Accounts(store)

    if args.command == "list":
        users = store.list_users()
        if not users:
            print("No seller accounts yet.")
        for u in users:
            print(f"{u['id']:>4}  {u['email']:<40}  created {when(u['created_at'])}  "
                  f"last login {when(u['last_login_at'])}  {'disabled' if u['disabled'] else 'active'}")
        return 0

    email = normalise_email(args.email)
    new_password = read_password_stdin if getattr(args, "password_stdin", False) else ask_password
    if args.command == "create":
        if store.user_by_email(email):
            raise SystemExit("An account with that email already exists.")
        try:
            user_id = accounts.create_user(email, new_password(email))
        except ValueError as err:
            raise SystemExit(str(err))
        print(f"Created seller account {user_id}.")
        return 0

    user = store.user_by_email(email)
    if not user:
        raise SystemExit("No account with that email.")
    if args.command == "reset-password":
        accounts.set_password(user["id"], new_password(email))  # ends every session
        print(f"Password reset for account {user['id']}; it has been signed out everywhere.")
    elif args.command == "disable":
        accounts.set_disabled(user["id"], True)
        print(f"Account {user['id']} disabled and signed out everywhere.")
    else:
        accounts.set_disabled(user["id"], False)
        print(f"Account {user['id']} enabled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
