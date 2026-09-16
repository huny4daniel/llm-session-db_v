"""서버 PC 자신의 에이전트(Local) 자동 등록."""

import pytest

from agent import config as agent_config
from server import local_agent, machines
from server.__main__ import main as server_main
from tests import samples
from tests.test_api import auth, payload


@pytest.fixture
def agent_json(tmp_path, monkeypatch):
    path = tmp_path / "state" / "agent.json"
    monkeypatch.setenv("LSDB_AGENT_CONFIG", str(path))
    return path


def test_creates_local_machine_and_config(conn, agent_json):
    result = local_agent.ensure_local_agent(conn, "127.0.0.1", 8765)
    assert result.changed and "등록했습니다" in result.note
    assert agent_json.exists()
    saved = agent_config.load()
    assert saved.server_url == "http://127.0.0.1:8765"
    assert machines.find_by_token(conn, saved.token)["name"] == "Local"

    # 다시 실행하면 아무것도 바꾸지 않는다
    again = local_agent.ensure_local_agent(conn, "127.0.0.1", 8765)
    assert not again.changed and again.cfg.token == saved.token

    # 서버 주소가 바뀌면 설정만 갱신하고 토큰은 유지한다
    moved = local_agent.ensure_local_agent(conn, "100.64.0.1", 9000)
    assert moved.changed and moved.cfg.token == saved.token and moved.cfg.server_url == "http://100.64.0.1:9000"


def test_renames_existing_machine_of_this_pc(api, conn, agent_json):
    http, token = api  # api-pc 토큰
    assert http.post("/api/agent/ingest", json=payload(samples.MAIN_KEY, samples.main_lines()), headers=auth(token)).status_code == 200
    agent_config.save(agent_config.AgentConfig(server_url="http://127.0.0.1:8765", token=token, claude_root="x"))

    result = local_agent.ensure_local_agent(conn, "127.0.0.1", 8765)
    assert not result.changed and "api-pc" in result.note
    assert [m["name"] for m in machines.list_machines(conn)] == ["Local"]
    assert http.get("/api/sessions").json()["items"][0]["machine_name"] == "Local"  # 세션은 그대로
    assert agent_config.load().token == token  # 토큰도 그대로


def test_rotates_token_when_config_invalid(conn, agent_json):
    machines.create_machine(conn, "Local", allow_protected=True)
    agent_config.save(agent_config.AgentConfig(server_url="http://127.0.0.1:8765", token="stale", claude_root="root-x"))
    result = local_agent.ensure_local_agent(conn, "127.0.0.1", 8765)
    assert result.changed and "새로 발급" in result.note
    saved = agent_config.load()
    assert saved.token != "stale" and saved.claude_root == "root-x"
    assert machines.find_by_token(conn, saved.token)["name"] == "Local"


def test_local_is_protected(conn, agent_json):
    local_agent.ensure_local_agent(conn, "127.0.0.1", 8765)
    with pytest.raises(ValueError):
        machines.rename_machine(conn, "Local", "other")
    with pytest.raises(ValueError):
        machines.delete_machine(conn, "Local")
    with pytest.raises(ValueError):
        machines.create_machine(conn, "Local")
    machines.create_machine(conn, "laptop")
    with pytest.raises(ValueError):
        machines.rename_machine(conn, "laptop", "Local")


def test_cli_rejects_local_changes(db_path, monkeypatch, agent_json):
    monkeypatch.setenv("LSDB_DATA_DIR", str(db_path.parent))
    assert server_main(["add-machine", "Local"]) == 1
    assert server_main(["add-machine", "pc"]) == 0
    assert server_main(["rename-machine", "pc", "Local"]) == 1
