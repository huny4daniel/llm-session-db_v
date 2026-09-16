"""GUI·상태·서비스 관리 테스트. 창은 화면에 띄우지 않고 위젯만 만들어 동작을 확인한다."""

import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

import pytest

from agent import config, service
from agent.status import collect_status
from tests import samples
from tests.test_agent import InProcessClient
from tests.test_api import auth, payload


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """에이전트 설정·잠금·PID 파일을 임시 폴더에 두고, 세션 폴더도 임시로 만든다."""
    monkeypatch.setenv("LSDB_AGENT_CONFIG", str(tmp_path / "state" / "agent.json"))
    monkeypatch.setenv("LSDB_DATA_DIR", str(tmp_path / "data"))  # 서버 탭이 실제 data/를 건드리지 않게
    root = tmp_path / "projects"
    samples.write_jsonl(root / samples.MAIN_KEY, samples.main_lines())
    return root


@pytest.fixture
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"tkinter 창을 만들 수 없음: {e}")
    root.withdraw()
    yield root
    root.destroy()


def drain(widget, until=None, timeout=15.0):
    """스레드 작업이 끝나 콜백이 실행될 때까지 메인 루프 대신 이벤트를 처리한다.

    until이 있으면 그 조건이 참이 될 때까지(최대 timeout초) 기다린다.
    """
    from agent.gui import dispatch_finished

    deadline = time.monotonic() + (timeout if until else 1.0)
    while True:
        widget.update()
        dispatch_finished()
        if until is None:
            if time.monotonic() >= deadline:
                return
        elif until() or time.monotonic() >= deadline:
            return
        time.sleep(0.02)


def test_agent_sessions_endpoint(api):
    http, token = api
    assert http.get("/api/agent/sessions", headers=auth(token)).json() == {"total": 0, "items": []}
    assert http.post("/api/agent/ingest", json=payload(samples.MAIN_KEY, samples.main_lines()), headers=auth(token)).status_code == 200

    data = http.get("/api/agent/sessions", headers=auth(token)).json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["session_uid"] == samples.SESSION
    assert set(item) == {"id", "session_uid", "title", "project_path", "machine_name", "last_activity_at", "user_turns"}
    assert http.get("/api/agent/sessions?q=없는검색어", headers=auth(token)).json()["items"] == []
    assert http.get("/api/agent/sessions").status_code == 401  # PC 토큰 필요


def test_collect_status_reports_waiting_files(api, agent_env):
    http, token = api
    cfg = config.AgentConfig(server_url="http://testserver", token=token, claude_root=str(agent_env))
    report = collect_status(cfg, InProcessClient(http, token))
    assert report.ok and report.running is False
    assert (report.local_files, report.synced_files, len(report.waiting)) == (1, 0, 1)
    text = "\n".join(report.lines())
    assert samples.MAIN_KEY in text and "미전송" in text

    failed = collect_status(cfg, InProcessClient(http, "bad-token"))
    assert not failed.ok and "서버 연결  실패" in "\n".join(failed.lines())


def test_pid_file_roundtrip(agent_env):
    assert service.read_pid() is None
    service.write_pid()
    assert service.read_pid() == os.getpid()
    service.clear_pid()
    assert service.read_pid() is None


