"""GUI 서버 탭. 서버 시작·중지·자동 실행, 에이전트 PC 등록·토큰 발급, 웹 비밀번호 설정을 창에서 다룬다.

에이전트 GUI(`agent/gui.py`)가 서버 패키지를 불러올 수 있을 때만 붙인다. DB는 CLI와 같은 모듈로 직접 다룬다.
"""

from __future__ import annotations

import sqlite3
import tkinter as tk
import webbrowser
from contextlib import closing
from tkinter import messagebox, simpledialog, ttk

from agent.gui import PAD, Message, open_path, run_async

from . import auth, config, db, local_agent, machines, service


class ServerPanel(ttk.Frame):
    def __init__(self, master, on_local_agent=None):
        """on_local_agent: 이 PC의 에이전트 설정을 새로 썼을 때 호출(에이전트 탭이 다시 읽도록)."""
        super().__init__(master, padding=10)
        self.on_local_agent = on_local_agent
        self.host_var = tk.StringVar(value=config.DEFAULT_HOST)
        self.port_var = tk.StringVar(value=str(config.DEFAULT_PORT))
        self.machine_name_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.confirm_var = tk.StringVar()
        self._load_registered_bind()

        self._build_server_box()
        self._build_machines_box()
        self._build_password_box()
        self.message = Message(self)
        self.message.pack(fill="x", pady=(6, 0))
        self.refresh()

    # ── 서버 실행 ────────────────────────────────────────────────

    def _load_registered_bind(self) -> None:
        """자동 실행에 등록된 주소·포트가 있으면 그 값을 기본으로 보여준다."""
        command = service.registered_command() or ""
        parts = command.split()
        for flag, var in (("--host", self.host_var), ("--port", self.port_var)):
            if flag in parts and parts.index(flag) + 1 < len(parts):
                var.set(parts[parts.index(flag) + 1].strip("'\""))

    def _build_server_box(self) -> None:
        box = ttk.LabelFrame(self, text="서버 실행", padding=6)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="주소").pack(side="left")
        ttk.Entry(row, textvariable=self.host_var, width=18).pack(side="left", padx=(4, 10))
        ttk.Label(row, text="포트").pack(side="left")
        ttk.Entry(row, textvariable=self.port_var, width=7).pack(side="left", padx=(4, 10))
        self.server_state = ttk.Label(row, text="확인 중…")
        self.server_state.pack(side="left", padx=(6, 0))

        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="상태 확인", command=self.refresh).pack(side="left", padx=(0, 4))
        ttk.Button(buttons, text="웹 열기", command=self.open_web).pack(side="left", padx=4)
        ttk.Button(buttons, text="지금 시작", command=self.start).pack(side="left", padx=(12, 4))
        ttk.Button(buttons, text="중지", command=self.stop).pack(side="left", padx=4)
        ttk.Button(buttons, text="자동 실행 등록", command=self.install).pack(side="left", padx=(12, 4))
        ttk.Button(buttons, text="등록 해제", command=self.uninstall).pack(side="left", padx=4)
        ttk.Button(buttons, text="데이터 폴더", command=lambda: open_path(config.data_dir())).pack(side="left", padx=(12, 4))
        self.autostart_state = ttk.Label(box, text="", foreground="gray", wraplength=680, justify="left")
        self.autostart_state.pack(fill="x", pady=(4, 0))

    def _bind(self) -> tuple[str, int] | None:
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.message.error("포트는 숫자여야 합니다.")
            return None
        if not host:
            self.message.error("주소를 입력하세요.")
            return None
        if host == "0.0.0.0":
            self.message.error("0.0.0.0은 같은 LAN 전체에 열리므로 쓰지 않습니다. Tailscale IP나 127.0.0.1을 쓰세요.")
            return None
        if service.remote_bind_without_password(host):
            self.message.error("루프백이 아닌 주소로 열려면 먼저 아래에서 웹 비밀번호를 설정하세요.")
            return None
        return host, port

    def refresh(self) -> None:
        host, port = self.host_var.get().strip(), self.port_var.get().strip()
        self.autostart_state.configure(text=f"자동 실행: {service.registered_command() or '등록 안 됨'}")
        self.server_state.configure(text="확인 중…")
        try:
            port_number = int(port)
        except ValueError:
            self.server_state.configure(text="포트가 올바르지 않음")
            return
        run_async(lambda: service.is_running(host, port_number),
            lambda running: self.server_state.configure(
                text=f"{'실행 중' if running else '중지됨'}  {service.url(host, port_number)}",
                foreground="#1b7f3b" if running else "#b00020"),
            lambda e: self.server_state.configure(text=f"확인 실패: {e}"),
        )
        self.load_machines()
        self._refresh_password_state()

    def open_web(self) -> None:
        webbrowser.open(service.url(self.host_var.get().strip(), self.port_var.get().strip()))

    def _guard(self, action) -> None:
        try:
            self.message.info(action())
        except (RuntimeError, OSError) as e:
            self.message.error(str(e))
        self.after(1500, self.refresh)  # 시작·중지가 반영될 시간을 준다

    def start(self) -> None:
        bind = self._bind()
        if bind is None:
            return
        self._guard(lambda: f"백그라운드에서 서버를 시작했습니다: {service.url(*bind)}  (로그: {config.data_dir() / 'server.log'})"
                    if service.start_background(*bind) else "이미 실행 중입니다.")

    def stop(self) -> None:
        host, port = self.host_var.get().strip(), int(self.port_var.get() or config.DEFAULT_PORT)

        def action():
            if not service.is_running(host, port):
                return "실행 중인 서버가 없습니다."
            if service.stop_background(host, port):
                return "서버를 종료했습니다."
            raise RuntimeError("종료하지 못했습니다. 이 GUI나 install로 시작한 서버만 중지할 수 있습니다. 작업 관리자에서 직접 종료하세요.")

        self._guard(action)

    def install(self) -> None:
        bind = self._bind()
        if bind is None:
            return

        def action():
            command = service.register_autostart(*bind)
            started = service.start_background(*bind)
            with closing(self._conn()) as conn:
                agent_note = local_agent.install_local_agent(conn, *bind)
            if self.on_local_agent:
                self.on_local_agent()
            return (f"로그인 시 자동 실행을 등록했습니다{'. 지금 시작했습니다' if started else ' (이미 실행 중)'}.\n"
                    f"{command}\n{agent_note}")

        self._guard(action)

    def uninstall(self) -> None:
        self._guard(lambda: "자동 실행 등록을 해제했습니다. 실행 중이면 중지 버튼으로 종료하세요."
                    if service.unregister_autostart() else "등록된 자동 실행이 없습니다.")

    # ── 에이전트 PC ─────────────────────────────────────────────

    def _build_machines_box(self) -> None:
        box = ttk.LabelFrame(self, text="에이전트 PC (이 PC는 Local로 자동 등록, 토큰은 발급할 때 한 번만 표시)", padding=6)
        box.pack(fill="both", expand=True, pady=(8, 0))
        table = ttk.Frame(box)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("id", "name", "sessions", "seen"), show="headings", height=5, selectmode="browse")
        for key, heading, width in (("id", "번호", 50), ("name", "이름", 180), ("sessions", "세션 수", 80), ("seen", "마지막 수신", 160)):
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, stretch=key == "name", anchor="w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="PC 이름").pack(side="left")
        entry = ttk.Entry(row, textvariable=self.machine_name_var, width=20)
        entry.pack(side="left", padx=(4, 6))
        entry.bind("<Return>", lambda _e: self.add_machine())
        ttk.Button(row, text="등록", command=self.add_machine).pack(side="left", padx=4)
        ttk.Label(row, text="선택한 PC:").pack(side="left", padx=(12, 2))
        ttk.Button(row, text="토큰 재발급", command=self.rotate_token).pack(side="left", padx=2)
        ttk.Button(row, text="이름 바꾸기", command=self.rename_machine).pack(side="left", padx=2)
        ttk.Button(row, text="삭제", command=self.delete_machine).pack(side="left", padx=2)

        token_row = ttk.Frame(box)
        token_row.pack(fill="x", pady=(6, 0))
        ttk.Label(token_row, text="토큰").pack(side="left")
        ttk.Entry(token_row, textvariable=self.token_var, state="readonly").pack(side="left", fill="x", expand=True, padx=(4, 6))
        ttk.Button(token_row, text="복사", command=self.copy_token).pack(side="left")

    def _conn(self) -> sqlite3.Connection:
        path = config.db_path()
        db.init_db(path)
        return db.connect(path)

    def load_machines(self) -> None:
        with closing(self._conn()) as conn:
            rows = machines.list_machines(conn)
        self.tree.delete(*self.tree.get_children())
        for m in rows:
            self.tree.insert("", "end", iid=m["name"], values=(m["id"], m["name"], m["session_count"], (m["last_seen_at"] or "-")[:19].replace("T", " ")))

    def add_machine(self) -> None:
        name = self.machine_name_var.get().strip()
        if not name:
            self.message.error("PC 이름을 입력하세요.")
            return
        try:
            with closing(self._conn()) as conn:
                token = machines.create_machine(conn, name)
        except sqlite3.IntegrityError:
            self.message.error(f"이미 등록된 이름입니다: {name}")
            return
        except ValueError as e:
            self.message.error(str(e))
            return
        self.token_var.set(token)
        self.machine_name_var.set("")
        self.load_machines()
        self.message.info(f"{name}을(를) 등록했습니다. 토큰을 복사해 그 PC의 에이전트 설정에 넣으세요 (다시 표시되지 않습니다).")

    def _selected_machine(self) -> str | None:
        chosen = self.tree.selection()
        if not chosen:
            self.message.error("목록에서 PC를 고르세요.")
            return None
        return chosen[0]

    def rename_machine(self, new_name: str | None = None) -> None:
        name = self._selected_machine()
        if name is None:
            return
        if local_agent.is_local(name):
            self.message.error(f"{name}은 서버 PC 자신이라 이름을 바꿀 수 없습니다.")
            return
        if new_name is None:
            new_name = simpledialog.askstring("이름 바꾸기", f"{name}의 새 이름 (세션은 그대로 유지됩니다):", initialvalue=name, parent=self)
        if not new_name or new_name.strip() == name:
            return
        try:
            with closing(self._conn()) as conn:
                machines.rename_machine(conn, name, new_name)
        except sqlite3.IntegrityError:
            self.message.error(f"이미 있는 이름입니다: {new_name.strip()}")
            return
        except ValueError as e:
            self.message.error(str(e))
            return
        self.load_machines()
        self.tree.selection_set(new_name.strip())
        self.message.info(f"이름을 바꿨습니다: {name} → {new_name.strip()}")

    def delete_machine(self, confirmed: bool | None = None) -> None:
        name = self._selected_machine()
        if name is None:
            return
        if local_agent.is_local(name):
            self.message.error(f"{name}은 서버 PC 자신이라 삭제할 수 없습니다.")
            return
        sessions = self.tree.set(name, "sessions")
        if confirmed is None:
            confirmed = messagebox.askyesno(
                "PC 삭제",
                f"{name}과(와) 이 PC의 세션 {sessions}개를 모두 삭제합니다. 되돌릴 수 없습니다.\n"
                "토큰도 사라지므로 이 PC를 다시 쓰려면 새로 등록해야 합니다. 삭제할까요?",
                icon="warning", default="no", parent=self,
            )
        if not confirmed:
            return
        with closing(self._conn()) as conn:
            count = machines.delete_machine(conn, name)
        self.load_machines()
        self.message.info(f"삭제했습니다: {name} (세션 {count}개)")

    def rotate_token(self) -> None:
        name = self._selected_machine()
        if name is None:
            return
        if not messagebox.askyesno("토큰 재발급", f"{name}의 토큰을 새로 발급할까요? 기존 토큰으로는 더 이상 접속할 수 없습니다.", parent=self):
            return
        with closing(self._conn()) as conn:
            token = machines.rotate_token(conn, name)
        if token is None:
            self.message.error(f"등록되지 않은 이름입니다: {name}")
            return
        self.token_var.set(token)
        self.message.info(f"{name}의 새 토큰을 발급했습니다. 그 PC의 에이전트 설정을 바꾸세요.")

    def copy_token(self) -> None:
        token = self.token_var.get()
        if not token:
            self.message.error("표시된 토큰이 없습니다. PC를 등록하거나 토큰을 재발급하세요.")
            return
        self.clipboard_clear()
        self.clipboard_append(token)
        self.message.info("토큰을 복사했습니다.")

    # ── 웹 비밀번호 ─────────────────────────────────────────────

    def _build_password_box(self) -> None:
        box = ttk.LabelFrame(self, text="웹 비밀번호 (원격 접속에 필요, 설정하면 이 PC에서도 로그인 필요)", padding=6)
        box.pack(fill="x", pady=(8, 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="새 비밀번호").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Entry(box, textvariable=self.password_var, show="•").grid(row=0, column=1, sticky="ew", **PAD)
        ttk.Label(box, text="확인").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(box, textvariable=self.confirm_var, show="•").grid(row=1, column=1, sticky="ew", **PAD)
        buttons = ttk.Frame(box)
        buttons.grid(row=0, column=2, rowspan=2, sticky="n", **PAD)
        ttk.Button(buttons, text="설정", command=self.set_password).pack(fill="x")
        ttk.Button(buttons, text="제거", command=self.clear_password).pack(fill="x", pady=(4, 0))
        self.password_state = ttk.Label(box, text="", foreground="gray")
        self.password_state.grid(row=2, column=0, columnspan=3, sticky="w", padx=6)

    def _refresh_password_state(self) -> None:
        with closing(self._conn()) as conn:
            enabled = auth.password_enabled(conn)
        self.password_state.configure(
            text="현재: 비밀번호 설정됨 (모든 접속에 로그인 필요)" if enabled else "현재: 비밀번호 없음 (이 PC에서 온 요청만 허용)")

    def set_password(self) -> None:
        password, confirm = self.password_var.get(), self.confirm_var.get()
        if password != confirm:
            self.message.error("비밀번호가 일치하지 않습니다.")
            return
        try:
            with closing(self._conn()) as conn:
                auth.set_password(conn, password)
        except ValueError as e:
            self.message.error(str(e))
            return
        self.password_var.set("")
        self.confirm_var.set("")
        self._refresh_password_state()
        self.message.info("비밀번호를 설정했습니다. 기존 로그인은 모두 해제됩니다.")

    def clear_password(self) -> None:
        if not messagebox.askyesno("비밀번호 제거", "비밀번호를 제거하면 이 PC에서만 웹 UI에 접속할 수 있습니다. 제거할까요?", parent=self):
            return
        with closing(self._conn()) as conn:
            auth.clear_password(conn)
        self._refresh_password_state()
        self.message.info("비밀번호를 제거했습니다.")
