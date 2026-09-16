from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config
from .client import Client, OffsetConflict
from .collector import LocalFile, discover_claude, iter_batches

MAX_CONFLICT_RETRIES = 3


@dataclass
class SyncResult:
    files: int = 0
    lines: int = 0


def sync_once(cfg: config.AgentConfig) -> SyncResult:
    """설정에 따라 한 번 수집한다(CLI run·GUI '지금 한 번 수집')."""
    client = Client(cfg.server_url, cfg.token)
    return sync_files(client, "claude", discover_claude(Path(cfg.claude_root)))


def sync_files(client: Client, source: str, files: list[LocalFile]) -> SyncResult:
    """서버의 파일별 수신 위치를 기준으로 새로 추가된 줄만 보낸다."""
    remote = client.remote_files(source)
    result = SyncResult()
    for local in files:
        state = remote.get(local.file_key)
        sent = _sync_file(client, source, local, state["next_offset"] if state else 0)
        if sent:
            result.files += 1
            result.lines += sent
    return result


def _sync_file(client: Client, source: str, local: LocalFile, offset: int) -> int:
    if local.size == offset:
        return 0
    sent = 0
    for _ in range(MAX_CONFLICT_RETRIES):
        # 파일이 서버 기록보다 작아졌으면 교체된 것으로 보고 처음부터 다시 보낸다.
        reset = local.size < offset
        if reset:
            offset = 0
        try:
            return sent + _send_from(client, source, local, offset, reset)
        except OffsetConflict as conflict:
            offset = conflict.expected_offset
    raise OffsetConflict(offset)


def _send_from(client: Client, source: str, local: LocalFile, offset: int, reset: bool) -> int:
    def payload(start: int, lines: list[dict], reset_flag: bool) -> dict:
        return {
            "source": source,
            "file_key": local.file_key,
            "start_offset": start,
            "size": local.size,
            "mtime": local.mtime,
            "reset": reset_flag,
            "lines": lines,
        }

    sent = 0
    first = True
    for start, lines in iter_batches(local.path, offset):
        client.ingest(payload(start, lines, reset and first))
        first = False
        sent += len(lines)
    if reset and first:
        client.ingest(payload(0, [], True))
    return sent
