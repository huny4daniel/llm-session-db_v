import argparse
import getpass
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from . import auth, config, db, ingest, machines, queries, service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server", description="LLM 세션 수집 서버 (명령 없이 실행하면 GUI 창을 엽니다)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="GUI 창 열기(기본). 서버 시작·중지, PC 등록, 비밀번호 설정")

    serve = sub.add_parser("serve", help="서버 실행")
    _add_bind_args(serve)
    serve.add_argument("--log-file", action="store_true", help="로그를 데이터 폴더의 server.log에 기록(백그라운드 실행용)")

    install = sub.add_parser("install", help="Windows 로그인 시 서버 자동 실행 등록")
    _add_bind_args(install)
    install.add_argument("--no-start", action="store_true", help="등록만 하고 지금 시작하지 않음")
    sub.add_parser("uninstall", help="서버 자동 실행 등록 해제")
    stop = sub.add_parser("stop", help="백그라운드로 실행 중인 서버 종료")
    _add_bind_args(stop)

    password = sub.add_parser("set-password", help="웹 UI 비밀번호 설정(원격 접속에 필요)")
    password.add_argument("--stdin", action="store_true", help="표준 입력 첫 줄에서 비밀번호를 읽음")
    sub.add_parser("clear-password", help="웹 UI 비밀번호 제거(이 PC에서만 접속 가능해짐)")

    add = sub.add_parser("add-machine", help="에이전트용 PC 등록 후 토큰 발급")
    add.add_argument("name")
    rotate = sub.add_parser("rotate-token", help="PC 토큰 재발급")
    rotate.add_argument("name")
    sub.add_parser("machines", help="등록된 PC 목록")
    delete = sub.add_parser("delete-session", help="세션 삭제(원본 파일은 PC에 남고 다시 수집되지 않음)")
    delete.add_argument("ids", nargs="+", help="세션 번호(웹 주소 #/session/<번호>) 또는 세션 ID(앞부분만도 가능)")
    sub.add_parser("rebuild", help="원본 이벤트로 파생 데이터 전체 재생성")

    args = parser.parse_args(argv)
    path = config.db_path()
    db.init_db(path)

    if args.command in (None, "gui"):
        from agent.gui import main as gui_main

        return gui_main()
    if args.command == "serve":
        return _serve(args, path)
    if args.command == "stop":
        return _stop(args)

    with closing(db.connect(path)) as conn:
        if args.command == "install":
            return _install(args, conn)
        if args.command == "uninstall":
            removed = service.unregister_autostart()
            print("자동 실행 등록을 해제했습니다. 실행 중인 서버는 그대로이니 필요하면 직접 종료하세요."
                  if removed else "등록된 자동 실행이 없습니다.")
        elif args.command == "set-password":
            return _set_password(args, conn)
        elif args.command == "clear-password":
            auth.clear_password(conn)
            print("비밀번호를 제거했습니다. 이제 이 PC에서만 웹 UI에 접속할 수 있습니다.")
        elif args.command == "add-machine":
            try:
                token = machines.create_machine(conn, args.name)
            except sqlite3.IntegrityError:
                print(f"이미 등록된 이름입니다: {args.name}", file=sys.stderr)
                return 1
            print(f"PC 등록 완료: {args.name}")
            print(f"토큰 (다시 표시되지 않으니 보관하세요): {token}")
        elif args.command == "rotate-token":
            token = machines.rotate_token(conn, args.name)
            if token is None:
                print(f"등록되지 않은 이름입니다: {args.name}", file=sys.stderr)
                return 1
            print(f"새 토큰: {token}")
        elif args.command == "machines":
            for m in machines.list_machines(conn):
                print(f"{m['id']:>3}  {m['name']:<20} 세션 {m['session_count']:>5}  마지막 수신 {m['last_seen_at'] or '-'}")
        elif args.command == "delete-session":
            return _delete_sessions(conn, args.ids)
        elif args.command == "rebuild":
            print(f"세션 {ingest.rebuild_all(conn)}개 재생성 완료")
    return 0


