"""테스트용 가짜 대화형 claude. 받은 인자·작업 폴더·가져오기 사본 존재 여부를 기록하고 끝난다."""

import json
import os
import sys
from pathlib import Path


def main() -> None:
    args = sys.argv[1:]
    session_uid = args[args.index("--resume") + 1]
    projects = Path(os.environ["FAKE_CLAUDE_PROJECTS"])
    Path(os.environ["FAKE_CLAUDE_ARGS"]).write_text(json.dumps({
        "args": args,
        "cwd": os.getcwd(),
        "claudecode_env": os.environ.get("CLAUDECODE"),
        "import_copy_exists": (projects / "llm-session-db-import" / f"{session_uid}.jsonl").exists(),
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
