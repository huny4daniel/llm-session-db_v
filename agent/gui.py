"""tkinter GUI. 에이전트 설정·상태·가져오기를 한 창에서 다루고, 서버 모듈을 불러올 수 있는 PC(저장소+venv)에서는 서버 탭도 붙인다.

표준 라이브러리만 쓰므로 exe로 묶어도 추가 의존성이 없다.
서버 요청처럼 오래 걸리는 작업은 스레드에서 돌리고 결과만 `after`로 창에 반영한다(tkinter는 메인 스레드에서만 만져야 한다).
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from . import config, service
from .claude_cli import claude_command
from .client import Client
from .pull import PullError, PullPlan, cleanup_copy, launch, plan_pull, prepare_copy
from .status import collect_status
from .sync import sync_once

PAD = {"padx": 6, "pady": 3}


# 스레드에서 끝난 작업의 (콜백, 결과). 메인 루프가 주기적으로 꺼내 실행한다(다른 스레드에서 tkinter를 직접 만지지 않는다).
_finished: queue.Queue = queue.Queue()


def run_async(work: Callable[[], object], on_done: Callable, on_error: Callable[[Exception], None]) -> None:
    """작업을 스레드에서 실행하고 결과를 메인 루프의 콜백으로 넘긴다."""

    def runner():
        try:
            result = work()
        except Exception as e:  # noqa: BLE001 — 어떤 오류든 창에 보여준다
            _finished.put((on_error, e))
            return
        _finished.put((on_done, result))

    threading.Thread(target=runner, daemon=True).start()


def dispatch_finished() -> int:
    """끝난 작업의 콜백을 메인 스레드에서 실행한다. 실행한 개수를 돌려준다."""
    count = 0
    while True:
        try:
            callback, value = _finished.get_nowait()
        except queue.Empty:
            return count
        try:
            callback(value)
        except tk.TclError:
            pass  # 결과가 오기 전에 위젯이 사라짐
        count += 1


def start_dispatcher(root: tk.Misc, interval_ms: int = 50) -> None:
    def poll():
        dispatch_finished()
        root.after(interval_ms, poll)

    poll()


def open_path(path: Path) -> None:
    """파일·폴더를 탐색기 등 기본 프로그램으로 연다."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Message(ttk.Label):
    """탭 아래쪽의 결과·오류 안내 줄."""

    def __init__(self, master):
        super().__init__(master, anchor="w", wraplength=680, justify="left")

    def info(self, text: str) -> None:
        self.configure(text=text, foreground="")

    def error(self, text: str) -> None:
        self.configure(text=text, foreground="#b00020")


class SettingsTab(ttk.Frame):
    def __init__(self, master, app: "App"):
        super().__init__(master, padding=10)
        self.app = app
        self.server_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.root_var = tk.StringVar()
        self.show_token = tk.BooleanVar(value=False)

        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="서버 주소").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Entry(self, textvariable=self.server_var).grid(row=0, column=1, columnspan=2, sticky="ew", **PAD)
        ttk.Label(self, text="예: http://127.0.0.1:8765 — 서버 PC의 GUI 서버 탭이나 add-machine 명령으로 발급한 토큰을 씁니다",
                  foreground="gray").grid(row=1, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(self, text="PC 토큰").grid(row=2, column=0, sticky="w", **PAD)
        self.token_entry = ttk.Entry(self, textvariable=self.token_var, show="•")
        self.token_entry.grid(row=2, column=1, sticky="ew", **PAD)
        ttk.Checkbutton(self, text="표시", variable=self.show_token, command=self._toggle_token).grid(row=2, column=2, **PAD)

        ttk.Label(self, text="세션 폴더").grid(row=3, column=0, sticky="w", **PAD)
        ttk.Entry(self, textvariable=self.root_var).grid(row=3, column=1, sticky="ew", **PAD)
        ttk.Button(self, text="찾아보기…", command=self._browse).grid(row=3, column=2, **PAD)

        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Button(buttons, text="저장", command=self.save).pack(side="left", padx=(6, 4))
        ttk.Button(buttons, text="연결 확인", command=self.check).pack(side="left", padx=4)

        self.message = Message(self)
        self.message.grid(row=5, column=0, columnspan=3, sticky="ew", **PAD)
        ttk.Label(self, text=f"설정 파일: {config.config_path()}", foreground="gray").grid(
            row=6, column=0, columnspan=3, sticky="w", padx=6, pady=(12, 0))
        self.load()

    def _toggle_token(self) -> None:
        self.token_entry.configure(show="" if self.show_token.get() else "•")

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.root_var.get() or str(Path.home()), title="Claude Code 세션 폴더")
        if chosen:
            self.root_var.set(str(Path(chosen)))

    def load(self) -> None:
        cfg = self.app.cfg
        self.server_var.set(cfg.server_url if cfg else "http://127.0.0.1:8765")
        self.token_var.set(cfg.token if cfg else "")
        self.root_var.set(cfg.claude_root if cfg else str(config.default_claude_root()))

    def _build_config(self) -> config.AgentConfig | None:
        server = self.server_var.get().strip()
        token = self.token_var.get().strip()
        if not server.startswith(("http://", "https://")):
            self.message.error("서버 주소는 http:// 또는 https://로 시작해야 합니다.")
            return None
        if not token:
            self.message.error("PC 토큰을 입력하세요.")
            return None
        root = self.root_var.get().strip() or str(config.default_claude_root())
        return config.AgentConfig(server_url=server, token=token, claude_root=root)

    def save(self) -> None:
        cfg = self._build_config()
        if cfg is None:
            return
        path = config.save(cfg)
        self.app.set_config(cfg)
        self.message.info(f"저장했습니다: {path}")

    def check(self) -> None:
        cfg = self._build_config()
        if cfg is None:
            return
        self.message.info("서버에 연결하는 중…")
        client = Client(cfg.server_url, cfg.token, timeout=10)
        run_async(lambda: client.remote_files("claude"),
            lambda files: self.message.info(f"연결 정상 — 서버에 이 PC의 파일 {len(files)}개가 기록되어 있습니다."),
            lambda e: self.message.error(f"연결 실패: {e}"),
        )