def _delete_sessions(conn: sqlite3.Connection, ids: list[str]) -> int:
    failed = False
    for value in ids:
        ids = queries.resolve_session_refs(conn, value)
        if len(ids) != 1:
            print(f"{'찾을 수 없음' if not ids else '여러 세션과 일치'}: {value}", file=sys.stderr)
            failed = True
            continue
        session = conn.execute(
            "SELECT s.id, s.session_uid, s.project_path, m.name AS machine,"
            " COALESCE(s.ai_title, s.first_prompt, s.session_uid) AS title"
            " FROM sessions s JOIN machines m ON m.id = s.machine_id WHERE s.id = ?",
            (ids[0],),
        ).fetchone()
        ingest.delete_session(conn, session["id"])
        print(f"삭제: #{session['id']} {session['session_uid'][:8]} [{session['machine']}] {session['project_path']} — {session['title'][:50]}")
    return 1 if failed else 0


def _add_bind_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=config.DEFAULT_HOST, help="기본값: 127.0.0.1(이 PC에서만 접속)")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)


def _remote_bind_without_password(host: str, conn: sqlite3.Connection) -> bool:
    if host in auth.LOOPBACK_HOSTS or auth.password_enabled(conn):
        return False
    print("루프백이 아닌 주소로 열려면 먼저 비밀번호를 설정하세요: python -m server set-password", file=sys.stderr)
    return True


def _serve(args, path: Path) -> int:
    with closing(db.connect(path)) as conn:
        if _remote_bind_without_password(args.host, conn):
            return 2
        if not auth.password_enabled(conn):
            print("비밀번호가 설정되지 않아 이 PC에서만 웹 UI에 접속할 수 있습니다.", flush=True)

    import uvicorn

    from .app import create_app

    options = {}
    if args.log_file:
        options["log_config"] = _file_log_config(config.data_dir() / "server.log")
    service.write_pid()  # GUI·stop 명령이 이 프로세스를 종료할 수 있게 남긴다
    try:
        uvicorn.run(create_app(path), host=args.host, port=args.port, **options)
    finally:
        service.clear_pid()
    return 0


def _install(args, conn: sqlite3.Connection) -> int:
    if _remote_bind_without_password(args.host, conn):
        return 2
    try:
        command = service.register_autostart(args.host, args.port)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"로그인 시 자동 실행 등록 완료\n  {command}")
    if args.no_start:
        return 0
    if service.start_background(args.host, args.port):
        print(f"백그라운드에서 서버를 시작했습니다: {service.url(args.host, args.port)}  (로그: {config.data_dir() / 'server.log'})")
    else:
        print("이미 실행 중인 서버가 있어 새로 시작하지 않았습니다.")
    return 0


def _stop(args) -> int:
    if not service.is_running(args.host, args.port):
        print(f"응답하는 서버가 없습니다: {service.url(args.host, args.port)}")
        return 0
    if service.stop_background(args.host, args.port):
        print("서버를 종료했습니다.")
        return 0
    print("서버를 종료하지 못했습니다. serve/install로 시작한 서버가 아니면 작업 관리자에서 직접 종료하세요.", file=sys.stderr)
    return 1


def _set_password(args, conn: sqlite3.Connection) -> int:
    if args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("새 비밀번호: ")
        if password != getpass.getpass("비밀번호 확인: "):
            print("비밀번호가 일치하지 않습니다.", file=sys.stderr)
            return 1
    try:
        auth.set_password(conn, password)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print("비밀번호를 설정했습니다. 기존 로그인은 모두 해제됩니다.")
    return 0


def _file_log_config(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(path),
                "maxBytes": 1_000_000,
                "backupCount": 3,
                "encoding": "utf-8",
                "formatter": "default",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
        },
    }


if __name__ == "__main__":
    sys.exit(main())
