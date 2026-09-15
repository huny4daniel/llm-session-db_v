import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);

-- 에이전트가 올린 파일별 수신 위치. 에이전트는 로컬 상태 없이 이 값을 기준으로 이어서 보낸다.
CREATE TABLE IF NOT EXISTS source_files (
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    file_key TEXT NOT NULL,
    session_uid TEXT NOT NULL,
    next_offset INTEGER NOT NULL DEFAULT 0,
    size INTEGER,
    mtime REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (machine_id, source, file_key)
);

-- 원본 줄 그대로 보관. 아래 파생 테이블은 모두 여기서 재생성할 수 있다.
CREATE TABLE IF NOT EXISTS raw_events (
    id INTEGER PRIMARY KEY,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    file_key TEXT NOT NULL,
    byte_offset INTEGER NOT NULL,
    line TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (machine_id, source, file_key, byte_offset)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    session_uid TEXT NOT NULL,
    project_key TEXT,
    project_path TEXT,
    git_branch TEXT,
    cli_version TEXT,
    ai_title TEXT,
    first_prompt TEXT,
    last_prompt TEXT,
    started_at TEXT,
    last_activity_at TEXT,
    user_turns INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    parent_session_uid TEXT,
    UNIQUE (machine_id, source, session_uid)
);
CREATE INDEX IF NOT EXISTS sessions_activity ON sessions (last_activity_at);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions (project_path);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    raw_event_id INTEGER NOT NULL REFERENCES raw_events(id) ON DELETE CASCADE,
    agent_id TEXT,
    uuid TEXT,
    parent_uuid TEXT,
    kind TEXT NOT NULL,
    timestamp TEXT,
    model TEXT,
    api_message_id TEXT,
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS messages_session ON messages (session_id, agent_id);
CREATE INDEX IF NOT EXISTS messages_raw ON messages (raw_event_id);

-- 한 응답이 여러 줄로 나뉘어 usage가 반복 기록되므로 API 메시지 ID 단위로 한 번만 센다.
CREATE TABLE IF NOT EXISTS api_usage (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    api_message_id TEXT NOT NULL,
    agent_id TEXT,
    model TEXT,
    timestamp TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, api_message_id)
);
CREATE INDEX IF NOT EXISTS api_usage_time ON api_usage (timestamp);

-- trigram: 한국어처럼 공백 단위 토큰화가 맞지 않는 언어도 부분 문자열로 검색한다(3자 이상).
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO messages_fts (rowid, text) VALUES (new.id, new.text);
END;
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # FastAPI는 한 요청의 의존성 생성·엔드포인트·정리를 서로 다른 스레드에서 실행할 수 있다.
    # 연결은 요청마다 새로 만들고 동시에 공유하지 않으므로 스레드 검사를 끈다.
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
