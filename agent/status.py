"""설정·자동 실행·서버 연결·미전송 파일 상태. CLI `status`와 GUI 상태 탭이 함께 쓴다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config, service
from .client import Client, ServerError
from .collector import LocalFile, discover_claude


@dataclass
class StatusReport:
    server_url: str
    claude_root: str
    autostart_command: str | None
    running: bool
    local_files: int = 0
    synced_files: int = 0
    waiting: list[tuple[LocalFile, int]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def lines(self) -> list[str]:
        lines = [
            f"설정 파일  {config.config_path()}",
            f"서버       {self.server_url}",
            f"세션 폴더  {self.claude_root}",
            f"로그       {config.log_path()}",
            f"자동 실행  {self.autostart_command or '등록 안 됨'}",
            f"에이전트   {'실행 중' if self.running else '실행 중 아님'}",
        ]
        if self.error:
            lines.append(f"서버 연결  실패: {self.error}")
            return lines
        lines.append(
            f"서버 연결  정상 — 로컬 세션 파일 {self.local_files}개 중 {self.synced_files}개 반영, {len(self.waiting)}개 대기"
        )
        for local, diff in self.waiting[:10]:
            note = f"{diff:,}바이트 미전송" if diff > 0 else "파일이 줄어듦(다시 전송 예정)"
            lines.append(f"  - {local.file_key}  {note}")
        if len(self.waiting) > 10:
            lines.append(f"  … 외 {len(self.waiting) - 10}개")
        return lines


def collect_status(cfg: config.AgentConfig, client: Client | None = None) -> StatusReport:
    report = StatusReport(
        server_url=cfg.server_url,
        claude_root=cfg.claude_root,
        autostart_command=service.registered_command(),
        running=service.is_running(),
    )
    files = discover_claude(Path(cfg.claude_root))
    report.local_files = len(files)
    try:
        remote = (client or Client(cfg.server_url, cfg.token, timeout=10)).remote_files("claude")
    except (OSError, ServerError) as e:
        report.error = str(e)
        return report
    waiting = [(f, f.size - remote.get(f.file_key, {}).get("next_offset", 0)) for f in files]
    report.waiting = [(f, diff) for f, diff in waiting if diff != 0]
    report.synced_files = len(files) - len(report.waiting)
    return report
