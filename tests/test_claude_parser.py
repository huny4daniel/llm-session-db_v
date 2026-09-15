import json

from server.parsers import claude
from tests import samples

MAIN = claude.FileRef(samples.PROJECT_KEY, samples.SESSION)
SUB = claude.FileRef(samples.PROJECT_KEY, samples.SESSION, samples.AGENT)


def parse_all(lines, ref):
    return [claude.parse_line(line, ref) for line in lines]


def test_parse_file_key():
    assert claude.parse_file_key(samples.MAIN_KEY) == MAIN
    assert claude.parse_file_key(samples.SUB_KEY) == SUB
    assert claude.parse_file_key(f"{samples.PROJECT_KEY}/memory/MEMORY.md") is None
    assert claude.parse_file_key(f"{samples.PROJECT_KEY}/{samples.SESSION}/tool-results/x.txt") is None


def test_message_kinds():
    kinds = [p.message.kind for p in parse_all(samples.main_lines(), MAIN) if p.message]
    assert kinds == [
        "command", "user", "meta", "assistant", "assistant", "assistant",
        "tool_result", "assistant", "assistant", "system", "interrupted", "user",
    ]


def test_first_prompt_skips_command_tags():
    firsts = [p.meta_first.get("first_prompt") for p in parse_all(samples.main_lines(), MAIN)]
    assert [f for f in firsts if f] == ["세션 저장 서버를 만들어줘", "스크린샷 확인해줘"]


def test_usage_extraction_and_synthetic_excluded():
    usages = [p.usage for p in parse_all(samples.main_lines(), MAIN) if p.usage]
    assert [u.api_message_id for u in usages] == ["msg_A", "msg_A", "msg_A", "msg_B"]
    assert usages[2].output_tokens == 80
    assert usages[3].cache_read_tokens == 2000


def test_subagent_lines_do_not_touch_session_meta():
    for parsed in parse_all(samples.subagent_lines(), SUB):
        assert parsed.meta_first == {}
        assert parsed.meta_last == {}


def test_search_text_uses_raw_tool_input_values():
    content = [{"type": "tool_use", "name": "Bash", "input": {"command": 'echo "안녕"\nls', "timeout": 5, "env": ["A=1"]}}]
    assert claude.search_text(content) == 'Bash echo "안녕"\nls\nA=1'


def test_invalid_line_is_ignored():
    parsed = claude.parse_line("{not json", MAIN)
    assert parsed.message is None and parsed.usage is None


def test_view_blocks_strip_image_data():
    line = samples.main_lines()[16]
    blocks = claude.view_blocks(line)
    assert blocks == [
        {"type": "image", "media_type": "image/png"},
        {"type": "text", "text": "스크린샷 확인해줘"},
    ]


def test_view_blocks_tool_use_and_result():
    tool_use = claude.view_blocks(samples.main_lines()[6])
    assert tool_use == [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls -la"}}]
    result = claude.view_blocks(samples.main_lines()[7])
    assert result == [{"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False, "text": "file.txt"}]


def test_view_blocks_truncate_long_text():
    long_text = "가" * (claude.VIEW_TEXT_LIMIT + 10)
    line = json.dumps({"type": "user", "message": {"role": "user", "content": long_text}}, ensure_ascii=False)
    text = claude.view_blocks(line)[0]["text"]
    assert text.startswith("가" * claude.VIEW_TEXT_LIMIT)
    assert text.endswith("(10자 생략)")
