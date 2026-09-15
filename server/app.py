import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, ingest, machines, queries
from .parsers import PARSERS

STATIC_DIR = Path(__file__).parent / "static"


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


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = Path(db_path) if db_path else config.db_path()
    db.init_db(path)
    app = FastAPI(title="llm-session-db", docs_url=None, redoc_url=None, openapi_url=None)

    def get_conn():
        conn = db.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def get_machine(conn: Conn, authorization: Annotated[str | None, Header()] = None) -> sqlite3.Row:
        scheme, _, token = (authorization or "").partition(" ")
        machine = machines.find_by_token(conn, token) if scheme.lower() == "bearer" and token else None
        if machine is None:
            raise HTTPException(status_code=401, detail="유효하지 않은 에이전트 토큰입니다")
        return machine

    Machine = Annotated[sqlite3.Row, Depends(get_machine)]

    # ── 에이전트 API ──────────────────────────────────────────────

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

    # ── 조회 API ─────────────────────────────────────────────────

    @app.get("/api/machines")
    def get_machines(conn: Conn):
        return machines.list_machines(conn)

    @app.get("/api/projects")
    def get_projects(conn: Conn):
        return queries.list_projects(conn)

    @app.get("/api/sessions")
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

    @app.get("/api/sessions/{session_id}")
    def get_session(conn: Conn, session_id: int):
        session = queries.get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
        return session

    @app.get("/api/sessions/{session_id}/messages")
    def get_session_messages(conn: Conn, session_id: int, agent_id: str | None = None, include_meta: bool = False):
        messages = queries.session_messages(conn, session_id, agent_id=agent_id, include_meta=include_meta)
        if messages is None:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
        return messages

    @app.get("/api/search")
    def search(conn: Conn, q: Annotated[str, Query(min_length=1)], limit: Annotated[int, Query(ge=1, le=200)] = 50):
        return queries.search_messages(conn, q, limit=limit)

    @app.get("/api/stats")
    def get_stats(conn: Conn, days: Annotated[int, Query(ge=1, le=365)] = 30):
        return queries.stats(conn, days=days)

    # ── 웹 UI ───────────────────────────────────────────────────

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
