"""웹에서 세션 이어가기. 서버 PC에서 Claude Code CLI를 headless로 실행하고 이벤트를 모은다.

CLAUDE.md '검증된 CLI 동작'에 기댄다.
- 서버 PC에 세션 파일이 있으면 그 세션에 그대로 이어간다(--resume).
- 없으면(다른 PC 세션) 수집된 원본으로 가져오기 폴더에 사본을 만들고 --fork-session으로 새 세션을 만든다.
  CLI는 세션을 ID로 모든 프로젝트 폴더에서 찾으므로 사본 위치는 상관없고, 가져오기 폴더는 에이전트가 수집하지 않는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agent.collector import IMPORT_FOLDER

from .parsers import claude as claude_parser

TOOL_GROUPS = {
    "edit": ("Edit", "Write", "NotebookEdit"),
    "shell": ("Bash", "PowerShell"),
    "web": ("WebFetch", "WebSearch"),
}
MODELS = ("opus", "sonnet", "haiku")
MAX_ACTIVE_RUNS = 2
RUN_RETENTION_SECONDS = 30 * 60
STDERR_TAIL_CHARS = 4000

# 서버가 Claude Code 세션 안에서 시작되면 물려받는 연결 정보. 자식 CLI가 그 세션에 붙지 않도록 뺀다.
INHERITED_SESSION_ENV = frozenset({
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
})

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class RunnerError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Plan:
    cwd: Path
    fork: bool  # 서버 PC에 세션 파일이 없어 새 세션으로 이어감
    chat_only: bool  # 원래 프로젝트 폴더가 없어 도구 없이 대화만
    local_file: Path | None

    def describe(self) -> dict:
        return {"fork": self.fork, "chat_only": self.chat_only, "cwd": str(self.cwd), "local_file": self.local_file is not None}


@dataclass
class Run:
    id: str
    session_id: int
    session_uid: str
    prompt: str
    plan: Plan
    allowed_tools: list[str]
    model: str | None
    created_at: float = field(default_factory=time.time)
    status: str = "starting"  # starting | running | completed | failed | cancelled
    error: str | None = None
    result_session_uid: str | None = None
    finished_at: float | None = None
    events: list[dict] = field(default_factory=list)
    process: subprocess.Popen | None = None
    cancel_requested: bool = False
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)

    @property
    def done(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def append(self, event: dict) -> None:
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()

    def wait_events(self, since: int, timeout: float) -> tuple[list[dict], bool]:
        with self._cond:
            if since >= len(self.events) and not self.done:
                self._cond.wait(timeout)
            return self.events[since:], self.done

    def finish(self, status: str, error: str | None = None) -> None:
        with self._cond:
            self.status = status
            self.error = error
            self.finished_at = time.time()
            self.events.append({"type": "run_status", "status": status, "error": error, "session_uid": self.result_session_uid})
            self._cond.notify_all()

    def summary(self) -> dict:
        return {
            "id": self.id, "session_id": self.session_id, "status": self.status, "prompt": self.prompt,
            "error": self.error, "allowed_tools": self.allowed_tools, "model": self.model,
            "result_session_uid": self.result_session_uid, "created_at": self.created_at,
            "finished_at": self.finished_at, "event_count": len(self.events), **self.plan.describe(),
        }


class RunManager:
    def __init__(
        self,
        command_factory: Callable[[], list[str] | None],
        projects_dir: Path,
        workspace_dir: Path,
        on_fork: Callable[[int, str], None] | None = None,
    ):
        self.command_factory = command_factory
        self.projects_dir = Path(projects_dir)
        self.workspace_dir = Path(workspace_dir)
        self.on_fork = on_fork
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def find_local_file(self, session_uid: str) -> Path | None:
        if not self.projects_dir.is_dir():
            return None
        for path in self.projects_dir.glob(f"*/{session_uid}.jsonl"):
            if path.parent.name != IMPORT_FOLDER:
                return path
        return None

    def plan(self, session: dict) -> Plan:
        local = self.find_local_file(session["session_uid"])
        project = Path(session["project_path"]) if session.get("project_path") else None
        if project is not None and project.is_dir():
            cwd, chat_only = project, False
        else:
            cwd, chat_only = self.workspace_dir / session["session_uid"], True
        return Plan(cwd=cwd, fork=local is None, chat_only=chat_only, local_file=local)

    def start(
        self,
        session: dict,
        prompt: str,
        tool_groups: list[str],
        model: str | None,
        source_lines: Callable[[], list[str]],
    ) -> Run:
        command = self.command_factory()
        if not command:
            raise RunnerError("서버 PC에서 claude 실행 파일을 찾을 수 없습니다 (환경 변수 LSDB_CLAUDE_BIN으로 지정 가능)", 503)
        unknown = [g for g in tool_groups if g not in TOOL_GROUPS]
        if unknown:
            raise RunnerError(f"알 수 없는 도구 묶음입니다: {', '.join(unknown)}")
        if model is not None and model not in MODELS:
            raise RunnerError(f"지원하지 않는 모델입니다: {model}")

        plan = self.plan(session)
        lines = source_lines() if plan.fork else None
        if plan.fork and not lines:
            raise RunnerError("서버 PC에 세션 파일이 없고 수집된 원본도 없어 이어갈 수 없습니다")

        with self._lock:
            self._prune()
            if any(r.session_id == session["id"] and not r.done for r in self._runs.values()):
                raise RunnerError("이 세션은 이미 실행 중입니다", 409)
            if sum(not r.done for r in self._runs.values()) >= MAX_ACTIVE_RUNS:
                raise RunnerError("동시에 실행할 수 있는 수를 넘었습니다. 진행 중인 실행이 끝난 뒤 다시 시도하세요", 429)
            allowed = [] if plan.chat_only else [tool for g in tool_groups for tool in TOOL_GROUPS[g]]
            run = Run(
                id=uuid.uuid4().hex, session_id=session["id"], session_uid=session["session_uid"],
                prompt=prompt, plan=plan, allowed_tools=allowed, model=model,
            )
            self._runs[run.id] = run

        copy_path = None
        try:
            plan.cwd.mkdir(parents=True, exist_ok=True)
            if plan.fork:
                copy_path = self._write_import_copy(session["session_uid"], lines)
            run.process = subprocess.Popen(
                [*command, *build_args(run)],
                cwd=plan.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=child_env(), creationflags=NO_WINDOW,
            )
        except OSError as e:
            _remove(copy_path)
            run.finish("failed", f"CLI를 실행하지 못했습니다: {e}")
            return run
        run.status = "running"
        threading.Thread(target=self._pump, args=(run, copy_path), daemon=True).start()
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def latest_for(self, session_id: int) -> Run | None:
        runs = [r for r in self._runs.values() if r.session_id == session_id]
        return max(runs, key=lambda r: r.created_at) if runs else None

    def cancel(self, run_id: str) -> Run | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        if not run.done and run.process is not None:
            run.cancel_requested = True
            _kill_tree(run.process)
        return run

    def _write_import_copy(self, session_uid: str, lines: list[str]) -> Path:
        target = self.projects_dir / IMPORT_FOLDER / f"{session_uid}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return target

    def _pump(self, run: Run, copy_path: Path | None) -> None:
        process = run.process
        stderr_parts: list[str] = []
        stderr_thread = threading.Thread(target=lambda: stderr_parts.append(process.stderr.read()), daemon=True)
        stderr_thread.start()
        try:
            process.stdin.write(run.prompt)  # 긴 프롬프트·한국어를 명령줄 인자 대신 표준 입력으로 넘긴다
            process.stdin.close()
        except OSError:
            pass

        result_error = None
        saw_result = False
        for line in process.stdout:
            event = translate(line)
            if event is None:
                continue
            if event["type"] == "init":
                new_uid = event.get("session_id")
                if run.plan.fork and new_uid and new_uid != run.session_uid:
                    run.result_session_uid = new_uid
                    if self.on_fork is not None:
                        try:
                            self.on_fork(run.session_id, new_uid)
                        except Exception:  # 링크 기록 실패가 실행을 멈추게 하지 않는다
                            pass
            elif event["type"] == "result":
                saw_result = True
                if event.get("is_error"):
                    result_error = event.get("result") or "CLI가 오류를 반환했습니다"
            run.append(event)

        code = process.wait()
        stderr_thread.join(timeout=5)
        _remove(copy_path)
        stderr = "".join(stderr_parts).strip()[-STDERR_TAIL_CHARS:]
        if run.cancel_requested:
            run.finish("cancelled")
        elif code == 0 and saw_result and result_error is None:
            run.finish("completed")
        else:
            run.finish("failed", result_error or stderr or f"CLI가 종료 코드 {code}로 끝났습니다")

    def _prune(self) -> None:
        cutoff = time.time() - RUN_RETENTION_SECONDS
        for run_id in [i for i, r in self._runs.items() if r.done and (r.finished_at or 0) < cutoff]:
            del self._runs[run_id]


def build_args(run: Run) -> list[str]:
    args = [
        "-p", "--resume", run.session_uid,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--permission-mode", "default",
    ]
    if run.plan.fork:
        args.append("--fork-session")
    if run.plan.chat_only:
        args += ["--tools", ""]
    elif run.allowed_tools:
        args += ["--allowedTools", ",".join(run.allowed_tools)]
    if run.model:
        args += ["--model", run.model]
    return args


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in INHERITED_SESSION_ENV}


def translate(line: str) -> dict | None:
    """stream-json 한 줄을 웹 UI가 쓰는 이벤트로 바꾼다. 필요 없는 이벤트는 None."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None

    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return {
            "type": "init", "session_id": event.get("session_id"), "cwd": event.get("cwd"),
            "model": event.get("model"), "permission_mode": event.get("permissionMode"),
        }
    if kind == "stream_event":
        inner = event.get("event") or {}
        inner_type = inner.get("type")
        if inner_type == "message_start":
            return {"type": "message_start"}
        if inner_type == "content_block_start":
            block_kind = (inner.get("content_block") or {}).get("type")
            if block_kind in ("text", "thinking"):
                return {"type": "block_start", "index": inner.get("index"), "kind": block_kind}
        if inner_type == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                return {"type": "delta", "index": inner.get("index"), "text": delta.get("text", "")}
            if delta.get("type") == "thinking_delta":
                return {"type": "delta", "index": inner.get("index"), "text": delta.get("thinking", "")}
        return None
    if kind in ("assistant", "user"):
        if event.get("parent_tool_use_id"):
            return None  # 서브에이전트 내부 대화는 실시간 화면에서 생략(수집 후 기록에서 볼 수 있음)
        return {"type": kind, "blocks": claude_parser.view_blocks(line)}
    if kind == "result":
        denials = [
            {"tool_name": d.get("tool_name"), "tool_input": claude_parser.truncate_json(d.get("tool_input"))}
            for d in event.get("permission_denials") or [] if isinstance(d, dict)
        ]
        return {
            "type": "result", "is_error": bool(event.get("is_error")), "result": event.get("result"),
            "session_id": event.get("session_id"), "total_cost_usd": event.get("total_cost_usd"),
            "duration_ms": event.get("duration_ms"), "permission_denials": denials,
        }
    return None


def _kill_tree(process: subprocess.Popen) -> None:
    """도구가 띄운 하위 프로세스까지 함께 종료한다."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        process.kill()


def _remove(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
