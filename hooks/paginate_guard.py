#!/usr/bin/env python3
"""PreToolUse guard: reject Discord replies that would auto-paginate a code block.

Discord's reply tool silently chunks any `text` longer than 2000 chars at
character boundaries. When the text contains a fenced ``` code block, that
auto-split butchers the block — table headers separated from rows, code
separated from comments, fences left dangling. The fix is always to write
the content to a `.md` or `.txt` file and attach via the reply tool's `files`
parameter instead. This hook enforces it at the harness level.

Activates only on `mcp__plugin_discord_discord__reply` calls.

Block conditions (BOTH must be true):
  1. `len(text) > MAT_PAGINATE_GUARD_LIMIT` (default 1900 — leaves headroom
     below Discord's 2000-char hard cap)
  2. text contains >= 2 fenced ``` markers (i.e. a code block exists)

Action when blocked: exit 2 with a structured stderr message that explains
the violation and tells the model exactly what to do — write the body to a
temp file and re-call reply with files=["..."].

Pass-through cases (hook returns 0):
  - Tool name is not the Discord reply tool
  - text is short (<= limit)
  - text is long but contains no code block (prose-only long messages still
    chunk badly, but they at least stay readable; the hook scope is the
    code-block-butchering case)
  - Hook input is malformed (we never want to block on parse errors)

Env vars:
  MAT_PAGINATE_GUARD_LIMIT  override the 1900-char threshold (must be int)
  MAT_PAGINATE_GUARD_LOG    override the log path
                            default ~/.local/state/multiagent-tools/paginate_guard.log
"""

from __future__ import annotations

import json
import os
import sys

DISCORD_REPLY_TOOL = "mcp__plugin_discord_discord__reply"


def _max_inline_chars() -> int:
    raw = os.environ.get("MAT_PAGINATE_GUARD_LIMIT", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 1900


def _log_path() -> str:
    explicit = os.environ.get("MAT_PAGINATE_GUARD_LOG")
    if explicit:
        return explicit
    state_dir = os.path.expanduser("~/.local/state/multiagent-tools")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(state_dir, "paginate_guard.log")


LOG_PATH = _log_path()
MAX_INLINE_CHARS = _max_inline_chars()


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("bad JSON input, passing through")
        return 0

    if payload.get("tool_name") != DISCORD_REPLY_TOOL:
        return 0

    args = payload.get("tool_input") or payload.get("arguments") or {}
    text = args.get("text", "") if isinstance(args, dict) else ""
    if not isinstance(text, str):
        return 0

    if len(text) <= MAX_INLINE_CHARS:
        return 0

    fence_count = text.count("```")
    if fence_count < 2:
        log(f"pass: long text but no code block (len={len(text)})")
        return 0

    chat_id = args.get("chat_id", "<unknown>")
    msg = (
        "BLOCKED: Discord reply text is "
        f"{len(text)} chars (limit {MAX_INLINE_CHARS}) AND contains a fenced "
        f"``` code block ({fence_count // 2}+ blocks). The Discord reply tool "
        "auto-chunks at character boundaries, which butchers code blocks "
        "(separates table headers from rows, code from comments, leaves "
        "fences dangling).\n\n"
        "Required action: write the body to a temp file and attach instead.\n\n"
        "1. Pick a descriptive filename, e.g. /tmp/output.md\n"
        "2. Write the long content (the part that includes the code block) "
        "to that file\n"
        "3. Re-call mcp__plugin_discord_discord__reply with:\n"
        "     - chat_id: same chat_id\n"
        "     - text: a short 1-3 line summary referring to the attachment\n"
        "     - files: [\"/tmp/output.md\"]\n\n"
        f"chat_id from blocked call: {chat_id}"
    )
    log(f"BLOCK chat={chat_id} len={len(text)} fences={fence_count}")
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
