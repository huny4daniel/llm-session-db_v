import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("LSDB_AGENT_CONFIG", Path.home() / ".llm-session-db" / "agent.json"))


def state_dir() -> Path:
    return config_path().parent


def log_path() -> Path:
    return state_dir() / "agent.log"


def lock_path() -> Path:
    return state_dir() / "agent.lock"


def pid_path() -> Path:
    return state_dir() / "agent.pid"


def default_claude_root() -> Path:
    return Path.home() / ".claude" / "projects"


@dataclass
class AgentConfig:
    server_url: str
    token: str
    claude_root: str


def load(path: Path | None = None) -> AgentConfig:
    data = json.loads(Path(path or config_path()).read_text(encoding="utf-8"))
    return AgentConfig(
        server_url=data["server_url"],
        token=data["token"],
        claude_root=data.get("claude_root") or str(default_claude_root()),
    )


def save(cfg: AgentConfig, path: Path | None = None) -> Path:
    target = Path(path or config_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    return target
