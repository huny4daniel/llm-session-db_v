import pytest
from fastapi.testclient import TestClient

from server import db, machines
from server.app import create_app
from server.ingest import Line


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "sessions.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path)
    yield connection
    connection.close()


@pytest.fixture
def machine_id(conn):
    machines.create_machine(conn, "home-pc")
    return conn.execute("SELECT id FROM machines WHERE name = 'home-pc'").fetchone()["id"]


@pytest.fixture
def api(db_path):
    """(TestClient, 에이전트 토큰)"""
    connection = db.connect(db_path)
    token = machines.create_machine(connection, "api-pc")
    connection.close()
    # 비밀번호 없이 조회 API를 쓰려면 이 PC(루프백)에서 온 요청이어야 한다.
    with TestClient(create_app(db_path), client=("127.0.0.1", 50000)) as client:
        yield client, token


def to_lines(texts: list[str], start: int = 0) -> list[Line]:
    """텍스트 줄을 개행 포함 바이트 위치를 가진 Line 목록으로 만든다."""
    result = []
    offset = start
    for text in texts:
        length = len(text.encode("utf-8")) + 1
        result.append(Line(offset, length, text))
        offset += length
    return result


def end_offset(lines: list[Line]) -> int:
    return lines[-1].offset + lines[-1].length
