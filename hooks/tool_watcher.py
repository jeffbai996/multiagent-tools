"""PostToolUse hook: surface tool calls into the Discord narrate placeholder.

Reads the per-channel `tools` mode from the bot's tools.json sibling file
and, based on that mode, appends a one-line ticker entry (and optionally
a diff block or Bash stdout block) into the same per-turn placeholder
that narrate.py owns. We share state file + helpers with narrate.py so
prose + tool traces live as one cohesive segment per reply boundary.

Modes:
  off      — no surfacing (default; this script exits 0)
  collapse — same as `diffs` while live (ticker + edit diffs), but the
             entire tool message gets deleted at Stop. Symmetric with
             narrate's 'collapse' mode — pair them when you want full
             visibility during the turn and a clean channel afterwards.
  ticker   — one-line `! ToolName(short args)` per tool call (orange on
             ```diff highlighter; `- ...` for errored calls renders red).
             Cross-platform color: works on Discord desktop AND mobile.
             Persists past Stop.
  diffs    — ticker + ```diff unified diff for Edit/Write/MultiEdit (kept)
  full     — diffs + plain ```fenced Bash stdout, secret-stripped (kept)

The hook input is the standard Claude Code PostToolUse payload:
  {
    "session_id": "...",
    "transcript_path": "/path/to/transcript.jsonl",
    "tool_name": "Bash",
    "tool_input": {...tool-specific...},
    "tool_response": {...tool-specific...}
  }
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys

# Reuse narrate.py's machinery so we share state, segment rotation,
# token resolution, and the same Discord HTTP helpers.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from narrate import (  # noqa: E402
    DISCORD_LIMIT,
    NARRATE_PREFIX_AUTO,
    _blockquote,
    _byte_offset_after_current_user_turn,
    _get_turn,
    _load_state,
    _save_state,
    _state_lock,
    _seal_segment,
    _turn_key,
    detect_discord_state_dir,
    discord_edit_message,
    discord_send_message,
    log as narrate_log,
    parse_discord_origins,
    read_bot_token,
    _last_user_entry,
    _extract_user_text,
    count_discord_replies,
)

_STATE_DIR = os.path.expanduser("~/.local/state/cc-discord-kit")
try:
    os.makedirs(_STATE_DIR, exist_ok=True)
except OSError:
    pass
LOG_PATH = os.environ.get(
    "CCDK_TOOL_WATCHER_LOG", os.path.join(_STATE_DIR, "tool_watcher.log")
)


def log(msg: str) -> None:
    """Tool-watcher log line. Separate file from narrate so the streams
    don't interleave when both fire on the same PostToolUse."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts} {msg}\n")
    except OSError:
        pass


# Pattern for "smells like a credential" — long base64/hex-ish runs.
# Used by full-mode Bash stdout stripping. Conservative on false positives
# is fine; the goal is "obvious tokens", not full-coverage DLP.
_SECRET_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")


def _redact_secrets(text: str) -> str:
    """Replace anything looking like a credential with <REDACTED>."""
    return _SECRET_RE.sub("<REDACTED>", text)


