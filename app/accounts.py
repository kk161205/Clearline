"""Seller accounts: Argon2id passwords, lockout, DB-backed sessions, the one-time setup code and
agent API keys.

Nothing here is a credential in the environment: the first account is created at /setup (with a
setup code printed to the server log) or with `python -m app.users create`. Sessions live in the
database; the browser only holds a random token whose SHA-256 is the session id.
"""

import hashlib
import hmac
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .store import TimelineStore

HASHER = PasswordHasher()  # Argon2id with the library's defaults; tests swap in a fast profile
GENERIC_LOGIN_ERROR = "Email or password is incorrect."
MAX_FAILED = 5
LOCK_SECONDS = 15 * 60
SESSION_IDLE = 7 * 86400
SESSION_ABSOLUTE = 30 * 86400
SESSION_COOKIE = "clearline_session"
API_KEY_PREFIX = "cl_live_"
# Common passwords at least 12 characters long (shorter ones are already too short).
COMMON_PASSWORDS = {
    "123456789012", "1234567890123", "12345678901234", "123456123456", "111111111111",
    "000000000000", "password1234", "password12345", "password123!", "passwordpassword",
    "qwertyuiopas", "qwertyuiop12", "qwerty123456", "1q2w3e4r5t6y", "1qaz2wsx3edc",
    "iloveyou1234", "letmein12345", "welcome12345", "administrator", "admin1234567",
    "changeme1234", "abcdefghijkl", "abc123abc123", "trustno11234", "football1234",
    "baseball1234", "sunshine1234", "princess1234", "superman1234", "monkey123456",
    "dragon123456", "master123456", "correcthorsebatterystaple", "passw0rd1234",
}


def now() -> float:
    """The clock (tests freeze it)."""
    return time.time()


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def password_problem(password: str, email: str) -> str | None:
    """Why a password can't be used, or None. 12-128 characters, not a common password, and not
    containing the email's name part. No other rules."""
    password = password or ""
    if len(password) < 12:
        return "Use at least 12 characters."
    if len(password) > 128:
        return "Use at most 128 characters."
    if password.lower() in COMMON_PASSWORDS:
        return "That password is too common. Pick another one."
    local = normalise_email(email).split("@")[0]
    if len(local) >= 3 and local in password.lower():
        return "Don't use your email address in the password."
    return None


def hash_password(password: str) -> str:
    return HASHER.hash(password)


def _dummy_hash() -> str:
    """A hash to verify against when the email is unknown, so both cases take the same time."""
    global _DUMMY
    if _DUMMY is None or _DUMMY[0] is not HASHER:
        _DUMMY = (HASHER, HASHER.hash(secrets.token_urlsafe(16)))
    return _DUMMY[1]


_DUMMY: tuple | None = None


def _verify(password_hash: str, password: str) -> bool:
    try:
        return HASHER.verify(password_hash, password or "")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


