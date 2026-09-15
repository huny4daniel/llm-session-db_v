import json

import pytest

from server import ingest, queries
from tests import samples
from tests.conftest import end_offset, to_lines


def ingest_all(conn, machine_id, file_key, texts, start=0, **kwargs):
    lines = to_lines(texts, start)
    return ingest.ingest_batch(
        conn, machine_id=machine_id, source="claude", file_key=file_key,
        start_offset=start, lines=lines, **kwargs,
    )


def session_row(conn):
    return conn.execute("SELECT * FROM sessions WHERE session_uid = ?", (samples.SESSION,)).fetchone()


def test_full_ingest_builds_session(conn, machine_id):
    next_offset = ingest_all(conn, machine_id, samples.MAIN_KEY, samples.main_lines())
    assert next_offset == end_offset(to_lines(samples.main_lines()))
    ingest_all(conn, machine_id, samples.SUB_KEY, samples.subagent_lines())

    s = session_row(conn)
    assert s["project_path"] == samples.CWD
    assert s["project_key"] == samples.PROJECT_KEY
    assert s["ai_title"] == "세션 서버 구현"
    assert s["first_prompt"] == "세션 저장 서버를 만들어줘"
    assert s["cost_usd"] == 1.25
    assert s["git_branch"] == "main"
    assert s["user_turns"] == 2
    # 사용자 메시지 3개(메인 2 + 서브 1) + 서로 다른 응답 4개(msg_A, msg_B, msg_C, msg_S)
    assert s["message_count"] == 7
    assert s["started_at"] == samples.ts(0)
    assert s["last_activity_at"] == samples.ts(21)
    for column, value in samples.EXPECTED_TOKENS.items():
        assert s[column] == value, column


def test_split_batches_match_single_batch(conn, machine_id):
    texts = samples.main_lines()
    first = to_lines(texts[:6])  # msg_A 응답이 두 묶음에 걸치도록 자른다
    ingest_all(conn, machine_id, samples.MAIN_KEY, texts[:6])
    ingest_all(conn, machine_id, samples.MAIN_KEY, texts[6:], start=end_offset(first))

    s = session_row(conn)
    assert (s["input_tokens"], s["output_tokens"], s["cache_read_tokens"]) == (12, 100, 3000)
    assert s["ai_title"] == "세션 서버 구현"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 12


def test_offset_mismatch_reports_expected(conn, machine_id):
    texts = samples.main_lines()
    ingest_all(conn, machine_id, samples.MAIN_KEY, texts[:3])
    expected = end_offset(to_lines(texts[:3]))

    with pytest.raises(ingest.OffsetMismatch) as exc:
        ingest_all(conn, machine_id, samples.MAIN_KEY, texts[:3])  # 같은 묶음 재전송
    assert exc.value.expected_offset == expected
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 3


def test_non_contiguous_lines_rejected(conn, machine_id):
    lines = to_lines(samples.main_lines()[:3])
    broken = [lines[0], lines[2]]
    with pytest.raises(ingest.IngestError):
        ingest.ingest_batch(
            conn, machine_id=machine_id, source="claude", file_key=samples.MAIN_KEY,
            start_offset=0, lines=broken,
        )


def test_unknown_file_key_rejected(conn, machine_id):
    with pytest.raises(ingest.IngestError):
        ingest_all(conn, machine_id, f"{samples.PROJECT_KEY}/memory/MEMORY.md", ["x"])


def test_reset_rebuilds_from_remaining_lines(conn, machine_id):
    texts = samples.main_lines()
    ingest_all(conn, machine_id, samples.MAIN_KEY, texts)
    ingest_all(conn, machine_id, samples.MAIN_KEY, texts[:4], reset=True)

    s = session_row(conn)
    assert s["ai_title"] is None
    assert s["cost_usd"] is None
    assert s["first_prompt"] == "세션 저장 서버를 만들어줘"
    assert s["output_tokens"] == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


MAIN_KINDS = ["command", "user", "assistant", "assistant", "assistant", "tool_result",
              "assistant", "assistant", "system", "interrupted", "user"]


def test_same_session_in_two_folders_is_deduplicated(conn, machine_id):
    """가져온 세션을 다른 폴더에서 이어가면 앞부분이 겹치는 파일이 두 개 생긴다."""
    texts = samples.main_lines()
    copy_key = f"C--elsewhere/{samples.SESSION}.jsonl"
    ingest_all(conn, machine_id, samples.MAIN_KEY, texts[:9])
    ingest_all(conn, machine_id, copy_key, texts)

    s = session_row(conn)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 12
    assert s["user_turns"] == 2
    assert (s["input_tokens"], s["output_tokens"], s["cache_read_tokens"]) == (12, 100, 3000)
    assert [m["kind"] for m in queries.session_messages(conn, s["id"])] == MAIN_KINDS

    ingest.rebuild_all(conn)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 12
    assert [m["kind"] for m in queries.session_messages(conn, s["id"])] == MAIN_KINDS


