# Claude Sessions

Stop burning tokens re-reading old conversations. Let Claude remember what it already figured out.

## The problem

Claude Code doesn't remember past sessions. Every time you come back to a project, you either re-explain context from scratch or ask Claude to read through old transcripts — which eats a huge chunk of your context window before you've even started the real work.

## What this does

Claude Sessions runs quietly in the background, turning your past Claude Code sessions into short, searchable summaries. When you want to pick up old context, you just run `/sessions` — Claude shows you a table of past sessions for the current repo (what was discussed, what changed, when) and loads a short summary instead of the entire transcript. Full transcripts are still there if you ever need the deep detail, but you rarely will.

No manual work. No copy-pasting old chats. It updates itself every time you start or end a session.

## Requirements

- macOS or Linux (no Windows support currently)
- Python 3.8+
- Claude Code already installed and used at least once (so `~/.claude` exists)
- `jq` is optional — used if present, otherwise a built-in fallback handles it

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Scientist1781999/claude-code-session-management-plugin/main/install.sh | bash
```

Then restart Claude Code.

The installer is safe to re-run — it backs up any files it changes and never overwrites something it can't undo.

## Usage

In any repo, just run:

```
/sessions
```

- With **no arguments**, it shows your recent sessions in that repo — last active time, files touched, key decisions, and a one-line summary.
- With a **search term** (e.g. `/sessions login bug`), it searches across your past sessions for that repo and shows the closest matches.
- **Pick a session number** and Claude loads a summary of it. If the summary isn't enough, just say **"load full"** and Claude will pull the entire transcript.

That's it — no flags to memorize, no setup per project.

## How it works (short version)

1. A background hook runs automatically when you start or end a Claude Code session — it never runs while you're actively working, and never blocks you.
2. It parses your session transcripts into short markdown summaries, cached locally on your machine under `~/.claude/context-cache/`.
3. Nothing leaves your machine. Summaries are generated locally using your own Claude Code CLI.

## Uninstall

```bash
bash install.sh --uninstall
```

This removes the installed script, the skill, and the hook entries from your settings — your cached summaries are left alone by default. To also delete the cache:

```bash
bash install.sh --uninstall --purge
```

## Troubleshooting

- **`/sessions` seems stuck or slow:** a background summary update may still be running for this repo. It'll say so — just try again in a minute.
- **Still stuck after a few minutes:** run

  ```bash
  python3 ~/.claude/scripts/parse_sessions.py --force-unlock --cwd "$PWD"
  ```

- **Nothing shows up in `/sessions`:** the cache builds itself the first time you use Claude Code in a repo after installing — give it one session cycle (start + end) to populate.