def test_stop_by_pid_terminates_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert service.stop_by_pid(child.pid, lambda: child.poll() is None, timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
    assert service.stop_by_pid(None, lambda: False)  # 원래 실행 중이 아니면 성공
    assert not service.stop_by_pid(None, lambda: True)  # PID를 모르면 실패


def test_app_without_config_opens_settings_tab(agent_env, tk_root, monkeypatch):
    from agent.gui import App

    app = App(tk_root)
    try:
        drain(app)
        assert app.cfg is None
        assert app.notebook.tab(app.notebook.select(), "text") == "설정"

        app.settings.server_var.set("http://127.0.0.1:8765")
        app.settings.token_var.set("abc")
        app.settings.root_var.set(str(agent_env))
        app.settings.save()
        drain(app, until=lambda: "확인하는 중" not in app.status.text.get("1.0", "end"))
        assert config.load().token == "abc"
        assert app.cfg is not None
        assert "설정 파일" in app.status.text.get("1.0", "end")
    finally:
        app.destroy()


def test_pull_tab_lists_sessions_and_copies_command(api, agent_env, tk_root, monkeypatch, tmp_path):
    from agent.gui import App

    http, token = api
    assert http.post("/api/agent/ingest", json=payload(samples.MAIN_KEY, samples.main_lines()), headers=auth(token)).status_code == 200
    root = tmp_path / "empty-projects"  # 원본이 없는 PC처럼 동작하게 빈 폴더
    config.save(config.AgentConfig(server_url="http://testserver", token=token, claude_root=str(root)))
    monkeypatch.setattr(App, "client", lambda self: InProcessClient(http, token))
    app = App(tk_root)
    try:
        drain(app)
        app.pull.load()
        drain(app, until=lambda: app.pull.tree.get_children())
        ids = app.pull.tree.get_children()
        assert len(ids) == 1
        app.pull.tree.selection_set(ids[0])
        app.pull._on_select()
        assert samples.SESSION in app.pull.message.cget("text")

        workdir = tmp_path / "work"
        workdir.mkdir()
        app.pull.workdir_var.set(str(workdir))
        app.pull.copy_command()
        drain(app, until=lambda: "복사했습니다" in app.pull.message.cget("text"))
        expected = f'cd "{workdir.resolve()}"; claude --resume {samples.SESSION} --fork-session'
        assert app.clipboard_get() == expected
        assert (root / "llm-session-db-import" / f"{samples.SESSION}.jsonl").exists()
    finally:
        app.destroy()


def test_server_panel_registers_machine_and_password(db_path, tk_root, monkeypatch):
    from server import auth, db, machines
    from server.gui import ServerPanel

    monkeypatch.setenv("LSDB_DATA_DIR", str(db_path.parent))
    panel = ServerPanel(tk_root)
    drain(panel)
    panel.machine_name_var.set("laptop")
    panel.add_machine()
    token = panel.token_var.get()
    assert token and [m["name"] for m in machines.list_machines(db.connect(db_path))] == ["laptop"]
    assert panel.tree.get_children() == ("laptop",)

    panel.tree.selection_set("laptop")
    panel.rename_machine("desk")
    assert [m["name"] for m in machines.list_machines(db.connect(db_path))] == ["desk"]
    assert panel.tree.selection() == ("desk",)
    panel.rename_machine("desk")  # 같은 이름이면 아무 일도 없음
    panel.delete_machine(confirmed=False)
    assert panel.tree.get_children() == ("desk",)
    panel.delete_machine(confirmed=True)
    assert panel.tree.get_children() == ()
    assert machines.list_machines(db.connect(db_path)) == []

    panel.password_var.set("secret-pass")
    panel.confirm_var.set("secret-pass")
    panel.set_password()
    assert auth.password_enabled(db.connect(db_path))
    assert "설정됨" in panel.password_state.cget("text")
    panel.destroy()


def test_launch_args_in_frozen_exe(monkeypatch):
    from agent import autostart

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\apps\LlmSessionServer.exe")
    monkeypatch.setattr(autostart.importlib.util, "find_spec", lambda name: object())  # 서버가 함께 들어 있는 exe
    assert autostart.launch_args("agent", ["run"]) == [r"C:\apps\LlmSessionServer.exe", "agent", "run"]
    assert autostart.launch_args("server", ["serve"]) == [r"C:\apps\LlmSessionServer.exe", "serve"]

    monkeypatch.setattr(autostart.importlib.util, "find_spec", lambda name: None)  # 에이전트만 있는 exe
    assert autostart.launch_args("agent", ["run"]) == [r"C:\apps\LlmSessionServer.exe", "run"]


def test_server_entry_dispatches_agent_commands(monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("server_entry", Path("packaging/server_entry.py"))
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    calls = []
    monkeypatch.setattr(entry, "agent_main", lambda argv: calls.append(("agent", argv)) or 0)
    monkeypatch.setattr(entry, "server_main", lambda argv: calls.append(("server", argv)) or 0)
    entry.main(["agent", "status"])
    entry.main(["serve", "--port", "1"])
    entry.main([])
    assert calls == [("agent", ["status"]), ("server", ["serve", "--port", "1"]), ("server", [])]


def test_default_data_dir_next_to_frozen_exe(monkeypatch):
    from server import config

    monkeypatch.delenv("LSDB_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\apps\LlmSessionServer.exe")
    assert config.data_dir() == Path(r"C:\apps\data")


def test_detached_env_drops_pyinstaller_variables():
    from agent import autostart

    env = {"PATH": "x", "_PYI_APPLICATION_HOME_DIR": "tmp", "_PYI_ARCHIVE_FILE": "a", "_MEIPASS2": "m", "LSDB_DATA_DIR": "d"}
    assert autostart.detached_env(env) == {"PATH": "x", "LSDB_DATA_DIR": "d"}
