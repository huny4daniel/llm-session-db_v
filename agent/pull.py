"""서버에 수집된 세션을 이 PC로 가져와 Claude Code로 이어가기.

CLAUDE.md '검증된 CLI 동작'에 기댄다.
- CLI는 `--resume <ID>`로 모든 프로젝트 폴더에서 세션을 찾으므로 사본은 어느 폴더에 두어도 된다.
- `--fork-session`은 현재 폴더 기준으로 새 세션 파일을 만들고 원본(사본)은 건드리지 않는다.
따라서 사본은 에이전트가 수집하지 않는 가져오기 폴더에 두고 새 세션으로 분기하면, 새 기록만 수집된다.

CLI(`pull_session`)와 GUI 가져오기 탭이 같은 단계를 쓴다: 계획(`plan_pull`) → 사본 준비(`prepare_copy`) → 실행(`launch`).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .claude_cli import child_env, claude_command
from .client import ServerError
from .collector import IMPORT_FOLDER


class PullError(Exception):
    pass


@dataclass
class PullPlan:
    session: dict
    workdir: Path
    original: Path | None  # 이 PC에 있는 원본 세션 파일(있으면 사본 없이 그대로 이어간다)
    lines: list[str] = field(default_factory=list)
    copy_path: Path | None = None

    @property
    def uid(self) -> str:
        return self.session["session_uid"]

    @property
    def fork(self) -> bool:
        return self.original is None

    def args(self) -> list[str]:
        return resume_args(self.uid, fork=self.fork)

    def command_text(self) -> str:
        return f'cd "{self.workdir}"; claude {" ".join(self.args())}'

    def notes(self) -> list[str]:
        """사용자에게 보여줄 안내(원본 사용 여부·작업 폴더 차이)."""
        notes = [f"세션: {self.session['title'][:80]}  ({self.uid[:8]}, {self.session['machine_name']})"]
        if self.original is not None:
            notes.append(f"이 PC에 원본 세션 파일이 있어 그대로 이어갑니다: {self.original}")
        project = self.session.get("project_path")
        if project and Path(project) != self.workdir:
            notes.append(
                f"원래 프로젝트 폴더({project})가 아닌 {self.workdir}에서 이어갑니다. 대화 기억은 이어지지만 파일은 이 폴더 기준입니다."
            )
        return notes


def find_original(claude_root: Path, session_uid: str) -> Path | None:
    if not claude_root.is_dir():
        return None
    for path in claude_root.glob(f"*/{session_uid}.jsonl"):
        if path.parent.name != IMPORT_FOLDER:
            return path
    return None


def choose_workdir(explicit: str | None, project_path: str | None) -> Path:
    """--dir > 이 PC에 있는 원래 프로젝트 폴더 > 현재 폴더."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if project_path and Path(project_path).is_dir():
        return Path(project_path)
    return Path.cwd()


def write_import_copy(claude_root: Path, session_uid: str, lines: list[str]) -> Path:
    target = claude_root / IMPORT_FOLDER / f"{session_uid}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return target


def resume_args(session_uid: str, fork: bool) -> list[str]:
    return ["--resume", session_uid, *(["--fork-session"] if fork else [])]


def plan_pull(client, claude_root: Path, ref: str, workdir: str | None = None) -> PullPlan:
    """서버에서 세션을 받아 어디서 어떻게 이어갈지 정한다. 파일은 아직 만들지 않는다."""
    try:
        data = client.session_source(ref)
    except (OSError, ServerError) as e:
        raise PullError(f"세션을 가져오지 못했습니다: {e}") from e
    session = data["session"]
    target_dir = choose_workdir(workdir, session.get("project_path"))
    if not target_dir.is_dir():
        raise PullError(f"작업 폴더가 없습니다: {target_dir}")
    original = find_original(claude_root, session["session_uid"])
    if original is None and not data["lines"]:
        raise PullError("서버에 수집된 원본이 없어 가져올 수 없습니다.")
    return PullPlan(session=session, workdir=target_dir, original=original, lines=data["lines"])


def prepare_copy(plan: PullPlan, claude_root: Path) -> Path | None:
    """원본이 없으면 가져오기 폴더에 사본을 만든다(수집하지 않는 폴더)."""
    if plan.original is not None:
        return None
    plan.copy_path = write_import_copy(claude_root, plan.uid, plan.lines)
    return plan.copy_path


def cleanup_copy(plan: PullPlan) -> None:
    """새 세션이 만들어졌으므로 사본은 더 필요 없다."""
    if plan.copy_path is not None:
        plan.copy_path.unlink(missing_ok=True)
        plan.copy_path = None


def launch(plan: PullPlan, command: list[str], *, new_console: bool = False) -> subprocess.Popen:
    """작업 폴더에서 claude를 실행한다. GUI에서는 새 콘솔 창을 띄운다(창 없는 프로세스에는 터미널이 없으므로)."""
    flags = subprocess.CREATE_NEW_CONSOLE if new_console and sys.platform == "win32" else 0
    return subprocess.Popen([*command, *plan.args()], cwd=plan.workdir, env=child_env(), creationflags=flags)


def pull_session(
    client,
    claude_root: Path,
    ref: str,
    *,
    workdir: str | None = None,
    run: bool = False,
    command_factory: Callable[[], list[str] | None] = claude_command,
    out: Callable[[str], None] = print,
) -> int:
    command = command_factory() if run else None
    if run and not command:
        out("claude 실행 파일을 찾을 수 없습니다 (환경 변수 LSDB_CLAUDE_BIN으로 지정 가능).")
        return 1
    try:
        plan = plan_pull(client, claude_root, ref, workdir)
    except PullError as e:
        out(str(e))
        return 1

    notes = plan.notes()
    out(notes[0])
    copy_path = prepare_copy(plan, claude_root)
    if copy_path is not None:
        out(f"가져온 사본: {copy_path}  (수집하지 않는 폴더)")
    for note in notes[1:]:
        out(note)

    if not run:
        suffix = " (새 세션으로 기록되어 이 PC 에이전트가 수집합니다)" if plan.fork else ""
        out(f"다음 명령으로 이어가세요{suffix}:")
        out(f"  {plan.command_text()}")
        return 0

    out(f"{plan.workdir}에서 claude를 실행합니다…")
    try:
        return launch(plan, command).wait()
    finally:
        cleanup_copy(plan)
