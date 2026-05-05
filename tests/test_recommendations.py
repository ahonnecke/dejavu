"""Synthetic-fixture tests for the recommendation engine.

Each test builds a parsed-session structure directly and asserts which rules fire.
Avoids JSONL round-tripping for the rule logic (covered separately in test_parse).
"""
import dejavu


def _tool_use(tool: str, **inputs) -> dict:
    return {"tool": tool, "input": inputs, "id": "x", "timestamp": "2026-01-01T00:00:00Z"}


def _make_parsed(tool_uses: list[dict], cwd: str = "/proj") -> dict:
    return {
        "meta": {"sessionId": "s1", "gitBranch": "main", "cwd": cwd},
        "path": "/fake/session.jsonl",
        "tool_uses": tool_uses,
    }


def _rule_ids(recs: list[dict]) -> set[str]:
    return {r["rule"] for r in recs}


# --- Single-session rules ---


def test_R1_high_cost_repeated_bash_fires():
    parsed = _make_parsed([
        _tool_use("Bash", command="rm -rf /tmp/foo"),
        _tool_use("Bash", command="rm -rf /tmp/foo"),
    ])
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(parsed))
    assert "R1" in _rule_ids(recs)
    r1 = next(r for r in recs if r["rule"] == "R1")
    assert r1["kind"] == "hook"
    assert r1["priority"] == 1


def test_R1_does_not_fire_on_safe_repeat():
    parsed = _make_parsed([_tool_use("Bash", command="ls -la")] * 5)
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(parsed))
    assert "R1" not in _rule_ids(recs)


def test_R2_repeated_bash_wraps():
    parsed = _make_parsed([_tool_use("Bash", command="npm test")] * 4)
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(parsed))
    assert "R2" in _rule_ids(recs)
    r = next(r for r in recs if r["rule"] == "R2")
    assert r["kind"] == "wrapper"


def test_R2_skips_already_wrapped():
    # Absolute-path invocation — we don't recommend wrapping a wrapper
    parsed = _make_parsed([_tool_use("Bash", command="/usr/local/bin/foo")] * 5)
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(parsed))
    assert "R2" not in _rule_ids(recs)


def test_R2_skips_high_cost():
    # High-cost commands get R1 (hook), not R2 (wrapper)
    parsed = _make_parsed([_tool_use("Bash", command="rm -rf /tmp/x")] * 5)
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(parsed))
    rule_ids = _rule_ids(recs)
    assert "R1" in rule_ids
    assert "R2" not in rule_ids


def test_R3_repeated_read_fires():
    parsed = _make_parsed([
        _tool_use("Read", file_path="/proj/main.py"),
        _tool_use("Read", file_path="/proj/main.py"),
        _tool_use("Read", file_path="/proj/main.py"),
    ])
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(parsed))
    assert "R3" in _rule_ids(recs)
    r = next(r for r in recs if r["rule"] == "R3")
    assert r["kind"] == "claudemd"
    assert r["target"] == "/proj/main.py"


def test_R4_exploration_runs_fires():
    # Three exploration runs of >=3 search/read tools, separated by edits
    grep = _tool_use("Grep", pattern="foo")
    read = _tool_use("Read", file_path="/proj/x.py")
    edit = _tool_use("Edit", file_path="/proj/x.py")
    parsed = _make_parsed(
        [grep, grep, read, edit] * 3
    )
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(parsed))
    assert "R4" in _rule_ids(recs)


def test_R6_grep_thrashing_fires():
    greps = [_tool_use("Grep", pattern=f"sym_{i}") for i in range(10)]
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(_make_parsed(greps)))
    assert "R6" in _rule_ids(recs)


# --- Aggregation + cross-session ---


def test_aggregate_counts_distinct_sessions():
    a1 = dejavu.analyze_session(_make_parsed([
        _tool_use("Bash", command="git status"),
        _tool_use("Bash", command="git status"),
    ]))
    a2 = dejavu.analyze_session(_make_parsed([
        _tool_use("Bash", command="git status"),
    ]))
    a3 = dejavu.analyze_session(_make_parsed([
        _tool_use("Bash", command="other-cmd"),
    ]))
    agg = dejavu.aggregate_analyses([a1, a2, a3])
    assert agg["n_sessions"] == 3
    assert agg["bash"]["git status"]["sessions"] == 2  # a1 and a2
    assert agg["bash"]["git status"]["total"] == 3     # 2 in a1, 1 in a2


