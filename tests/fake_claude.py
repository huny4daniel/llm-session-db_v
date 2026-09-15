"""테스트용 가짜 Claude Code CLI. `-p --output-format stream-json` 출력 형식을 흉내 낸다.

환경 변수
- FAKE_CLAUDE_ARGS: 받은 인자·프롬프트·작업 폴더를 기록할 JSON 파일
- FAKE_CLAUDE_PROJECTS: 가져오기 사본 확인용 projects 폴더
- FAKE_CLAUDE_MODE: sleep(오래 대기) | deny(권한 거부 포함) | fail(오류 종료)
"""

import json
import os
import sys
import time
from pathlib import Path

FORK_ID = "99999999-0000-4000-8000-000000000000"


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main() -> None:
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    session_id = args[args.index("--resume") + 1]
    new_id = FORK_ID if "--fork-session" in args else session_id
    mode = os.environ.get("FAKE_CLAUDE_MODE", "")

    log = os.environ.get("FAKE_CLAUDE_ARGS")
    if log:
        projects = os.environ.get("FAKE_CLAUDE_PROJECTS")
        copy = Path(projects) / "llm-session-db-import" / f"{session_id}.jsonl" if projects else None
        Path(log).write_text(json.dumps({
            "args": args,
            "prompt": prompt,
            "cwd": os.getcwd(),
            "claudecode_env": os.environ.get("CLAUDECODE"),
            "import_copy_lines": len(copy.read_text(encoding="utf-8").splitlines()) if copy and copy.exists() else None,
        }, ensure_ascii=False), encoding="utf-8")

    if mode == "fail":
        print("fake failure", file=sys.stderr)
        sys.exit(3)

    emit({"type": "system", "subtype": "init", "session_id": new_id, "cwd": os.getcwd(), "model": "fake-model", "permissionMode": "default"})
    if mode == "sleep":
        time.sleep(60)

    text = f"에코: {prompt}"
    emit({"type": "system", "subtype": "thinking_tokens"})
    emit({"type": "stream_event", "event": {"type": "message_start"}})
    emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}})
    for part in ("에코: ", prompt):
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": part}}})
    emit({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    emit({"type": "assistant", "parent_tool_use_id": None, "session_id": new_id,
          "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    denials = [{"tool_name": "Write", "tool_use_id": "toolu_x", "tool_input": {"file_path": "a.txt", "content": "hi"}}] if mode == "deny" else []
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": new_id,
          "total_cost_usd": 0.001, "duration_ms": 5, "permission_denials": denials})


if __name__ == "__main__":
    main()
