import hashlib
import secrets
import sqlite3

from .db import utcnow


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_machine(conn: sqlite3.Connection, name: str) -> str:
    """PC를 등록하고 평문 토큰을 반환한다. DB에는 해시만 저장한다."""
    token = secrets.token_urlsafe(32)
    with conn:
        conn.execute(
            "INSERT INTO machines (name, token_hash, created_at) VALUES (?, ?, ?)",
            (name, hash_token(token), utcnow()),
        )
    return token


def rotate_token(conn: sqlite3.Connection, name: str) -> str | None:
    token = secrets.token_urlsafe(32)
    with conn:
        cur = conn.execute("UPDATE machines SET token_hash = ? WHERE name = ?", (hash_token(token), name))
    return token if cur.rowcount else None


def find_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, name FROM machines WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()


def list_machines(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.id, m.name, m.created_at, m.last_seen_at, COUNT(s.id) AS session_count
        FROM machines m LEFT JOIN sessions s ON s.machine_id = m.id AND s.message_count > 0
        GROUP BY m.id ORDER BY m.name
        """
    )
    return [dict(r) for r in rows]
