"""Windows 로그인 시 자동 실행 등록. HKCU Run 키를 써서 관리자 권한이 필요 없다."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def server_bundled() -> bool:
    """서버 패키지까지 들어 있는 배포 exe인지. 이 exe는 에이전트 명령을 `agent` 접두어 뒤에 받는다."""
    return frozen() and importlib.util.find_spec("server") is not None


def command_prefix(package: str) -> list[str]:
    """exe에서 패키지 명령 앞에 붙는 인자. 서버 exe의 에이전트 명령만 `agent` 접두어가 필요하다."""
    return ["agent"] if package == "agent" and server_bundled() else []


def launch_args(package: str, argv: list[str]) -> list[str]:
    """콘솔 창 없이 `python -m <package> <argv>`와 같은 동작을 하는 명령.

    Run 키는 작업 디렉터리를 지정할 수 없어 `-m`을 쓸 수 없으므로 프로젝트 경로를 직접 넣는다.
    PyInstaller로 묶인 실행 파일이면 자기 자신을 실행한다(서버 exe는 에이전트 명령에 `agent` 접두어).
    """
    if frozen():
        return [sys.executable, *command_prefix(package), *argv]
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


def detached_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """분리 실행할 자식에게 넘길 환경. PyInstaller onefile 부트로더 변수(`_PYI_*`, `_MEIPASS2`)를 뺀다.

    exe가 자기 자신을 다시 실행하면 자식이 부모의 임시 압축 해제 폴더를 그대로 쓰는데,
    부모(install 명령)가 끝나면서 그 폴더를 지워 자식(serve/run)이 죽는다. 변수를 지우면 자식이 따로 압축을 푼다.
    """
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}


def start_detached(args: list[str]) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen(
        args, creationflags=flags, close_fds=True, env=detached_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _winreg():
    if sys.platform != "win32":
        raise RuntimeError("자동 실행 등록은 Windows에서만 지원합니다")
    import winreg

    return winreg
