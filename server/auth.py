"""웹 UI 비밀번호 인증. 에이전트 API는 PC별 토큰으로 따로 인증한다.

- 비밀번호가 없으면 이 PC(루프백)에서 온 요청만 허용한다.
- 비밀번호가 있으면 모든 조회 요청에 로그인 쿠키를 요구한다.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

COOKIE_NAME = "lsdb_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# tailscale serve 같은 리버스 프록시를 거친 요청은 루프백에서 온 것처럼 보이므로 이 헤더가 있으면 원격으로 본다.
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "tailscale-user-login")

PASSWORD_KEY = "ui_password_hash"
SECRET_KEY = "session_secret"


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def password_enabled(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, PASSWORD_KEY) is not None


def set_password(conn: sqlite3.Connection, password: str) -> None:
    """비밀번호를 바꾸면 서명 키도 새로 만들어 기존 로그인을 모두 끊는다."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다")
    with conn:
        _put(conn, PASSWORD_KEY, hash_password(password))
        _put(conn, SECRET_KEY, secrets.token_hex(32))


def clear_password(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM settings WHERE key IN (?, ?)", (PASSWORD_KEY, SECRET_KEY))


def verify_login(conn: sqlite3.Connection, password: str) -> bool:
    stored = get_setting(conn, PASSWORD_KEY)
    return stored is not None and verify_password(password, stored)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        rounds = int(iterations)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(digest.hex(), digest_hex)


def issue_session(conn: sqlite3.Connection, now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + SESSION_TTL_SECONDS)
    signature = _sign(conn, expires)
    if signature is None:
        raise RuntimeError("비밀번호가 설정되어 있지 않습니다")
    return f"{expires}.{signature}"


def check_session(conn: sqlite3.Connection, token: str | None, now: float | None = None) -> bool:
    if not token:
        return False
    expires, _, signature = token.partition(".")
    if not expires.isdigit() or int(expires) < (time.time() if now is None else now):
        return False
    expected = _sign(conn, int(expires))
    return expected is not None and hmac.compare_digest(signature, expected)


def is_local_request(client_host: str | None, headers) -> bool:
    return client_host in LOOPBACK_HOSTS and not any(name in headers for name in PROXY_HEADERS)


class LoginLimiter:
    """연속 실패가 쌓인 주소를 잠시 잠근다."""

    def __init__(self, max_failures: int = MAX_FAILURES, lockout_seconds: float = LOCKOUT_SECONDS, clock=time.monotonic):
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.clock = clock
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> float:
        with self._lock:
            count, locked_until = self._failures.get(key, (0, 0.0))
            return max(0.0, locked_until - self.clock()) if count >= self.max_failures else 0.0

    def failure(self, key: str) -> None:
        with self._lock:
            count, locked_until = self._failures.get(key, (0, 0.0))
            if count >= self.max_failures and locked_until <= self.clock():
                count = 0  # 잠금이 끝난 뒤 다시 실패하면 새로 센다
            count += 1
            if count >= self.max_failures:
                locked_until = self.clock() + self.lockout_seconds
            self._failures[key] = (count, locked_until)

    def success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def _put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _sign(conn: sqlite3.Connection, expires: int) -> str | None:
    secret = get_setting(conn, SECRET_KEY)
    if secret is None:
        return None
    return hmac.new(bytes.fromhex(secret), str(expires).encode("ascii"), hashlib.sha256).hexdigest()
