import sqlite3

import pytest

from server import machines
from server.__main__ import main as server_main
from tests import samples
from tests.test_api import auth, payload


def test_rename_keeps_sessions(api, db_path, conn):
    http, token = api
    assert http.post("/api/agent/ingest", json=payload(samples.MAIN_KEY, samples.main_lines()), headers=auth(token)).status_code == 200

    assert machines.rename_machine(conn, "api-pc", "renamed-pc") is True
    assert machines.rename_machine(conn, "missing", "x") is False
    with pytest.raises(ValueError):
        machines.rename_machine(conn, "renamed-pc", "  ")
    machines.create_machine(conn, "other")
    with pytest.raises(sqlite3.IntegrityError):
        machines.rename_machine(conn, "renamed-pc", "other")

    listed = {m["name"]: m["session_count"] for m in machines.list_machines(conn)}
    assert listed == {"renamed-pc": 1, "other": 0}
    assert http.get("/api/sessions").json()["items"][0]["machine_name"] == "renamed-pc"
    # 토큰은 그대로 유효하다
    assert http.get("/api/agent/files?source=claude", headers=auth(token)).status_code == 200


def test_delete_machine_removes_everything(api, conn):
    http, token = api
    assert http.post("/api/agent/ingest", json=payload(samples.MAIN_KEY, samples.main_lines()), headers=auth(token)).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] > 0

    assert machines.delete_machine(conn, "missing") is None
    assert machines.delete_machine(conn, "api-pc") == 1
    for table in ("machines", "sessions", "messages", "raw_events", "source_files", "api_usage", "session_links"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert http.get("/api/agent/files?source=claude", headers=auth(token)).status_code == 401  # 토큰도 사라짐


def test_cli_rename_and_delete(db_path, monkeypatch, capsys):
    monkeypatch.setenv("LSDB_DATA_DIR", str(db_path.parent))
    assert server_main(["add-machine", "pc-a"]) == 0
    assert server_main(["rename-machine", "pc-a", "pc-b"]) == 0
    assert server_main(["rename-machine", "nope", "pc-c"]) == 1
    assert server_main(["add-machine", "pc-c"]) == 0
    assert server_main(["rename-machine", "pc-b", "pc-c"]) == 1  # 겹치는 이름

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert server_main(["delete-machine", "pc-b"]) == 1  # 취소
    assert server_main(["delete-machine", "pc-b", "--yes"]) == 0
    assert server_main(["delete-machine", "pc-b", "--yes"]) == 1  # 이미 없음
    capsys.readouterr()
    assert server_main(["machines"]) == 0
    assert "pc-c" in capsys.readouterr().out
