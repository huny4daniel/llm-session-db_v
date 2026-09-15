"""웹 UI용 조회 쿼리."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from .parsers import PARSERS

TOTAL_TOKENS = "(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens)"

SESSION_COLUMNS = """
    s.id, s.source, s.session_uid, s.project_key, s.project_path, s.git_branch, s.cli_version,
    COALESCE(s.ai_title, s.first_prompt, s.last_prompt, s.session_uid) AS title,
    s.ai_title, s.first_prompt, s.last_prompt, s.started_at, s.last_activity_at,
    s.user_turns, s.message_count, s.input_tokens, s.output_tokens,
    s.cache_creation_tokens, s.cache_read_tokens, s.cost_usd, s.parent_session_uid,
    mc.id AS machine_id, mc.name AS machine_name
"""


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT project_path, COUNT(*) AS session_count, MAX(last_activity_at) AS last_activity_at,
               SUM({TOTAL_TOKENS}) AS total_tokens
        FROM sessions
        WHERE message_count > 0 AND project_path IS NOT NULL
        GROUP BY project_path
        ORDER BY last_activity_at DESC
        """
    )
    return [dict(r) for r in rows]


def list_sessions(
    conn: sqlite3.Connection,
    *,
    machine_id: int | None = None,
    project_path: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where = ["s.message_count > 0"]
    params: list = []
    if machine_id is not None:
        where.append("s.machine_id = ?")
        params.append(machine_id)
    if project_path:
        where.append("s.project_path = ?")
        params.append(project_path)
    if source:
        where.append("s.source = ?")
        params.append(source)
    if q:
        pattern = f"%{_escape_like(q)}%"
        where.append(
            "(s.ai_title LIKE ? ESCAPE '\\' OR s.first_prompt LIKE ? ESCAPE '\\'"
            " OR s.project_path LIKE ? ESCAPE '\\' OR s.session_uid LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 4)
    where_sql = " AND ".join(where)

    total = conn.execute(f"SELECT COUNT(*) FROM sessions s WHERE {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT {SESSION_COLUMNS}
        FROM sessions s JOIN machines mc ON mc.id = s.machine_id
        WHERE {where_sql}
        ORDER BY s.last_activity_at DESC NULLS LAST
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    return {"total": total, "items": [dict(r) for r in rows]}


def get_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM sessions s JOIN machines mc ON mc.id = s.machine_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    session = dict(row)

    agents = []
    for agent in conn.execute(
        """
        SELECT agent_id, COUNT(*) AS message_count, MIN(timestamp) AS started_at,
               MAX(timestamp) AS last_activity_at
        FROM messages WHERE session_id = ? AND agent_id IS NOT NULL
        GROUP BY agent_id ORDER BY started_at
        """,
        (session_id,),
    ):
        prompt = conn.execute(
            "SELECT text FROM messages WHERE session_id = ? AND agent_id = ? AND kind = 'user'"
            " ORDER BY raw_event_id LIMIT 1",
            (session_id, agent["agent_id"]),
        ).fetchone()
        agents.append({**dict(agent), "prompt": prompt["text"][:300] if prompt else None})
    session["agents"] = agents
    return session


def session_messages(
    conn: sqlite3.Connection, session_id: int, *, agent_id: str | None = None, include_meta: bool = False
) -> list[dict] | None:
    session = conn.execute("SELECT source FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        return None
    parser = PARSERS[session["source"]]
    kind_filter = "" if include_meta else "AND m.kind != 'meta'"
    rows = conn.execute(
        f"""
        SELECT m.id, m.kind, m.uuid, m.parent_uuid, m.timestamp, m.model, m.api_message_id, r.line
        FROM messages m JOIN raw_events r ON r.id = m.raw_event_id
        WHERE m.session_id = ? AND m.agent_id IS ? {kind_filter}
        ORDER BY r.byte_offset
        """,
        (session_id, agent_id),
    )
    result = []
    for row in rows:
        item = dict(row)
        item["blocks"] = parser.view_blocks(item.pop("line"))
        result.append(item)
    return result


def search_messages(conn: sqlite3.Connection, query: str, *, limit: int = 50) -> list[dict]:
    """공백으로 나눈 모든 단어를 포함하는 메시지. 3자 이상은 trigram 인덱스, 짧은 단어는 LIKE로 거른다."""
    terms = query.split()
    if not terms:
        return []
    long_terms = [t for t in terms if len(t) >= 3]
    short_terms = [t for t in terms if len(t) < 3]

    select = """
        SELECT m.id AS message_id, m.session_id, m.agent_id, m.kind, m.timestamp, m.text,
               COALESCE(s.ai_title, s.first_prompt, s.last_prompt, s.session_uid) AS session_title,
               s.project_path, mc.name AS machine_name
    """
    params: list = []
    if long_terms:
        sql = f"""{select}
            FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id JOIN machines mc ON mc.id = s.machine_id
            WHERE messages_fts MATCH ?"""
        params.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms))
    else:
        sql = f"""{select}
            FROM messages m JOIN sessions s ON s.id = m.session_id JOIN machines mc ON mc.id = s.machine_id
            WHERE 1 = 1"""
    for term in short_terms:
        sql += " AND m.text LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(term)}%")
    sql += " AND m.kind != 'meta' ORDER BY m.timestamp DESC LIMIT ?"
    params.append(limit)

    results = []
    for row in conn.execute(sql, params):
        item = dict(row)
        item["snippet"] = _snippet(item.pop("text"), terms)
        results.append(item)
    return results


def stats(conn: sqlite3.Connection, *, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    sums = """
        SUM(u.input_tokens) AS input_tokens, SUM(u.output_tokens) AS output_tokens,
        SUM(u.cache_creation_tokens) AS cache_creation_tokens, SUM(u.cache_read_tokens) AS cache_read_tokens,
        SUM(u.input_tokens + u.output_tokens + u.cache_creation_tokens + u.cache_read_tokens) AS total_tokens
    """

    daily_rows = {
        r["day"]: dict(r)
        for r in conn.execute(
            f"SELECT date(u.timestamp, 'localtime') AS day, {sums} FROM api_usage u"
            " WHERE u.timestamp >= ? GROUP BY day",
            (since,),
        )
    }
    today = date.today()
    empty = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0}
    daily = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        daily.append({**empty, **daily_rows.get(day, {}), "day": day})

    def grouped(column: str, joins: str = "") -> list[dict]:
        rows = conn.execute(
            f"SELECT {column} AS name, COUNT(DISTINCT u.session_id) AS session_count, {sums}"
            f" FROM api_usage u {joins} WHERE u.timestamp >= ? GROUP BY name ORDER BY total_tokens DESC",
            (since,),
        )
        return [dict(r) for r in rows]

    sessions_join = "JOIN sessions s ON s.id = u.session_id"
    totals = conn.execute(
        f"SELECT COUNT(DISTINCT u.session_id) AS session_count, {sums} FROM api_usage u WHERE u.timestamp >= ?",
        (since,),
    ).fetchone()
    cost = conn.execute(
        "SELECT SUM(cost_usd) AS cost_usd, COUNT(cost_usd) AS sessions_with_cost FROM sessions"
        " WHERE last_activity_at >= ?",
        (since,),
    ).fetchone()

    return {
        "days": days,
        "totals": {**empty, **{k: v or 0 for k, v in dict(totals).items()}, **dict(cost)},
        "daily": daily,
        "by_model": grouped("u.model"),
        "by_project": grouped("s.project_path", sessions_join),
        "by_machine": grouped("mc.name", f"{sessions_join} JOIN machines mc ON mc.id = s.machine_id"),
    }


def _snippet(text: str, terms: list[str], radius: int = 60) -> dict:
    lower = text.lower()
    position, hit = -1, ""
    for term in terms:
        index = lower.find(term.lower())
        if index >= 0 and (position < 0 or index < position):
            position, hit = index, term
    if position < 0:
        return {"before": text[: radius * 2], "match": "", "after": ""}
    start = max(0, position - radius)
    end = position + len(hit)
    return {
        "before": ("…" if start else "") + text[start:position],
        "match": text[position:end],
        "after": text[end : end + radius] + ("…" if end + radius < len(text) else ""),
    }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
