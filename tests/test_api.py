import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import db, machines, runner
from server.app import create_app
from tests import samples
from tests.conftest import end_offset, to_lines
from tests.fake_claude import FORK_ID


def payload(file_key, texts, start=0, **extra):
    return {
        "source": "claude",
        "file_key": file_key,
        "start_offset": start,
        "lines": [{"offset": l.offset, "length": l.length, "text": l.text} for l in to_lines(texts, start)],
        **extra,
    }


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def sse_events(stream):
    """SSE 응답의 데이터 이벤트. 스트림 종료를 알리는 `event: end`의 빈 데이터는 뺀다."""
    events = [json.loads(line[len("data: "):]) for line in stream.iter_lines() if line.startswith("data: ")]
    return [e for e in events if e]


@pytest.fixture
def loaded(api):
    client, token = api
    for key, texts in [(samples.MAIN_KEY, samples.main_lines()), (samples.SUB_KEY, samples.subagent_lines())]:
        r = client.post("/api/agent/ingest", json=payload(key, texts), headers=auth(token))
        assert r.status_code == 200, r.text
    session_id = client.get("/api/sessions").json()["items"][0]["id"]
    return client, token, session_id


def test_ingest_requires_token(api):
    client, _ = api
    r = client.post("/api/agent/ingest", json=payload(samples.MAIN_KEY, samples.main_lines()))
    assert r.status_code == 401
    r = client.post("/api/agent/ingest", json=payload(samples.MAIN_KEY, samples.main_lines()), headers=auth("wrong"))
    assert r.status_code == 401


def test_ingest_conflict_returns_expected_offset(loaded):
    client, token, _ = loaded
    r = client.post("/api/agent/ingest", json=payload(samples.MAIN_KEY, samples.main_lines()[:2]), headers=auth(token))
    assert r.status_code == 409
    assert r.json()["expected_offset"] == end_offset(to_lines(samples.main_lines()))


def test_agent_files_lists_offsets(loaded):
    client, token, _ = loaded
    files = client.get("/api/agent/files?source=claude", headers=auth(token)).json()["files"]
    assert set(files) == {samples.MAIN_KEY, samples.SUB_KEY}
    assert files[samples.SUB_KEY]["next_offset"] == end_offset(to_lines(samples.subagent_lines()))


def test_sessions_and_filters(loaded):
    client, _, _ = loaded
    data = client.get("/api/sessions").json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["title"] == "세션 서버 구현"
    assert item["machine_name"] == "api-pc"

    assert client.get("/api/sessions", params={"q": "구현"}).json()["total"] == 1
    assert client.get("/api/sessions", params={"q": "없는제목"}).json()["total"] == 0
    assert client.get("/api/sessions", params={"project": samples.CWD}).json()["total"] == 1

    projects = client.get("/api/projects").json()
    assert projects[0]["project_path"] == samples.CWD
    machines = client.get("/api/machines").json()
    assert machines[0]["session_count"] == 1


def test_session_detail_and_messages(loaded):
    client, _, session_id = loaded
    session = client.get(f"/api/sessions/{session_id}").json()
    assert session["session_uid"] == samples.SESSION
    assert session["agents"] == [{
        "agent_id": samples.AGENT, "message_count": 2,
        "started_at": samples.ts(20), "last_activity_at": samples.ts(21), "prompt": "하위 작업 조사",
    }]

    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [m["kind"] for m in messages].count("meta") == 0
    assert len(messages) == 11
    assert messages[1]["blocks"] == [{"type": "text", "text": "세션 저장 서버를 만들어줘"}]

    with_meta = client.get(f"/api/sessions/{session_id}/messages", params={"include_meta": True}).json()
    assert len(with_meta) == 12

    agent_messages = client.get(
        f"/api/sessions/{session_id}/messages", params={"agent_id": samples.AGENT}
    ).json()
    assert [m["kind"] for m in agent_messages] == ["user", "assistant"]

    assert client.get("/api/sessions/9999").status_code == 404


@pytest.mark.parametrize(
    ("query", "expected_texts"),
    [
        ("trigram", ["trigram"]),          # 3자 이상: FTS
        ("서버", ["서버"]),                  # 2자: LIKE
        ("구조를 설계", ["설계"]),            # 혼합: 두 단어 모두 포함
        ("TRIGRAM", ["trigram"]),          # 대소문자 무시
    ],
)
def test_search(loaded, query, expected_texts):
    client, _, _ = loaded
    hits = client.get("/api/search", params={"q": query}).json()
    assert len(hits) == len(expected_texts)
    for hit, text in zip(hits, expected_texts):
        snippet = hit["snippet"]
        assert text in (snippet["before"] + snippet["match"] + snippet["after"])
        assert snippet["match"]


