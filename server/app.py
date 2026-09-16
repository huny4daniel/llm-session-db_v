import asyncio
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, db, ingest, machines, queries, runner
from .parsers import PARSERS

STATIC_DIR = Path(__file__).parent / "static"
SSE_KEEPALIVE_SECONDS = 15


class IngestLine(BaseModel):
    offset: int = Field(ge=0)
    length: int = Field(gt=0)
    text: str


class IngestRequest(BaseModel):
    source: str
    file_key: str
    start_offset: int = Field(ge=0)
    size: int | None = None
    mtime: float | None = None
    reset: bool = False
    lines: list[IngestLine]


class LoginRequest(BaseModel):
    password: str


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    allowed_tools: list[str] = Field(default_factory=list)
    model: str | None = None


def create_app(db_path: str | Path | None = None, runs: runner.RunManager | None = None) -> FastAPI:
    path = Path(db_path) if db_path else config.db_path()
    db.init_db(path)
    app = FastAPI(title="llm-session-db", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = auth.LoginLimiter()

    def record_fork(parent_session_id: int, child_uid: str) -> None:
        with closing(db.connect(path)) as conn:
            ingest.record_fork(conn, parent_session_id, child_uid)

    if runs is None:
        runs = runner.RunManager(config.claude_command, config.claude_projects_dir(), config.workspace_dir())
    runs.on_fork = record_fork

    def get_conn():
        conn = db.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def client_host(request: Request) -> str | None:
        return request.client.host if request.client else None

    def get_machine(conn: Conn, authorization: Annotated[str | None, Header()] = None) -> sqlite3.Row:
        scheme, _, token = (authorization or "").partition(" ")
        machine = machines.find_by_token(conn, token) if scheme.lower() == "bearer" and token else None
        if machine is None:
            raise HTTPException(status_code=401, detail="유효하지 않은 에이전트 토큰입니다")
        return machine

    Machine = Annotated[sqlite3.Row, Depends(get_machine)]

    def require_viewer(request: Request, conn: Conn) -> None:
        if auth.password_enabled(conn):
            if auth.check_session(conn, request.cookies.get(auth.COOKIE_NAME)):
                return
            raise HTTPException(status_code=401, detail="로그인이 필요합니다")
        if not auth.is_local_request(client_host(request), request.headers):
            raise HTTPException(
                status_code=403,
                detail="원격 접속은 서버에서 비밀번호를 설정한 뒤 사용할 수 있습니다 (python -m server set-password)",
            )

    # ── 에이전트 API (PC별 토큰) ──────────────────────────────────

    @app.get("/api/agent/files")
    def agent_files(conn: Conn, machine: Machine, source: str):
        rows = conn.execute(
            "SELECT file_key, next_offset, size, mtime FROM source_files WHERE machine_id = ? AND source = ?",
            (machine["id"], source),
        )
        return {"files": {r["file_key"]: {k: r[k] for k in ("next_offset", "size", "mtime")} for r in rows}}

    @app.post("/api/agent/ingest")
    def agent_ingest(body: IngestRequest, conn: Conn, machine: Machine):
        if body.source not in PARSERS:
            raise HTTPException(status_code=400, detail=f"지원하지 않는 소스입니다: {body.source}")
        try:
            next_offset = ingest.ingest_batch(
                conn,
                machine_id=machine["id"],
                source=body.source,
                file_key=body.file_key,
                start_offset=body.start_offset,
                lines=[ingest.Line(l.offset, l.length, l.text) for l in body.lines],
                size=body.size,
                mtime=body.mtime,
                reset=body.reset,
            )
        except ingest.OffsetMismatch as e:
            return JSONResponse(
                status_code=409,
                content={"detail": "수신 위치가 다릅니다", "expected_offset": e.expected_offset},
            )
        except ingest.IngestError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"accepted": len(body.lines), "next_offset": next_offset}

    @app.get("/api/agent/sessions")
    def agent_sessions(conn: Conn, machine: Machine, q: str = "", limit: Annotated[int, Query(ge=1, le=200)] = 50):
        """가져오기(agent pull) 대상을 고르기 위한 세션 목록(최근 활동 순)."""
        keys = ("id", "session_uid", "title", "project_path", "machine_name", "last_activity_at", "user_turns")
        result = queries.list_sessions(conn, source="claude", q=q or None, limit=limit)
        return {"total": result["total"], "items": [{k: s[k] for k in keys} for s in result["items"]]}

    @app.get("/api/agent/sessions/{ref}/source")
    def agent_session_source(conn: Conn, machine: Machine, ref: str):
        """다른 PC로 가져가 이어가기(agent pull)용 세션 정보와 메인 파일 원본."""
        ids = queries.resolve_session_refs(conn, ref)
        if not ids:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
        if len(ids) > 1:
            raise HTTPException(status_code=409, detail="여러 세션과 일치합니다. 세션 ID를 더 길게 입력하세요")
        session = queries.get_session(conn, ids[0])
        if session["source"] != "claude":
            raise HTTPException(status_code=400, detail="이 도구의 세션은 아직 가져올 수 없습니다")
        keys = ("id", "source", "session_uid", "title", "project_path", "machine_name", "last_activity_at")
        return {"session": {k: session[k] for k in keys}, "lines": queries.main_file_lines(conn, ids[0])}

    # ── 로그인 ──────────────────────────────────────────────────

    @app.get("/api/auth/status")
    def auth_status(request: Request, conn: Conn):
        enabled = auth.password_enabled(conn)
        if enabled:
            authenticated = auth.check_session(conn, request.cookies.get(auth.COOKIE_NAME))
        else:
            authenticated = auth.is_local_request(client_host(request), request.headers)
        return {"password_enabled": enabled, "authenticated": authenticated}

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request, response: Response, conn: Conn):
        if not auth.password_enabled(conn):
            raise HTTPException(status_code=400, detail="비밀번호가 설정되어 있지 않습니다")
        key = client_host(request) or "unknown"
        wait = limiter.locked_for(key)
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"로그인 시도가 너무 많습니다. {math.ceil(wait)}초 후 다시 시도하세요")
        if not auth.verify_login(conn, body.password):
            limiter.failure(key)
            raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다")
        limiter.success(key)
        response.set_cookie(
            auth.COOKIE_NAME,
            auth.issue_session(conn),
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return {"ok": True}

    # ── 조회·조작 API (로그인 또는 로컬 접속) ─────────────────────────

    viewer = APIRouter(prefix="/api", dependencies=[Depends(require_viewer)])

    @viewer.get("/machines")
    def get_machines(conn: Conn):
        return machines.list_machines(conn)

    @viewer.get("/projects")
    def get_projects(conn: Conn):
        return queries.list_projects(conn)

    @viewer.get("/sessions")
    def get_sessions(
        conn: Conn,
        machine_id: int | None = None,
        project: str | None = None,
        source: str | None = None,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return queries.list_sessions(
            conn, machine_id=machine_id, project_path=project, source=source, q=q, limit=limit, offset=offset
        )

    @viewer.get("/session-lookup")
    def session_lookup(conn: Conn, uid: str):
        return {"id": queries.lookup_session(conn, uid)}

    @viewer.get("/sessions/{session_id}")
    def get_session(conn: Conn, session_id: int):
        session = queries.get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
        session["continue_plan"] = runs.plan(session).describe() if session["source"] == "claude" else None
        latest = runs.latest_for(session_id)
        session["latest_run"] = latest.summary() if latest else None
        return session

    @viewer.delete("/sessions/{session_id}")
    def remove_session(conn: Conn, session_id: int):
        if ingest.delete_session(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
        return {"ok": True}

    @viewer.get("/sessions/{session_id}/messages")
    def get_session_messages(conn: Conn, session_id: int, agent_id: str | None = None, include_meta: bool = False):
        messages = queries.session_messages(conn, session_id, agent_id=agent_id, include_meta=include_meta)
        if messages is None:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
        return messages

    @viewer.post("/sessions/{session_id}/runs")
    def start_run(conn: Conn, session_id: int, body: RunRequest):
        session = queries.get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
        if session["source"] != "claude":
            raise HTTPException(status_code=400, detail="이 도구의 세션은 아직 웹에서 이어갈 수 없습니다")
        try:
            run = runs.start(
                session, body.prompt, body.allowed_tools, body.model,
                source_lines=lambda: queries.main_file_lines(conn, session_id),
            )
        except runner.RunnerError as e:
            raise HTTPException(status_code=e.status, detail=str(e))
        return run.summary()

    @viewer.get("/runs/{run_id}")
    def get_run(run_id: str):
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="실행 기록을 찾을 수 없습니다")
        return run.summary()

    @viewer.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        run = runs.cancel(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="실행 기록을 찾을 수 없습니다")
        return run.summary()

    @viewer.get("/runs/{run_id}/events")
    async def run_events(
        request: Request,
        run_id: str,
        last_event_id: Annotated[str | None, Header(alias="last-event-id")] = None,
    ):
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="실행 기록을 찾을 수 없습니다")
        start = int(last_event_id) + 1 if last_event_id and last_event_id.isdigit() else 0

        async def stream():
            index = start
            while True:
                events, done = await asyncio.to_thread(run.wait_events, index, SSE_KEEPALIVE_SECONDS)
                for event in events:
                    yield f"id: {index}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    index += 1
                if done and index >= len(run.events):
                    yield "event: end\ndata: {}\n\n"
                    return
                if not events:
                    if await request.is_disconnected():
                        return
                    yield ": keepalive\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @viewer.get("/search")
    def search(conn: Conn, q: Annotated[str, Query(min_length=1)], limit: Annotated[int, Query(ge=1, le=200)] = 50):
        return queries.search_messages(conn, q, limit=limit)

    @viewer.get("/stats")
    def get_stats(conn: Conn, days: Annotated[int, Query(ge=1, le=365)] = 30):
        return queries.stats(conn, days=days)

    app.include_router(viewer)

    # ── 웹 UI (화면 셸은 공개, 데이터는 위 API가 보호) ─────────────────

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
