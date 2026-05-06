# dejavu

Claude Code session autopsy tool. Parses `~/.claude/projects/*/`*.jsonl* session
files to surface repeated commands, re-read files, and exploration patterns,
then recommends what to do about them: a CLAUDE.md addition, a hook, a wrapper
script, or a doc note.

Single-module Python 3, no runtime dependencies.

## Install

    pip install dejavu-claude

This installs the `dejavu` command. Or run from a checkout:

    git clone https://github.com/ahonnecke/dejavu
    cd dejavu
    python3 dejavu.py --help

## Usage

    dejavu                          # analyze latest session in current project
    dejavu <session.jsonl>          # analyze specific session file
    dejavu --project <path>         # analyze latest session for a project dir
    dejavu --all                    # all sessions in current project
    dejavu --recent N               # N most recent sessions (default 5)
    dejavu --list                   # list available sessions

    dejavu --recommend              # emit only actionable recommendations
    dejavu --all --recommend        # aggregate recs across a project's sessions
    dejavu --self-eval              # cross-project rollup over ~/.claude/projects/

    dejavu --json                   # JSON output

Useful flags:

    --threshold N            min repeat count for raw output (default 2)
    --top N                  cap recommendations per kind (default 10, 0 = no cap)
    --per-project-cap N      in --self-eval, cap sessions per project (default 20)
    --cross-min-projects N   in --self-eval, override breadth threshold for X-rules

## Recommendation kinds

- **hook** — high-blast-radius command worth gating in `.claude/settings.json`
- **wrapper** — repeated raw command that wants an alias / script / rtk addition
- **skill** — recurring multi-step workflow worth packaging as a model-invokable Skill (`SKILL.md`) instead of a wrapper or CLAUDE.md rule
- **claudemd** — file or symbol that should be referenced once in CLAUDE.md
- **docs / codebase-map** — exploration patterns suggesting missing project docs

### When to choose `skill` vs `wrapper` vs `claudemd` vs `hook`

| Pattern shape | Right tool | Why |
|---|---|---|
| Same `Bash` command repeated | wrapper | shell-level efficiency; no model judgment needed |
| Same file read or grep recurring | claudemd | one-line pointer is cheaper than re-derivation |
| High-blast-radius command (any frequency) | hook | deterministic, mechanical enforcement |
| Multi-step sequence (read→search→edit→verify) recurring | **skill** | needs judgment between steps; loaded on demand, not always |
| Soft preference, violations cheap | claudemd | ~50-line attention budget |
| Soft preference, violations expensive | hook | promote when CLAUDE.md is ignored |

## Rules

### Single-session (one `.jsonl`)

| ID  | Trigger                                                       | Recommendation       |
|-----|---------------------------------------------------------------|----------------------|
| R1  | High-cost bash pattern, count ≥2                              | hook (priority 1)    |
| R2  | Same bash command, count ≥4, not high-cost                    | wrapper              |
| R3  | Same file read ≥3                                             | CLAUDE.md pointer    |
| R4  | ≥3 exploration runs (≥3 search/read tools without an edit)    | docs / codebase map  |
| R5  | Reads ≥10 and reads:edits ≥5                                  | docs (orientation)   |
| R6  | Unique greps ≥8 and edits ≤2                                  | codebase map         |
| R10 | ≥15 tool uses, ≥4 distinct tool types, ≥2 exploration runs, ≥2 files modified | **skill** (workflow shape) |

### Cross-session (`--all --recommend`)

| ID        | Trigger                                          | Recommendation                     |
|-----------|--------------------------------------------------|------------------------------------|
| R7        | Bash command in ≥auto-threshold of sessions      | wrapper (or hook if high-cost)     |
| R8        | File read in ≥auto-threshold of sessions         | CLAUDE.md pointer                  |
| R9        | Grep pattern in ≥auto-threshold of sessions      | CLAUDE.md pointer                  |
| R10-cross | ≥auto-threshold of sessions are workflow-shaped  | **skill** (project-level)          |

Auto-threshold: `max(2, min(n_sessions // 4, 30))`.

### Cross-project (`--self-eval`)

| ID | Trigger                                                | Recommendation                  |
|----|--------------------------------------------------------|---------------------------------|
| X1 | High-cost bash recurring across distinct projects      | global hook                     |
| X2 | Bash command recurring across distinct projects        | global wrapper / rtk addition   |
| X3 | File read across distinct projects                     | global (`~/.claude`) CLAUDE.md  |
| X4 | Grep pattern across distinct projects                  | global CLAUDE.md                |
| X5 | Workflow-shaped sessions in ≥min-projects projects     | **global skill** (personal group / `~/.claude/skills/`) |

Auto-threshold: pattern in `max(5, min(n_projects // 20, 15))` distinct projects.

### High-cost bash patterns (R1, X1)

These trigger a hook recommendation regardless of frequency:

    rm -rf?, git push --force/-f, git reset --hard, git checkout .,
    git clean -f, git branch -D, curl ... | sh, wget ... | sh,
    --no-verify, chmod -R, dropdb, DROP TABLE, TRUNCATE, DELETE FROM

