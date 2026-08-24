#!/usr/bin/env python3
"""Parse Claude Code session transcripts (~/.claude/projects/*/*.jsonl) into
readable markdown, cached under ~/.claude/context-cache/.

Schema reference: ~/.claude/context/SCHEMA.md
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import datetime

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
CACHE_DIR = os.path.join(CLAUDE_DIR, "context-cache")
INDEX_PATH = os.path.join(CACHE_DIR, "index.json")
LOG_PATH = os.path.join(CACHE_DIR, "parse.log")
LOCK_STALE_SECONDS = 1800

KNOWN_BLOCK_TYPES = {"text", "thinking", "tool_use", "tool_result"}
SKIP_LINE_TYPES = {"attachment", "file-history-snapshot"}
SKIP_FLAGS = ("isSidechain", "isMeta", "isVisibleInTranscriptOnly", "isApiErrorMessage")
TOOL_ARG_FIELDS = ("file_path", "command", "pattern", "path", "description")
THINKING_TRUNC = 4000
ARG_TRUNC = 100
PROMPT_TRUNC = 120
FRP_TRUNC = 150
TRIVIAL_PROMPTS = {"continue", "yes", "ok", "go on", "proceed", "do it", "next", "thanks"}
SUMMARIZER_PROMPT_PREFIXES = (
    "summarize this claude code session transcript",
    "you are updating a claude code session summary",
)
IDE_TAG_PREFIXES = (
    "<ide_selection>", "<ide_opened_file>", "<system-reminder>",
    "<command-name>", "<command-message>", "<local-command-stdout>",
)
# Best-effort: no field in the observed schema formally links a session to a
# prior one (resume appends to the same jsonl), so this usually resolves null.
PARENT_SESSION_FIELDS = ("parentSessionId", "parentSession", "resumedFromSessionId", "originSessionId")
SIMILAR_WINDOW_SECONDS = 60

DIRTY_MSG_THRESHOLD = 20
MERGE_FULL_REBUILD_THRESHOLD = 10
SUMMARY_MAX_LINES = 40
CLAUDE_TIMEOUT = 180
STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "in", "on", "and", "or", "with", "is",
    "are", "was", "were", "be", "this", "that", "it", "as", "by", "from", "at",
    "into", "over", "via", "use", "using", "add", "added", "fix", "fixed",
    "update", "updated", "session", "claude", "code", "not", "but", "so",
    "no", "then", "than", "when", "what", "how", "why", "which",
}

GEN_INSTRUCTION = (
    "Summarize this Claude Code session transcript. Output ONLY markdown, no "
    "preamble, no code blocks, max 40 lines total. Format exactly:\n"
    "# <short title derived from the work, not the first prompt>\n"
    "<branch> · <date> · <N> messages\n\n"
    "## Decisions\nbullets, past tense, concrete\n\n"
    "## Rationale\n2-4 lines: the WHY, which git cannot show\n\n"
    "## Changed\nfiles added/edited/deleted\n\n"
    "## Open\nunfinished work, known gaps\n\n"
    "## Rejected\nalternatives explicitly ruled out\n\n"
    "Omit any section with nothing to say. Use the SESSION METADATA line "
    "given on stdin for the branch/date/message-count line instead of "
    "guessing. After all sections, add exactly one final line starting with "
    "'TOPICS: ' followed by 3-6 comma-separated kebab-case tags summarizing "
    "the work."
)

MERGE_INSTRUCTION = (
    "You are updating a Claude Code session summary. Below is the SESSION "
    "METADATA, the CURRENT SUMMARY, and NEW TRANSCRIPT CONTENT to incorporate. "
    "Return the FULL updated summary (not a diff), same section format as the "
    "current summary (# title / branch · date · messages / ## Decisions / "
    "## Rationale / ## Changed / ## Open / ## Rejected, omitting empty "
    "sections), max 40 lines, markdown bullets, no preamble, no code blocks. "
    "After all sections, add exactly one final line starting with 'TOPICS: ' "
    "followed by 3-6 comma-separated kebab-case tags."
)

_run_unknown_types_logged = set()
_run_unknown_type_counts = {}
_run_new_versions = set()


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def session_dir(session_uuid):
    return os.path.join(CACHE_DIR, session_uuid)


def transcript_path(session_uuid):
    return os.path.join(session_dir(session_uuid), "transcript.md")


def summary_file_path(session_uuid):
    return os.path.join(session_dir(session_uuid), "summary.md")


def log(msg, also_print=False):
    ensure_cache_dir()
    line = f"{now_iso()} {msg}"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if also_print:
        print(line)


def atomic_write(path, content):
    ensure_cache_dir_for(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def ensure_cache_dir_for(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def cwd_slug(cwd):
    if not cwd:
        return "_global"
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return slug or "_global"


def lock_path_for(cwd_filter):
    return os.path.join(CACHE_DIR, f"summarize.{cwd_slug(cwd_filter)}.lock")


def _read_lock(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _lock_age_seconds(lock):
    started = parse_ts(lock.get("started")) if lock else None
    if not started:
        return None
    return int((datetime.datetime.now() - started).total_seconds())


def _lock_is_stale(lock):
    if not lock:
        return False
    age = _lock_age_seconds(lock)
    if age is not None and age > LOCK_STALE_SECONDS:
        return True
    return not _pid_alive(lock.get("pid"))


def acquire_lock(cwd_filter=None):
    ensure_cache_dir()
    path = lock_path_for(cwd_filter)
    existing = _read_lock(path)
    if existing and not _lock_is_stale(existing):
        return False
    if existing:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "started": now_iso()}))
    return True


def release_lock(cwd_filter=None):
    try:
        os.remove(lock_path_for(cwd_filter))
    except OSError:
        pass


def load_index():
    if not os.path.exists(INDEX_PATH):
        return {"schema": 1, "known_versions": [], "known_block_types": sorted(KNOWN_BLOCK_TYPES), "sessions": {}}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"WARNING: failed to load index.json ({e}); starting fresh", also_print=True)
        data = {}
    data.setdefault("schema", 1)
    data.setdefault("known_versions", [])
    data.setdefault("known_block_types", sorted(KNOWN_BLOCK_TYPES))
    data.setdefault("sessions", {})
    return data


def save_index(index):
    atomic_write(INDEX_PATH, json.dumps(index, indent=2, sort_keys=True) + "\n")


def discover_session_files():
    return sorted(glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")))


def find_session_file(session_uuid):
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_uuid}.jsonl"))
    return matches[0] if matches else None


def collapse_blank_lines(text):
    return re.sub(r"\n{3,}", "\n\n", text)


def short_path(p):
    """basename + immediate parent dir only, e.g. 'workflow/dynamic.py'."""
    parts = [seg for seg in str(p).rstrip("/").split("/") if seg]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else str(p)


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def within_window(ts1, ts2, seconds=SIMILAR_WINDOW_SECONDS):
    a = parse_ts(ts1)
    b = parse_ts(ts2)
    if not a or not b:
        return False
    return abs((a - b).total_seconds()) <= seconds


def is_ide_boilerplate(text):
    t = text.strip().lower()
    return t.startswith(IDE_TAG_PREFIXES)


def is_trivial(text):
    return text.strip().lower() in TRIVIAL_PROMPTS


def is_summarizer_prompt(text):
    return (text or "").strip().lower().startswith(SUMMARIZER_PROMPT_PREFIXES)


def qualifies_as_prompt(text):
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if is_ide_boilerplate(stripped):
        return False
    if is_trivial(stripped):
        return False
    return True


def update_first_real_prompt(entry, text):
    if entry.get("_frp_locked"):
        return
    if qualifies_as_prompt(text):
        entry["first_real_prompt"] = text.strip()[:FRP_TRUNC]
        entry["_frp_locked"] = True


def render_thinking(text):
    truncated = False
    if len(text) > THINKING_TRUNC:
        text = text[:THINKING_TRUNC]
        truncated = True
    quoted = "\n".join("> " + line for line in text.split("\n"))
    if truncated:
        quoted += "\n> ...[truncated]"
    return f"> **Thinking**\n{quoted}\n"


def render_tool_use(block):
    name = block.get("name", "?")
    inp = block.get("input") or {}
    arg = ""
    for field in TOOL_ARG_FIELDS:
        val = inp.get(field)
        if val is not None:
            arg = str(val)
            break
    arg = arg[:ARG_TRUNC].replace("`", "'").replace("\n", " ")
    return f"`[tool] {name}: {arg}`\n"


def record_tool_use_stats(entry, block):
    name = block.get("name")
    if name:
        tools_used = entry.setdefault("tools_used", {})
        tools_used[name] = tools_used.get(name, 0) + 1

    inp = block.get("input") or {}
    path = inp.get("file_path") or inp.get("path")
    if path:
        file_counts = entry.setdefault("file_counts", {})
        key = short_path(path)
        file_counts[key] = file_counts.get(key, 0) + 1


def process_content_blocks(blocks, entry):
    """blocks come from an assistant message; returns list of markdown chunks."""
    chunks = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if not entry.get("_first_assistant_text"):
                entry["_first_assistant_text"] = text
            chunks.append(f"### Claude\n{text}\n")
        elif btype == "thinking":
            chunks.append(render_thinking(block.get("thinking", "")))
        elif btype == "tool_use":
            record_tool_use_stats(entry, block)
            chunks.append(render_tool_use(block))
        elif btype == "tool_result":
            continue  # never emitted for assistant lines either
        else:
            count = _run_unknown_type_counts.get(btype, 0) + 1
            _run_unknown_type_counts[btype] = count
            if btype not in _run_unknown_types_logged:
                _run_unknown_types_logged.add(btype)
                log(f"WARNING: unknown content block type '{btype}' encountered", also_print=True)
    return chunks


def process_line(obj, line_no, entry, index):
    """Returns list of markdown chunks for this line, or [] if skipped."""
    if not entry.get("parent_session"):
        for f in PARENT_SESSION_FIELDS:
            v = obj.get(f)
            if v:
                entry["parent_session"] = v
                break

    for flag in SKIP_FLAGS:
        if obj.get(flag) is True:
            return []

    ltype = obj.get("type")
    if ltype in SKIP_LINE_TYPES:
        return []

    version = obj.get("version")
    if version:
        versions = set(entry.get("cc_versions") or [])
        if version not in versions:
            entry.setdefault("cc_versions", [])
            entry["cc_versions"].append(version)
        known_versions = set(index.get("known_versions") or [])
        if version not in known_versions and version not in _run_new_versions:
            _run_new_versions.add(version)
            log(f"WARNING: new claude version seen: {version}", also_print=True)

    if ltype in ("user", "assistant"):
        ts = obj.get("timestamp")
        if ts:
            if not entry.get("first_active") or ts < entry["first_active"]:
                entry["first_active"] = ts
            if not entry.get("last_active") or ts > entry["last_active"]:
                entry["last_active"] = ts
            date_part = str(ts)[:10]
            dates = entry.setdefault("_active_dates", [])
            if date_part not in dates:
                dates.append(date_part)

    for field in ("cwd", "gitBranch"):
        if not entry.get(field) and obj.get(field):
            entry[field] = obj.get(field)

    if ltype == "system" and obj.get("subtype") == "compact_boundary":
        entry["had_compaction"] = True
        return ["---\n_[compaction boundary]_\n---\n"]

    if ltype == "user":
        entry["msg_count"] = entry.get("msg_count", 0) + 1
        content = obj.get("message", {}).get("content")
        if isinstance(content, str):
            if not entry.get("first_prompt"):
                entry["first_prompt"] = content[:PROMPT_TRUNC]
                if is_summarizer_prompt(content):
                    entry["is_summarizer"] = True
            update_first_real_prompt(entry, content)
            return [f"## User\n{content}\n"]
        elif isinstance(content, list):
            chunks = []
            for block in content:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if not entry.get("first_prompt"):
                        entry["first_prompt"] = text[:PROMPT_TRUNC]
                        if is_summarizer_prompt(text):
                            entry["is_summarizer"] = True
                    update_first_real_prompt(entry, text)
                    chunks.append(f"## User\n{text}\n")
                elif block.get("type") == "tool_result":
                    continue
                else:
                    btype = block.get("type")
                    count = _run_unknown_type_counts.get(btype, 0) + 1
                    _run_unknown_type_counts[btype] = count
                    if btype not in _run_unknown_types_logged:
                        _run_unknown_types_logged.add(btype)
                        log(f"WARNING: unknown content block type '{btype}' encountered", also_print=True)
            return chunks
        return []

    if ltype == "assistant":
        entry["msg_count"] = entry.get("msg_count", 0) + 1
        content = obj.get("message", {}).get("content")
        if isinstance(content, list):
            return process_content_blocks(content, entry)
        return []

    return []


def build_header(session_uuid, entry):
    versions = ", ".join(entry.get("cc_versions") or []) or "unknown"
    lines = [
        f"# Session {session_uuid}",
        "",
        f"- cwd: {entry.get('cwd', '')}",
        f"- gitBranch: {entry.get('gitBranch', '')}",
        f"- first_active: {entry.get('first_active', '')}",
        f"- last_active: {entry.get('last_active', '')}",
        f"- claude version(s): {versions}",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def read_new_lines(path, start_byte):
    """Returns (list_of_raw_lines, new_byte_offset, rotated)."""
    size = os.path.getsize(path)
    if size < start_byte:
        return None, 0, True  # rotated, caller should reparse from 0
    with open(path, "rb") as f:
        f.seek(start_byte)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], start_byte, False  # nothing complete yet; leave partial for next run
    complete = data[:last_nl + 1]
    new_offset = start_byte + last_nl + 1
    text = complete.decode("utf-8", errors="replace")
    raw_lines = [l for l in text.split("\n") if l.strip() != ""]
    return raw_lines, new_offset, False


def parse_lines_into_entry(raw_lines, start_line_no, entry, index):
    md_chunks = []
    line_no = start_line_no
    for raw in raw_lines:
        line_no += 1
        try:
            obj = json.loads(raw)
            chunks = process_line(obj, line_no, entry, index)
            md_chunks.extend(chunks)
        except Exception as e:
            log(f"line {line_no}: {e}")
        entry["lines_parsed"] = entry.get("lines_parsed", 0) + 1
    return md_chunks


SUMMARY_CARRYOVER_FIELDS = (
    "summary", "topics", "decisions", "merge_count", "dirty",
    "summary_source", "_summary_through_md_bytes",
)


def fresh_entry(jsonl_path, session_uuid):
    return {
        "cwd": "", "gitBranch": "",
        "jsonl": jsonl_path,
        "bytes_parsed": 0, "lines_parsed": 0,
        "first_prompt": "", "first_real_prompt": "", "msg_count": 0, "had_compaction": False,
        "cc_versions": [], "tools_used": {}, "file_counts": {}, "files_touched": [],
        "first_active": "", "last_active": "", "active_days": 0, "span_days": 0,
        "parent_session": None,
        "_frp_locked": False, "_first_assistant_text": "", "_active_dates": [],
        "topics": [], "decisions": [], "summary": "", "merge_count": 0,
        "dirty": True, "summary_source": None, "_summary_through_md_bytes": 0,
    }


def finalize_entry(entry):
    file_counts = entry.get("file_counts") or {}
    top5 = sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    entry["files_touched"] = [k for k, _ in top5]

    entry["active_days"] = len(entry.get("_active_dates") or [])

    a = parse_ts(entry.get("first_active"))
    b = parse_ts(entry.get("last_active"))
    entry["span_days"] = (b - a).days if a and b else 0

    if not entry.get("_frp_locked"):
        first_asst = entry.get("_first_assistant_text")
        if first_asst:
            entry["first_real_prompt"] = "[no user prompt] " + first_asst.strip()[:FRP_TRUNC]


def parse_session(jsonl_path, index, overwrite, write_md=True, track_dirty=False):
    session_uuid = os.path.splitext(os.path.basename(jsonl_path))[0]
    md_path = transcript_path(session_uuid)
    sessions = index["sessions"]

    if overwrite or session_uuid not in sessions:
        old = sessions.get(session_uuid)
        entry = fresh_entry(jsonl_path, session_uuid)
        if old:
            for k in SUMMARY_CARRYOVER_FIELDS:
                if k in old:
                    entry[k] = old[k]
        start_byte = 0
        start_line_no = 0
    else:
        entry = sessions[session_uuid]
        entry["jsonl"] = jsonl_path
        start_byte = entry.get("bytes_parsed", 0)
        start_line_no = entry.get("lines_parsed", 0)

    msg_count_before = entry.get("msg_count", 0)

    raw_lines, new_offset, rotated = read_new_lines(jsonl_path, start_byte)

    if rotated:
        log(f"WARNING: {session_uuid} shrank (rotated); reparsing from byte 0", also_print=True)
        old = entry
        entry = fresh_entry(jsonl_path, session_uuid)
        for k in SUMMARY_CARRYOVER_FIELDS:
            if k in old:
                entry[k] = old[k]
        raw_lines, new_offset, _rotated2 = read_new_lines(jsonl_path, 0)
        start_line_no = 0
        msg_count_before = 0

    if raw_lines is None:
        raw_lines = []

    md_chunks = parse_lines_into_entry(raw_lines, start_line_no, entry, index)
    entry["bytes_parsed"] = new_offset
    finalize_entry(entry)

    if track_dirty and not entry.get("is_summarizer"):
        new_msgs = entry.get("msg_count", 0) - msg_count_before
        if new_msgs >= DIRTY_MSG_THRESHOLD:
            entry["dirty"] = True

    if write_md and not entry.get("is_summarizer"):
        body = collapse_blank_lines("\n".join(md_chunks))
        if overwrite or not os.path.exists(md_path):
            header = build_header(session_uuid, entry)
            atomic_write(md_path, header + body)
        elif md_chunks:
            with open(md_path, "r", encoding="utf-8") as f:
                existing = f.read()
            atomic_write(md_path, collapse_blank_lines(existing + "\n" + body))

    if entry.get("msg_count", 0) == 0:
        log(f"WARNING: session {session_uuid} parsed to 0 messages", also_print=True)

    sessions[session_uuid] = entry
    return entry


def finalize_index(index):
    known_versions = set(index.get("known_versions") or [])
    known_versions |= _run_new_versions
    for entry in index["sessions"].values():
        known_versions |= set(entry.get("cc_versions") or [])
    index["known_versions"] = sorted(known_versions)

    known_block_types = set(index.get("known_block_types") or []) | KNOWN_BLOCK_TYPES
    index["known_block_types"] = sorted(known_block_types)


def cmd_backfill():
    index = load_index()
    files = discover_session_files()
    for path in files:
        parse_session(path, index, overwrite=True)
    finalize_index(index)
    save_index(index)
    report(index, files)


def cmd_incremental(cwd_filter=None):
    index = load_index()
    files = discover_session_files()
    processed = []
    for path in files:
        session_uuid = os.path.splitext(os.path.basename(path))[0]
        existing = index["sessions"].get(session_uuid)
        if cwd_filter:
            # Scope the hook to this project only. Existing sessions match on
            # their recorded cwd; brand-new files (never filed) match on the
            # project dir name, which IS the cwd slug.
            if existing:
                in_scope = cwd_filter in (existing.get("cwd") or "")
            else:
                in_scope = os.path.basename(os.path.dirname(path)) == cwd_slug(cwd_filter)
            if not in_scope:
                continue
        parse_session(path, index, overwrite=False, track_dirty=True)
        processed.append(path)
    finalize_index(index)
    save_index(index)
    report(index, processed)


def cmd_session(session_uuid):
    index = load_index()
    path = find_session_file(session_uuid)
    if not path:
        print(f"session not found: {session_uuid}", file=sys.stderr)
        sys.exit(1)
    parse_session(path, index, overwrite=True)
    finalize_index(index)
    save_index(index)
    report(index, [path])


def cmd_reindex(silent=False):
    """Recompute index metadata for all cached sessions without touching .md files."""
    index = load_index()
    files = discover_session_files()
    for path in files:
        parse_session(path, index, overwrite=True, write_md=False)
    finalize_index(index)
    save_index(index)
    if not silent:
        report(index, files)


def report(index, files):
    total_md_bytes = 0
    for uuid in index["sessions"]:
        md_path = transcript_path(uuid)
        if os.path.exists(md_path):
            total_md_bytes += os.path.getsize(md_path)

    warnings = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            warnings = [l.rstrip("\n") for l in f if "WARNING" in l]

    print(f"sessions parsed: {len(files)}")
    print(f"total markdown cache size: {total_md_bytes} bytes")
    print(f"warnings in parse.log: {len(warnings)}")
    for w in warnings[-50:]:
        print(f"  {w}")
    if _run_unknown_type_counts:
        print("unknown content block types found:")
        for t, c in sorted(_run_unknown_type_counts.items()):
            print(f"  {t}: {c}")
    else:
        print("unknown content block types found: none")


def format_files(files_touched, width=32):
    basenames = [f.split("/")[-1] for f in (files_touched or [])]
    s = ", ".join(basenames)
    if len(s) > width:
        s = s[:width - 3] + "..."
    return s


def format_prompt(text, width=60):
    text = (text or "").replace("\n", " ").strip()
    if len(text) > width:
        return text[:width - 3] + "..."
    return text


def format_last_active(ts):
    dt = parse_ts(ts)
    if dt:
        return dt.strftime("%Y-%m-%d %H:%M")
    return (ts or "")[:16]


def minutes_since(ts):
    dt = parse_ts(ts)
    if not dt:
        return None
    now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
    return max(0, int((now - dt).total_seconds() // 60))


def format_relative(ts):
    mins = minutes_since(ts)
    if mins is None:
        return ""
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def is_junk_session(entry):
    if entry.get("is_summarizer") is True:
        return True
    if entry.get("msg_count", 0) < 3:
        return True
    if is_summarizer_prompt(entry.get("first_real_prompt") or ""):
        return True
    if not entry.get("files_touched") and entry.get("msg_count", 0) < 5:
        return True
    return False


def build_rows(sessions, cwd_filter):
    rows = []
    for uuid, entry in sessions.items():
        if cwd_filter and cwd_filter not in (entry.get("cwd") or ""):
            continue
        if is_junk_session(entry):
            continue
        rows.append({
            "uuid": uuid,
            "gitBranch": entry.get("gitBranch", ""),
            "first_active": entry.get("first_active", ""),
            "last_active": entry.get("last_active", ""),
            "last_active_relative": format_relative(entry.get("last_active")),
            "active_days": entry.get("active_days", 0),
            "span_days": entry.get("span_days", 0),
            "files_touched": entry.get("files_touched", []),
            "decisions": entry.get("decisions", []),
            "summary": entry.get("summary", ""),
            "first_real_prompt": entry.get("first_real_prompt", ""),
            "msg_count": entry.get("msg_count", 0),
            "parent_session": entry.get("parent_session"),
            "similar_count": 0,
        })
    rows.sort(key=lambda r: r["last_active"] or "", reverse=True)
    return rows


def collapse_similar(rows):
    """Collapse rows with identical files_touched sets whose first_active
    are within SIMILAR_WINDOW_SECONDS, keeping the one with more messages."""
    groups = {}
    for r in rows:
        key = frozenset(r["files_touched"] or [])
        groups.setdefault(key, []).append(r)

    collapsed = []
    for key, group in groups.items():
        if not key or len(group) == 1:
            collapsed.extend(group)
            continue

        clusters = []
        for r in group:
            placed = False
            for cluster in clusters:
                if any(within_window(r["first_active"], m["first_active"]) for m in cluster):
                    cluster.append(r)
                    placed = True
                    break
            if not placed:
                clusters.append([r])

        for cluster in clusters:
            if len(cluster) == 1:
                collapsed.append(cluster[0])
            else:
                best = dict(max(cluster, key=lambda r: r["msg_count"]))
                best["similar_count"] = best.get("similar_count", 0) + (len(cluster) - 1)
                collapsed.append(best)

    return collapsed


def cmd_list(cwd_filter, as_json, limit=None):
    index = load_index()
    rows = build_rows(index["sessions"], cwd_filter)
    rows = collapse_similar(rows)
    rows.sort(key=lambda r: r["last_active"] or "", reverse=True)
    if limit:
        rows = rows[:limit]

    if as_json:
        print(json.dumps(rows, indent=2))
        return

    print(f"{'#':<3} {'BRANCH':<18} {'LAST ACTIVE':<17} {'MSGS':<6} {'FILES':<32} {'PROMPT'}")
    for i, r in enumerate(rows, 1):
        branch = (r["gitBranch"] or "")[:16]
        last_active = format_last_active(r.get("last_active"))
        files_s = format_files(r["files_touched"])
        prompt = format_prompt(r["first_real_prompt"])
        if r.get("similar_count"):
            prompt = f"{prompt} (+{r['similar_count']} similar)"
        print(f"{i:<3} {branch:<18} {last_active:<17} {r['msg_count']:<6} {files_s:<32} {prompt}")


# ---------------------------------------------------------------------------
# Summary layer
# ---------------------------------------------------------------------------

def slugify(text, max_words=4):
    text = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    words = [w for w in re.split(r"[\s-]+", text) if w]
    return "-".join(words[:max_words]) if words else ""


def extract_topics_from_text(text, max_topics=6):
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", text.lower())
    significant = [w for w in words if w not in STOPWORDS and len(w) > 2]
    topics = []
    seen = set()
    i = 0
    while i < len(significant) and len(topics) < max_topics:
        tag = significant[i]
        if i + 1 < len(significant) and len(tag) <= 5:
            tag2 = f"{tag}-{significant[i + 1]}"
            if tag2 not in seen:
                topics.append(tag2)
                seen.add(tag2)
                i += 2
                continue
        if tag not in seen:
            topics.append(tag)
            seen.add(tag)
        i += 1
    return topics[:max_topics]


def extract_section(summary_text, heading):
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    lines = summary_text.split("\n")
    out = []
    in_section = False
    for line in lines:
        if re.match(pattern, line.strip(), re.IGNORECASE):
            in_section = True
            continue
        if in_section and line.strip().startswith("##"):
            break
        if in_section:
            stripped = line.strip()
            if stripped.startswith("-") or stripped.startswith("*"):
                out.append(stripped.lstrip("-*").strip())
    return out


def enforce_max_lines(text, max_lines=SUMMARY_MAX_LINES):
    lines = text.rstrip("\n").split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines).rstrip() + "\n"


def split_topics_line(text):
    lines = text.rstrip().split("\n")
    topics = []
    if lines and lines[-1].strip().upper().startswith("TOPICS:"):
        raw = lines[-1].split(":", 1)[1]
        topics = [slugify(t.strip()) for t in raw.split(",") if t.strip()]
        lines = lines[:-1]
    return "\n".join(lines).rstrip() + "\n", [t for t in topics if t]


def memory_dir_for(jsonl_path):
    return os.path.join(os.path.dirname(jsonl_path), "memory")


def find_memory_files_for_session(jsonl_path, session_uuid):
    mem_dir = memory_dir_for(jsonl_path)
    if not os.path.isdir(mem_dir):
        return []
    hits = []
    for path in sorted(glob.glob(os.path.join(mem_dir, "*.md"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        m = re.search(r"originSessionId:\s*([a-zA-Z0-9-]+)", text)
        if m and m.group(1) == session_uuid:
            hits.append((path, text))
    return hits


def build_memory_summary(entry, mem_hits):
    decisions = []
    rationale = []
    topics = []

    for path, text in mem_hits:
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else text.strip()
        why = ""
        main_lines = []
        for line in body.split("\n"):
            s = line.strip()
            if re.match(r"(?i)^\*\*why:\*\*", s):
                why = re.sub(r"(?i)^\*\*why:\*\*\s*", "", s).strip()
            elif re.match(r"(?i)^\*\*how to apply:\*\*", s):
                continue
            elif s:
                main_lines.append(s)
        main_text = " ".join(main_lines).strip()
        if main_text:
            decisions.append(main_text[:200])
        if why:
            rationale.append(why[:300])

        m = re.search(r"^name:\s*(.+?)\s*$", text, re.MULTILINE)
        if m:
            tag = m.group(1).strip()
            for pre in ("feedback-", "project-", "user-", "reference-"):
                if tag.startswith(pre):
                    tag = tag[len(pre):]
                    break
            if tag:
                topics.append(tag)

    title = ""
    for path, text in mem_hits:
        m = re.search(r'^description:\s*"?(.*?)"?\s*$', text, re.MULTILINE)
        if m and m.group(1):
            title = m.group(1)[:70]
            break
    if not title:
        title = entry.get("first_real_prompt") or "Session summary"

    sections = [f"# {title}"]
    branch = entry.get("gitBranch") or "HEAD"
    date = format_last_active(entry.get("last_active"))[:10]
    sections.append(f"{branch} · {date} · {entry.get('msg_count', 0)} messages")

    if decisions:
        sections.append("\n## Decisions")
        sections.extend(f"- {d}" for d in decisions[:10])
    if rationale:
        sections.append("\n## Rationale")
        sections.extend(rationale[:4])
    changed = entry.get("files_touched") or []
    if changed:
        sections.append("\n## Changed")
        sections.extend(f"- {c}" for c in changed)

    summary_text = enforce_max_lines("\n".join(sections) + "\n")
    if not topics:
        topics = extract_topics_from_text(summary_text)
    return summary_text, topics[:6]


def build_stub_summary(entry):
    fp = entry.get("first_real_prompt") or ""
    title = fp.replace("[no user prompt] ", "").strip() or "Session summary"
    title = title[:70]

    sections = [f"# {title}"]
    branch = entry.get("gitBranch") or "HEAD"
    date = format_last_active(entry.get("last_active"))[:10]
    sections.append(f"{branch} · {date} · {entry.get('msg_count', 0)} messages")

    if fp:
        sections.append("\n## Decisions")
        sections.append(f"- {fp[:150]}")
    changed = entry.get("files_touched") or []
    if changed:
        sections.append("\n## Changed")
        sections.extend(f"- {c}" for c in changed)

    summary_text = enforce_max_lines("\n".join(sections) + "\n")
    topics = extract_topics_from_text(summary_text)
    return summary_text, topics


def build_metadata_header(entry):
    branch = entry.get("gitBranch") or "HEAD"
    date = format_last_active(entry.get("last_active"))[:10]
    return (
        f"SESSION METADATA: branch={branch} last_active_date={date} "
        f"msg_count={entry.get('msg_count', 0)}\n\n"
    )


def run_claude_p(instruction, stdin_text, timeout=CLAUDE_TIMEOUT):
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return None
    env = dict(os.environ)
    env["CLAUDE_SESSIONS_SUMMARIZER"] = "1"
    try:
        result = subprocess.run(
            [claude_bin, "-p", instruction],
            input=stdin_text, capture_output=True, text=True, timeout=timeout,
            env=env,
        )
    except Exception as e:
        log(f"WARNING: claude -p failed to run: {e}")
        return None
    if result.returncode != 0:
        log(f"WARNING: claude -p exited {result.returncode}: {result.stderr[:300]}")
        return None
    out = (result.stdout or "").strip()
    return out or None


def summarize_session(entry, session_uuid, force_full=False):
    """Returns the summary_source used ('memory', 'generated', or 'stub').
    Mutates entry in place: summary, summary_source, topics, decisions,
    merge_count, dirty, _summary_through_md_bytes.
    """
    jsonl_path = entry.get("jsonl")
    mem_hits = find_memory_files_for_session(jsonl_path, session_uuid) if jsonl_path else []

    if mem_hits:
        summary_text, topics = build_memory_summary(entry, mem_hits)
        entry["summary"] = summary_text
        entry["summary_source"] = "memory"
        entry["topics"] = topics
        entry["decisions"] = extract_section(summary_text, "Decisions")[:10]
        entry["dirty"] = False
        return "memory"

    md_path = transcript_path(session_uuid)
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            full_md = f.read()
    except Exception as e:
        log(f"WARNING: cannot read .md for summarizing {session_uuid}: {e}", also_print=True)
        full_md = ""

    existing_summary = entry.get("summary") or ""
    through = entry.get("_summary_through_md_bytes", 0) or 0
    through = max(0, min(through, len(full_md)))
    needs_full = force_full or not existing_summary or entry.get("merge_count", 0) >= MERGE_FULL_REBUILD_THRESHOLD

    raw = None
    if needs_full:
        stdin_text = build_metadata_header(entry) + "TRANSCRIPT:\n" + full_md
        raw = run_claude_p(GEN_INSTRUCTION, stdin_text)
    else:
        delta = full_md[through:]
        if not delta.strip():
            entry["dirty"] = False
            return entry.get("summary_source") or "generated"
        stdin_text = (
            build_metadata_header(entry)
            + f"CURRENT SUMMARY:\n{existing_summary}\n\nNEW TRANSCRIPT CONTENT:\n{delta}\n"
        )
        raw = run_claude_p(MERGE_INSTRUCTION, stdin_text)

    if raw:
        summary_text, topics = split_topics_line(raw)
        summary_text = enforce_max_lines(summary_text)
        if not topics:
            topics = extract_topics_from_text(summary_text)
        entry["summary"] = summary_text
        entry["summary_source"] = "generated"
        entry["topics"] = topics[:6]
        entry["decisions"] = extract_section(summary_text, "Decisions")[:10]
        entry["_summary_through_md_bytes"] = len(full_md)
        entry["merge_count"] = 0 if needs_full else entry.get("merge_count", 0) + 1
        entry["dirty"] = False
        return "generated"

    summary_text, topics = build_stub_summary(entry)
    entry["summary"] = summary_text
    entry["summary_source"] = "stub"
    entry["topics"] = topics
    entry["decisions"] = extract_section(summary_text, "Decisions")[:10]
    entry["_summary_through_md_bytes"] = len(full_md)
    entry["dirty"] = False
    return "stub"


def cmd_summarize_dirty(cwd_filter=None, max_items=None):
    if not acquire_lock(cwd_filter):
        print("another summarize is running; skipping")
        return {}, 0
    try:
        index = load_index()
        sessions = index["sessions"]
        summarized = {"memory": 0, "generated": 0, "stub": 0}
        failures = 0
        dirty_items = [
            (u, e) for u, e in sessions.items()
            if e.get("dirty") and (not cwd_filter or cwd_filter in (e.get("cwd") or ""))
            and not is_junk_session(e)
        ]
        dirty_items.sort(key=lambda kv: kv[1].get("last_active") or "", reverse=True)
        if max_items:
            dirty_items = dirty_items[:max_items]
        for uuid, entry in dirty_items:
            try:
                source = summarize_session(entry, uuid)
                atomic_write(summary_file_path(uuid), entry.get("summary") or "")
                summarized[source] = summarized.get(source, 0) + 1
            except Exception as e:
                log(f"WARNING: summarize failed for {uuid}: {e}", also_print=True)
                failures += 1
                entry["dirty"] = True
        save_index(index)
        return summarized, failures
    finally:
        release_lock(cwd_filter)


def cmd_resummarize(session_uuid):
    index = load_index()
    entry = index["sessions"].get(session_uuid)
    if not entry:
        print(f"session not found in index: {session_uuid}", file=sys.stderr)
        sys.exit(1)
    try:
        source = summarize_session(entry, session_uuid, force_full=True)
        summary_path = summary_file_path(session_uuid)
        atomic_write(summary_path, entry.get("summary") or "")
        save_index(index)
        print(f"resummarized {session_uuid} via {source}")
    except Exception as e:
        log(f"WARNING: resummarize failed for {session_uuid}: {e}", also_print=True)
        print(f"resummarize failed for {session_uuid}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(cwd_filter=None):
    index = load_index()
    sessions = index["sessions"]

    lock = _read_lock(lock_path_for(cwd_filter))
    lock_pid = lock.get("pid") if lock else None
    lock_age_seconds = _lock_age_seconds(lock) if lock else None
    running_here = bool(lock) and not _lock_is_stale(lock)

    running_anywhere = False
    for path in glob.glob(os.path.join(CACHE_DIR, "summarize.*.lock")):
        other = _read_lock(path)
        if other and not _lock_is_stale(other):
            running_anywhere = True
            break

    result = {
        "running_here": running_here,
        "running_anywhere": running_anywhere,
        "lock_age_seconds": lock_age_seconds,
        "lock_pid": lock_pid,
        "dirty": sum(1 for e in sessions.values() if e.get("dirty")),
        "total": len(sessions),
    }
    if cwd_filter:
        result["dirty_here"] = sum(
            1 for e in sessions.values()
            if e.get("dirty") and cwd_filter in (e.get("cwd") or "")
        )
    print(json.dumps(result, indent=2))


def cmd_force_unlock(cwd_filter=None):
    if cwd_filter:
        paths = [lock_path_for(cwd_filter)]
    else:
        paths = glob.glob(os.path.join(CACHE_DIR, "summarize.*.lock"))
    removed = 0
    for path in paths:
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"failed to remove lock {path}: {e}", file=sys.stderr)
    print(f"removed {removed} lock(s)" if removed else "no lock present")


def compute_total_cache_size():
    total = 0
    for root, _, files in os.walk(CACHE_DIR):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
    return total


def report_summarize(summarized, failures):
    print(f"sessions summarized: {sum(summarized.values())}")
    for src in ("memory", "generated", "stub"):
        print(f"  {src}: {summarized.get(src, 0)}")
    print(f"failures: {failures}")
    print(f"total cache size: {compute_total_cache_size()} bytes")


def cmd_search(term, cwd_filter, as_json):
    index = load_index()
    rows = build_rows(index["sessions"], cwd_filter)
    rows_by_uuid = {r["uuid"]: r for r in rows}
    term_lower = term.lower()

    # (weight, uuid, matched_on, matching_decision)
    scored = []
    for uuid, row in rows_by_uuid.items():
        entry = index["sessions"][uuid]
        decisions = entry.get("decisions") or []
        topics = entry.get("topics") or []

        hit_decision = next((d for d in decisions if term_lower in d.lower()), None)
        hit_topic = next((t for t in topics if term_lower in t.lower()), None)
        if hit_decision or hit_topic:
            scored.append((3, uuid, "decisions" if hit_decision else "topics", hit_decision or hit_topic or ""))
            continue

        fp = entry.get("first_real_prompt") or ""
        files = entry.get("files_touched") or []
        hit_file = next((f for f in files if term_lower in f.lower()), None)
        if term_lower in fp.lower() or hit_file:
            matched_on = "first_real_prompt" if term_lower in fp.lower() else "files_touched"
            scored.append((2, uuid, matched_on, fp[:100]))
            continue

        summary_path = summary_file_path(uuid)
        if os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                text = ""
            if term_lower in text.lower():
                line = next((l.strip()[:100] for l in text.split("\n") if term_lower in l.lower()), "")
                scored.append((1, uuid, "summary", line))

    scored.sort(key=lambda t: -t[0])

    hits = []
    for weight, uuid, matched_on, matching_decision in scored:
        if len(hits) >= 5:
            break
        row = dict(rows_by_uuid[uuid])
        row["matched_on"] = matched_on
        row["matching_decision"] = matching_decision
        hits.append(row)

    if as_json:
        print(json.dumps(hits, indent=2))
        return

    print(f"{'#':<3} {'BRANCH':<18} {'LAST ACTIVE':<17} {'MSGS':<6} {'FILES':<32} {'PROMPT':<40} {'ON':<16} MATCH")
    for i, r in enumerate(hits, 1):
        branch = (r["gitBranch"] or "")[:16]
        last_active = format_last_active(r.get("last_active"))
        files_s = format_files(r["files_touched"])
        prompt = format_prompt(r["first_real_prompt"], width=38)
        matched_on = r["matched_on"][:16]
        match = (r["matching_decision"] or "")[:60]
        print(f"{i:<3} {branch:<18} {last_active:<17} {r['msg_count']:<6} {files_s:<32} {prompt:<40} {matched_on:<16} {match}")


def needs_bootstrap():
    return not os.path.exists(INDEX_PATH)


def main():
    parser = argparse.ArgumentParser(description="Parse Claude Code session transcripts into cached markdown.")
    parser.add_argument("--backfill", action="store_true", help="parse every session from byte 0, overwrite cache")
    parser.add_argument("--incremental", action="store_true", help="parse only new bytes for each session")
    parser.add_argument("--session", metavar="UUID", help="reparse one session from scratch")
    parser.add_argument("--reindex", action="store_true", help="recompute index metadata for all sessions without rewriting .md files")
    parser.add_argument("--summarize-dirty", action="store_true", help="(re)generate summaries for sessions flagged dirty")
    parser.add_argument("--resummarize", metavar="UUID", help="force a full summary rebuild for one session")
    parser.add_argument("--list", action="store_true", help="print a table of cached sessions")
    parser.add_argument("--search", metavar="TERM", help="search the summary index for TERM (case-insensitive)")
    parser.add_argument("--cwd", metavar="PATH", help="filter to sessions whose cwd contains PATH")
    parser.add_argument("--limit", type=int, metavar="N", help="limit --list to the N most recent sessions")
    parser.add_argument("--max", type=int, metavar="N", help="manual override: cap sessions processed by --summarize-dirty (hook never sets this)")
    parser.add_argument("--json", action="store_true", help="machine-readable output for --list/--search")
    parser.add_argument("--status", action="store_true", help="print lock/dirty status as JSON")
    parser.add_argument("--force-unlock", action="store_true", help="remove the summarize lock")
    args = parser.parse_args()

    ensure_cache_dir()

    if needs_bootstrap() and discover_session_files():
        log("bootstrapping: first-run backfill", also_print=True)
        cmd_backfill()

    ran = False

    if args.status:
        cmd_status(args.cwd)
        ran = True
    elif args.force_unlock:
        cmd_force_unlock(args.cwd)
        ran = True
    elif args.session:
        cmd_session(args.session)
        ran = True
    elif args.search:
        cmd_search(args.search, args.cwd, args.json)
        ran = True
    elif args.list:
        cmd_list(args.cwd, args.json, args.limit)
        ran = True
    elif args.resummarize:
        cmd_resummarize(args.resummarize)
        ran = True
    else:
        # Composable maintenance commands — order matters: filing must
        # happen before summarizing, or the summarize pass reads a stale .md.
        if args.backfill:
            cmd_backfill()
            ran = True
        if args.reindex:
            cmd_reindex(silent=args.summarize_dirty)
            ran = True
        if args.incremental:
            cmd_incremental(cwd_filter=args.cwd)
            ran = True
        if args.summarize_dirty:
            summarized, failures = cmd_summarize_dirty(cwd_filter=args.cwd, max_items=args.max)
            report_summarize(summarized, failures)
            ran = True

    if not ran:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
   