def _channel_mode(state_dir: str, chat_id: str) -> str:
    """Resolve the channel's tools mode by reading tools.json next to
    access.json in the bot's Discord state dir. Returns 'off' on any
    missing-file or parse error so we fail safe (silent)."""
    path = os.path.join(state_dir, "tools.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "off"
    if not isinstance(data, dict):
        return "off"
    val = data.get(chat_id, "off")
    return val if isinstance(val, str) else "off"


# Tools whose input contains a file path we'd format. Keys map to the
# input field that holds the path.
_PATH_TOOL_FIELDS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Tool input fields we'll show in the ticker arg digest. Falls back to
# json.dumps with truncation when none of these match.
_ARG_DIGEST_PREFERENCE = (
    "file_path", "notebook_path", "pattern", "command", "url",
    "symbols", "symbol", "ticker", "query",
)


def _arg_digest(tool_name: str, tool_input: dict, max_len: int = 80) -> str:
    """Short, one-line arg representation for the ticker.

    Picks the most "ID-shaped" field of the input, falls back to a json
    dump. Always single-line, always under max_len.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in _ARG_DIGEST_PREFERENCE:
        if key in tool_input:
            v = tool_input[key]
            if isinstance(v, str):
                s = v.strip().replace("\n", " ")
                if len(s) > max_len:
                    s = s[: max_len - 1] + "…"
                return s
    # Fallback: short JSON dump
    try:
        s = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        s = str(tool_input)
    s = s.replace("\n", " ")
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


TOOL_PREFIX = "🔧 ***Tool trace…***\n"
TOOL_PREFIX_FINAL = "🔧 **Tool trace**\n"


_TOOL_BLOCK_LEFT_PAD = "  "  # 2 cells from the left edge for readability


def _tool_message_content(tool_buffer: str, prefix: str = TOOL_PREFIX) -> str:
    """Render the tool-trace message: prefix + ```diff``` fenced buffer.

    Lives as its OWN Discord message (not merged into narrate's
    placeholder) so the narration flow stays clean.

    Uses Discord's `diff` syntax highlighter, which works on BOTH
    desktop AND mobile (unlike `ansi` which only colors on desktop).
    The `+`-prefix renders green, `-` red, `!` orange, leading-space
    plain. Trade-off vs ANSI: only 3-4 colors available and the prefix
    glyph is visible, but it's universal.

    Each line gets 2 cells of left padding so the content breathes
    away from the code-block edge. NB: Discord's diff highlighter
    requires the colorizing char (+/-/!/@) at column 0 to colorize the
    line, so the pad goes BEFORE the diff prefix only if we accept
    losing color on those lines. We chose to keep color — so padding
    only applies to lines that don't start with a diff colorizer
    (typically context / Bash-stdout lines that were already prefixed
    with a single space).
    """
    if not tool_buffer:
        return prefix
    # Colorizer chars (+/-/!/@) must stay at column 0 for Discord's diff
    # highlighter to fire. So we don't leading-pad those lines — but we
    # DO ensure there's a single space between the marker and the
    # content (so '+<p>' becomes '+ <p>'), which aligns colorized
    # content at the same visual column as the 2-space-padded plain
    # context lines below.
    padded_lines: list[str] = []
    for ln in tool_buffer.splitlines():
        if not ln:
            padded_lines.append(ln)
            continue
        first = ln[0]
        if first in "+-!@":
            # Ensure a space after the marker. If the next char is
            # already a space, leave it; otherwise insert one.
            if len(ln) > 1 and ln[1] != " ":
                ln = ln[0] + " " + ln[1:]
            padded_lines.append(ln)
        else:
            # Plain context line (typically Bash stdout already
            # space-prefixed). Apply the 2-cell pad.
            padded_lines.append(_TOOL_BLOCK_LEFT_PAD + ln)
    padded = "\n".join(padded_lines)
    return prefix + "```diff\n" + padded + "\n```"


def _ticker_line(tool_name: str, tool_input: dict, errored: bool = False) -> str:
    """One-line tool-invocation header: `+ ● ToolName(short-args)`.

    Goes inside a ```diff``` fence. Discord's diff highlighter colors a
    whole line by its leading character. Empirically: `+` and `!` both
    render GREEN, `-` renders RED.
    Since `+`/`!` are indistinguishable, we use `+` for the header and rely
    on the `●` dot as the marker that says "this is a tool invocation, not a
    file-edit line." Edit lines stay bare `+`/`-` with NO dot, so:

      `+ ● Read(file)`   → green header, has the dot  (a tool ran)
      `+     new code`   → green edit line, no dot     (a line was added)
      `-     old code`   → red edit line, no dot       (a line was removed)

    Errored calls render red and carry a FAILED tail:
      `- ● Bash(cmd) FAILED`
    """
    prefix = "- ● " if errored else "+ ● "
    digest = _arg_digest(tool_name, tool_input)
    short_name = tool_name
    if short_name.startswith("mcp__"):
        parts = short_name.split("__")
        if len(parts) >= 3:
            short_name = parts[-1]
    tail = " FAILED" if errored else ""
    return f"{prefix}{short_name}({digest}){tail}"


def _diff_counts(before: str, after: str) -> tuple[int, int]:
    """Count added / removed lines between before and after. Used for the
    `⎿ [ +N,  -M ]` summary line above an edit's diff body."""
    added = removed = 0
    for ln in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm=""):
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
    return added, removed


def _read_line_count(tool_response: dict) -> int | None:
    """Best-effort line count for a Read tool result. The PostToolUse hook's
    tool_response shape varies; try the common fields, fall back to None
    (caller then shows no summary rather than a wrong number)."""
    if not isinstance(tool_response, dict):
        return None
    # Common shapes: {"file": {"content": "..."}} or {"content": "..."} or
    # a raw string under "output"/"stdout".
    content = None
    f = tool_response.get("file")
    if isinstance(f, dict):
        content = f.get("content")
    if content is None:
        content = tool_response.get("content") or tool_response.get("output") or tool_response.get("stdout")
    if isinstance(content, str) and content:
        return len(content.splitlines())
    return None