def test_deleted_session_is_not_recreated(conn, machine_id):
    texts = samples.main_lines()
    first = to_lines(texts[:5])
    ingest_all(conn, machine_id, samples.MAIN_KEY, texts[:5])
    ingest_all(conn, machine_id, samples.SUB_KEY, samples.subagent_lines())
    session_id = session_row(conn)["id"]

    assert ingest.delete_session(conn, session_id) is not None
    assert session_row(conn) is None
    for table in ("raw_events", "messages", "api_usage"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    fts_hits = conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH '\"trigram\"'").fetchone()[0]
    assert fts_hits == 0

    # 에이전트가 같은 파일의 뒷부분을 보내도 저장하지 않고 수신 위치만 옮긴다.
    next_offset = ingest_all(conn, machine_id, samples.MAIN_KEY, texts[5:], start=end_offset(first))
    assert next_offset == end_offset(to_lines(texts))
    assert session_row(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 0
    assert ingest.delete_session(conn, session_id) is None


def fork_lines(new_uid, extra_prompt=None):
    """--fork-session 결과처럼 원본 줄의 sessionId만 바꾸고(uuid 유지) 필요하면 새 질문을 덧붙인다."""
    rows = []
    for line in samples.main_lines():
        event = json.loads(line)
        if event.get("sessionId") == samples.SESSION:
            event["sessionId"] = new_uid
        rows.append(json.dumps(event, ensure_ascii=False))
    if extra_prompt:
        rows.append(json.dumps({
            "type": "user", "uuid": f"new-{new_uid[:4]}", "sessionId": new_uid, "timestamp": samples.ts(40),
            "message": {"role": "user", "content": extra_prompt},
        }, ensure_ascii=False))
    return rows


def links(conn):
    return sorted(tuple(r) for r in conn.execute("SELECT child_uid, parent_session_id FROM session_links"))


def session_id_of(conn, uid):
    return conn.execute("SELECT id FROM sessions WHERE session_uid = ?", (uid,)).fetchone()["id"]


def test_fork_session_linked_to_parent_by_shared_uuids(conn, machine_id):
    ingest_all(conn, machine_id, samples.MAIN_KEY, samples.main_lines())
    parent_id = session_row(conn)["id"]

    child = "22222222-0000-4000-8000-000000000000"
    ingest_all(conn, machine_id, f"C--pc-b/{child}.jsonl", fork_lines(child, "분기 후 질문"))
    assert links(conn) == [(child, parent_id)]

    # 같은 원본에서 갈라진 형제: 원본과 첫 분기 모두와 같은 수로 겹치지만 자기 메시지가 없는 원본이 부모
    sibling = "33333333-0000-4000-8000-000000000000"
    ingest_all(conn, machine_id, f"C--pc-c/{sibling}.jsonl", fork_lines(sibling))
    assert links(conn) == sorted([(child, parent_id), (sibling, parent_id)])

    # 분기의 분기: 첫 분기의 새 질문까지 복사하므로 첫 분기가 부모
    grandchild = "44444444-0000-4000-8000-000000000000"
    rows = fork_lines(grandchild) + [
        fork_lines(child, "분기 후 질문")[-1].replace(child, grandchild),
    ]
    ingest_all(conn, machine_id, f"C--pc-d/{grandchild}.jsonl", rows)
    assert (grandchild, session_id_of(conn, child)) in links(conn)

    # 자식을 지우면 링크도 사라진다
    ingest.delete_session(conn, session_id_of(conn, sibling))
    assert sibling not in [uid for uid, _ in links(conn)]


def test_unrelated_session_has_no_parent(conn, machine_id):
    ingest_all(conn, machine_id, samples.MAIN_KEY, samples.main_lines())
    ingest_all(conn, machine_id, samples.SUB_KEY, samples.subagent_lines())  # 서브에이전트 파일은 부모 추정 대상 아님
    assert links(conn) == []


def test_rebuild_all_is_idempotent(conn, machine_id):
    ingest_all(conn, machine_id, samples.MAIN_KEY, samples.main_lines())
    ingest_all(conn, machine_id, samples.SUB_KEY, samples.subagent_lines())
    before = dict(session_row(conn))

    assert ingest.rebuild_all(conn) == 1
    assert dict(session_row(conn)) == before
    fts_hits = conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH '\"trigram\"'").fetchone()[0]
    assert fts_hits == 1
