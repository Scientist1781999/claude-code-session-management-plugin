# claude-sessions

Recall a past Claude Code session for the current repo — a hook keeps a
markdown + summary cache of every session, and `/sessions` searches or
lists it without you having to re-read raw transcripts.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Scientist1781999/claude-code-session-management-plugin/main/install.sh | bash
```

This installs `parse_sessions.py` and the `sessions` skill into `~/.claude`,
merges the `SessionStart`/`SessionEnd` hooks into `~/.claude/settings.json`
(backing up anything it touches), and creates `~/.claude/context-cache/`.
Re-running it is safe — existing files are left alone if unchanged, and the
hook merge never duplicates entries.

Restart Claude Code, then try `/sessions` in any repo.

## Uninstall

```bash
bash install.sh --uninstall        # add --purge to also delete the cache
```

See `install.sh --help` for `--dry-run` and `--yes`.