def _summary_line(text: str) -> str:
    """A plain (grey) summary line under a tool header. Leading TWO spaces
    keep it off column 0 so the diff highlighter leaves it uncolored, and
    give the `⎿` connector a small indent under the header.

    Format examples (tight brackets, no inner padding, per spec):
      `  ⎿ [34 lines]`     (Read)
      `  ⎿ [+4, -0]`       (Edit/Write/MultiEdit)
    """
    return f"  ⎿ {text}"


def _is_textish_path(path: str) -> bool:
    """Heuristic: is this a text file we should diff?"""
    if not path:
        return False
    lower = path.lower()
    textish_exts = (
        ".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".txt", ".json",
        ".yaml", ".yml", ".toml", ".sh", ".html", ".css", ".sql",
        ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp", ".rb",
        ".env", ".cfg", ".ini", ".conf", ".dockerfile",
    )
    if lower.endswith(textish_exts):
        return True
    # Extensionless files in well-known config dirs — treat as text
    if "/" in path and "." not in os.path.basename(path):
        return True
    return False


def _diff_block(
    before: str, after: str, max_lines: int = 30, max_line_chars: int = 88,
) -> str | None:
    """Compact unified diff capped to max_lines + (N more) line.

    Each line further truncated to max_line_chars so Discord's mobile
    renderer doesn't wrap mid-line — when a +/- diff line wraps, the
    continuation segment loses its prefix character and renders
    uncolored, which looks like the diff is half-broken. 88 chars is
    the widest column that still survives mobile-Discord's mono-space
    rendering for most font/zoom combinations without horizontal
    overflow; tight enough to avoid wrap, generous enough not to chop
    typical 80-col source.

    Returns None if nothing changed (caller skips the diff block).
    """
    if before == after:
        return None
    diff_lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        n=1, lineterm="",
    ))
    body: list[str] = []
    for ln in diff_lines:
        if ln.startswith("---") or ln.startswith("+++"):
            continue
        # Drop @@ hunk markers — Discord's diff highlighter colors
        # them inconsistently (sometimes orange, sometimes plain) and
        # the line-number context they provide is noise on a 30-line
        # cap anyway.
        if ln.startswith("@@"):
            continue
        if len(ln) > max_line_chars:
            ln = ln[: max_line_chars - 1] + "…"
        body.append(ln)
    if not body:
        return None
    if len(body) > max_lines:
        elided = len(body) - max_lines
        body = body[:max_lines] + [f"... ({elided} more lines)"]
    return "\n".join(body)


def _read_file_safe(path: str) -> str:
    """Read a file, returning '' if missing/binary/too-big.

    Used to reconstruct the "before" side of Write/Edit operations
    when we only have the post-action transcript hook input.
    """
    if not path or not os.path.exists(path):
        return ""
    try:
        size = os.path.getsize(path)
        if size > 1_000_000:  # 1MB cap; bigger files don't get diffed
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _detect_error(tool_response: dict) -> bool:
    """Heuristic: did this tool call return an error?

    Common error shapes across hook tools:
      - dict with `is_error: True` or non-zero `exit_code`
      - dict whose `output` / `stdout` contains 'error' / 'failed' tokens
    Conservative — only flag confident error signals."""
    if not isinstance(tool_response, dict):
        return False
    if tool_response.get("is_error") is True:
        return True
    code = tool_response.get("exit_code")
    if isinstance(code, int) and code != 0:
        return True
    return False