class StatusTab(ttk.Frame):
    def __init__(self, master, app: "App"):
        super().__init__(master, padding=10)
        self.app = app
        self.interval_var = tk.StringVar(value=str(int(service.DEFAULT_INTERVAL)))

        self.text = tk.Text(self, height=14, wrap="word", state="disabled", font=("Consolas", 10))
        self.text.pack(fill="both", expand=True)

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(8, 2))
        ttk.Button(row, text="새로고침", command=self.refresh).pack(side="left", padx=(0, 4))
        ttk.Button(row, text="지금 한 번 수집", command=self.sync_now).pack(side="left", padx=4)
        ttk.Button(row, text="로그 열기", command=lambda: self._open(config.log_path())).pack(side="left", padx=4)
        ttk.Button(row, text="설정 폴더 열기", command=lambda: self._open(config.state_dir())).pack(side="left", padx=4)

        box = ttk.LabelFrame(self, text="백그라운드 수집", padding=6)
        box.pack(fill="x", pady=(8, 2))
        ttk.Label(box, text="수집 간격(초)").pack(side="left")
        ttk.Spinbox(box, from_=5, to=3600, width=6, textvariable=self.interval_var).pack(side="left", padx=(4, 12))
        ttk.Button(box, text="자동 실행 등록", command=self.install).pack(side="left", padx=4)
        ttk.Button(box, text="등록 해제", command=self.uninstall).pack(side="left", padx=4)
        ttk.Button(box, text="지금 시작", command=self.start).pack(side="left", padx=(12, 4))
        ttk.Button(box, text="중지", command=self.stop).pack(side="left", padx=4)

        self.message = Message(self)
        self.message.pack(fill="x", pady=(6, 0))

    def _open(self, path: Path) -> None:
        if not path.exists():
            self.message.error(f"아직 없습니다: {path}")
            return
        open_path(path)

    def _interval(self) -> float | None:
        try:
            value = float(self.interval_var.get())
            if value < 1:
                raise ValueError
            return value
        except ValueError:
            self.message.error("수집 간격은 1 이상의 숫자여야 합니다.")
            return None

    def _show(self, lines: list[str]) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "\n".join(lines))
        self.text.configure(state="disabled")

    def refresh(self) -> None:
        cfg = self.app.cfg
        if cfg is None:
            self._show(["설정이 없습니다. 설정 탭에서 서버 주소와 토큰을 저장하세요."])
            return
        self._show(["상태를 확인하는 중…"])
        run_async(lambda: collect_status(cfg),
            lambda report: self._show(report.lines()),
            lambda e: self._show([f"상태 확인 실패: {e}"]),
        )

    def sync_now(self) -> None:
        cfg = self.app.cfg
        if cfg is None:
            self.message.error("먼저 설정을 저장하세요.")
            return
        self.message.info("수집하는 중…")

        def done(result):
            self.message.info(f"파일 {result.files}개, 줄 {result.lines}개를 전송했습니다.")
            self.refresh()

        run_async(lambda: sync_once(cfg), done, lambda e: self.message.error(f"수집 실패: {e}"))

    def _guard(self, action: Callable[[], str]) -> None:
        try:
            self.message.info(action())
        except (RuntimeError, OSError) as e:
            self.message.error(str(e))
        self.refresh()

    def install(self) -> None:
        if self.app.cfg is None:
            self.message.error("먼저 설정을 저장하세요.")
            return
        interval = self._interval()
        if interval is None:
            return

        def action():
            command = service.register_autostart(interval)
            started = service.start_background(interval)
            return f"로그인 시 자동 실행을 등록했습니다{'. 지금 시작했습니다' if started else ' (이미 실행 중)'}.\n{command}"

        self._guard(action)

    def uninstall(self) -> None:
        self._guard(lambda: "자동 실행 등록을 해제했습니다. 실행 중이면 중지 버튼으로 종료하세요."
                    if service.unregister_autostart() else "등록된 자동 실행이 없습니다.")

    def start(self) -> None:
        if self.app.cfg is None:
            self.message.error("먼저 설정을 저장하세요.")
            return
        interval = self._interval()
        if interval is None:
            return
        self._guard(lambda: f"백그라운드 수집을 시작했습니다. 로그: {config.log_path()}"
                    if service.start_background(interval) else "이미 실행 중입니다.")

    def stop(self) -> None:
        def action():
            if not service.is_running():
                return "실행 중인 에이전트가 없습니다."
            if service.stop_background():
                return "에이전트를 종료했습니다."
            raise RuntimeError("종료하지 못했습니다. 작업 관리자에서 직접 종료하세요.")

        self._guard(action)


