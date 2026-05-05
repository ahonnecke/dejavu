"""End-to-end test of JSONL parsing using a synthetic session file."""
import json
from pathlib import Path

import dejavu


def _write_session(tmp_path: Path, lines: list[dict]) -> Path:
    p = tmp_path / "session.jsonl"
    with open(p, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
    return p


def _assistant(*tool_uses: dict) -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"content": list(tool_uses)},
    }


def _tool_use(name: str, **inputs) -> dict:
    return {"type": "tool_use", "name": name, "input": inputs, "id": "t1"}


def test_parses_metadata(tmp_path):
    path = _write_session(tmp_path, [
        {"sessionId": "abc", "version": "1.0", "gitBranch": "feat/x", "cwd": "/proj"},
        _assistant(_tool_use("Bash", command="echo hi")),
    ])
    parsed = dejavu.parse_session(path)
    assert parsed["meta"]["sessionId"] == "abc"
    assert parsed["meta"]["gitBranch"] == "feat/x"
    assert parsed["meta"]["cwd"] == "/proj"


def test_extracts_tool_uses(tmp_path):
    path = _write_session(tmp_path, [
        {"cwd": "/proj"},
        _assistant(_tool_use("Bash", command="ls")),
        _assistant(
            _tool_use("Read", file_path="/proj/x.py"),
            _tool_use("Grep", pattern="foo"),
        ),
    ])
    parsed = dejavu.parse_session(path)
    tools = [tu["tool"] for tu in parsed["tool_uses"]]
    assert tools == ["Bash", "Read", "Grep"]


def test_skips_malformed_lines(tmp_path):
    p = tmp_path / "session.jsonl"
    with open(p, "w") as f:
        f.write('{"cwd": "/proj"}\n')
        f.write("this is not json\n")
        f.write(json.dumps(_assistant(_tool_use("Bash", command="ls"))) + "\n")
    parsed = dejavu.parse_session(p)
    assert len(parsed["tool_uses"]) == 1


def test_full_pipeline_recommends(tmp_path):
    """Parse → analyze → recommend on a realistic synthetic session."""
    tool_uses = [
        _tool_use("Bash", command="rm -rf /tmp/x"),
        _tool_use("Bash", command="rm -rf /tmp/x"),
    ] + [
        _tool_use("Read", file_path="/proj/main.py"),
    ] * 4
    path = _write_session(tmp_path, [
        {"cwd": "/proj"},
        _assistant(*tool_uses),
    ])
    parsed = dejavu.parse_session(path)
    analysis = dejavu.analyze_session(parsed)
    recs = dejavu.recommend_from_analysis(analysis)
    rule_ids = {r["rule"] for r in recs}
    assert "R1" in rule_ids  # rm -rf hook
    assert "R3" in rule_ids  # main.py CLAUDE.md
