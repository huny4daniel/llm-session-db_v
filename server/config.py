import os
from pathlib import Path

from agent.claude_cli import claude_command  # noqa: F401  (서버 설정에서도 같은 규칙으로 찾는다)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def data_dir() -> Path:
    return Path(os.environ.get("LSDB_DATA_DIR", PROJECT_ROOT / "data"))


def db_path() -> Path:
    return data_dir() / "sessions.db"


def pid_path() -> Path:
    """실행 중인 서버 프로세스 ID. GUI에서 백그라운드 서버를 중지할 때 쓴다."""
    return data_dir() / "server.pid"


def workspace_dir() -> Path:
    """원래 프로젝트 폴더가 없는 세션을 웹에서 이어갈 때 쓰는 작업 폴더."""
    return data_dir() / "workspaces"


def claude_projects_dir() -> Path:
    return Path(os.environ.get("LSDB_CLAUDE_ROOT", Path.home() / ".claude" / "projects"))
