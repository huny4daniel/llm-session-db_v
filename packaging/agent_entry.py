"""PyInstaller 진입점. `python -m agent`와 같은 동작을 하는 단일 실행 파일을 만든다."""

import sys

from agent.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
