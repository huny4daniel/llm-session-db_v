import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def data_dir() -> Path:
    return Path(os.environ.get("LSDB_DATA_DIR", PROJECT_ROOT / "data"))


def db_path() -> Path:
    return data_dir() / "sessions.db"


def workspace_dir() -> Path:
    """원래 프로젝트 폴더가 없는 세션을 웹에서 이어갈 때 쓰는 작업 폴더."""
    return data_dir() / "workspaces"


def claude_projects_dir() -> Path:
    return Path(os.environ.get("LSDB_CLAUDE_ROOT", Path.home() / ".claude" / "projects"))


def claude_command() -> list[str] | None:
    override = os.environ.get("LSDB_CLAUDE_BIN")
    if override:
        return [override]
    found = shutil.which("claude")
    return [found] if found else None
