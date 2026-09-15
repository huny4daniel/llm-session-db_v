import json
import sys
from pathlib import Path

import pytest

from agent.collector import IMPORT_FOLDER
from agent.pull import pull_session
from tests import samples
from tests.test_agent import InProcessClient
from tests.test_api import auth, payload

FAKE = Path(__file__).with_name("fake_claude_interactive.py")


@pytest.fixture
def remote(api):
    http, token = api
    for key, texts in [(samples.MAIN_KEY, samples.main_lines()), (samples.SUB_KEY, samples.subagent_lines())]:
        assert http.post("/api/agent/ingest", json=payload(key, texts), headers=auth(token)).status_code == 200
    return InProcessClient(http, token), http


def run_pull(client, root, ref, **kwargs):
    output = []
    code = pull_session(client, root, ref, out=output.append, **kwargs)
    return code, "\n".join(output)


def test_pull_writes_import_copy_and_prints_fork_command(remote, tmp_path):
    client, _ = remote
    root = tmp_path / "projects"
    workdir = tmp_path / "work"
    workdir.mkdir()

    code, output = run_pull(client, root, samples.SESSION[:8], workdir=str(workdir))
    assert code == 0
    copy = root / IMPORT_FOLDER / f"{samples.SESSION}.jsonl"
    assert copy.read_text(encoding="utf-8").splitlines() == samples.main_lines()
    assert f'cd "{workdir.resolve()}"; claude --resume {samples.SESSION} --fork-session' in output
    assert samples.CWD in output  # 원래 프로젝트 폴더와 다른 곳에서 이어간다는 안내


def test_pull_uses_original_file_when_present(remote, tmp_path):
    client, _ = remote
    root = tmp_path / "projects"
    samples.write_jsonl(root / samples.MAIN_KEY, samples.main_lines())

    code, output = run_pull(client, root, samples.SESSION, workdir=str(tmp_path))
    assert code == 0
    assert "--fork-session" not in output
    assert not (root / IMPORT_FOLDER).exists()


def test_pull_run_launches_claude_and_removes_copy(remote, tmp_path, monkeypatch):
    client, http = remote
    root = tmp_path / "projects"
    log = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_PROJECTS", str(root))
    monkeypatch.setenv("CLAUDECODE", "1")
    session_id = http.get("/api/sessions").json()["items"][0]["id"]

    code, _ = run_pull(
        client, root, str(session_id), workdir=str(tmp_path), run=True,
        command_factory=lambda: [sys.executable, str(FAKE)],
    )
    assert code == 0
    info = json.loads(log.read_text(encoding="utf-8"))
    assert info["args"] == ["--resume", samples.SESSION, "--fork-session"]
    assert Path(info["cwd"]) == tmp_path.resolve()
    assert info["import_copy_exists"] is True  # 실행 중에는 사본이 있어야 CLI가 찾는다
    assert info["claudecode_env"] is None
    assert not (root / IMPORT_FOLDER / f"{samples.SESSION}.jsonl").exists()  # 끝나면 삭제


def test_pull_errors(remote, tmp_path):
    client, _ = remote
    assert run_pull(client, tmp_path, "zzzz")[0] == 1  # 없는 세션
    assert run_pull(client, tmp_path, samples.SESSION, workdir=str(tmp_path / "missing"))[0] == 1
    code, output = run_pull(client, tmp_path, samples.SESSION, run=True, command_factory=lambda: None)
    assert code == 1 and "claude" in output
    assert not (tmp_path / IMPORT_FOLDER).exists()  # 실행할 수 없으면 사본도 만들지 않는다
