#!/usr/bin/env bash
# claude-sessions installer. Idempotent — safe to re-run. macOS + Linux only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"

SRC_SCRIPT="$SCRIPT_DIR/scripts/parse_sessions.py"
SRC_SKILL="$SCRIPT_DIR/skills/sessions/SKILL.md"
SRC_FRAGMENT="$SCRIPT_DIR/settings-fragment.json"

DST_SCRIPT="$CLAUDE_DIR/scripts/parse_sessions.py"
DST_SKILL="$CLAUDE_DIR/skills/sessions/SKILL.md"
DST_SETTINGS="$CLAUDE_DIR/settings.json"
DST_CACHE="$CLAUDE_DIR/context-cache"

DRY_RUN=0
UNINSTALL=0
PURGE=0
ASSUME_YES=0

# Space-separated list of "<backup_path>|<original_path>" pairs created this
# run, used for rollback on failure. Bash 3.2 has no assoc arrays, so we keep
# this as a plain string rather than a real array-of-pairs.
BACKUPS_MADE=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help)
      echo "usage: install.sh [--dry-run] [--uninstall [--purge]] [--yes]"
      exit 0
      ;;
    *)
      echo "unknown flag: $arg" >&2
      exit 1
      ;;
  esac
done

if [ -t 1 ]; then
  C_OK=$'\033[32m'
  C_FAIL=$'\033[31m'
  C_WARN=$'\033[33m'
  C_RESET=$'\033[0m'
else
  C_OK=""
  C_FAIL=""
  C_WARN=""
  C_RESET=""
fi

ok()   { echo "${C_OK}[ok]${C_RESET} $1"; }
fail() { echo "${C_FAIL}[fail]${C_RESET} $1" >&2; }
warn() { echo "${C_WARN}[warn]${C_RESET} $1"; }

epoch() { date +%s; }

# realpath -m isn't portable to macOS's BSD realpath; roll our own for an
# existing file/dir.
abspath() {
  if [ -d "$1" ]; then
    (cd "$1" && pwd)
  else
    dir="$(cd "$(dirname "$1")" && pwd)"
    echo "$dir/$(basename "$1")"
  fi
}

rollback() {
  fail "rolling back changes made this run"
  for pair in $BACKUPS_MADE; do
    backup="${pair%%|*}"
    original="${pair##*|}"
    if [ -f "$backup" ]; then
      mv "$backup" "$original"
      echo "  restored $original"
    fi
  done
}

on_err() {
  code=$?
  if [ "$code" -ne 0 ] && [ -n "$BACKUPS_MADE" ]; then
    rollback
  fi
  exit "$code"
}
trap on_err ERR

JQ_AVAILABLE=0

preflight() {
  os="$(uname -s)"
  case "$os" in
    Darwin|Linux) ok "OS: $os" ;;
    *)
      fail "unsupported OS: $os — claude-sessions only supports macOS and Linux"
      exit 1
      ;;
  esac

  if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found on PATH"
    exit 1
  fi
  pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  pymajor="$(echo "$pyver" | cut -d. -f1)"
  pyminor="$(echo "$pyver" | cut -d. -f2)"
  if [ "$pymajor" -lt 3 ] || { [ "$pymajor" -eq 3 ] && [ "$pyminor" -lt 8 ]; }; then
    fail "python3 >= 3.8 required, found $pyver"
    exit 1
  fi
  ok "python3 $pyver"

  if command -v claude >/dev/null 2>&1; then
    ok "claude CLI found on PATH"
  else
    warn "claude CLI not found on PATH (not fatal — hooks still work once installed)"
  fi

  if [ ! -d "$CLAUDE_DIR" ]; then
    fail "$CLAUDE_DIR does not exist — run \`claude\` once first, then re-run this installer"
    exit 1
  fi
  ok "$CLAUDE_DIR exists"

  if command -v jq >/dev/null 2>&1; then
    JQ_AVAILABLE=1
    ok "jq found — will use it for the settings.json merge"
  else
    JQ_AVAILABLE=0
    warn "jq not found — falling back to python3 for the settings.json merge"
  fi
}

