import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def data_dir() -> Path:
    return Path(os.environ.get("LSDB_DATA_DIR", PROJECT_ROOT / "data"))


def db_path() -> Path:
    return data_dir() / "sessions.db"
