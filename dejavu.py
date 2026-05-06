#!/usr/bin/env python3
"""
dejavu — Claude Code session autopsy tool.

Parses Claude Code JSONL session files to surface repeated commands,
re-read files, exploratory search patterns, and other signals that
indicate tooling gaps or documentation needs.

Usage:
  dejavu                          # Analyze latest session in current project
  dejavu <session.jsonl>          # Analyze specific session file
  dejavu --project <path>         # Analyze latest session for a project dir
  dejavu --all                    # Analyze all sessions in current project
  dejavu --list                   # List available sessions for current project
  dejavu --recent [N]             # Analyze N most recent sessions (default: 5)
  dejavu --threshold N            # Minimum repeat count to report (default: 2)
  dejavu --json                   # Output as JSON instead of markdown
  dejavu --recommend              # Emit only actionable recommendations
  dejavu --self-eval              # Cross-project recommendations from full history
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude" / "projects"


# --- Heuristic tables (in-code; tune freely) ---

# Bash patterns where a single violation is expensive enough that we
# recommend a hook regardless of frequency. Compiled once at import.
HIGH_COST_BASH_PATTERNS = [
    re.compile(p)
    for p in [
        r"\brm\s+-[rRfF]+\b",
        r"\bgit\s+push\b[^\n]*--force\b",
        r"\bgit\s+push\b[^\n]*\s-f\b",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+checkout\s+\.\s*$",
        r"\bgit\s+clean\s+-[fF]d?\b",
        r"\bgit\s+branch\s+-D\b",
        r"\bcurl\b[^|]*\|\s*(?:ba)?sh\b",
        r"\bwget\b[^|]*\|\s*(?:ba)?sh\b",
        r"--no-verify\b",
        r"\bchmod\s+-R\b",
        r"\bdropdb\b",
        r"\bDROP\s+TABLE\b",
        r"\bTRUNCATE\s+TABLE\b",
        r"\bDELETE\s+FROM\b",
    ]
]


def is_high_cost_bash(cmd: str) -> bool:
    return any(p.search(cmd) for p in HIGH_COST_BASH_PATTERNS)


def normalize_cmd(cmd: str) -> str:
    """Normalize a bash command for grouping.

    Collapses UUIDs, strips trailing output-shaping suffixes (| head -N, | tail -N,
    2>&1 redirections). The point is to merge commands that differ only in how they
    truncate output — the underlying intent is the same.
    """
    cmd = cmd.strip()
    cmd = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "<UUID>", cmd
    )
    # Strip trailing output-shaping pipes/redirects (repeatedly).
    # Order matters: handle 2>&1 and 2>/dev/null before bare >, so the bare >
    # rule doesn't strip just `> /dev/null` from `foo 2> /dev/null`.
    prev = None
    while cmd != prev:
        prev = cmd
        cmd = re.sub(r"\s*\|\s*(?:head|tail)\s+-n\s*\d+\s*$", "", cmd)
        cmd = re.sub(r"\s*\|\s*(?:head|tail)\s+-\d+\s*$", "", cmd)
        cmd = re.sub(r"\s*2>&1\s*$", "", cmd)
        cmd = re.sub(r"\s*2>\s*/dev/null\s*$", "", cmd)
        cmd = re.sub(r"\s*>\s*/dev/null\s*$", "", cmd)
        cmd = cmd.strip()
    return cmd


def is_already_wrapped(cmd: str) -> bool:
    """True if the command is an invocation of a user script (absolute path / ./).

    These are already wrapped — recommending another wrapper is noise.
    """
    first = cmd.split(None, 1)[0] if cmd else ""
    if not first:
        return False
    return first.startswith(("/", "./", "~/")) or "/bin/" in first


def project_key_from_path(project_path: str) -> str:
    """Convert a filesystem path to Claude's project key format."""
    return project_path.rstrip("/").replace("/", "-")


def find_project_dir(project_path: str | None = None) -> Path | None:
    """Find the Claude project directory for a given or current working dir."""
    if project_path is None:
        project_path = os.getcwd()

    project_path = os.path.realpath(project_path)
    key = project_key_from_path(project_path)

    candidate = CLAUDE_DIR / key
    if candidate.is_dir():
        return candidate

    # Try parent directories (for subdirectory invocations)
    parts = project_path.split("/")
    for i in range(len(parts), 1, -1):
        partial = "/".join(parts[:i])
        key = project_key_from_path(partial)
        candidate = CLAUDE_DIR / key
        if candidate.is_dir():
            return candidate

    return None


def list_sessions(project_dir: Path) -> list[Path]:
    """List all JSONL session files in a project dir, newest first."""
    sessions = list(project_dir.glob("*.jsonl"))
    sessions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return sessions


