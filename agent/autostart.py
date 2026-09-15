"""Windows 로그인 시 자동 실행 등록. HKCU Run 키를 써서 관리자 권한이 필요 없다."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def launch_args(package: str, argv: list[str]) -> list[str]:
    """콘솔 창 없이 `python -m <package> <argv>`와 같은 동작을 하는 명령.

    Run 키는 작업 디렉터리를 지정할 수 없어 `-m`을 쓸 수 없으므로 프로젝트 경로를 직접 넣는다.
    PyInstaller로 묶인 실행 파일이면 자기 자신을 실행한다.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *argv]
    code = (
        f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
        f"from {package}.__main__ import main; sys.exit(main({argv!r}))"
    )
    return [background_python(), "-c", code]


def background_python() -> str:
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else executable)


def register(name: str, args: list[str]) -> str:
    winreg = _winreg()
    command = subprocess.list2cmdline(args)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    return command


def unregister(name: str) -> bool:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        return False
    return True


def registered_command(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def start_detached(args: list[str]) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen(
        args, creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _winreg():
    if sys.platform != "win32":
        raise RuntimeError("자동 실행 등록은 Windows에서만 지원합니다")
    import winreg

    return winreg