# copy_file <src> <dst> [mode]
copy_file() {
  src="$1"
  dst="$2"
  mode="${3:-}"

  if [ ! -f "$src" ]; then
    fail "source missing: $src"
    exit 1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would install $dst"
    return 0
  fi

  mkdir -p "$(dirname "$dst")"

  src_real="$(abspath "$src")"
  if [ -e "$dst" ]; then
    dst_real="$(abspath "$dst")"
  else
    dst_real=""
  fi

  if [ "$src_real" = "$dst_real" ]; then
    ok "$dst is already the install source; skipping"
    return 0
  fi

  if [ -f "$dst" ]; then
    if cmp -s "$src" "$dst"; then
      ok "$dst already up to date"
      return 0
    fi
    backup="$dst.bak-$(epoch)"
    cp "$dst" "$backup"
    BACKUPS_MADE="$BACKUPS_MADE $backup|$dst"
    echo "  backed up $dst -> $backup"
  fi

  cp "$src" "$dst"
  if [ -n "$mode" ]; then
    chmod "$mode" "$dst"
  fi
  ok "installed $dst"
}

copy_files() {
  copy_file "$SRC_SCRIPT" "$DST_SCRIPT" 755
  copy_file "$SRC_SKILL" "$DST_SKILL"
}

merge_settings() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would merge $SRC_FRAGMENT hooks into $DST_SETTINGS"
    return 0
  fi

  if [ ! -f "$SRC_FRAGMENT" ]; then
    fail "fragment missing: $SRC_FRAGMENT"
    exit 1
  fi

  if [ -f "$DST_SETTINGS" ]; then
    backup="$DST_SETTINGS.bak-$(epoch)"
    cp "$DST_SETTINGS" "$backup"
    BACKUPS_MADE="$BACKUPS_MADE $backup|$DST_SETTINGS"
    echo "  backed up $DST_SETTINGS -> $backup"
  else
    echo '{}' > "$DST_SETTINGS"
  fi

  merged="$DST_SETTINGS.merge-tmp-$(epoch)"

  if [ "$JQ_AVAILABLE" -eq 1 ]; then
    jq -s '
      .[0] as $cur
      | .[1] as $frag
      | $cur
      | .hooks = (
          ($cur.hooks // {}) as $curhooks
          | ($frag.hooks // {}) as $fraghooks
          | $curhooks + (
              $fraghooks
              | to_entries
              | map(
                  .key as $event
                  | ($curhooks[$event] // []) as $existing
                  | ($existing | map(.hooks[]?.command // empty)) as $existing_cmds
                  | {
                      key: $event,
                      value: ($existing + (
                        .value
                        | map(select(
                            (.hooks[0].command) as $c
                            | ($existing_cmds | index($c)) == null
                          ))
                      ))
                    }
                )
              | from_entries
            )
        )
    ' "$DST_SETTINGS" "$SRC_FRAGMENT" > "$merged"
  else
    python3 -c "
import json
cur = json.load(open('$DST_SETTINGS'))
frag = json.load(open('$SRC_FRAGMENT'))
hooks = cur.setdefault('hooks', {})
for event, entries in frag.get('hooks', {}).items():
    existing = hooks.setdefault(event, [])
    existing_cmds = set()
    for e in existing:
        for h in e.get('hooks', []):
            if 'command' in h:
                existing_cmds.add(h['command'])
    for entry in entries:
        cmds = [h.get('command') for h in entry.get('hooks', [])]
        if any(c in existing_cmds for c in cmds if c):
            continue
        existing.append(entry)
with open('$merged', 'w') as f:
    json.dump(cur, f, indent=2)
    f.write('\n')
"
  fi

  mv "$merged" "$DST_SETTINGS"
  ok "merged hooks into $DST_SETTINGS"
}

make_cache_dir() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would mkdir -p $DST_CACHE"
    return 0
  fi
  mkdir -p "$DST_CACHE"
  ok "$DST_CACHE ready"
}

sanity_run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would run: python3 $DST_SCRIPT --status --cwd \"\$PWD\""
    return 0
  fi
  status_json="$(python3 "$DST_SCRIPT" --status --cwd "$PWD")"
  if echo "$status_json" | python3 -c "import json,sys; json.load(sys.stdin)" >/dev/null 2>&1; then
    ok "sanity check passed — --status returned valid JSON"
    echo "$status_json"
  else
    fail "sanity check failed — --status did not return valid JSON"
    exit 1
  fi
}

install_flow() {
  preflight
  copy_files
  merge_settings
  make_cache_dir
  sanity_run

  echo ""
  echo "Next steps:"
  echo "  1. Restart Claude Code."
  echo "  2. Try /sessions in this or any other repo."
}

remove_file_if_present() {
  target="$1"
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ -e "$target" ]; then
      echo "[dry-run] would remove $target"
    fi
    return 0
  fi
  if [ -e "$target" ]; then
    backup="$target.bak-$(epoch)"
    cp -r "$target" "$backup"
    rm -rf "$target"
    ok "removed $target (backup: $backup)"
  else
    ok "$target already absent"
  fi
}

unmerge_settings() {
  if [ ! -f "$DST_SETTINGS" ]; then
    ok "$DST_SETTINGS absent; nothing to unmerge"
    return 0
  fi
  if [ ! -f "$SRC_FRAGMENT" ]; then
    warn "fragment missing: $SRC_FRAGMENT — leaving $DST_SETTINGS untouched"
    return 0
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would remove fragment's hook entries from $DST_SETTINGS"
    return 0
  fi

  backup="$DST_SETTINGS.bak-$(epoch)"
  cp "$DST_SETTINGS" "$backup"
  BACKUPS_MADE="$BACKUPS_MADE $backup|$DST_SETTINGS"
  echo "  backed up $DST_SETTINGS -> $backup"

  merged="$DST_SETTINGS.merge-tmp-$(epoch)"

  if [ "$JQ_AVAILABLE" -eq 1 ]; then
    jq -s '
      .[0] as $cur
      | .[1] as $frag
      | $cur
      | .hooks = (
          ($cur.hooks // {}) as $curhooks
          | ($frag.hooks // {}) as $fraghooks
          | $curhooks
          | with_entries(
              (.key) as $k
              | (.value) as $v
              | if ($fraghooks | has($k)) then
                  ($fraghooks[$k] | map(.hooks[]?.command // empty)) as $frag_cmds
                  | {key: $k, value: ($v | map(select(
                      (.hooks[0].command) as $c
                      | ($frag_cmds | index($c)) == null
                    )))}
                else
                  {key: $k, value: $v}
                end
            )
        )
    ' "$DST_SETTINGS" "$SRC_FRAGMENT" > "$merged"
  else
    python3 -c "
import json
cur = json.load(open('$DST_SETTINGS'))
frag = json.load(open('$SRC_FRAGMENT'))
hooks = cur.get('hooks', {})
for event, entries in frag.get('hooks', {}).items():
    frag_cmds = set()
    for entry in entries:
        for h in entry.get('hooks', []):
            if 'command' in h:
                frag_cmds.add(h['command'])
    if event in hooks:
        hooks[event] = [
            e for e in hooks[event]
            if not any(h.get('command') in frag_cmds for h in e.get('hooks', []))
        ]
with open('$merged', 'w') as f:
    json.dump(cur, f, indent=2)
    f.write('\n')
"
  fi

  mv "$merged" "$DST_SETTINGS"
  ok "removed fragment's hook entries from $DST_SETTINGS"
}

uninstall_flow() {
  os="$(uname -s)"
  case "$os" in
    Darwin|Linux) : ;;
    *) fail "unsupported OS: $os"; exit 1 ;;
  esac
  if command -v jq >/dev/null 2>&1; then
    JQ_AVAILABLE=1
  fi

  remove_file_if_present "$DST_SCRIPT"
  remove_file_if_present "$DST_SKILL"
  unmerge_settings

  if [ "$PURGE" -eq 1 ]; then
    if [ "$ASSUME_YES" -eq 1 ]; then
      do_purge=1
    else
      printf "Remove %s and all cached summaries? [y/N] " "$DST_CACHE"
      read -r reply
      case "$reply" in
        y|Y|yes|YES) do_purge=1 ;;
        *) do_purge=0 ;;
      esac
    fi
    if [ "$do_purge" -eq 1 ]; then
      if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would rm -rf $DST_CACHE"
      else
        rm -rf "$DST_CACHE"
        ok "removed $DST_CACHE"
      fi
    else
      ok "leaving $DST_CACHE in place"
    fi
  else
    ok "leaving $DST_CACHE in place (use --purge to remove it)"
  fi

  echo ""
  echo "Uninstall complete."
}

if [ "$UNINSTALL" -eq 1 ]; then
  uninstall_flow
else
  install_flow
fi

