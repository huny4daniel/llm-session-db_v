"""백그라운드 서버 관리(실행 확인·시작·중지·자동 실행 등록). CLI와 GUI 서버 탭이 함께 쓴다."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from contextlib import closing

from agent import autostart
from agent.service import stop_by_pid

from . import auth, config, db

AUTOSTART_NAME = "llm-session-db-server"


def launch_args(host: str, port: int) -> list[str]:
    return autostart.launch_args("server", ["serve", "--host", host, "--port", str(port), "--log-file"])


def url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def is_running(host: str, port: int, timeout: float = 2.0) -> bool:
    """서버가 응답하는지 HTTP로 확인한다(다른 프로그램이 포트를 쓰는 경우와 구분하려고 응답 내용까지 본다)."""
    try:
        with urllib.request.urlopen(f"{url(host, port)}/api/auth/status", timeout=timeout) as response:
            return "password_enabled" in json.loads(response.read() or b"{}")
    except (OSError, ValueError, urllib.error.URLError):
        return False


def write_pid() -> None:
    path = config.pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")


def read_pid() -> int | None:
    try:
        return int(config.pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def clear_pid() -> None:
    config.pid_path().unlink(missing_ok=True)


def remote_bind_without_password(host: str) -> bool:
    """루프백이 아닌 주소는 비밀번호가 있어야 열 수 있다."""
    if host in auth.LOOPBACK_HOSTS:
        return False
    with closing(db.connect(config.db_path())) as conn:
        return not auth.password_enabled(conn)


def registered_command() -> str | None:
    return autostart.registered_command(AUTOSTART_NAME)


def register_autostart(host: str, port: int) -> str:
    return autostart.register(AUTOSTART_NAME, launch_args(host, port))


def unregister_autostart() -> bool:
    return autostart.unregister(AUTOSTART_NAME)


def start_background(host: str, port: int) -> bool:
    if is_running(host, port):
        return False
    autostart.start_detached(launch_args(host, port))
    return True


def stop_background(host: str, port: int, timeout: float = 5.0) -> bool:
    stopped = stop_by_pid(read_pid(), lambda: is_running(host, port), timeout=timeout)
    if stopped:
        clear_pid()  # 강제 종료된 프로세스는 자기 PID 파일을 못 지운다
    return stopped
