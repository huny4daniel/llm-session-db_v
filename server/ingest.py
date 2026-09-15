"""에이전트가 올린 원본 줄을 저장하고 파생 테이블(sessions/messages/api_usage)을 갱신한다."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from .db import utcnow
from .parsers import PARSERS


class IngestError(ValueError):
    pass


class OffsetMismatch(Exception):
    """에이전트가 보낸 시작 위치가 서버의 수신 위치와 다를 때. 에이전트는 expected_offset부터 다시 보낸다."""

    def __init__(self, expected_offset: int):
        super().__init__(f"expected offset {expected_offset}")
        self.expected_offset = expected_offset


@dataclass(frozen=True)
class Line:
    offset: int
    length: int  # 개행 포함 바이트 길이
    text: str


def ingest_batch(
    conn: sqlite3.Connection,
    *,
    machine_id: int,
    source: str,
    file_key: str,
    start_offset: int,
    lines: list[Line],
    size: int | None = None,
    mtime: float | None = None,
    reset: bool = False,
) -> int:
    """한 파일의 연속된 줄 묶음을 반영하고 새 수신 위치를 반환한다."""
    parser = _parser(source)
    ref = parser.parse_file_key(file_key)
    if ref is None:
        raise IngestError(f"알 수 없는 파일 경로입니다: {file_key}")
    _check_contiguous(start_offset, lines)
    next_offset = lines[-1].offset + lines[-1].length if lines else start_offset
    now = utcnow()

    with conn:
        row = conn.execute(
            "SELECT next_offset FROM source_files WHERE machine_id = ? AND source = ? AND file_key = ?",
            (machine_id, source, file_key),
        ).fetchone()
        current = 0 if row is None or reset else row["next_offset"]
        if start_offset != current:
            raise OffsetMismatch(current)

        if reset:
            conn.execute(
                "DELETE FROM raw_events WHERE machine_id = ? AND source = ? AND file_key = ?",
                (machine_id, source, file_key),
            )
        if _is_deleted(conn, machine_id, source, ref.session_uid):
            # 삭제한 세션은 다시 만들지 않고 수신 위치만 앞으로 옮긴다.
            _upsert_source_file(conn, machine_id, source, file_key, ref, next_offset, size, mtime, now)
            conn.execute("UPDATE machines SET last_seen_at = ? WHERE id = ?", (now, machine_id))
            return next_offset
        session_id = _get_or_create_session(conn, machine_id, source, ref)
        first_main_batch = ref.agent_id is None and not _has_main_messages(conn, session_id)

        inserted = []
        for line in lines:
            cur = conn.execute(
                "INSERT OR IGNORE INTO raw_events (machine_id, source, file_key, byte_offset, line, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (machine_id, source, file_key, line.offset, line.text, now),
            )
            if cur.rowcount:
                inserted.append((cur.lastrowid, line.text))

        _upsert_source_file(conn, machine_id, source, file_key, ref, next_offset, size, mtime, now)

        if reset:
            rebuild_session(conn, session_id)
        else:
            _apply_lines(conn, parser, session_id, ref, inserted)
            refresh_session_stats(conn, session_id)
        if first_main_batch:
            _link_fork_parent(conn, session_id, source, ref.session_uid)
        conn.execute("UPDATE machines SET last_seen_at = ? WHERE id = ?", (now, machine_id))
    return next_offset


def rebuild_session(conn: sqlite3.Connection, session_id: int) -> None:
    """세션의 파생 데이터를 지우고 원본 줄에서 다시 만든다. 호출자가 트랜잭션을 관리한다."""
    session = conn.execute(
        "SELECT machine_id, source, session_uid FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    parser = _parser(session["source"])

    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM api_usage WHERE session_id = ?", (session_id,))
    meta_columns = sorted(parser.META_FIRST_COLUMNS | parser.META_LAST_COLUMNS)
    conn.execute(
        f"UPDATE sessions SET {', '.join(f'{c} = NULL' for c in meta_columns)} WHERE id = ?",
        (session_id,),
    )

    file_keys = [
        r["file_key"]
        for r in conn.execute(
            "SELECT file_key FROM source_files WHERE machine_id = ? AND source = ? AND session_uid = ?"
            " ORDER BY file_key",
            (session["machine_id"], session["source"], session["session_uid"]),
        )
    ]
    for file_key in file_keys:
        ref = parser.parse_file_key(file_key)
        rows = conn.execute(
            "SELECT id, line FROM raw_events WHERE machine_id = ? AND source = ? AND file_key = ?"
            " ORDER BY byte_offset",
            (session["machine_id"], session["source"], file_key),
        ).fetchall()
        _apply_lines(conn, parser, session_id, ref, ((r["id"], r["line"]) for r in rows))
    refresh_session_stats(conn, session_id)


def delete_session(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    """세션과 원본 줄을 지우고 삭제 표시를 남긴다. PC의 원본 파일은 건드리지 않는다.

    수신 위치(source_files)는 남겨 에이전트가 같은 파일을 처음부터 다시 보내지 않게 한다.
    """
    with conn:
        session = conn.execute(
            "SELECT id, machine_id, source, session_uid FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            return None
        key = (session["machine_id"], session["source"], session["session_uid"])
        conn.execute(
            "INSERT OR REPLACE INTO deleted_sessions (machine_id, source, session_uid, deleted_at) VALUES (?, ?, ?, ?)",
            (*key, utcnow()),
        )
        conn.execute(
            "DELETE FROM raw_events WHERE (machine_id, source, file_key) IN"
            " (SELECT machine_id, source, file_key FROM source_files"
            "  WHERE machine_id = ? AND source = ? AND session_uid = ?)",
            key,
        )
        conn.execute("DELETE FROM session_links WHERE source = ? AND child_uid = ?", (session["source"], session["session_uid"]))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return session


def record_fork(conn: sqlite3.Connection, parent_session_id: int, child_uid: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_links (source, child_uid, parent_session_id, created_at)"
            " SELECT source, ?, id, ? FROM sessions WHERE id = ?",
            (child_uid, utcnow(), parent_session_id),
        )


def rebuild_all(conn: sqlite3.Connection) -> int:
    session_ids = [r["id"] for r in conn.execute("SELECT id FROM sessions")]
    for session_id in session_ids:
        with conn:
            rebuild_session(conn, session_id)
    return len(session_ids)


def refresh_session_stats(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute(
        """
        UPDATE sessions SET
            user_turns = (SELECT COUNT(*) FROM messages
                          WHERE session_id = :id AND kind = 'user' AND agent_id IS NULL),
            message_count = (SELECT COUNT(*) FROM messages WHERE session_id = :id AND kind = 'user')
                          + (SELECT COUNT(DISTINCT api_message_id) FROM messages
                             WHERE session_id = :id AND kind = 'assistant'),
            started_at = (SELECT MIN(timestamp) FROM messages WHERE session_id = :id),
            last_activity_at = (SELECT MAX(timestamp) FROM messages WHERE session_id = :id),
            input_tokens = (SELECT COALESCE(SUM(input_tokens), 0) FROM api_usage WHERE session_id = :id),
            output_tokens = (SELECT COALESCE(SUM(output_tokens), 0) FROM api_usage WHERE session_id = :id),
            cache_creation_tokens = (SELECT COALESCE(SUM(cache_creation_tokens), 0) FROM api_usage
                                     WHERE session_id = :id),
            cache_read_tokens = (SELECT COALESCE(SUM(cache_read_tokens), 0) FROM api_usage
                                 WHERE session_id = :id)
        WHERE id = :id
        """,
        {"id": session_id},
    )


def _apply_lines(conn, parser, session_id: int, ref, rows: Iterable[tuple[int, str]]) -> None:
    meta_first: dict = {}
    meta_last: dict = {}
    for raw_event_id, text in rows:
        parsed = parser.parse_line(text, ref)
        message = parsed.message
        if message is not None and not _message_exists(conn, session_id, message.uuid):
            conn.execute(
                "INSERT INTO messages (session_id, raw_event_id, agent_id, uuid, parent_uuid, kind,"
                " timestamp, model, api_message_id, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, raw_event_id, ref.agent_id, message.uuid, message.parent_uuid,
                    message.kind, message.timestamp, message.model, message.api_message_id, message.text,
                ),
            )
        usage = parsed.usage
        if usage is not None:
            # 같은 응답의 뒤쪽 줄일수록 누적 토큰이 크므로 최댓값을 유지한다.
            conn.execute(
                "INSERT INTO api_usage (session_id, api_message_id, agent_id, model, timestamp,"
                " input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (session_id, api_message_id) DO UPDATE SET"
                " input_tokens = MAX(input_tokens, excluded.input_tokens),"
                " output_tokens = MAX(output_tokens, excluded.output_tokens),"
                " cache_creation_tokens = MAX(cache_creation_tokens, excluded.cache_creation_tokens),"
                " cache_read_tokens = MAX(cache_read_tokens, excluded.cache_read_tokens)",
                (
                    session_id, usage.api_message_id, ref.agent_id, usage.model, usage.timestamp,
                    usage.input_tokens, usage.output_tokens, usage.cache_creation_tokens,
                    usage.cache_read_tokens,
                ),
            )
        for column, value in parsed.meta_first.items():
            meta_first.setdefault(column, value)
        meta_last.update(parsed.meta_last)

    for column, value in meta_first.items():
        if column not in parser.META_FIRST_COLUMNS:
            raise IngestError(f"허용되지 않은 세션 컬럼입니다: {column}")
        conn.execute(f"UPDATE sessions SET {column} = COALESCE({column}, ?) WHERE id = ?", (value, session_id))
    for column, value in meta_last.items():
        if column not in parser.META_LAST_COLUMNS:
            raise IngestError(f"허용되지 않은 세션 컬럼입니다: {column}")
        conn.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?", (value, session_id))


def _has_main_messages(conn, session_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? AND agent_id IS NULL LIMIT 1", (session_id,)
    ).fetchone() is not None


def _link_fork_parent(conn, session_id: int, source: str, session_uid: str) -> None:
    """fork(--fork-session)로 생긴 세션은 원본 메시지의 uuid를 복사해 온다(검증된 CLI 동작).

    가장 많이 겹치는 세션을 부모로 기록하고, 같으면 자기만의 메시지가 가장 적은 세션을 고른다
    (같은 원본에서 갈라진 형제 세션보다 원본이 우선).
    """
    row = conn.execute(
        """
        SELECT other.session_id AS parent_id, COUNT(*) AS shared,
               (SELECT COUNT(*) FROM messages x
                WHERE x.session_id = other.session_id AND x.agent_id IS NULL AND x.uuid IS NOT NULL) - COUNT(*) AS extra
        FROM messages mine
        JOIN messages other ON other.uuid = mine.uuid AND other.session_id != mine.session_id AND other.agent_id IS NULL
        JOIN sessions p ON p.id = other.session_id AND p.source = ?
        WHERE mine.session_id = ? AND mine.agent_id IS NULL AND mine.uuid IS NOT NULL
        GROUP BY other.session_id
        ORDER BY shared DESC, extra ASC
        LIMIT 1
        """,
        (source, session_id),
    ).fetchone()
    if row is not None:
        conn.execute(
            "INSERT OR IGNORE INTO session_links (source, child_uid, parent_session_id, created_at) VALUES (?, ?, ?, ?)",
            (source, session_uid, row["parent_id"], utcnow()),
        )


def _message_exists(conn, session_id: int, uuid: str | None) -> bool:
    """같은 세션 파일이 여러 폴더에 있으면(가져오기·복사) 같은 메시지가 여러 번 들어오므로 uuid로 거른다."""
    if uuid is None:
        return False
    return conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? AND uuid = ? LIMIT 1", (session_id, uuid)
    ).fetchone() is not None


def _is_deleted(conn, machine_id: int, source: str, session_uid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM deleted_sessions WHERE machine_id = ? AND source = ? AND session_uid = ?",
        (machine_id, source, session_uid),
    ).fetchone() is not None


def _upsert_source_file(conn, machine_id, source, file_key, ref, next_offset, size, mtime, now) -> None:
    conn.execute(
        "INSERT INTO source_files (machine_id, source, file_key, session_uid, next_offset, size, mtime, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (machine_id, source, file_key) DO UPDATE SET"
        " next_offset = excluded.next_offset, size = excluded.size,"
        " mtime = excluded.mtime, updated_at = excluded.updated_at",
        (machine_id, source, file_key, ref.session_uid, next_offset, size, mtime, now),
    )


def _get_or_create_session(conn, machine_id: int, source: str, ref) -> int:
    row = conn.execute(
        "SELECT id FROM sessions WHERE machine_id = ? AND source = ? AND session_uid = ?",
        (machine_id, source, ref.session_uid),
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO sessions (machine_id, source, session_uid, project_key) VALUES (?, ?, ?, ?)",
        (machine_id, source, ref.session_uid, ref.project_key),
    )
    return cur.lastrowid


def _check_contiguous(start_offset: int, lines: list[Line]) -> None:
    expected = start_offset
    for line in lines:
        if line.offset != expected or line.length <= 0:
            raise IngestError(f"줄 위치가 연속적이지 않습니다: {line.offset} (예상 {expected})")
        expected += line.length


def _parser(source: str):
    parser = PARSERS.get(source)
    if parser is None:
        raise IngestError(f"지원하지 않는 소스입니다: {source}")
    return parser
