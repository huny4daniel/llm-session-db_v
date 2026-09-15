import pytest

from server import ingest
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


def test_rebuild_all_is_idempotent(conn, machine_id):
    ingest_all(conn, machine_id, samples.MAIN_KEY, samples.main_lines())
    ingest_all(conn, machine_id, samples.SUB_KEY, samples.subagent_lines())
    before = dict(session_row(conn))

    assert ingest.rebuild_all(conn) == 1
    assert dict(session_row(conn)) == before
    fts_hits = conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH '\"trigram\"'").fetchone()[0]
    assert fts_hits == 1