class PullTab(ttk.Frame):
    COLUMNS = (("id", "번호", 50), ("title", "제목", 300), ("machine", "PC", 90), ("project", "프로젝트", 200), ("at", "마지막 활동", 130))

    def __init__(self, master, app: "App"):
        super().__init__(master, padding=10)
        self.app = app
        self.sessions: dict[str, dict] = {}
        self.query_var = tk.StringVar()
        self.workdir_var = tk.StringVar()

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="검색").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.query_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self.load())
        ttk.Button(top, text="목록 불러오기", command=self.load).pack(side="left")

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True, pady=6)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings", selectmode="browse")
        for key, heading, width in self.COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, stretch=key == "title", anchor="w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        dir_row = ttk.Frame(self)
        dir_row.pack(fill="x")
        ttk.Label(dir_row, text="작업 폴더").pack(side="left")
        ttk.Entry(dir_row, textvariable=self.workdir_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(dir_row, text="찾아보기…", command=self._browse).pack(side="left")
        ttk.Label(self, text="비워 두면 원래 프로젝트 폴더가 이 PC에 있을 때 그곳, 없으면 현재 폴더에서 이어갑니다. 대화 기억만 옮겨지고 코드 파일은 옮겨지지 않습니다.",
                  foreground="gray", wraplength=680, justify="left").pack(fill="x", pady=(2, 6))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="명령 복사", command=self.copy_command).pack(side="left", padx=(0, 4))
        ttk.Button(buttons, text="claude 실행", command=self.run).pack(side="left", padx=4)

        self.message = Message(self)
        self.message.pack(fill="x", pady=(6, 0))

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.workdir_var.get() or os.getcwd(), title="이어갈 작업 폴더")
        if chosen:
            self.workdir_var.set(str(Path(chosen)))

    def _on_select(self, _event=None) -> None:
        session = self._selected()
        if session is None:
            return
        project = session.get("project_path")
        self.message.info(f"세션 ID {session['session_uid']}" + (f"  ·  원래 폴더 {project}" if project else ""))
        if project and Path(project).is_dir():
            self.workdir_var.set(project)

    def _selected(self) -> dict | None:
        chosen = self.tree.selection()
        return self.sessions.get(chosen[0]) if chosen else None

    def load(self) -> None:
        client = self.app.client()
        if client is None:
            self.message.error("먼저 설정을 저장하세요.")
            return
        self.message.info("세션 목록을 불러오는 중…")
        query = self.query_var.get().strip()
        run_async(lambda: client.list_sessions(query, limit=100), self._fill, lambda e: self.message.error(f"불러오기 실패: {e}"))

    def _fill(self, items: list[dict]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.sessions = {}
        for item in items:
            iid = str(item["id"])
            self.sessions[iid] = item
            self.tree.insert("", "end", iid=iid, values=(
                item["id"], (item.get("title") or "")[:120], item.get("machine_name") or "",
                item.get("project_path") or "", (item.get("last_activity_at") or "")[:16].replace("T", " "),
            ))
        self.message.info(f"세션 {len(items)}개 (최근 활동 순, 최대 100개)")

    def _plan(self, on_done: Callable[[PullPlan], None]) -> None:
        session = self._selected()
        client = self.app.client()
        if session is None or client is None:
            self.message.error("목록에서 세션을 고르세요." if client else "먼저 설정을 저장하세요.")
            return
        root = Path(self.app.cfg.claude_root)
        workdir = self.workdir_var.get().strip() or None
        run_async(lambda: plan_pull(client, root, session["session_uid"], workdir), on_done,
            lambda e: self.message.error(str(e) if isinstance(e, PullError) else f"가져오기 실패: {e}"),
        )

    def copy_command(self) -> None:
        def done(plan: PullPlan):
            copy = prepare_copy(plan, Path(self.app.cfg.claude_root))
            self.clipboard_clear()
            self.clipboard_append(plan.command_text())
            notes = plan.notes()
            if copy is not None:
                notes.append(f"가져온 사본: {copy}  (수집하지 않는 폴더)")
            notes.append("명령을 복사했습니다. 터미널에 붙여 넣어 실행하세요:")
            notes.append(plan.command_text())
            self.message.info("\n".join(notes))

        self._plan(done)

    def run(self) -> None:
        command = claude_command()
        if not command:
            self.message.error("claude 실행 파일을 찾을 수 없습니다 (환경 변수 LSDB_CLAUDE_BIN으로 지정 가능).")
            return

        def done(plan: PullPlan):
            prepare_copy(plan, Path(self.app.cfg.claude_root))
            try:
                process = launch(plan, command, new_console=True)
            except OSError as e:
                cleanup_copy(plan)
                self.message.error(f"claude를 실행하지 못했습니다: {e}")
                return
            self.message.info("\n".join([*plan.notes(), f"새 창에서 claude를 실행했습니다: {plan.workdir}"]))

            def wait():
                try:
                    return process.wait()
                finally:
                    cleanup_copy(plan)

            run_async(wait, lambda code: self.message.info(f"claude가 종료되었습니다 (코드 {code}). 새 기록은 에이전트가 수집합니다."),
                      lambda e: self.message.error(str(e)))

        self._plan(done)


class App(ttk.Frame):
    """창 본체. 테스트에서는 숨긴 루트 창에 붙여 만든다."""

    def __init__(self, master: tk.Misc):
        super().__init__(master)
        try:
            self.cfg: config.AgentConfig | None = config.load()
        except (FileNotFoundError, ValueError, KeyError):
            self.cfg = None

        self.notebook = ttk.Notebook(self)
        self.settings = SettingsTab(self.notebook, self)
        self.status = StatusTab(self.notebook, self)
        self.pull = PullTab(self.notebook, self)
        self.notebook.add(self.settings, text="설정")
        self.notebook.add(self.status, text="상태")
        self.notebook.add(self.pull, text="가져와 이어가기")
        server_panel = _server_panel(self.notebook, self.reload_config)
        if server_panel is not None:
            self.notebook.add(server_panel, text="서버")
        self.notebook.pack(fill="both", expand=True)
        self.pack(fill="both", expand=True)

        if self.cfg is None:
            self.notebook.select(self.settings)
        else:
            self.notebook.select(self.status)
            self.status.refresh()

    def set_config(self, cfg: config.AgentConfig) -> None:
        self.cfg = cfg
        self.status.refresh()

    def reload_config(self) -> None:
        """서버 탭이 이 PC의 에이전트 설정(Local)을 새로 썼을 때 설정 탭·상태 탭을 갱신한다."""
        try:
            self.cfg = config.load()
        except (FileNotFoundError, ValueError, KeyError):
            return
        self.settings.load()
        self.status.refresh()

    def client(self) -> Client | None:
        return Client(self.cfg.server_url, self.cfg.token, timeout=30) if self.cfg else None


def _server_panel(master, on_local_agent) -> ttk.Frame | None:
    """서버 패키지가 있는 PC(저장소·서버 exe)에서만 서버 탭을 만든다. 에이전트 exe에는 서버가 들어 있지 않다."""
    try:
        from server.gui import ServerPanel
    except ImportError:
        return None
    return ServerPanel(master, on_local_agent=on_local_agent)


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"GUI를 열 수 없습니다: {e}", file=sys.stderr)
        return 1
    root.title("LLM 세션 에이전트")
    root.geometry("780x620")
    root.minsize(640, 500)
    App(root)
    start_dispatcher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
