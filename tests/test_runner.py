import json
import sys
import time
from pathlib import Path

import pytest

from agent.collector import IMPORT_FOLDER, discover_claude
from server import runner
from tests import samples
from tests.fake_claude import FORK_ID

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    args_log = tmp_path / "args.json"
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(args_log))
    monkeypatch.setenv("FAKE_CLAUDE_PROJECTS", str(projects))
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    forks = []
    manager = runner.RunManager(
        lambda: [sys.executable, str(FAKE)], projects, tmp_path / "workspaces",
        on_fork=lambda parent, child: forks.append((parent, child)),
    )
    return manager, projects, args_log, forks, tmp_path


def make_session(tmp_path, project_exists=True):
    project = tmp_path / "project"
    if project_exists:
        project.mkdir(exist_ok=True)
    return {"id": 7, "source": "claude", "session_uid": samples.SESSION, "project_path": str(project)}


def wait_done(run, timeout=30):
    deadline = time.monotonic() + timeout
    while not run.done and time.monotonic() < deadline:
        time.sleep(0.05)
    assert run.done, run.status


def logged(args_log):
    return json.loads(args_log.read_text(encoding="utf-8"))


def no_source():
    pytest.fail("서버 PC에 세션 파일이 있으면 수집된 원본을 읽지 않아야 한다")


def test_local_session_resumes_in_place(env, monkeypatch):
    manager, projects, args_log, forks, tmp = env
    monkeypatch.setenv("CLAUDECODE", "1")
    samples.write_jsonl(projects / samples.MAIN_KEY, samples.main_lines())

    run = manager.start(make_session(tmp), "안녕 세션", ["edit"], "haiku", source_lines=no_source)
    wait_done(run)
    assert run.status == "completed", run.error

    info = logged(args_log)
    args = info["args"]
    assert info["prompt"] == "안녕 세션"  # 한국어 프롬프트가 표준 입력으로 그대로 전달
    assert Path(info["cwd"]) == tmp / "project"
    assert info["claudecode_env"] is None  # 부모 Claude Code 세션 정보는 넘기지 않음
    assert args[args.index("--resume") + 1] == samples.SESSION
    assert args[args.index("--permission-mode") + 1] == "default"
    assert args[args.index("--allowedTools") + 1] == "Edit,Write,NotebookEdit"
    assert args[args.index("--model") + 1] == "haiku"
    assert "--fork-session" not in args and "--tools" not in args

    assert [e["type"] for e in run.events] == [
        "init", "message_start", "block_start", "delta", "delta", "assistant", "result", "run_status",
    ]
    assert "".join(e["text"] for e in run.events if e["type"] == "delta") == "에코: 안녕 세션"
    assert run.events[5]["blocks"] == [{"type": "text", "text": "에코: 안녕 세션"}]
    assert run.result_session_uid is None and forks == []


def test_remote_session_forks_from_import_copy(env):
    manager, projects, args_log, forks, tmp = env
    session = make_session(tmp, project_exists=False)
    plan = manager.plan(session)
    assert plan.describe() == {"fork": True, "chat_only": True, "cwd": str(tmp / "workspaces" / samples.SESSION), "local_file": False}

    run = manager.start(session, "이어가기", ["shell"], None, source_lines=samples.main_lines)
    wait_done(run)
    assert run.status == "completed", run.error

    info = logged(args_log)
    args = info["args"]
    assert "--fork-session" in args
    assert args[args.index("--tools") + 1] == ""  # 프로젝트 폴더가 없으면 도구 없이 대화만
    assert "--allowedTools" not in args and "--model" not in args
    assert Path(info["cwd"]) == tmp / "workspaces" / samples.SESSION
    assert info["import_copy_lines"] == len(samples.main_lines())
    assert not (projects / IMPORT_FOLDER / f"{samples.SESSION}.jsonl").exists()  # 실행 후 사본 삭제
    assert run.result_session_uid == FORK_ID
    assert forks == [(7, FORK_ID)]
    assert run.events[-1] == {"type": "run_status", "status": "completed", "error": None, "session_uid": FORK_ID}


def test_remote_session_without_source_is_rejected(env):
    manager, _, _, _, tmp = env
    with pytest.raises(runner.RunnerError):
        manager.start(make_session(tmp), "x", [], None, source_lines=lambda: [])


@pytest.mark.parametrize(("groups", "model"), [(["root"], None), ([], "gpt")])
def test_invalid_options_rejected(env, groups, model):
    manager, projects, _, _, tmp = env
    samples.write_jsonl(projects / samples.MAIN_KEY, samples.main_lines())
    with pytest.raises(runner.RunnerError):
        manager.start(make_session(tmp), "x", groups, model, source_lines=no_source)


def test_missing_cli_reports_503(tmp_path):
    manager = runner.RunManager(lambda: None, tmp_path, tmp_path / "ws")
    with pytest.raises(runner.RunnerError) as exc:
        manager.start(make_session(tmp_path), "x", [], None, source_lines=no_source)
    assert exc.value.status == 503


def test_concurrent_run_rejected_and_cancel(env, monkeypatch):
    manager, projects, _, _, tmp = env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    samples.write_jsonl(projects / samples.MAIN_KEY, samples.main_lines())
    session = make_session(tmp)

    run = manager.start(session, "오래 걸리는 작업", [], None, source_lines=no_source)
    deadline = time.monotonic() + 15
    while not run.events and time.monotonic() < deadline:
        time.sleep(0.05)
    with pytest.raises(runner.RunnerError) as exc:
        manager.start(session, "또", [], None, source_lines=no_source)
    assert exc.value.status == 409

    assert manager.cancel(run.id) is run
    wait_done(run)
    assert run.status == "cancelled"
    assert run.events[-1]["status"] == "cancelled"
    assert manager.latest_for(7) is run
    assert manager.cancel("unknown") is None


def test_failure_carries_stderr(env, monkeypatch):
    manager, projects, _, _, tmp = env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    samples.write_jsonl(projects / samples.MAIN_KEY, samples.main_lines())
    run = manager.start(make_session(tmp), "x", [], None, source_lines=no_source)
    wait_done(run)
    assert run.status == "failed"
    assert "fake failure" in run.error


def test_permission_denials_translated(env, monkeypatch):
    manager, projects, _, _, tmp = env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "deny")
    samples.write_jsonl(projects / samples.MAIN_KEY, samples.main_lines())
    run = manager.start(make_session(tmp), "파일 만들어줘", [], None, source_lines=no_source)
    wait_done(run)
    result = next(e for e in run.events if e["type"] == "result")
    assert result["permission_denials"] == [{"tool_name": "Write", "tool_input": {"file_path": "a.txt", "content": "hi"}}]


def test_translate_skips_subagent_and_unknown_events():
    subagent = json.dumps({"type": "assistant", "parent_tool_use_id": "toolu_1", "message": {"content": []}})
    assert runner.translate(subagent) is None
    assert runner.translate('{"type": "rate_limit_event"}') is None
    assert runner.translate("not json") is None
    tool_start = {"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use"}}}
    assert runner.translate(json.dumps(tool_start)) is None


def test_agent_ignores_import_folder(tmp_path):
    samples.write_jsonl(tmp_path / samples.MAIN_KEY, samples.main_lines())
    samples.write_jsonl(tmp_path / IMPORT_FOLDER / f"{samples.SESSION}.jsonl", samples.main_lines())
    assert [f.file_key for f in discover_claude(tmp_path)] == [samples.MAIN_KEY]
