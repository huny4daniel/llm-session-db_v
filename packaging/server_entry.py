"""PyInstaller 진입점(서버 exe). 서버와 에이전트를 모두 담아 서버 PC 한 대에 이 파일 하나만 두면 된다.

- 명령 없이 실행: 관리 GUI(에이전트 탭 + 서버 탭)
- `LlmSessionServer.exe serve|install|stop|add-machine ...`: 서버 명령
- `LlmSessionServer.exe agent run|setup|status|pull ...`: 이 PC의 수집 에이전트 명령
"""

import sys

from agent.__main__ import main as agent_main
from server.__main__ import main as server_main


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "agent":
        return agent_main(args[1:])
    return server_main(args)


if __name__ == "__main__":
    sys.exit(main())
