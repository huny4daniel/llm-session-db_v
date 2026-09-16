import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import autostart, config, service
from .client import Client, OffsetConflict, ServerError
from .lock import AlreadyRunning, single_instance
from .pull import pull_session
from .status import collect_status
from .sync import sync_once

log = logging.getLogger("agent")


def cli_name() -> str:
    """도움말·안내 문구에 쓸 실행 명령. exe로 묶였으면 exe 이름(서버 exe는 `agent` 접두어 포함)을 쓴다."""
    if not autostart.frozen():
        return "python -m agent"
    return " ".join([Path(sys.executable).name, *autostart.command_prefix("agent")])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=cli_name(), description="LLM 세션 수집 에이전트 (명령 없이 실행하면 GUI 창을 엽니다)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="GUI 창 열기(기본)")

    setup = sub.add_parser("setup", help="서버 주소와 토큰 저장")
    setup.add_argument("--server", required=True, help="예: http://127.0.0.1:8765")
    setup.add_argument("--token", required=True)
    setup.add_argument("--claude-root", help="기본값: ~/.claude/projects")

    run = sub.add_parser("run", help="세션 수집 실행")
    run.add_argument("--once", action="store_true", help="한 번만 수집하고 종료")
    run.add_argument("--interval", type=float, default=service.DEFAULT_INTERVAL, help="반복 수집 간격(초)")
    run.add_argument("--log-file", action="store_true", help="로그를 설정 폴더의 agent.log에 기록(백그라운드 실행용)")

    sub.add_parser("status", help="설정·서버 연결·미전송 파일 확인")

    install = sub.add_parser("install", help="Windows 로그인 시 에이전트 자동 실행 등록")
    install.add_argument("--interval", type=float, default=service.DEFAULT_INTERVAL, help="반복 수집 간격(초)")
    install.add_argument("--no-start", action="store_true", help="등록만 하고 지금 시작하지 않음")
    sub.add_parser("uninstall", help="에이전트 자동 실행 등록 해제")
    sub.add_parser("stop", help="백그라운드로 실행 중인 에이전트 종료")

    pull = sub.add_parser("pull", help="서버의 세션을 이 PC로 가져와 이어가기")
    pull.add_argument("session", help="세션 번호(웹 주소 #/session/<번호>) 또는 세션 ID 앞부분")
    pull.add_argument("--dir", help="이어갈 작업 폴더 (기본: 원래 프로젝트 폴더가 이 PC에 있으면 그곳, 없으면 현재 폴더)")
    pull.add_argument("--run", action="store_true", help="가져온 뒤 바로 claude 실행")

    args = parser.parse_args(argv)

    if args.command in (None, "gui"):
        from .gui import main as gui_main

        return gui_main()
    if args.command == "setup":
        cfg = config.AgentConfig(
            server_url=args.server,
            token=args.token,
            claude_root=args.claude_root or str(config.default_claude_root()),
        )
        print(f"설정 저장: {config.save(cfg)}")
        return 0
    if args.command == "uninstall":
        return _uninstall()
    if args.command == "stop":
        return _stop()

    try:
        cfg = config.load()
    except FileNotFoundError:
        print(f"설정이 없습니다. 먼저 {cli_name()} setup --server URL --token TOKEN 을 실행하세요.", file=sys.stderr)
        return 1

    if args.command == "status":
        return _status(cfg)
    if args.command == "pull":
        client = Client(cfg.server_url, cfg.token)
        return pull_session(client, Path(cfg.claude_root), args.session, workdir=args.dir, run=args.run)
    if args.command == "install":
        return _install(args)
    return _run(cfg, once=args.once, interval=args.interval, log_file=args.log_file)


def _run(cfg: config.AgentConfig, *, once: bool, interval: float, log_file: bool) -> int:
    _configure_logging(config.log_path() if log_file else None)
    try:
        with single_instance(config.lock_path()):
            service.write_pid()
            try:
                return _loop(cfg, once=once, interval=interval)
            finally:
                service.clear_pid()
    except AlreadyRunning:
        log.error("이미 다른 에이전트가 실행 중입니다 (잠금 파일: %s)", config.lock_path())
        return 1


def _loop(cfg: config.AgentConfig, *, once: bool, interval: float) -> int:
    if not once:
        log.info("수집 시작: %s → %s (%s초 간격)", cfg.claude_root, cfg.server_url, interval)
    failing = False
    try:
        while True:
            try:
                started = time.monotonic()
                result = sync_once(cfg)
                if failing:
                    log.info("서버 연결이 복구되었습니다")
                failing = False
                if once or result.lines:
                    log.info("파일 %d개, 줄 %d개 전송 (%.1f초)", result.files, result.lines, time.monotonic() - started)
            except (OSError, ServerError, OffsetConflict) as e:
                # 서버가 꺼져 있으면 매번 같은 오류가 반복되므로 처음 실패할 때만 남긴다.
                if once or not failing:
                    log.warning("동기화 실패: %s", e)
                failing = True
                if once:
                    return 1
            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _configure_logging(log_file: Path | None) -> None:
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def _status(cfg: config.AgentConfig) -> int:
    report = collect_status(cfg)
    print("\n".join(report.lines()))
    return 0 if report.ok else 1


def _install(args) -> int:
    try:
        command = service.register_autostart(args.interval)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"로그인 시 자동 실행 등록 완료\n  {command}")
    if args.no_start:
        return 0
    if service.start_background(args.interval):
        print(f"백그라운드에서 에이전트를 시작했습니다. 로그: {config.log_path()}")
    else:
        print("이미 실행 중인 에이전트가 있어 새로 시작하지 않았습니다.")
    return 0


def _uninstall() -> int:
    try:
        removed = service.unregister_autostart()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print("자동 실행 등록을 해제했습니다. 실행 중인 에이전트는 그대로이니 필요하면 직접 종료하세요."
          if removed else "등록된 자동 실행이 없습니다.")
    return 0


def _stop() -> int:
    if not service.is_running():
        print("실행 중인 에이전트가 없습니다.")
        return 0
    if service.stop_background():
        print("에이전트를 종료했습니다.")
        return 0
    print("에이전트를 종료하지 못했습니다. 작업 관리자에서 직접 종료하세요.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
