"""백그라운드 에이전트 관리(실행 여부·시작·중지·자동 실행 등록). CLI와 GUI가 함께 쓴다."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from . import autostart, config
from .lock import AlreadyRunning, single_instance

AUTOSTART_NAME = "llm-session-db-agent"
DEFAULT_INTERVAL = 30.0


def launch_args(interval: float) -> list[str]:
    return autostart.launch_args("agent", ["run", "--interval", str(interval), "--log-file"])


def is_running() -> bool:
    try:
        with single_instance(config.lock_path()):
            return False
    except AlreadyRunning:
        return True


def write_pid(path: Path | None = None) -> None:
    target = path or config.pid_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(os.getpid()), encoding="utf-8")


def read_pid(path: Path | None = None) -> int | None:
    try:
        return int((path or config.pid_path()).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def clear_pid(path: Path | None = None) -> None:
    (path or config.pid_path()).unlink(missing_ok=True)


def registered_command() -> str | None:
    return autostart.registered_command(AUTOSTART_NAME)


def register_autostart(interval: float = DEFAULT_INTERVAL) -> str:
    return autostart.register(AUTOSTART_NAME, launch_args(interval))


def unregister_autostart() -> bool:
    return autostart.unregister(AUTOSTART_NAME)


def start_background(interval: float = DEFAULT_INTERVAL) -> bool:
    """실행 중이 아니면 백그라운드로 시작한다. 시작했으면 True."""
    if is_running():
        return False
    autostart.start_detached(launch_args(interval))
    return True


def stop_background(timeout: float = 5.0) -> bool:
    """PID 파일의 프로세스를 종료한다. 종료됐으면(또는 원래 실행 중이 아니었으면) True."""
    return stop_by_pid(read_pid(), is_running, timeout=timeout)


def stop_by_pid(pid: int | None, still_running, *, timeout: float = 5.0) -> bool:
    if not still_running():
        return True
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)  # Windows에서는 TerminateProcess
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not still_running():
            return True
        time.sleep(0.1)
    return not still_running()
