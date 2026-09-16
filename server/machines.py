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


def rename_machine(conn: sqlite3.Connection, name: str, new_name: str) -> bool:
    """이름만 바꾼다. 세션·수신 위치는 PC 번호로 연결되어 있어 그대로 유지된다. 이름이 겹치면 IntegrityError."""
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("새 이름을 입력하세요")
    with conn:
        cur = conn.execute("UPDATE machines SET name = ? WHERE name = ?", (new_name, name))
    return bool(cur.rowcount)


def delete_machine(conn: sqlite3.Connection, name: str) -> int | None:
    """PC와 그 PC의 세션·원본 줄·수신 위치·삭제 표시를 모두 지운다. 지운 세션 수를 돌려준다(없는 이름이면 None).

    토큰도 함께 사라지므로 그 PC의 에이전트는 더 이상 올릴 수 없다. 다시 쓰려면 새로 등록해 토큰을 바꿔야 한다.
    """
    row = conn.execute("SELECT id FROM machines WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    with conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE machine_id = ? AND message_count > 0", (row["id"],)
        ).fetchone()[0]
        # 이 PC 세션을 자식으로 둔 분기 링크는 세션 uid로만 연결되어 있어 직접 지운다(나머지는 외래 키 CASCADE).
        conn.execute(
            "DELETE FROM session_links WHERE (source, child_uid) IN"
            " (SELECT source, session_uid FROM sessions WHERE machine_id = ?)",
            (row["id"],),
        )
        conn.execute("DELETE FROM machines WHERE id = ?", (row["id"],))
    return count


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
