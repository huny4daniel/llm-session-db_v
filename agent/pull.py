"""서버에 수집된 세션을 이 PC로 가져와 Claude Code로 이어가기.

CLAUDE.md '검증된 CLI 동작'에 기댄다.
- CLI는 `--resume <ID>`로 모든 프로젝트 폴더에서 세션을 찾으므로 사본은 어느 폴더에 두어도 된다.
- `--fork-session`은 현재 폴더 기준으로 새 세션 파일을 만들고 원본(사본)은 건드리지 않는다.
따라서 사본은 에이전트가 수집하지 않는 가져오기 폴더에 두고 새 세션으로 분기하면, 새 기록만 수집된다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from .claude_cli import child_env, claude_command
from .client import ServerError
from .collector import IMPORT_FOLDER


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
        data = client.session_source(ref)
    except (OSError, ServerError) as e:
        out(f"세션을 가져오지 못했습니다: {e}")
        return 1

    session = data["session"]
    uid = session["session_uid"]
    target_dir = choose_workdir(workdir, session.get("project_path"))
    if not target_dir.is_dir():
        out(f"작업 폴더가 없습니다: {target_dir}")
        return 1
    out(f"세션: {session['title'][:80]}  ({uid[:8]}, {session['machine_name']})")

    original = find_original(claude_root, uid)
    copy_path = None
    if original is not None:
        out(f"이 PC에 원본 세션 파일이 있어 그대로 이어갑니다: {original}")
    elif not data["lines"]:
        out("서버에 수집된 원본이 없어 가져올 수 없습니다.")
        return 1
    else:
        copy_path = write_import_copy(claude_root, uid, data["lines"])
        out(f"가져온 사본: {copy_path}  (수집하지 않는 폴더)")

    project = session.get("project_path")
    if project and Path(project) != target_dir:
        out(f"원래 프로젝트 폴더({project})가 아닌 {target_dir}에서 이어갑니다. 대화 기억은 이어지지만 파일은 이 폴더 기준입니다.")

    args = resume_args(uid, fork=original is None)
    if not run:
        suffix = " (새 세션으로 기록되어 이 PC 에이전트가 수집합니다)" if original is None else ""
        out(f"다음 명령으로 이어가세요{suffix}:")
        out(f'  cd "{target_dir}"; claude {" ".join(args)}')
        return 0

    out(f"{target_dir}에서 claude를 실행합니다…")
    try:
        return subprocess.call([*command, *args], cwd=target_dir, env=child_env())
    finally:
        if copy_path is not None:
            copy_path.unlink(missing_ok=True)  # 새 세션이 만들어졌으므로 사본은 더 필요 없다
