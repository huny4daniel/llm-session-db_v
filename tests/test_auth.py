import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from agent import autostart
from agent.lock import AlreadyRunning, single_instance
from server import auth, machines
from server.app import create_app

REMOTE = "100.64.0.2"


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)


def make_client(db_path, host="127.0.0.1"):
    return TestClient(create_app(db_path), client=(host, 50000))


def test_password_hash_roundtrip():
    stored = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("wrong horse", stored)
    assert not auth.verify_password("anything", "not-a-hash")


def test_short_password_rejected(conn):
    with pytest.raises(ValueError):
        auth.set_password(conn, "short")
    assert not auth.password_enabled(conn)


def test_session_token_expiry_tamper_and_rotation(conn):
    auth.set_password(conn, "password1")
    token = auth.issue_session(conn, now=1000)
    assert auth.check_session(conn, token, now=1060)
    assert not auth.check_session(conn, token, now=1000 + auth.SESSION_TTL_SECONDS + 1)
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert not auth.check_session(conn, tampered, now=1060)
    assert not auth.check_session(conn, "garbage", now=1060)

    auth.set_password(conn, "password2")  # 비밀번호를 바꾸면 기존 로그인 무효
    assert not auth.check_session(conn, token, now=1060)


@pytest.mark.parametrize(
    ("host", "headers", "expected"),
    [
        ("127.0.0.1", {}, True),
        ("::1", {}, True),
        (REMOTE, {}, False),
        ("127.0.0.1", {"x-forwarded-for": REMOTE}, False),
        ("127.0.0.1", {"tailscale-user-login": "me@example.com"}, False),
        (None, {}, False),
    ],
)
def test_is_local_request(host, headers, expected):
    assert auth.is_local_request(host, headers) is expected


def test_login_limiter_locks_and_recovers():
    now = [0.0]
    limiter = auth.LoginLimiter(max_failures=3, lockout_seconds=10, clock=lambda: now[0])
    limiter.failure("ip")
    limiter.failure("ip")
    assert limiter.locked_for("ip") == 0
    limiter.failure("ip")
    assert limiter.locked_for("ip") == 10

    now[0] = 11
    assert limiter.locked_for("ip") == 0
    limiter.failure("ip")  # 잠금이 끝난 뒤에는 새로 센다
    assert limiter.locked_for("ip") == 0
    limiter.success("ip")
    assert limiter.locked_for("ip") == 0


def test_without_password_only_local_requests_allowed(db_path):
    with make_client(db_path, REMOTE) as remote:
        assert remote.get("/api/sessions").status_code == 403
        assert remote.get("/api/auth/status").json() == {"password_enabled": False, "authenticated": False}
        assert remote.get("/").status_code == 200  # 화면 셸은 공개

    with make_client(db_path) as local:
        assert local.get("/api/sessions").status_code == 200
        assert local.get("/api/sessions", headers={"X-Forwarded-For": REMOTE}).status_code == 403
        assert local.post("/api/auth/login", json={"password": "password1"}).status_code == 400


def test_login_flow(db_path, conn):
    auth.set_password(conn, "password1")
    with make_client(db_path, REMOTE) as client:
        assert client.get("/api/sessions").status_code == 401
        assert client.post("/api/auth/login", json={"password": "wrong-pass"}).status_code == 401

        response = client.post("/api/auth/login", json={"password": "password1"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie

        assert client.get("/api/sessions").status_code == 200
        assert client.get("/api/auth/status").json() == {"password_enabled": True, "authenticated": True}

        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/sessions").status_code == 401


def test_password_required_even_for_local_requests(db_path, conn):
    auth.set_password(conn, "password1")
    with make_client(db_path) as local:
        assert local.get("/api/sessions").status_code == 401


def test_login_lockout(db_path, conn):
    auth.set_password(conn, "password1")
    with make_client(db_path) as client:
        for _ in range(auth.MAX_FAILURES):
            assert client.post("/api/auth/login", json={"password": "wrong-pass"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "password1"}).status_code == 429


def test_agent_api_uses_token_not_password(db_path, conn):
    token = machines.create_machine(conn, "remote-pc")
    auth.set_password(conn, "password1")
    with make_client(db_path, REMOTE) as client:
        response = client.get("/api/agent/files", params={"source": "claude"}, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200


def test_launch_args_work_from_any_directory(tmp_path):
    args = autostart.launch_args("agent", ["--help"])
    assert args[1] == "-c"
    # Run 키는 작업 디렉터리를 지정할 수 없으므로 다른 폴더에서도 패키지를 찾아야 한다.
    result = subprocess.run([sys.executable, *args[1:]], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "usage: python -m agent" in result.stdout


def test_single_instance(tmp_path):
    lock = tmp_path / "agent.lock"
    with single_instance(lock):
        with pytest.raises(AlreadyRunning):
            with single_instance(lock):
                pass
    with single_instance(lock):
        pass