def parse_session(path: Path) -> dict:
    """Parse a JSONL session file into structured data."""
    tool_uses = []
    session_meta = {}

    with open(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Grab session metadata from first record
            if not session_meta:
                for key in ("sessionId", "version", "gitBranch", "cwd"):
                    if key in obj:
                        session_meta[key] = obj[key]

            if obj.get("type") == "assistant":
                msg = obj.get("message", obj)
                ts = obj.get("timestamp")
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_uses.append({
                            "tool": block["name"],
                            "input": block.get("input", {}),
                            "id": block.get("id", ""),
                            "timestamp": ts,
                        })

    return {"meta": session_meta, "tool_uses": tool_uses, "path": str(path)}


def analyze_session(parsed: dict, threshold: int = 2) -> dict:
    """Analyze a parsed session for patterns."""
    tool_uses = parsed["tool_uses"]

    # --- Tool usage counts ---
    tool_counts = Counter(tu["tool"] for tu in tool_uses)

    # --- Bash command analysis ---
    bash_commands = []
    for tu in tool_uses:
        if tu["tool"] == "Bash":
            cmd = tu["input"].get("command", "")
            bash_commands.append(cmd)

    bash_normalized = [normalize_cmd(c) for c in bash_commands]
    bash_counts = Counter(bash_normalized)
    repeated_bash = {
        cmd: count for cmd, count in bash_counts.most_common()
        if count >= threshold
    }

    # --- Read file analysis ---
    read_files = []
    for tu in tool_uses:
        if tu["tool"] == "Read":
            fp = tu["input"].get("file_path", "")
            read_files.append(fp)

    read_counts = Counter(read_files)
    repeated_reads = {
        fp: count for fp, count in read_counts.most_common()
        if count >= threshold
    }

    # --- Grep pattern analysis ---
    grep_patterns = []
    for tu in tool_uses:
        if tu["tool"] == "Grep":
            pat = tu["input"].get("pattern", "")
            grep_patterns.append(pat)

    grep_counts = Counter(grep_patterns)
    repeated_greps = {
        pat: count for pat, count in grep_counts.most_common()
        if count >= threshold
    }

    # --- Glob pattern analysis ---
    glob_patterns = []
    for tu in tool_uses:
        if tu["tool"] == "Glob":
            pat = tu["input"].get("pattern", "")
            glob_patterns.append(pat)

    glob_counts = Counter(glob_patterns)
    repeated_globs = {
        pat: count for pat, count in glob_counts.most_common()
        if count >= threshold
    }

    # --- Edit file analysis ---
    edit_files = []
    for tu in tool_uses:
        if tu["tool"] == "Edit":
            fp = tu["input"].get("file_path", "")
            edit_files.append(fp)

    edit_counts = Counter(edit_files)

    # --- Write file analysis ---
    write_files = []
    for tu in tool_uses:
        if tu["tool"] == "Write":
            fp = tu["input"].get("file_path", "")
            write_files.append(fp)

    # --- Exploratory sequence detection ---
    # Find clusters of search tools (Grep, Glob, Read) without intervening edits
    exploration_runs = []
    current_run = []
    for tu in tool_uses:
        if tu["tool"] in ("Grep", "Glob", "Read"):
            current_run.append(tu)
        elif tu["tool"] in ("Edit", "Write", "Bash"):
            if len(current_run) >= 3:
                exploration_runs.append(current_run)
            current_run = []
    if len(current_run) >= 3:
        exploration_runs.append(current_run)

    # --- Files touched (read + edited + written) ---
    files_read_only = set(read_files) - set(edit_files) - set(write_files)
    files_modified = set(edit_files) | set(write_files)

    # --- Workflow-shape detection (skill candidate signal) ---
    # A session is "workflow-shaped" when it shows multi-step exploration AND
    # multi-file edits AND tool diversity. Pure repeat-the-same-command sessions
    # are wrapper-shaped; pure read sessions are docs-shaped; this intermediate
    # shape — explore→search→read→edit→verify across several files — is what
    # benefits from being packaged as a Skill (a model-invokable workflow).
    distinct_tools = len(tool_counts)
    exploration_lengths = [len(r) for r in exploration_runs]
    avg_exploration = sum(exploration_lengths) / len(exploration_lengths) if exploration_lengths else 0
    has_workflow_shape = (
        len(tool_uses) >= 15
        and distinct_tools >= 4
        and len(exploration_runs) >= 2
        and len(files_modified) >= 2
    )

    return {
        "meta": parsed["meta"],
        "path": parsed["path"],
        "total_tool_uses": len(tool_uses),
        "tool_counts": dict(tool_counts.most_common()),
        "repeated_bash": repeated_bash,
        "repeated_reads": repeated_reads,
        "repeated_greps": repeated_greps,
        "repeated_globs": repeated_globs,
        "all_bash": bash_commands,
        "all_bash_normalized": bash_normalized,
        "all_greps": grep_patterns,
        "all_reads": read_files,
        "edit_counts": dict(edit_counts.most_common()),
        "files_read_only": sorted(files_read_only),
        "files_modified": sorted(files_modified),
        "exploration_runs": len(exploration_runs),
        "exploration_lengths": exploration_lengths,
        "avg_exploration_length": avg_exploration,
        "distinct_tools": distinct_tools,
        "has_workflow_shape": has_workflow_shape,
        "unique_bash": len(set(bash_normalized)),
        "unique_reads": len(set(read_files)),
        "unique_greps": len(set(grep_patterns)),
    }


# --- Recommendation engine ---

def recommend_from_analysis(analysis: dict) -> list[dict]:
    """Apply single-session heuristic rules. Returns sorted recommendations."""
    recs = []
    cwd = analysis["meta"].get("cwd", "")
    tool_counts = analysis["tool_counts"]
    reads = tool_counts.get("Read", 0)
    edits = tool_counts.get("Edit", 0) + tool_counts.get("Write", 0)

    # R1: high-cost bash, even count >= 2 is too many
    for cmd, count in analysis["repeated_bash"].items():
        if is_high_cost_bash(cmd):
            recs.append({
                "kind": "hook",
                "target": cmd,
                "reason": "high-blast-radius command — even one mistake is expensive",
                "evidence": f"{count}x in this session",
                "weight": count * 100,
                "priority": 1,
                "rule": "R1",
            })

    # R2: same bash command repeated a lot, not high-cost → wrapper candidate
    for cmd, count in analysis["repeated_bash"].items():
        if count >= 4 and not is_high_cost_bash(cmd) and not is_already_wrapped(cmd):
            recs.append({
                "kind": "wrapper",
                "target": cmd,
                "reason": "same command issued repeatedly in one session",
                "evidence": f"{count}x in this session",
                "weight": count,
                "priority": 2,
                "rule": "R2",
            })

    # R3: same file read >= 3 → CLAUDE.md pointer
    for fp, count in analysis["repeated_reads"].items():
        if count >= 3:
            scope = "in-project" if cwd and fp.startswith(cwd) else "external"
            recs.append({
                "kind": "claudemd",
                "target": fp,
                "reason": (
                    "load-bearing file repeatedly opened — "
                    "reference its purpose in CLAUDE.md"
                    if scope == "in-project"
                    else "external file repeatedly loaded — "
                    "context may be too large or duplicated"
                ),
                "evidence": f"{count}x in this session",
                "weight": count,
                "priority": 2,
                "rule": "R3",
            })

    # R4: many exploration runs → docs gap
    if analysis["exploration_runs"] >= 3:
        recs.append({
            "kind": "docs",
            "target": "codebase navigation",
            "reason": "multiple search/read sequences without edits suggest missing docs or codebase map",
            "evidence": (
                f"{analysis['exploration_runs']} exploration runs, "
                f"lengths={analysis['exploration_lengths']}"
            ),
            "weight": analysis["exploration_runs"],
            "priority": 2,
            "rule": "R4",
        })

    # R5: read-heavy session
    if reads >= 10 and edits >= 1 and reads / edits >= 5:
        recs.append({
            "kind": "docs",
            "target": "codebase orientation",
            "reason": "high read-to-edit ratio — exploration heavy",
            "evidence": f"{reads} reads vs {edits} edits ({reads/edits:.1f}:1)",
            "weight": int(reads / max(edits, 1)),
            "priority": 3,
            "rule": "R5",
        })

    # R6: grep thrashing
    unique_greps = analysis["unique_greps"]
    if unique_greps >= 8 and edits <= 2:
        recs.append({
            "kind": "codebase-map",
            "target": "search strategy",
            "reason": "many distinct grep patterns with few edits — symbols may be hard to locate",
            "evidence": f"{unique_greps} unique patterns, {edits} edits",
            "weight": unique_greps,
            "priority": 2,
            "rule": "R6",
        })

    # R10: workflow shape — multi-step exploration with multi-file edits and
    # tool diversity. The session looks like a packaged workflow: candidate
    # for becoming a Skill (model-invokable, on-demand) rather than a hook
    # (deterministic) or CLAUDE.md addition (always loaded).
    if analysis.get("has_workflow_shape"):
        n_files = len(analysis["files_modified"])
        runs = analysis["exploration_runs"]
        avg_len = analysis.get("avg_exploration_length", 0)
        recs.append({
            "kind": "skill",
            "target": "multi-step workflow",
            "reason": "session shows workflow shape (multi-step exploration + multi-file edits + tool diversity) — a recurring shape like this is a Skill candidate, not a wrapper or CLAUDE.md rule",
            "evidence": (
                f"{runs} exploration runs (avg len {avg_len:.1f}), "
                f"{n_files} files modified, {analysis['distinct_tools']} distinct tools"
            ),
            "weight": runs * 100 + n_files * 10 + int(avg_len),
            "priority": 3,
            "rule": "R10",
        })

    return sorted(recs, key=lambda r: (r["priority"], -r.get("weight", 0)))


def aggregate_analyses(analyses: list[dict]) -> dict:
    """Merge analyses across sessions. Counts how many sessions each pattern appears in."""
    bash_sessions = defaultdict(int)
    bash_total = defaultdict(int)
    read_sessions = defaultdict(int)
    read_total = defaultdict(int)
    grep_sessions = defaultdict(int)
    grep_total = defaultdict(int)

    for a in analyses:
        seen = set()
        for cmd in a.get("all_bash_normalized", []):
            bash_total[cmd] += 1
            if cmd not in seen:
                bash_sessions[cmd] += 1
                seen.add(cmd)

        seen = set()
        for fp in a.get("all_reads", []):
            read_total[fp] += 1
            if fp not in seen:
                read_sessions[fp] += 1
                seen.add(fp)

        seen = set()
        for pat in a.get("all_greps", []):
            grep_total[pat] += 1
            if pat not in seen:
                grep_sessions[pat] += 1
                seen.add(pat)

    workflow_sessions = sum(1 for a in analyses if a.get("has_workflow_shape"))

    return {
        "n_sessions": len(analyses),
        "workflow_sessions": workflow_sessions,
        "bash": {
            cmd: {"sessions": bash_sessions[cmd], "total": bash_total[cmd]}
            for cmd in bash_total
        },
        "reads": {
            fp: {"sessions": read_sessions[fp], "total": read_total[fp]}
            for fp in read_total
        },
        "greps": {
            pat: {"sessions": grep_sessions[pat], "total": grep_total[pat]}
            for pat in grep_total
        },
    }


def recommend_from_aggregate(agg: dict, min_sessions: int | None = None) -> list[dict]:
    """Apply cross-session heuristic rules.

    `min_sessions`: how many sessions a pattern must appear in. If None, auto-scales:
    n=2..4 → 2, n=10 → 3, n=100 → 10, n=400+ → 30.
    """
    recs = []
    n = agg["n_sessions"]
    if n < 2:
        return recs

    if min_sessions is None:
        threshold_sessions = max(2, min(n // 4, 30))
    else:
        threshold_sessions = max(2, min_sessions)

    # R7: bash recurring across sessions
    for cmd, info in agg["bash"].items():
        if info["sessions"] < threshold_sessions:
            continue
        evidence = f"{info['total']}x across {info['sessions']}/{n} sessions"
        weight = info["sessions"] * 1000 + info["total"]
        if is_high_cost_bash(cmd):
            recs.append({
                "kind": "hook",
                "target": cmd,
                "reason": "high-blast-radius command recurring across sessions",
                "evidence": evidence,
                "weight": weight + 100000,
                "priority": 1,
                "rule": "R7-hook",
            })
        elif not is_already_wrapped(cmd):
            recs.append({
                "kind": "wrapper",
                "target": cmd,
                "reason": "command recurs across sessions — wrapper or alias would amortize",
                "evidence": evidence,
                "weight": weight,
                "priority": 2,
                "rule": "R7-wrapper",
            })

    # R8: file reads recurring across sessions
    for fp, info in agg["reads"].items():
        if info["sessions"] < threshold_sessions:
            continue
        recs.append({
            "kind": "claudemd",
            "target": fp,
            "reason": "file repeatedly loaded as context across sessions",
            "evidence": f"{info['total']}x across {info['sessions']}/{n} sessions",
            "weight": info["sessions"] * 1000 + info["total"],
            "priority": 2,
            "rule": "R8",
        })

    # R9: grep patterns recurring across sessions
    for pat, info in agg["greps"].items():
        if info["sessions"] < threshold_sessions:
            continue
        recs.append({
            "kind": "claudemd",
            "target": f"grep: {pat}",
            "reason": "same search recurring across sessions — point at where it lives",
            "evidence": f"{info['total']}x across {info['sessions']}/{n} sessions",
            "weight": info["sessions"] * 1000 + info["total"],
            "priority": 2,
            "rule": "R9",
        })

    # R10-cross: workflow-shaped sessions recurring across the project — the
    # user keeps doing the same multi-step shape. Promote to a project-level
    # Skill so the model can recognize and follow the pattern explicitly.
    workflow_n = agg.get("workflow_sessions", 0)
    if workflow_n >= max(2, threshold_sessions):
        recs.append({
            "kind": "skill",
            "target": "recurring project workflow",
            "reason": "multiple sessions show the same workflow shape — package the steps as a project Skill so the model invokes the right sequence consistently",
            "evidence": f"{workflow_n}/{n} sessions are workflow-shaped",
            "weight": workflow_n * 1000,
            "priority": 2,
            "rule": "R10-cross",
        })

    return sorted(recs, key=lambda r: (r["priority"], -r.get("weight", 0)))


def cross_project_patterns(per_project_aggs: dict, min_sessions_per_project: int = 1) -> dict:
    """Pivot per-project aggregates into a pattern-keyed cross-project view.

    A pattern counts toward a project if it appears in >= min_sessions_per_project
    sessions of that project. Default of 1 means "the pattern was used at all in
    the project" — appropriate when many projects have ≤1 session each (e.g. branch
    worktrees). The cross-project breadth threshold (min_projects) is the real filter.
    """
    bash_proj = defaultdict(set)
    bash_total = defaultdict(int)
    read_proj = defaultdict(set)
    read_total = defaultdict(int)
    grep_proj = defaultdict(set)
    grep_total = defaultdict(int)

    for proj, agg in per_project_aggs.items():
        for cmd, info in agg["bash"].items():
            if info["sessions"] >= min_sessions_per_project:
                bash_proj[cmd].add(proj)
                bash_total[cmd] += info["total"]
        for fp, info in agg["reads"].items():
            if info["sessions"] >= min_sessions_per_project:
                read_proj[fp].add(proj)
                read_total[fp] += info["total"]
        for pat, info in agg["greps"].items():
            if info["sessions"] >= min_sessions_per_project:
                grep_proj[pat].add(proj)
                grep_total[pat] += info["total"]

    workflow_projects = [
        proj for proj, agg in per_project_aggs.items()
        if agg.get("workflow_sessions", 0) >= 1
    ]

    return {
        "n_projects": len(per_project_aggs),
        "workflow_projects": sorted(workflow_projects),
        "bash": {
            cmd: {"projects": sorted(bash_proj[cmd]), "total": bash_total[cmd]}
            for cmd in bash_proj
        },
        "reads": {
            fp: {"projects": sorted(read_proj[fp]), "total": read_total[fp]}
            for fp in read_proj
        },
        "greps": {
            pat: {"projects": sorted(grep_proj[pat]), "total": grep_total[pat]}
            for pat in grep_proj
        },
    }


def recommend_cross_project(cross: dict, min_projects: int | None = None) -> list[dict]:
    """Apply cross-project rules X1-X4.

    Patterns recurring across distinct projects suggest *global* changes
    (~/.claude/CLAUDE.md, global wrapper, rtk addition) rather than per-project ones.
    """
    recs = []
    n = cross["n_projects"]
    if n < 2:
        return recs

    if min_projects is None:
        # Auto: floor of 5 distinct projects, scaling up to 15 for huge corpora
        min_projects = max(5, min(n // 20, 15))

    for cmd, info in cross["bash"].items():
        n_proj = len(info["projects"])
        if n_proj < min_projects:
            continue
        evidence = f"{info['total']}x across {n_proj}/{n} projects"
        weight = n_proj * 1000 + info["total"]
        if is_high_cost_bash(cmd):
            recs.append({
                "kind": "hook",
                "scope": "global",
                "target": cmd,
                "reason": "high-blast-radius command recurring across multiple projects",
                "evidence": evidence,
                "weight": weight + 100000,
                "priority": 1,
                "rule": "X1",
            })
        elif not is_already_wrapped(cmd):
            recs.append({
                "kind": "wrapper",
                "scope": "global",
                "target": cmd,
                "reason": "command recurs across many projects — global alias / rtk addition",
                "evidence": evidence,
                "weight": weight,
                "priority": 2,
                "rule": "X2",
            })

    for fp, info in cross["reads"].items():
        n_proj = len(info["projects"])
        if n_proj < min_projects:
            continue
        recs.append({
            "kind": "claudemd",
            "scope": "global",
            "target": fp,
            "reason": "shared/external file repeatedly read across projects",
            "evidence": f"{info['total']}x across {n_proj}/{n} projects",
            "weight": n_proj * 1000 + info["total"],
            "priority": 2,
            "rule": "X3",
        })

    for pat, info in cross["greps"].items():
        n_proj = len(info["projects"])
        if n_proj < min_projects:
            continue
        recs.append({
            "kind": "claudemd",
            "scope": "global",
            "target": f"grep: {pat}",
            "reason": "same search recurring across many projects",
            "evidence": f"{info['total']}x across {n_proj}/{n} projects",
            "weight": n_proj * 1000 + info["total"],
            "priority": 2,
            "rule": "X4",
        })

    # X5: workflow-shaped sessions recurring in many distinct projects — the
    # same multi-step shape across projects argues for a *global* Skill (lives
    # in ~/.claude/skills/ or a personal group in your skills library).
    workflow_projs = cross.get("workflow_projects", [])
    if len(workflow_projs) >= min_projects:
        recs.append({
            "kind": "skill",
            "scope": "global",
            "target": "cross-project workflow",
            "reason": "workflow-shaped sessions recur across many projects — package as a global Skill (personal group / ~/.claude/skills)",
            "evidence": f"{len(workflow_projs)}/{n} projects have workflow-shaped sessions",
            "weight": len(workflow_projs) * 1000,
            "priority": 2,
            "rule": "X5",
        })

    return sorted(recs, key=lambda r: (r["priority"], -r.get("weight", 0)))


def format_recommendations(recs: list[dict], header: str = "", top: int = 10, heading_level: int = 2) -> str:
    """Render recommendations as terse markdown.

    `top`: cap the number of recommendations *per kind* (0 = no cap).
    `heading_level`: markdown heading level for per-kind sections (default 2 = ##).
    """
    h = "#" * heading_level
    if not recs:
        return (header + "\n\n_No actionable recommendations._\n") if header else "_No actionable recommendations._\n"

    lines = []
    if header:
        lines.append(header)
        lines.append("")

    by_kind = defaultdict(list)
    for r in recs:
        by_kind[r["kind"]].append(r)

    kind_titles = {
        "hook": "Hooks (priority 1)",
        "wrapper": "Wrappers / aliases",
        "skill": "Skills (model-invokable workflows)",
        "claudemd": "CLAUDE.md additions",
        "docs": "Documentation",
        "codebase-map": "Codebase map",
    }
    kind_order = ["hook", "wrapper", "skill", "claudemd", "codebase-map", "docs"]

    for kind in kind_order:
        items = by_kind.get(kind)
        if not items:
            continue
        truncated = top and len(items) > top
        shown = items[:top] if top else items
        suffix = f" (top {top} of {len(items)})" if truncated else ""
        lines.append(f"{h} {kind_titles.get(kind, kind)}{suffix}")
        lines.append("")
        for r in shown:
            target = r["target"]
            if len(target) > 140:
                target = target[:137] + "..."
            lines.append(f"- `{target}` — {r['evidence']} _[{r['rule']}]_")
            lines.append(f"  - {r['reason']}")
        lines.append("")

    return "\n".join(lines)


def shorten_path(p: str, cwd: str = "") -> str:
    """Shorten a path relative to cwd or home."""
    if cwd and p.startswith(cwd):
        return p[len(cwd):].lstrip("/")
    home = str(Path.home())
    if p.startswith(home):
        return "~" + p[len(home):]
    return p


def format_markdown(analysis: dict) -> str:
    """Format analysis results as markdown."""
    meta = analysis["meta"]
    cwd = meta.get("cwd", "")
    lines = []

    branch = meta.get("gitBranch", "unknown")
    session_id = meta.get("sessionId", "unknown")
    lines.append(f"# Session Analysis: {branch}")
    lines.append(f"")
    lines.append(f"**Session**: `{session_id}`")
    lines.append(f"**File**: `{shorten_path(analysis['path'])}`")
    lines.append(f"**Total tool uses**: {analysis['total_tool_uses']}")
    lines.append(f"")

    # Tool usage table
    lines.append("## Tool Usage")
    lines.append("")
    lines.append("| Tool | Count | Unique |")
    lines.append("|------|-------|--------|")
    for tool, count in analysis["tool_counts"].items():
        unique = {
            "Bash": analysis["unique_bash"],
            "Read": analysis["unique_reads"],
            "Grep": analysis["unique_greps"],
        }.get(tool, "")
        lines.append(f"| {tool} | {count} | {unique} |")
    lines.append("")

    # Repeated bash commands
    if analysis["repeated_bash"]:
        lines.append("## Repeated Bash Commands")
        lines.append("")
        lines.append("```")
        for cmd, count in sorted(analysis["repeated_bash"].items(), key=lambda x: -x[1]):
            short = cmd[:120] + ("..." if len(cmd) > 120 else "")
            lines.append(f"{count}x | {short}")
        lines.append("```")
        lines.append("")

    # Repeated file reads
    if analysis["repeated_reads"]:
        lines.append("## Repeatedly Read Files")
        lines.append("")
        lines.append("```")
        for fp, count in sorted(analysis["repeated_reads"].items(), key=lambda x: -x[1]):
            lines.append(f"{count}x | {shorten_path(fp, cwd)}")
        lines.append("```")
        lines.append("")

    # Repeated grep patterns
    if analysis["repeated_greps"]:
        lines.append("## Repeated Grep Patterns")
        lines.append("")
        lines.append("```")
        for pat, count in sorted(analysis["repeated_greps"].items(), key=lambda x: -x[1]):
            lines.append(f"{count}x | {pat}")
        lines.append("```")
        lines.append("")

    # All unique grep patterns (even if not repeated — useful for understanding exploration)
    if analysis["all_greps"]:
        lines.append("## All Grep Patterns")
        lines.append("")
        lines.append("```")
        seen = set()
        for pat in analysis["all_greps"]:
            if pat not in seen:
                seen.add(pat)
                lines.append(f"  {pat}")
        lines.append("```")
        lines.append("")

    # Exploration runs
    if analysis["exploration_runs"] > 0:
        lines.append("## Exploration Sequences")
        lines.append("")
        lines.append(
            f"Found **{analysis['exploration_runs']}** exploration run(s) "
            f"(3+ consecutive search/read tools without edits)."
        )
        lines.append(f"Lengths: {analysis['exploration_lengths']}")
        lines.append("")

    # Files summary
    if analysis["files_modified"]:
        lines.append("## Files Modified")
        lines.append("")
        for fp in analysis["files_modified"]:
            count = analysis["edit_counts"].get(fp, 0)
            suffix = f" ({count} edits)" if count > 1 else ""
            lines.append(f"- `{shorten_path(fp, cwd)}`{suffix}")
        lines.append("")

    if analysis["files_read_only"]:
        lines.append("## Files Read (not modified)")
        lines.append("")
        for fp in analysis["files_read_only"]:
            lines.append(f"- `{shorten_path(fp, cwd)}`")
        lines.append("")

    return "\n".join(lines)


def format_json(analysis: dict) -> str:
    """Format analysis results as JSON."""
    return json.dumps(analysis, indent=2, default=str)


def main():
    parser = argparse.ArgumentParser(
        prog="dejavu",
        description="Claude Code session autopsy — find repeated patterns and tooling gaps.",
    )
    parser.add_argument(
        "session_file",
        nargs="?",
        help="Path to a specific .jsonl session file",
    )
    parser.add_argument(
        "--project", "-p",
        help="Project directory path (default: current directory)",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Analyze all sessions for the project",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available sessions",
    )
    parser.add_argument(
        "--recent", "-r",
        nargs="?",
        const=5,
        type=int,
        metavar="N",
        help="Analyze N most recent sessions (default: 5)",
    )
    parser.add_argument(
        "--threshold", "-t",
        type=int,
        default=2,
        help="Minimum repeat count to report (default: 2)",
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--recommend",
        action="store_true",
        help="Emit only actionable recommendations (suppresses raw counts)",
    )
    parser.add_argument(
        "--self-eval",
        action="store_true",
        help="Cross-project mode: aggregate all sessions across all projects in ~/.claude/projects",
    )
    parser.add_argument(
        "--per-project-cap",
        type=int,
        default=20,
        help="In --self-eval, cap most-recent sessions per project (default: 20)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Cap recommendations per kind (default: 10; 0 = no cap)",
    )
    parser.add_argument(
        "--cross-min-projects",
        type=int,
        default=None,
        help="In --self-eval, override min projects threshold for cross-project rules",
    )

    args = parser.parse_args()

    # --- Self-eval mode (per-project + cross-project) ---
    if args.self_eval:
        if not CLAUDE_DIR.is_dir():
            print(f"No projects directory: {CLAUDE_DIR}", file=sys.stderr)
            sys.exit(1)

        # Group analyses by project
        project_analyses: dict[str, list[dict]] = {}
        total_sessions = 0
        for pd in sorted(CLAUDE_DIR.iterdir()):
            if not pd.is_dir():
                continue
            sessions = list_sessions(pd)
            if not sessions:
                continue
            picked = sessions[: args.per_project_cap]
            project_analyses[pd.name] = [
                analyze_session(parse_session(s), args.threshold) for s in picked
            ]
            total_sessions += len(picked)

        if not project_analyses:
            print("No sessions found across projects.", file=sys.stderr)
            sys.exit(1)

        # Per-project aggregates and recommendations
        per_project_aggs = {
            proj: aggregate_analyses(analyses)
            for proj, analyses in project_analyses.items()
        }
        per_project_recs = {
            proj: recommend_from_aggregate(agg)
            for proj, agg in per_project_aggs.items()
        }

        # Cross-project rollup
        cross = cross_project_patterns(per_project_aggs)
        cross_recs = recommend_cross_project(cross, min_projects=args.cross_min_projects)

        # Per-project highlights: top rec from each kind for each project.
        # Surfaces hooks/wrappers/etc. that would otherwise be hidden behind a
        # stronger claudemd rec from the same project (R8 file-reads tend to
        # dominate by raw weight).
        highlights = []
        for proj, recs in per_project_recs.items():
            seen_kinds = set()
            for rec in recs:  # already sorted: priority asc, weight desc
                if rec["kind"] in seen_kinds:
                    continue
                seen_kinds.add(rec["kind"])
                highlights.append((proj, rec))
        highlights.sort(key=lambda t: (t[1].get("priority", 99), -t[1].get("weight", 0)))
        highlight_cap = args.top if args.top else len(highlights)

        if args.json:
            print(json.dumps({
                "n_sessions_analyzed": total_sessions,
                "n_projects": len(project_analyses),
                "cross_project_recommendations": cross_recs,
                "per_project_highlights": [
                    {"project": p, "top_recommendation": r}
                    for p, r in highlights[:highlight_cap]
                ],
                "per_project_recommendations": {
                    proj: recs for proj, recs in per_project_recs.items() if recs
                },
            }, indent=2, default=str))
            return

        header = (
            f"# dejavu --self-eval\n\n"
            f"**Projects analyzed**: {len(project_analyses)}  "
            f"**Sessions analyzed**: {total_sessions}  "
            f"(cap {args.per_project_cap}/project)\n"
        )
        out = [header, "## Cross-project patterns", ""]
        if cross_recs:
            out.append(format_recommendations(cross_recs, "", top=args.top, heading_level=3))
        else:
            out.append("_No cross-project patterns met threshold._\n")

        out.append("## Per-project highlights")
        out.append("")
        if not highlights:
            out.append("_No per-project recommendations._")
        else:
            shown = highlights[:highlight_cap]
            for proj, rec in shown:
                target = rec["target"]
                if len(target) > 100:
                    target = target[:97] + "..."
                out.append(
                    f"- **{proj}** → {rec['kind']}: `{target}` "
                    f"({rec['evidence']}) _[{rec['rule']}]_"
                )
            if len(highlights) > highlight_cap:
                out.append(f"")
                out.append(f"_(showing top {highlight_cap} of {len(highlights)})_")
        out.append("")
        print("\n".join(out))
        return

    # Direct file mode
    if args.session_file:
        path = Path(args.session_file)
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        parsed = parse_session(path)
        analysis = analyze_session(parsed, args.threshold)
        if args.recommend:
            recs = recommend_from_analysis(analysis)
            if args.json:
                print(json.dumps(recs, indent=2, default=str))
            else:
                header = f"# Recommendations: `{shorten_path(str(path))}`"
                print(format_recommendations(recs, header, top=args.top))
            return
        print(format_json(analysis) if args.json else format_markdown(analysis))
        return

    # Project mode
    project_dir = find_project_dir(args.project)
    if not project_dir:
        target = args.project or os.getcwd()
        print(f"No Claude sessions found for: {target}", file=sys.stderr)
        print(f"Looked in: {CLAUDE_DIR}", file=sys.stderr)
        sys.exit(1)

    sessions = list_sessions(project_dir)
    if not sessions:
        print(f"No session files found in: {project_dir}", file=sys.stderr)
        sys.exit(1)

    # List mode
    if args.list:
        print(f"Sessions for {project_dir.name}:\n")
        for i, s in enumerate(sessions):
            mtime = datetime.fromtimestamp(s.stat().st_mtime)
            size_kb = s.stat().st_size / 1024
            # Quick parse for branch name
            branch = "?"
            with open(s) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        if "gitBranch" in obj:
                            branch = obj["gitBranch"]
                            break
                    except json.JSONDecodeError:
                        continue
            marker = " (latest)" if i == 0 else ""
            print(f"  {mtime:%Y-%m-%d %H:%M}  {size_kb:6.0f}K  {branch}{marker}")
            print(f"    {s.name}")
        return

    # Determine which sessions to analyze
    if args.all:
        targets = sessions
    elif args.recent:
        targets = sessions[: args.recent]
    else:
        targets = [sessions[0]]

    # --recommend with multiple sessions: aggregate then recommend
    if args.recommend and len(targets) > 1:
        analyses = [analyze_session(parse_session(s), args.threshold) for s in targets]
        agg = aggregate_analyses(analyses)
        cross_recs = recommend_from_aggregate(agg)
        if args.json:
            print(json.dumps(cross_recs, indent=2, default=str))
            return
        header = (
            f"# Recommendations: {project_dir.name}\n\n"
            f"**Sessions analyzed**: {len(analyses)}\n"
        )
        print(format_recommendations(cross_recs, header, top=args.top))
        return

    # Analyze
    for i, session_path in enumerate(targets):
        if i > 0:
            print("\n---\n")
        parsed = parse_session(session_path)
        analysis = analyze_session(parsed, args.threshold)
        if args.recommend:
            recs = recommend_from_analysis(analysis)
            if args.json:
                print(json.dumps(recs, indent=2, default=str))
            else:
                header = f"# Recommendations: `{shorten_path(str(session_path))}`"
                print(format_recommendations(recs, header, top=args.top))
            continue
        print(format_json(analysis) if args.json else format_markdown(analysis))


if __name__ == "__main__":
    main()
