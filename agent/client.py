import json
import urllib.error
import urllib.parse
import urllib.request


class ServerError(Exception):
    pass


class OffsetConflict(Exception):
    def __init__(self, expected_offset: int):
        super().__init__(f"서버 수신 위치: {expected_offset}")
        self.expected_offset = expected_offset


class Client:
    """서버 에이전트 API 클라이언트. 원격 PC에 설치 부담이 없도록 표준 라이브러리만 쓴다."""

    def __init__(self, server_url: str, token: str, timeout: float = 60):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def remote_files(self, source: str) -> dict:
        return self._request("GET", f"/api/agent/files?source={urllib.parse.quote(source)}")["files"]

    def ingest(self, payload: dict) -> dict:
        return self._request("POST", "/api/agent/ingest", payload)

    def list_sessions(self, q: str = "", limit: int = 50) -> list[dict]:
        """가져올 세션을 고를 수 있게 서버의 세션 목록(최근 순)을 받는다."""
        query = urllib.parse.urlencode({"q": q, "limit": limit})
        return self._request("GET", f"/api/agent/sessions?{query}")["items"]

    def session_source(self, ref: str) -> dict:
        """세션 번호 또는 세션 ID(앞부분)로 세션 정보와 메인 파일 원본 줄을 받는다."""
        return self._request("GET", f"/api/agent/sessions/{urllib.parse.quote(ref, safe='')}/source")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        status, data = self._send(method, path, body)
        if status == 409 and "expected_offset" in data:
            raise OffsetConflict(data["expected_offset"])
        if status >= 400:
            raise ServerError(f"{status} {data.get('detail', data)}")
        return data

    def _send(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        """(상태 코드, JSON 본문)을 반환한다. 테스트에서 전송 계층만 바꿔 끼울 수 있게 분리했다."""
        request = urllib.request.Request(
            self.server_url + path,
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"detail": raw.decode("utf-8", errors="replace")}
