"""테스트용 합성 Claude Code 세션. 실제 세션에서 관찰한 이벤트 구조만 흉내 내고 내용은 가짜다."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSION = "11111111-2222-3333-4444-555555555555"
AGENT = "a0123456789abcdef"
PROJECT_KEY = "C--work-demo"
CWD = "C:\\work\\demo"
MAIN_KEY = f"{PROJECT_KEY}/{SESSION}.jsonl"
SUB_KEY = f"{PROJECT_KEY}/{SESSION}/subagents/agent-{AGENT}.jsonl"

BASE_TIME = datetime.now(timezone.utc) - timedelta(hours=2)


def ts(seconds: float) -> str:
    return (BASE_TIME + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _usage(inp: int, out: int, cc: int, cr: int) -> dict:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
    }


def _assistant(uuid: str, parent: str, sec: float, msg_id: str, block: dict, usage: dict, model="claude-sonnet-5"):
    return {
        **MAIN_BASE, "type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": ts(sec),
        "message": {"id": msg_id, "model": model, "role": "assistant", "content": [block], "usage": usage},
    }


MAIN_BASE = {"sessionId": SESSION, "cwd": CWD, "gitBranch": "main", "version": "2.1.0", "isSidechain": False}


def main_lines() -> list[str]:
    usage_a = lambda out: _usage(10, out, 100, 1000)  # noqa: E731
    return [_dump(e) for e in [
        {"type": "mode", "mode": "normal", "sessionId": SESSION},
        {**MAIN_BASE, "type": "user", "uuid": "u1", "parentUuid": None, "timestamp": ts(0),
         "message": {"role": "user", "content": "<local-command-stdout>무시</local-command-stdout>"}},
        {**MAIN_BASE, "type": "user", "uuid": "u2", "parentUuid": "u1", "timestamp": ts(1),
         "message": {"role": "user", "content": "세션 저장 서버를 만들어줘"}},
        {**MAIN_BASE, "type": "user", "isMeta": True, "uuid": "u3", "parentUuid": "u2", "timestamp": ts(2),
         "message": {"role": "user", "content": [{"type": "text", "text": "메타 정보"}]}},
        _assistant("a1", "u3", 3, "msg_A", {"type": "thinking", "thinking": "먼저 구조를 생각한다"}, usage_a(5)),
        _assistant("a2", "a1", 4, "msg_A", {"type": "text", "text": "구조를 설계하겠습니다"}, usage_a(50)),
        _assistant("a3", "a2", 5, "msg_A",
                   {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls -la"}}, usage_a(80)),
        {**MAIN_BASE, "type": "user", "uuid": "r1", "parentUuid": "a3", "timestamp": ts(6),
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.txt"}]}},
        _assistant("a4", "r1", 7, "msg_B", {"type": "text", "text": "완료했습니다"}, _usage(2, 20, 0, 2000)),
        _assistant("a5", "a4", 8, "msg_C", {"type": "text", "text": "API Error"}, _usage(0, 0, 0, 0), model="<synthetic>"),
        {**MAIN_BASE, "type": "attachment", "uuid": "t1", "parentUuid": "a5", "timestamp": ts(9),
         "attachment": {"type": "date"}},
        {**MAIN_BASE, "type": "system", "subtype": "turn_duration", "uuid": "y1", "timestamp": ts(10), "durationMs": 1},
        {**MAIN_BASE, "type": "system", "subtype": "local_command", "uuid": "y2", "timestamp": ts(11), "content": "명령 출력"},
        {"type": "ai-title", "aiTitle": "세션 서버 설계", "sessionId": SESSION},
        {"type": "cost-state", "totalCostUSD": 0.5, "sessionId": SESSION},
        {**MAIN_BASE, "type": "user", "uuid": "u4", "parentUuid": "a5", "timestamp": ts(12),
         "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
        {**MAIN_BASE, "type": "user", "uuid": "u5", "parentUuid": "u4", "timestamp": ts(13),
         "message": {"role": "user", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
             {"type": "text", "text": "스크린샷 확인해줘"},
         ]}},
        {"type": "ai-title", "aiTitle": "세션 서버 구현", "sessionId": SESSION},
        {"type": "cost-state", "totalCostUSD": 1.25, "sessionId": SESSION},
    ]]


def subagent_lines() -> list[str]:
    base = {"sessionId": SESSION, "agentId": AGENT, "isSidechain": True, "cwd": "C:\\elsewhere"}
    return [_dump(e) for e in [
        {"type": "fork-context-ref", "agentId": AGENT, "parentSessionId": SESSION},
        {**base, "type": "user", "uuid": "s1", "parentUuid": None, "timestamp": ts(20),
         "message": {"role": "user", "content": "하위 작업 조사"}},
        {**base, "type": "assistant", "uuid": "s2", "parentUuid": "s1", "timestamp": ts(21),
         "message": {"id": "msg_S", "model": "claude-haiku-4-5", "role": "assistant",
                     "content": [{"type": "text", "text": "조사 결과 trigram 사용"}], "usage": _usage(5, 7, 0, 0)}},
    ]]


# 메인 + 서브에이전트 합계 기대값
EXPECTED_TOKENS = {"input_tokens": 17, "output_tokens": 107, "cache_creation_tokens": 100, "cache_read_tokens": 3000}


def write_jsonl(path: Path, lines: list[str], *, partial_tail: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(line + "\n" for line in lines)
    if partial_tail is not None:
        data += partial_tail
    path.write_bytes(data.encode("utf-8"))
