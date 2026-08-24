---
name: sessions
description: Recall a past Claude Code session from this repo. Lists or searches prior sessions for the current working directory and loads the selected one as full context. Use when the user asks about earlier work, prior decisions, or says they discussed something in a previous session.
---

# Recall a past session

Cache is pre-built by a hook. Never run --backfill or --incremental from here.

## Step 0 — freshness check

```bash
python3 ~/.claude/scripts/parse_sessions.py --status --cwd "$PWD"
```

Read the JSON. Cases:

- `running_here: true`
  Prepend this one-line notice to your response, then proceed normally
  with --list or --search:

    "⚠ Updating past session summaries for this repo — showing current
    data. If you load a session that's being refreshed, its summary may
    be missing the latest few messages. Try /sessions again in a minute
    for the fully-updated version."

  Do NOT block, do NOT ask, do NOT flag individual rows. Just show the
  notice once at the top and continue.

  If lock_age_seconds > 180, replace "in a minute" with "in a few minutes"
  and add on a new line:
    "(This has been running longer than expected — if /sessions is still
    unresponsive later, run: python3 ~/.claude/scripts/parse_sessions.py
    --force-unlock --cwd \"$PWD\")"

  (A summarize running in another project — `running_anywhere` — never blocks
  this one; only `running_here` matters.)

- `running_here: false, dirty_here > 0`  (dirty_here appears when --cwd is
  passed; fall back to `dirty` if the field is absent for older script versions)
  Run:
    python3 ~/.claude/scripts/parse_sessions.py --summarize-dirty --cwd "$PWD"
  Then search/list.

- `running_here: false, dirty_here == 0`
  Search/list silently.

Never claim more freshness than you have. If you ran --summarize-dirty and it
printed "another summarize is running; skipping", proceed anyway with current
data and mention it.

## If the user gave a search term

Example: `/sessions fieldMapper`

```bash
python3 ~/.claude/scripts/parse_sessions.py --search "<term>" --cwd "$PWD" --json
```

## If no term

```bash
python3 ~/.claude/scripts/parse_sessions.py --list --cwd "$PWD" --json
```

## Presenting results

The command returns raw JSON — never dump it as-is. Always render it into a
clean markdown table for the user, with these columns in this order:

  #, last active, files touched, decisions, summary, first prompt

- **last active**: use `last_active_relative` (e.g. "12m ago", "3h ago", "2d ago"), not the raw timestamp.
- **files touched**: the top file(s) from `files_touched`, basenames only.
- **decisions**: 1-2 short bullets from `decisions[]` (blank if none yet).
- **summary**: one short line distilled from `summary` — not the full text.
- **first prompt**: `first_real_prompt`, truncated to fit.
- For search results also show `matched_on` and the matching decision.

Newest first. Cap at 15 rows; state how many were hidden.

Then ask which session to load and STOP. Do not read any transcript until the user picks.

If zero results: say so, and offer to re-run without --cwd to search all repos.

## Loading

Two stages. Never claim more context than you have.

### Stage 1 — summary (default)

Read `~/.claude/context-cache/<uuid>/summary.md`.

Output 3-5 bullets: decisions, files changed, anything unfinished.

Then say exactly:
"Summary loaded (~40 lines). The full transcript is N messages — say
**load full** if you need the detailed reasoning."

Do NOT say "full context is loaded."

### Stage 2 — full transcript (explicit only)

Only when the user says "load full", "read the whole thing", or asks
something the summary demonstrably cannot answer.

Read `~/.claude/context-cache/<uuid>/transcript.md`.

If it exceeds ~2000 lines, say so and offer:
  (a) load it all anyway
  (b) grep it for a specific term first

Note any compaction boundary — content above it was not visible via
/resume.

## Flags the user may ask for

- all repos -> drop --cwd
- more rows -> raise the cap
- topic instead of number ("the fieldMapper one") -> --search that term, confirm before loading

## Recovery

If /sessions seems stuck: run
    python3 ~/.claude/scripts/parse_sessions.py --force-unlock --cwd "$PWD"
(drop --cwd to clear every project's lock)
