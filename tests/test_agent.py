from agent.client import Client
from agent.collector import discover_claude, iter_batches
from agent.sync import sync_files
from tests import samples


class InProcessClient(Client):
    """urllib 대신 FastAPI TestClient로 요청을 보내는 클라이언트."""

    def __init__(self, http, token):
        super().__init__("http://testserver", token)
        self.http = http

    def _send(self, method, path, body):
        response = self.http.request(method, path, json=body, headers={"Authorization": f"Bearer {self.token}"})
        return response.status_code, response.json()


def test_iter_batches_skips_partial_tail(tmp_path):
    path = tmp_path / "s.jsonl"
    samples.write_jsonl(path, ["가나다", "abc"], partial_tail='{"incomplete')
    batches = list(iter_batches(path, 0))
    assert len(batches) == 1
    start, lines = batches[0]
    assert start == 0
    assert [l["text"] for l in lines] == ["가나다", "abc"]
    assert lines[0]["length"] == len("가나다".encode()) + 1
    assert lines[1]["offset"] == lines[0]["length"]


def test_iter_batches_splits_by_size(tmp_path):
    path = tmp_path / "s.jsonl"
    samples.write_jsonl(path, ["x" * 9] * 5)  # 줄당 10바이트
    batches = list(iter_batches(path, 10, max_bytes=25))
    assert [(start, len(lines)) for start, lines in batches] == [(10, 2), (30, 2)]


def test_discover_claude(tmp_path):
    samples.write_jsonl(tmp_path / samples.SUB_KEY, samples.subagent_lines())
    samples.write_jsonl(tmp_path / samples.MAIN_KEY, samples.main_lines())
    (tmp_path / samples.PROJECT_KEY / "memory").mkdir()
    (tmp_path / samples.PROJECT_KEY / "memory" / "MEMORY.md").write_text("x", encoding="utf-8")
    keys = [f.file_key for f in discover_claude(tmp_path)]
    assert keys == [samples.MAIN_KEY, samples.SUB_KEY]
    assert discover_claude(tmp_path / "missing") == []


def session_count_and_messages(http):
    data = http.get("/api/sessions").json()
    if not data["items"]:
        return 0, 0
    return data["total"], data["items"][0]["message_count"]


def test_sync_incremental_partial_and_reset(api, tmp_path):
    http, token = api
    client = InProcessClient(http, token)
    root = tmp_path / "projects"
    main_path = root / samples.MAIN_KEY
    texts = samples.main_lines()

    # 1) 앞부분 + 덜 써진 줄
    samples.write_jsonl(main_path, texts[:5], partial_tail=texts[5][:10])
    result = sync_files(client, "claude", discover_claude(root))
    assert (result.files, result.lines) == (1, 5)

    # 2) 변화 없으면 아무것도 보내지 않음(덜 써진 줄은 계속 대기)
    assert sync_files(client, "claude", discover_claude(root)).lines == 0

    # 3) 나머지가 기록되면 이어서 전송
    samples.write_jsonl(main_path, texts)
    samples.write_jsonl(root / samples.SUB_KEY, samples.subagent_lines())
    result = sync_files(client, "claude", discover_claude(root))
    assert (result.files, result.lines) == (2, len(texts) - 5 + len(samples.subagent_lines()))
    assert session_count_and_messages(http) == (1, 7)

    # 4) 파일이 줄어들면 처음부터 다시 보내고 세션을 재구성
    samples.write_jsonl(main_path, texts[:4])
    result = sync_files(client, "claude", discover_claude(root))
    assert result.lines == 4
    # 메인 사용자 1개 + 서브 사용자 1개 + 서브 응답 1개
    assert session_count_and_messages(http) == (1, 3)