class Accounts:
    def __init__(self, store: TimelineStore):
        self.store = store

    # ---- users ------------------------------------------------------------------------

    def create_user(self, email: str, password: str) -> int:
        """A new account. Raises ValueError with a plain reason if it can't be created."""
        email = normalise_email(email)
        if "@" not in email or len(email) > 254 or email.startswith("@") or email.endswith("@"):
            raise ValueError("Enter a valid email address.")
        problem = password_problem(password, email)
        if problem:
            raise ValueError(problem)
        if self.store.user_by_email(email):
            raise ValueError("An account with that email already exists.")
        return self.store.create_user(email, hash_password(password), now())

    def authenticate(self, email: str, password: str) -> tuple[dict | None, str]:
        """(user, outcome): outcome is "ok", "bad", "locked" (just reached the limit or still
        locked) or "disabled". Every failure looks the same to the caller's user."""
        user = self.store.user_by_email(normalise_email(email))
        if not user:
            _verify(_dummy_hash(), password)  # same work as a real check
            return None, "bad"
        t = now()
        matches = _verify(user["password_hash"], password)
        if user["locked_until"] and user["locked_until"] > t:
            return user, "locked"
        if user["disabled"]:
            return user, "disabled"
        if not matches:
            failed = (user["failed_logins"] or 0) + 1
            if failed >= MAX_FAILED:
                self.store.update_user(user["id"], failed_logins=0, locked_until=t + LOCK_SECONDS)
                return user, "locked"
            self.store.update_user(user["id"], failed_logins=failed)
            return user, "bad"
        fields = {"failed_logins": 0, "locked_until": None, "last_login_at": t}
        if HASHER.check_needs_rehash(user["password_hash"]):
            fields["password_hash"] = hash_password(password)
        self.store.update_user(user["id"], **fields)
        return user, "ok"

    def check_password(self, user: dict, password: str) -> bool:
        return _verify(user["password_hash"], password)

    def set_password(self, user_id: int, password: str, *, keep_session: str | None = None) -> None:
        """New password; every session of the user ends except `keep_session` (the caller's)."""
        user = self.store.user_by_id(user_id)
        problem = password_problem(password, user["email"])
        if problem:
            raise ValueError(problem)
        self.store.update_user(user_id, password_hash=hash_password(password), failed_logins=0, locked_until=None)
        self.store.delete_user_sessions(user_id, keep=keep_session)

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        self.store.update_user(user_id, disabled=1 if disabled else 0)
        if disabled:
            self.store.delete_user_sessions(user_id)

    # ---- sessions ----------------------------------------------------------------------

    @staticmethod
    def session_id(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def start_session(self, user_id: int) -> str:
        """A new session; returns the raw token for the cookie (only its hash is stored)."""
        token = secrets.token_urlsafe(32)
        t = now()
        self.store.create_session(self.session_id(token), user_id, t, t + SESSION_ABSOLUTE)
        return token

    def session_user(self, token: str | None) -> tuple[dict, str] | None:
        """(user, session id) for a live session: within 7 days of last use, 30 days of creation,
        and the user not disabled. Expired sessions are deleted on sight."""
        if not token or len(token) > 200:
            return None
        sid = self.session_id(token)
        row = self.store.session(sid)
        if not row:
            return None
        t = now()
        if row["expires_at"] <= t or row["last_seen_at"] <= t - SESSION_IDLE:
            self.store.delete_session(sid)
            return None
        user = self.store.user_by_id(row["user_id"])
        if not user or user["disabled"]:
            self.store.delete_session(sid)
            return None
        if t - row["last_seen_at"] > 60:  # at most one write a minute per session
            self.store.touch_session(sid, t)
        return user, sid

    def end_session(self, token: str | None) -> None:
        if token:
            self.store.delete_session(self.session_id(token))

    def cleanup(self) -> int:
        return self.store.delete_expired_sessions(now(), SESSION_IDLE)

    # ---- agent API keys ------------------------------------------------------------------

    def create_api_key(self, user_id: int, name: str) -> dict:
        """A new key; the raw key is returned once and only its SHA-256 is stored."""
        key = API_KEY_PREFIX + secrets.token_urlsafe(32)
        key_id = self.store.create_api_key(user_id, name.strip()[:60] or "Agent", hashlib.sha256(key.encode()).hexdigest(),
                                           key[:len(API_KEY_PREFIX) + 4], now())
        return {"id": key_id, "key": key}

    def api_key_ok(self, key: str | None) -> bool:
        if not key or not key.startswith(API_KEY_PREFIX) or len(key) > 200:
            return False
        return self.store.use_api_key(hashlib.sha256(key.encode()).hexdigest(), now()) is not None


def new_setup_code() -> str:
    """XXXX-XXXX-XXXX from an alphabet without look-alike characters."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def setup_code_matches(given: str, expected: str | None) -> bool:
    if not expected:
        return False
    norm = (given or "").strip().upper().replace(" ", "")
    return hmac.compare_digest(norm.encode(), expected.encode())
