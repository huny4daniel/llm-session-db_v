"""세션 파일 탐색과 증분 읽기. 로컬 파일은 읽기만 한다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

MAX_BATCH_BYTES = 1_000_000


@dataclass(frozen=True)
class LocalFile:
    file_key: str
    path: Path
    size: int
    mtime: float


def discover_claude(root: Path) -> list[LocalFile]:
    """메인 세션 파일과 서브에이전트 파일을 file_key 순으로 반환한다(메인 파일이 먼저 온다)."""
    if not root.is_dir():
        return []
    files = []
    for path in [*root.glob("*/*.jsonl"), *root.glob("*/*/subagents/agent-*.jsonl")]:
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(LocalFile(path.relative_to(root).as_posix(), path, stat.st_size, stat.st_mtime))
    return sorted(files, key=lambda f: f.file_key)


def iter_batches(path: Path, start: int, max_bytes: int = MAX_BATCH_BYTES) -> Iterator[tuple[int, list[dict]]]:
    """start부터 개행으로 끝난 줄만 묶어서 (묶음 시작 위치, 줄 목록)으로 내보낸다.

    기록 중인 파일의 마지막 줄은 아직 덜 써졌을 수 있으므로 개행이 없으면 다음 수집으로 미룬다.
    """
    with open(path, "rb") as f:
        f.seek(start)
        offset = batch_start = start
        batch: list[dict] = []
        batch_bytes = 0
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            if batch and batch_bytes + len(raw) > max_bytes:
                yield batch_start, batch
                batch, batch_bytes, batch_start = [], 0, offset
            batch.append({
                "offset": offset,
                "length": len(raw),
                "text": raw.decode("utf-8", errors="replace").rstrip("\r\n"),
            })
            batch_bytes += len(raw)
            offset += len(raw)
        if batch:
            yield batch_start, batch