def _format_tool_block(
    mode: str, tool_name: str, tool_input: dict, tool_response: dict,
) -> str | None:
    """Assemble the per-tool surface block based on mode.

    Returns None when nothing should be appended.
    """
    if mode == "off":
        return None

    errored = _detect_error(tool_response)
    ticker = _ticker_line(tool_name, tool_input, errored=errored)

    # `ticker` mode: just the one-line per-tool call.
    if mode == "ticker":
        return ticker

    # `collapse` and `diffs` both render ticker + diffs while live; the
    # only difference is that handle_finalize (in narrate.py) deletes the
    # whole tool message at Stop for `collapse`, while `diffs` keeps it.
    # `full` adds Bash stdout on top.
    diff_body: str | None = None
    summary: str | None = None  # the `  ⎿ [ ... ]` grey line under the header
    if mode in ("collapse", "diffs", "full") and tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path") or ""
        if _is_textish_path(path):
            if tool_name == "Edit":
                before = tool_input.get("old_string", "")
                after = tool_input.get("new_string", "")
                diff_body = _diff_block(before, after)
                added, removed = _diff_counts(before, after)
                summary = _summary_line(f"[+{added}, -{removed}]")
            elif tool_name == "Write":
                # For Write we don't have a true "before" from the hook
                # input — the tool_response sometimes carries it, but
                # the safest reconstruction is just to show the new
                # content as +'d lines (no - side).
                after = tool_input.get("content", "")
                n_added = len(after.splitlines())
                diff_body = "\n".join(f"+{ln}" for ln in after.splitlines()[:30])
                if after.count("\n") > 30:
                    diff_body += f"\n... ({after.count(chr(10)) - 30} more lines)"
                summary = _summary_line(f"[+{n_added}, -0]")
            elif tool_name == "MultiEdit":
                edits = tool_input.get("edits", []) or []
                chunks: list[str] = []
                tot_added = tot_removed = 0
                for ed in edits[:5]:  # cap to first 5 edits
                    before = ed.get("old_string", "")
                    after = ed.get("new_string", "")
                    a, r = _diff_counts(before, after)
                    tot_added += a
                    tot_removed += r
                    d = _diff_block(before, after, max_lines=15)
                    if d:
                        chunks.append(d)
                # Count the edits beyond the 5-chunk display cap too.
                for ed in edits[5:]:
                    a, r = _diff_counts(ed.get("old_string", ""), ed.get("new_string", ""))
                    tot_added += a
                    tot_removed += r
                if len(edits) > 5:
                    chunks.append(f"... ({len(edits) - 5} more edits)")
                diff_body = "\n".join(chunks) or None
                summary = _summary_line(f"[+{tot_added}, -{tot_removed}]")

    # Read: no diff body, just a `  ⎿ [ N lines ]` summary so the user sees
    # how much was read. Read isn't in the edit branch above, so handle here.
    if mode in ("collapse", "diffs", "full") and tool_name == "Read":
        path = tool_input.get("file_path") or ""
        n = _read_line_count(tool_response)
        if n is not None:
            summary = _summary_line(f"[{n} lines]")

    bash_block: str | None = None
    if mode == "full" and tool_name == "Bash":
        # tool_response shape varies; try to grab stdout
        stdout = ""
        if isinstance(tool_response, dict):
            stdout = tool_response.get("stdout") or tool_response.get("output") or ""
            if not isinstance(stdout, str):
                stdout = str(stdout)
        if stdout.strip():
            lines = stdout.splitlines()
            if len(lines) > 20:
                lines = lines[:20] + [f"... ({len(stdout.splitlines()) - 20} more lines)"]
            # Truncate each line to ~88 chars so Discord doesn't wrap
            # them — wrapped lines lose their leading space prefix and
            # render as their own un-styled chunk, breaking visual flow.
            lines = [(ln[:87] + "…") if len(ln) > 88 else ln for ln in lines]
            redacted = _redact_secrets("\n".join(lines))
            # Prefix every line with a single space so the diff highlighter
            # renders them plain (no green/red), but keeps them within the
            # outer ```diff fence. Triple-backticks inside the body would
            # close the outer fence and break the message — neutralize them
            # the same way narrate.py does (zero-width space splice).
            safe = redacted.replace("```", "`​`​`")
            bash_block = "\n".join(" " + ln for ln in safe.splitlines())

    parts = [ticker]
    if summary is not None:
        parts.append(summary)
    if diff_body is not None:
        # diff_body is already +/- prefixed lines — embed directly
        # (no nested fence; the outer ```diff` fence is the only one)
        safe_diff = diff_body.replace("```", "`​`​`")
        parts.append(safe_diff)
    if bash_block is not None:
        parts.append(bash_block)
    return "\n".join(parts)


def handle_tool(payload: dict) -> int:
    """PostToolUse entry point. Append the formatted tool block to the
    current narrate placeholder if the channel mode warrants it."""
    transcript_path = payload.get("transcript_path") or ""
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}

    if not transcript_path or not tool_name:
        return 0

    # Don't surface Discord-side tools — they ARE the reply mechanism,
    # so surfacing them would be circular noise.
    if tool_name.startswith("mcp__plugin_discord_discord__"):
        return 0

    # Find the Discord origin for this turn — same path narrate uses
    user = _last_user_entry(transcript_path)
    user_text = _extract_user_text(user)
    origins = parse_discord_origins(user_text)
    if not origins:
        return 0
    chat_id, _ = origins[-1]

    state_dir = detect_discord_state_dir()
    mode = _channel_mode(state_dir, chat_id)
    if mode == "off":
        return 0

    block = _format_tool_block(mode, tool_name, tool_input, tool_response)
    if not block:
        return 0

    turn_key = _turn_key(transcript_path)
    if not turn_key:
        return 0
    turn_ts = turn_key.split(":", 1)[1] if ":" in turn_key else ""

    # Serialize state access — same flock infrastructure narrate.py uses.
    # Without this, multiple concurrent PostToolUse fires (Bash + Edit
    # within one second) can both create a tool_msg_id and we end up
    # with duplicate tool-trace messages for the same turn.
    with _state_lock():
        return _handle_tool_locked(
            block, chat_id, state_dir, turn_key, turn_ts, transcript_path,
        )