def test_search_hit_carries_agent_id(loaded):
    client, _, _ = loaded
    hit = client.get("/api/search", params={"q": "trigram"}).json()[0]
    assert hit["agent_id"] == samples.AGENT
    assert hit["session_title"] == "세션 서버 구현"


def test_stats(loaded):
    client, _, _ = loaded
    data = client.get("/api/stats", params={"days": 7}).json()
    assert len(data["daily"]) == 7
    totals = data["totals"]
    for column, value in samples.EXPECTED_TOKENS.items():
        assert totals[column] == value, column
    assert totals["session_count"] == 1
    assert totals["cost_usd"] == 1.25
    assert sum(d["total_tokens"] for d in data["daily"]) == sum(samples.EXPECTED_TOKENS.values())
    assert {m["name"] for m in data["by_model"]} == {"claude-sonnet-5", "claude-haiku-4-5"}
    assert data["by_project"][0]["name"] == samples.CWD
    assert data["by_machine"][0]["name"] == "api-pc"


def test_delete_session(loaded):
    client, token, session_id = loaded
    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    assert client.get("/api/sessions").json()["total"] == 0
    assert client.delete(f"/api/sessions/{session_id}").status_code == 404

    more = payload(samples.MAIN_KEY, ["{}"], start=end_offset(to_lines(samples.main_lines())))
    assert client.post("/api/agent/ingest", json=more, headers=auth(token)).status_code == 200
    assert client.get("/api/sessions").json()["total"] == 0


def test_web_run_streams_events_and_links_fork(db_path, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    fake = Path(__file__).with_name("fake_claude.py")
    manager = runner.RunManager(lambda: [sys.executable, str(fake)], projects, tmp_path / "ws")
    conn = db.connect(db_path)
    token = machines.create_machine(conn, "remote-pc")
    conn.close()

    with TestClient(create_app(db_path, runs=manager), client=("127.0.0.1", 50000)) as client:
        for key, texts in [(samples.MAIN_KEY, samples.main_lines()), (samples.SUB_KEY, samples.subagent_lines())]:
            assert client.post("/api/agent/ingest", json=payload(key, texts), headers=auth(token)).status_code == 200
        session_id = client.get("/api/sessions").json()["items"][0]["id"]

        detail = client.get(f"/api/sessions/{session_id}").json()
        assert detail["continue_plan"]["fork"] is True  # 서버 PC에 세션 파일 없음
        assert detail["continue_plan"]["chat_only"] is True  # 프로젝트 폴더 없음
        assert detail["latest_run"] is None and detail["parent"] is None and detail["children"] == []

        assert client.post(f"/api/sessions/{session_id}/runs", json={"prompt": "x", "allowed_tools": ["root"]}).status_code == 400
        assert client.post("/api/sessions/9999/runs", json={"prompt": "x"}).status_code == 404

        started = client.post(f"/api/sessions/{session_id}/runs", json={"prompt": "웹에서 안녕"})
        assert started.status_code == 200, started.text
        run_id = started.json()["id"]

        with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            events = sse_events(stream)
        assert events[0]["type"] == "init"
        assert "".join(e["text"] for e in events if e["type"] == "delta") == "에코: 웹에서 안녕"
        assert events[-1] == {"type": "run_status", "status": "completed", "error": None, "session_uid": FORK_ID}

        # 끊긴 뒤 다시 연결하면 마지막으로 받은 다음 이벤트부터 받는다.
        with client.stream("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(len(events) - 2)}) as stream:
            replay = sse_events(stream)
        assert replay == events[-1:]

        assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"
        detail = client.get(f"/api/sessions/{session_id}").json()
        assert detail["latest_run"]["id"] == run_id
        assert [c["child_uid"] for c in detail["children"]] == [FORK_ID]
        assert detail["children"][0]["id"] is None  # 새 세션은 아직 수집 전

        assert client.get("/api/session-lookup", params={"uid": FORK_ID}).json() == {"id": None}
        assert client.get("/api/session-lookup", params={"uid": samples.SESSION}).json() == {"id": session_id}
        assert client.post("/api/runs/unknown/cancel").status_code == 404
        assert client.get("/api/runs/unknown/events").status_code == 404


def test_index_served(api):
    client, _ = api
    r = client.get("/")
    assert r.status_code == 200
    assert "llm-session-db" in r.text
