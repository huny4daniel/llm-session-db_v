import argparse
import sys
import time
from pathlib import Path

from . import config
from .client import Client, OffsetConflict, ServerError
from .collector import discover_claude
from .sync import SyncResult, sync_files


def run_once(cfg: config.AgentConfig) -> SyncResult:
    client = Client(cfg.server_url, cfg.token)
    return sync_files(client, "claude", discover_claude(Path(cfg.claude_root)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent", description="LLM 세션 수집 에이전트")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="서버 주소와 토큰 저장")
    setup.add_argument("--server", required=True, help="예: http://127.0.0.1:8765")
    setup.add_argument("--token", required=True)
    setup.add_argument("--claude-root", help="기본값: ~/.claude/projects")

    run = sub.add_parser("run", help="세션 수집 실행")
    run.add_argument("--once", action="store_true", help="한 번만 수집하고 종료")
    run.add_argument("--interval", type=float, default=30, help="반복 수집 간격(초)")

    args = parser.parse_args(argv)

    if args.command == "setup":
        cfg = config.AgentConfig(
            server_url=args.server,
            token=args.token,
            claude_root=args.claude_root or str(config.default_claude_root()),
        )
        print(f"설정 저장: {config.save(cfg)}")
        return 0

    try:
        cfg = config.load()
    except FileNotFoundError:
        print("설정이 없습니다. 먼저 python -m agent setup --server URL --token TOKEN 을 실행하세요.", file=sys.stderr)
        return 1

    try:
        while True:
            try:
                started = time.monotonic()
                result = run_once(cfg)
                if args.once or result.lines:
                    elapsed = time.monotonic() - started
                    print(
                        f"[{time.strftime('%H:%M:%S')}] 파일 {result.files}개, 줄 {result.lines}개 전송 ({elapsed:.1f}초)",
                        flush=True,
                    )
            except (OSError, ServerError, OffsetConflict) as e:
                print(f"[{time.strftime('%H:%M:%S')}] 동기화 실패: {e}", file=sys.stderr, flush=True)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