def _handle_tool_locked(
    block: str, chat_id: str, state_dir: str, turn_key: str, turn_ts: str,
    transcript_path: str,
) -> int:
    """Body of handle_tool executed under _state_lock()."""
    state = _load_state()
    turn = _get_turn(state, turn_key)
    if turn is None:
        # No narrate turn yet — initialize the basic turn record so we
        # share keying with narrate. Tool state piggybacks on the same
        # turn entry but uses its OWN msg_id (tool_msg_id) so the
        # narrate placeholder stays pure prose.
        initial_offset = _byte_offset_after_current_user_turn(
            transcript_path, turn_ts
        )
        turn = {
            "chat_id": chat_id,
            "placeholder_msg_id": None,
            "last_byte_offset": initial_offset,
            "buffer": "",
            "mode": "always",
            "finalized": False,
            "replies_at_create": 0,
            "sealed_placeholders": [],
        }
    if turn.get("finalized"):
        return 0

    # Tool-specific state. Lives alongside narrate state on the same
    # turn entry but with its own message id + buffer so neither writer
    # clobbers the other's Discord message.
    turn.setdefault("tool_msg_id", None)
    turn.setdefault("tool_buffer", "")
    turn.setdefault("tool_msg_replies_at_create", 0)
    turn.setdefault("sealed_tool_messages", [])

    token = read_bot_token(state_dir)
    if not token:
        log(f"no token at {state_dir} — skipping tool surface")
        return 0

    current_reply_count = count_discord_replies(transcript_path)

    # Mid-turn reply rotation: if a reply landed since the current tool
    # message was created, seal it (finalize the live "Tool trace…"
    # prefix to "Tool trace") and start a fresh tool message below.
    if (
        turn.get("tool_msg_id")
        and current_reply_count > turn.get("tool_msg_replies_at_create", 0)
    ):
        prev_id = turn["tool_msg_id"]
        prev_buf = turn["tool_buffer"]
        final_content = _tool_message_content(prev_buf, TOOL_PREFIX_FINAL)
        if discord_edit_message(token, chat_id, prev_id, final_content):
            log(f"sealed tool message {prev_id} for {turn_key} (reply)")
        turn["sealed_tool_messages"].append({"msg_id": prev_id, "buffer": prev_buf})
        turn["tool_msg_id"] = None
        turn["tool_buffer"] = ""

    # Append block to tool buffer; rotate on Discord cap
    candidate_tools = (
        turn["tool_buffer"] + "\n" + block if turn["tool_buffer"] else block
    )
    candidate_content = _tool_message_content(candidate_tools)
    if (
        turn.get("tool_msg_id")
        and len(candidate_content) > DISCORD_LIMIT
    ):
        prev_id = turn["tool_msg_id"]
        prev_buf = turn["tool_buffer"]
        final_content = _tool_message_content(prev_buf, TOOL_PREFIX_FINAL)
        if discord_edit_message(token, chat_id, prev_id, final_content):
            log(f"sealed tool message {prev_id} for {turn_key} (length cap)")
        turn["sealed_tool_messages"].append({"msg_id": prev_id, "buffer": prev_buf})
        turn["tool_msg_id"] = None
        turn["tool_buffer"] = block
    else:
        turn["tool_buffer"] = candidate_tools

    content = _tool_message_content(turn["tool_buffer"])
    if turn.get("tool_msg_id"):
        if discord_edit_message(token, chat_id, turn["tool_msg_id"], content):
            log(f"edited tool message for {turn_key}")
    else:
        msg_id = discord_send_message(token, chat_id, content)
        if msg_id:
            turn["tool_msg_id"] = msg_id
            turn["tool_msg_replies_at_create"] = current_reply_count
            log(f"created tool message {msg_id} for {turn_key}")

    state[turn_key] = turn
    _save_state(state)
    return 0


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("bad JSON input")
        return 0
    return handle_tool(payload)


if __name__ == "__main__":
    sys.exit(main())
