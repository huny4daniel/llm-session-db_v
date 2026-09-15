import argparse
import sqlite3
import sys

from . import config, db, ingest, machines

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server", description="LLM 세션 수집 서버")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="서버 실행")
    serve.add_argument("--host", default=config.DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="루프백 외 주소 바인딩 허용 (웹 UI 인증이 붙기 전까지는 사설망에서만 사용)",
    )
    add = sub.add_parser("add-machine", help="에이전트용 PC 등록 후 토큰 발급")
    add.add_argument("name")
    rotate = sub.add_parser("rotate-token", help="PC 토큰 재발급")
    rotate.add_argument("name")
    sub.add_parser("machines", help="등록된 PC 목록")
    sub.add_parser("rebuild", help="원본 이벤트로 파생 데이터 전체 재생성")

    args = parser.parse_args(argv)
    path = config.db_path()

    if args.command == "serve":
        if args.host not in LOOPBACK_HOSTS and not args.allow_remote:
            print(
                "웹 UI에 아직 인증이 없어 루프백 주소로만 실행할 수 있습니다. "
                "사설망(Tailscale 등)에서만 쓸 경우 --allow-remote를 붙이세요.",
                file=sys.stderr,
            )
            return 2
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(path), host=args.host, port=args.port)
        return 0

    db.init_db(path)
    conn = db.connect(path)
    try:
        if args.command == "add-machine":
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
        elif args.command == "rebuild":
            count = ingest.rebuild_all(conn)
            print(f"세션 {count}개 재생성 완료")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