def test_R7_cross_session_wrapper():
    # Same command in 2 of 2 sessions → wrapper rec
    a1 = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="npm test")] * 2))
    a2 = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="npm test")] * 2))
    agg = dejavu.aggregate_analyses([a1, a2])
    recs = dejavu.recommend_from_aggregate(agg, min_sessions=2)
    rule_kinds = {(r["rule"], r["kind"]) for r in recs}
    assert ("R7-wrapper", "wrapper") in rule_kinds


def test_R7_cross_session_hook_for_high_cost():
    a1 = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="git push --force")] * 2))
    a2 = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="git push --force")] * 2))
    agg = dejavu.aggregate_analyses([a1, a2])
    recs = dejavu.recommend_from_aggregate(agg, min_sessions=2)
    assert any(r["rule"] == "R7-hook" and r["kind"] == "hook" for r in recs)


def test_R8_file_reads_across_sessions():
    a1 = dejavu.analyze_session(_make_parsed([_tool_use("Read", file_path="/proj/main.py")]))
    a2 = dejavu.analyze_session(_make_parsed([_tool_use("Read", file_path="/proj/main.py")]))
    agg = dejavu.aggregate_analyses([a1, a2])
    recs = dejavu.recommend_from_aggregate(agg, min_sessions=2)
    assert any(r["rule"] == "R8" for r in recs)


# --- Cross-project ---


def test_cross_project_pivots_by_project():
    # Project A has "git status" in both sessions; project B has it in one.
    a1 = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="git status")]))
    a2 = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="git status")]))
    b1 = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="git status")]))
    b2 = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="other-cmd")]))
    aggs = {
        "proj-a": dejavu.aggregate_analyses([a1, a2]),
        "proj-b": dejavu.aggregate_analyses([b1, b2]),
    }
    cross = dejavu.cross_project_patterns(aggs, min_sessions_per_project=1)
    assert set(cross["bash"]["git status"]["projects"]) == {"proj-a", "proj-b"}
    # other-cmd only in proj-b
    assert cross["bash"]["other-cmd"]["projects"] == ["proj-b"]


def test_X2_cross_project_wrapper():
    aggs = {}
    for i in range(5):
        a = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="pnpm install")]))
        aggs[f"proj-{i}"] = dejavu.aggregate_analyses([a])
    cross = dejavu.cross_project_patterns(aggs, min_sessions_per_project=1)
    recs = dejavu.recommend_cross_project(cross, min_projects=3)
    assert any(r["rule"] == "X2" and r["kind"] == "wrapper" for r in recs)


def test_X1_cross_project_hook_for_high_cost():
    aggs = {}
    for i in range(5):
        a = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="git push --force")]))
        aggs[f"proj-{i}"] = dejavu.aggregate_analyses([a])
    cross = dejavu.cross_project_patterns(aggs, min_sessions_per_project=1)
    recs = dejavu.recommend_cross_project(cross, min_projects=3)
    assert any(r["rule"] == "X1" and r["kind"] == "hook" for r in recs)


def test_X2_skips_already_wrapped():
    aggs = {}
    for i in range(5):
        a = dejavu.analyze_session(_make_parsed([_tool_use("Bash", command="/usr/local/bin/script")]))
        aggs[f"proj-{i}"] = dejavu.aggregate_analyses([a])
    cross = dejavu.cross_project_patterns(aggs, min_sessions_per_project=1)
    recs = dejavu.recommend_cross_project(cross, min_projects=3)
    assert not any(r["rule"] == "X2" for r in recs)


# --- Sort/priority ---


def test_recs_sorted_priority_then_weight():
    parsed = _make_parsed([
        _tool_use("Bash", command="rm -rf /a"),
        _tool_use("Bash", command="rm -rf /a"),
        _tool_use("Bash", command="npm test"),
        _tool_use("Bash", command="npm test"),
        _tool_use("Bash", command="npm test"),
        _tool_use("Bash", command="npm test"),
    ])
    recs = dejavu.recommend_from_analysis(dejavu.analyze_session(parsed))
    # First rec must be priority 1 (hook), even though wrapper has higher count
    assert recs[0]["priority"] == 1
    assert recs[0]["rule"] == "R1"