For compound commands (`make clean && rm -rf /tmp/build && echo done`), the
hook recommendation targets just the dangerous fragment (`rm -rf /tmp/build`),
not the whole compound — actionable instead of 140 chars of soup.

### Already-wrapped detection

Wrapper recommendations are skipped for:

- **Path invocations** — `/usr/bin/foo`, `./run.sh`, `~/bin/x`, anything in `/bin/`
- **Project task runners** — `make X`, `just X`, `rake X`
- **Package script targets** — `npm run X`, `pnpm run X`, `yarn run X`, `bun run X`
  (bare `npm install` is *not* filtered — it's still a legit wrapper candidate)
- **Bare orientation commands** — `ls`, `pwd`, `cd /tmp`, `which foo`, `echo X`
  when there's no shell separator. Compounds like `cd ~/.doom.d && git log` are
  still wrapper candidates.

## Output normalization

Bash commands are normalized for grouping before counting:

- UUIDs collapsed to `<UUID>`
- Trailing output-shaping suffixes stripped: `| head -N`, `| tail -N`, `2>&1`,
  `> /dev/null`, `2> /dev/null`. So `make test 2>&1 | tail -10` and
  `make test 2>&1 | tail -25` count as one pattern.

## Example: cross-project rollup

    $ dejavu --self-eval --per-project-cap 10

    # dejavu --self-eval

    **Projects analyzed**: 220  **Sessions analyzed**: 328  (cap 10/project)

    ## Cross-project patterns

    ### Wrappers / aliases

    - `git push -u upstream HEAD` — 88x across 57/220 projects [X2]
      - command recurs across many projects — global alias / rtk addition
    - `pnpm install` — 37x across 36/220 projects [X2]
    - `git log --oneline -5` — 30x across 17/220 projects [X2]

    ## Per-project highlights

    - **slopsmith-plugin-notedetect** → claudemd: `Makefile` (18x across 5/9 sessions) [R8]
    - **bitweight** → claudemd: `src/App.jsx` (97x across 2/3 sessions) [R8]
    - **rocksmith-tutor** → claudemd: `cli.py` (20x across 2/3 sessions) [R8]

## Design rationale

Right, "could CLAUDE.md solve it" is the wrong framing — it implies CLAUDE.md is free and hooks are the expensive escalation. Both have real costs. The honest question is which cost you're paying.

**CLAUDE.md costs:**

- **Token tax, every turn.** CLAUDE.md is prepended to context on every request in the session. A 500-line CLAUDE.md is 500 lines the model re-reads on every prompt — that's latency, dollars, and context window you can't use for actual code.
- **Attention dilution.** This is the bigger one. The model has finite attention. A CLAUDE.md with 40 rules treats them all as roughly equal priority, and the model's adherence to any single rule degrades as the list grows. Rule #37 about sleep gets weighted against rule #12 about import ordering and rule #23 about commit messages. The 5-rule CLAUDE.md is followed more reliably than the 40-rule one, even on the rules they share.
- **Probabilistic.** Even a single rule isn't 100%. The model can rationalize around it, forget it after a long task, or have it crowded out by a strong user instruction.
- **Invisible drift.** When the model violates a rule, you often don't notice until something breaks. No log, no alert.

**Hook costs:**

- **Latency** on the matched event (the 5–200ms we already covered).
- **Maintenance.** Hooks rot. Patterns get stale, false positives creep in, the script bitrots when Claude Code's input schema changes.
- **False positives are loud.** A bad hook blocks legitimate work and the user sees it immediately. (This is also a feature — failures are visible, unlike CLAUDE.md drift.)
- **Setup friction.** Each hook is a script + config + testing. Three hooks is a weekend; thirty is a part-time job.

**So the actual decision rule:**

| Situation | Preferred |
|---|---|
| Rule is soft preference, violations are cheap | CLAUDE.md |
| Rule is deterministic and pattern-matchable | Hook |
| Violation is expensive (data loss, secrets, prod) | Hook, regardless of frequency |
| Rule needs judgment (style, naming, "be concise") | CLAUDE.md — hooks can't do taste |
| You've put it in CLAUDE.md and it's still violated | Promote to hook |
| Rule fires constantly (every Edit) | Whichever is cheaper at that frequency — usually CLAUDE.md if it works |
| Rule fires rarely (only on `git push`) | Hook is basically free here |

**The balance, stated bluntly:** keep CLAUDE.md short (think: under ~50 lines for most projects). Treat it as a budget. If you're tempted to add rule #51, something on the list either belongs in a hook (deterministic enforcement) or doesn't belong anywhere (one-off, not worth the attention tax).

Hooks are not "always preferable." They're preferable when (a) the rule is mechanical and (b) violations matter enough to justify the maintenance. Most rules fail one or both tests and should stay in CLAUDE.md — or get cut entirely.

The trap is treating CLAUDE.md as the no-cost default. Every line in there competes with every other line for the model's attention. That's the real prompt inflation cost — not tokens, attention.

**summary**
this tool should ideally identify recurring patterns and recommend altering
CLAUDE.md, writing a hook, or building a utility/script/wrapper
