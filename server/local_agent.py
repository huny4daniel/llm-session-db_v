"""서버 PC 자신의 수집 에이전트를 고정 이름 `Local`로 자동 등록한다.

서버와 에이전트가 같은 PC에 있으므로 토큰을 손으로 옮길 필요가 없다. 서버를 설치·시작할 때
- 이 PC의 에이전트 설정(agent.json)에 있는 토큰이 가리키는 PC를 서버 PC 자신으로 보고 이름을 `Local`로 맞춘다.
- 설정이 없거나 토큰이 유효하지 않으면 `Local` PC를 만들거나 토큰을 재발급해 설정 파일을 새로 쓴다.
`Local`은 이름을 바꾸거나 지울 수 없다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from agent import config as agent_config
from agent import service as agent_service

from . import machines

LOCAL_NAME = "Local"


@dataclass
class LocalAgentResult:
    cfg: agent_config.AgentConfig
    changed: bool  # 설정 파일을 새로 썼는지
    note: str


def is_local(name: str) -> bool:
    return name == LOCAL_NAME


def ensure_local_agent(conn: sqlite3.Connection, host: str, port: int) -> LocalAgentResult:
    """`Local` PC와 이 PC의 에이전트 설정을 서로 맞춘다. 이미 맞으면 아무것도 바꾸지 않는다."""
    url = f"http://{host}:{port}"
    try:
        current = agent_config.load()
    except (FileNotFoundError, ValueError, KeyError):
        current = None
    claude_root = current.claude_root if current else str(agent_config.default_claude_root())

    mine = machines.find_by_token(conn, current.token) if current else None
    if mine is not None:
        if not is_local(mine["name"]):
            try:
                machines.rename_machine(conn, mine["name"], LOCAL_NAME, allow_protected=True)
                note = f"이 PC의 에이전트({mine['name']})를 {LOCAL_NAME}으로 이름을 바꿨습니다."
            except sqlite3.IntegrityError:
                note = f"이 PC의 에이전트는 {mine['name']}로 등록되어 있습니다({LOCAL_NAME} 이름은 다른 PC가 쓰는 중)."
        else:
            note = f"이 PC의 에이전트는 {LOCAL_NAME}으로 등록되어 있습니다."
        if current.server_url == url:
            return LocalAgentResult(current, False, note)
        cfg = agent_config.AgentConfig(server_url=url, token=current.token, claude_root=claude_root)
        agent_config.save(cfg)
        return LocalAgentResult(cfg, True, note + f" 서버 주소를 {url}로 맞췄습니다.")

    existing = conn.execute("SELECT id FROM machines WHERE name = ?", (LOCAL_NAME,)).fetchone()
    if existing is None:
        token = machines.create_machine(conn, LOCAL_NAME, allow_protected=True)
        note = f"이 PC의 에이전트를 {LOCAL_NAME}으로 등록했습니다."
    else:
        token = machines.rotate_token(conn, LOCAL_NAME)
        note = f"{LOCAL_NAME}의 토큰을 새로 발급해 이 PC의 에이전트 설정에 넣었습니다."
    cfg = agent_config.AgentConfig(server_url=url, token=token, claude_root=claude_root)
    agent_config.save(cfg)
    return LocalAgentResult(cfg, True, note)


def install_local_agent(conn: sqlite3.Connection, host: str, port: int, *, start: bool = True) -> str:
    """`Local` 등록 + 에이전트 자동 실행 등록(+ 지금 시작). 안내 문구를 돌려준다."""
    result = ensure_local_agent(conn, host, port)
    agent_service.register_autostart()
    notes = [result.note, "에이전트 자동 실행을 등록했습니다."]
    if start:
        if result.changed and agent_service.is_running():
            agent_service.stop_background()  # 바뀐 설정으로 다시 띄운다
        notes.append("에이전트를 시작했습니다." if agent_service.start_background() else "에이전트는 이미 실행 중입니다.")
    return " ".join(notes)
