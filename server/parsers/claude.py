"""Claude Code 세션 JSONL 파서.

file_key는 `~/.claude/projects` 기준 상대경로(구분자 /)다.
- 메인 세션: <project_key>/<session_uid>.jsonl
- 서브에이전트: <project_key>/<session_uid>/subagents/agent-<agent_id>.jsonl

형식이 공식 문서화되어 있지 않으므로 아는 필드만 방어적으로 읽고,
모르는 이벤트는 무시한다(원본은 raw_events에 그대로 남는다).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SOURCE = "claude"

# 사람이 입력한 프롬프트가 아니라 CLI가 사용자 메시지로 끼워 넣는 태그(슬래시 명령, 로컬 명령 출력 등)
COMMAND_TAG = re.compile(
    r"<(command-[a-z-]+|local-command-[a-z-]+|bash-[a-z-]+|task-notification|system-reminder|user-prompt-submit-hook|fork-boilerplate)>"
)

# 검색 인덱스에 넣을 도구 입력/결과 텍스트의 최대 길이
SEARCH_TOOL_TEXT_LIMIT = 2000
# 대화 뷰로 내려줄 긴 문자열의 최대 길이
VIEW_TEXT_LIMIT = 20000

# 먼저 들어온 값을 유지하는 세션 컬럼 / 나중 값으로 덮어쓰는 세션 컬럼
META_FIRST_COLUMNS = frozenset({"project_path", "first_prompt"})
META_LAST_COLUMNS = frozenset({"ai_title", "last_prompt", "git_branch", "cli_version", "cost_usd"})


@dataclass(frozen=True)
class FileRef:
    project_key: str
    session_uid: str
    agent_id: str | None = None


@dataclass
class Message:
    kind: str  # user | command | interrupted | assistant | tool_result | system | meta | compact_summary
    uuid: str | None
    parent_uuid: str | None
    timestamp: str | None
    text: str
    model: str | None = None
    api_message_id: str | None = None


@dataclass
class Usage:
    api_message_id: str
    model: str | None
    timestamp: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int


@dataclass
class ParsedLine:
    message: Message | None = None
    usage: Usage | None = None
    meta_first: dict = field(default_factory=dict)
    meta_last: dict = field(default_factory=dict)


def parse_file_key(file_key: str) -> FileRef | None:
    parts = file_key.split("/")
    if len(parts) == 2 and parts[1].endswith(".jsonl"):
        return FileRef(parts[0], parts[1][: -len(".jsonl")])
    if (
        len(parts) == 4
        and parts[2] == "subagents"
        and parts[3].startswith("agent-")
        and parts[3].endswith(".jsonl")
    ):
        return FileRef(parts[0], parts[1], parts[3][len("agent-") : -len(".jsonl")])
    return None


def parse_line(line: str, ref: FileRef) -> ParsedLine:
    out = ParsedLine()
    event = _load(line)
    if event is None:
        return out

    is_main = ref.agent_id is None
    if is_main:
        _put_str(out.meta_first, "project_path", event.get("cwd"))
        _put_str(out.meta_last, "git_branch", event.get("gitBranch"))
        _put_str(out.meta_last, "cli_version", event.get("version"))

    kind = event.get("type")
    if kind == "ai-title" and is_main:
        _put_str(out.meta_last, "ai_title", event.get("aiTitle"))
    elif kind == "last-prompt" and is_main:
        _put_str(out.meta_last, "last_prompt", event.get("lastPrompt"))
    elif kind == "cost-state" and is_main:
        cost = event.get("totalCostUSD")
        if isinstance(cost, (int, float)):
            out.meta_last["cost_usd"] = float(cost)
    elif kind == "user":
        out.message = _user_message(event)
        if is_main and out.message and out.message.kind == "user":
            out.meta_first["first_prompt"] = out.message.text.strip()[:500]
    elif kind == "assistant":
        out.message, out.usage = _assistant_message(event)
    elif kind == "system":
        content = event.get("content")
        if isinstance(content, str) and content.strip():
            out.message = _message(event, "system", content)
    return out


def search_text(content) -> str:
    """검색 인덱스용 평문. 도구 입출력은 앞부분만 넣는다."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(_str(block.get("text")))
        elif block_type == "thinking":
            parts.append(_str(block.get("thinking")))
        elif block_type == "tool_use":
            # JSON 문자열로 넣으면 따옴표·개행이 이스케이프되어 검색·미리보기가 어긋나므로 값만 모은다.
            values = "\n".join(_string_values(block.get("input")))
            parts.append(f"{_str(block.get('name'))} {values[:SEARCH_TOOL_TEXT_LIMIT]}")
        elif block_type == "tool_result":
            parts.append(tool_result_text(block.get("content"))[:SEARCH_TOOL_TEXT_LIMIT])
    return "\n".join(p for p in parts if p)


def tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(_str(block.get("text")))
        elif block.get("type") == "image":
            parts.append("[이미지]")
    return "\n".join(parts)


def view_blocks(line: str) -> list[dict]:
    """대화 뷰에 표시할 블록 목록. 이미지 데이터와 지나치게 긴 문자열은 잘라낸다."""
    event = _load(line)
    if event is None:
        return []
    if event.get("type") == "system":
        return [{"type": "text", "text": _truncate(_str(event.get("content")))}]

    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": _truncate(content)}]
    if not isinstance(content, list):
        return []

    blocks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            blocks.append({"type": "text", "text": _truncate(_str(block.get("text")))})
        elif block_type == "thinking":
            thinking = _str(block.get("thinking"))
            if thinking:
                blocks.append({"type": "thinking", "text": _truncate(thinking)})
        elif block_type == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": block.get("id"),
                "name": _str(block.get("name")),
                "input": _truncate_json(block.get("input")),
            })
        elif block_type == "tool_result":
            blocks.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id"),
                "is_error": bool(block.get("is_error")),
                "text": _truncate(tool_result_text(block.get("content"))),
            })
        elif block_type == "image":
            source = block.get("source")
            media_type = source.get("media_type") if isinstance(source, dict) else None
            blocks.append({"type": "image", "media_type": media_type})
    return blocks


def _user_message(event: dict) -> Message | None:
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    text = search_text(content)
    stripped = text.strip()
    if event.get("isMeta"):
        kind = "meta"
    elif event.get("isCompactSummary"):
        kind = "compact_summary"
    elif _is_tool_result_only(content):
        kind = "tool_result"
    elif stripped.startswith("[Request interrupted"):
        kind = "interrupted"
    elif COMMAND_TAG.match(stripped):
        kind = "command"
    else:
        kind = "user"
    return _message(event, kind, text)


def _assistant_message(event: dict) -> tuple[Message | None, Usage | None]:
    message = event.get("message")
    if not isinstance(message, dict):
        return None, None
    model = _opt_str(message.get("model"))
    api_message_id = _opt_str(message.get("id"))
    parsed = _message(
        event, "assistant", search_text(message.get("content")),
        model=model, api_message_id=api_message_id,
    )

    usage = None
    raw_usage = message.get("usage")
    # <synthetic>은 API 오류 등 CLI가 직접 만든 메시지라 실제 사용량이 아니다.
    if api_message_id and isinstance(raw_usage, dict) and model != "<synthetic>":
        usage = Usage(
            api_message_id=api_message_id,
            model=model,
            timestamp=_opt_str(event.get("timestamp")),
            input_tokens=_int(raw_usage.get("input_tokens")),
            output_tokens=_int(raw_usage.get("output_tokens")),
            cache_creation_tokens=_int(raw_usage.get("cache_creation_input_tokens")),
            cache_read_tokens=_int(raw_usage.get("cache_read_input_tokens")),
        )
    return parsed, usage


def _message(event: dict, kind: str, text: str, **extra) -> Message:
    return Message(
        kind=kind,
        uuid=_opt_str(event.get("uuid")),
        parent_uuid=_opt_str(event.get("parentUuid")),
        timestamp=_opt_str(event.get("timestamp")),
        text=text,
        **extra,
    )


def _is_tool_result_only(content) -> bool:
    return (
        isinstance(content, list)
        and len(content) > 0
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _load(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _put_str(target: dict, key: str, value) -> None:
    if isinstance(value, str) and value:
        target[key] = value


def _string_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _string_values(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _string_values(item)]
    return []


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _opt_str(value) -> str | None:
    return value if isinstance(value, str) else None


def _int(value) -> int:
    return value if isinstance(value, int) else 0


def _truncate(text: str) -> str:
    if len(text) <= VIEW_TEXT_LIMIT:
        return text
    return f"{text[:VIEW_TEXT_LIMIT]}\n… ({len(text) - VIEW_TEXT_LIMIT:,}자 생략)"


def _truncate_json(value):
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, list):
        return [_truncate_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _truncate_json(v) for k, v in value.items()}
    return value
