"""Claude Code CLI 실행 공통 부분. 에이전트의 가져오기(pull)와 서버의 웹 이어가기가 함께 쓴다."""

import os
import shutil

# Claude Code 세션 안에서 실행되면 물려받는 연결 정보. 자식 CLI가 그 세션에 붙지 않도록 뺀다.
INHERITED_SESSION_ENV = frozenset({
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
})


def claude_command() -> list[str] | None:
    override = os.environ.get("LSDB_CLAUDE_BIN")
    if override:
        return [override]
    found = shutil.which("claude")
    return [found] if found else None


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in INHERITED_SESSION_ENV}
