#!/usr/bin/env python3
"""PreToolUse scrubber: strip [MEMORY:...] / [JOURNAL:...] tags from Discord reply text.

Agents may emit memory/journal tags in their replies as a side-channel for the
Stop hook to capture (legacy save path). The tags are meant to be invisible to
the user — just state-mutation directives that ride along with the assistant's
reply. But when a tag ends up inside the `text` arg of the Discord reply tool
(rather than only in terminal text), it leaks visibly into Discord because
nothing strips it before send.

This hook fires on PreToolUse of `mcp__plugin_discord_discord__reply` and
mutates the tool input via the `updatedInput` field of `hookSpecificOutput`.
It removes any of these patterns from the `text` parameter:

  [MEMORY: ...]                          (with optional metadata attrs)
  [MEMORY type=project name="X" tags=a,b: ...]
  [MEMORY_EDIT: 42 | new text]
  [MEMORY_DELETE: 42]
  [JOURNAL: ...]
  [JOURNAL_DELETE: 42]

The Stop hook still picks the tags up from the assistant transcript (it sees
the original pre-mutation tool_use args via tool_use-block scanning). So saves
still happen — they just don't leak to Discord. Recommended save path is
still the explicit CLI; this hook is defensive for legacy tag emissions.

Pass-through cases (hook returns 0 with no mutation):
  - Tool name is not the Discord reply tool
  - text doesn't contain any of the tag patterns
  - Hook input is malformed (never block on parse errors)

Env vars:
  CCDK_SCRUB_TAGS_LOG  override log path
                      default ~/.local/state/cc-discord-kit/scrub_tags.log
"""

from __future__ import annotations

import json
import os
import re
import sys

DISCORD_REPLY_TOOL = "mcp__plugin_discord_discord__reply"

# Mirror the patterns used by the Stop hook. Kept in sync deliberately — if
# the tag syntax changes there, change here too.
TAG_PATTERNS = [
    re.compile(r"\[MEMORY(?:\s+[^:\]]+?)?:\s*.+?\]", re.DOTALL),
    re.compile(r"\[MEMORY_EDIT:\s*\d+\s*\|\s*.+?\]", re.DOTALL),
    re.compile(r"\[MEMORY_DELETE:\s*\d+\]"),
    re.compile(r"\[JOURNAL(?:\s+[^:\]]+?)?:\s*.+?\]", re.DOTALL),
    re.compile(r"\[JOURNAL_DELETE:\s*\d+\]"),
]


def _log_path() -> str:
    explicit = os.environ.get("CCDK_SCRUB_TAGS_LOG")
    if explicit:
        return explicit
    state_dir = os.path.expanduser("~/.local/state/cc-discord-kit")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(state_dir, "scrub_tags.log")


def _log(msg: str) -> None:
    try:
        with open(_log_path(), "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def _scrub(text: str) -> str:
    """Strip all matched tag patterns and collapse the whitespace they leave."""
    out = text
    for pat in TAG_PATTERNS:
        out = pat.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    return out.strip()


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    if payload.get("tool_name") != DISCORD_REPLY_TOOL:
        return 0

    tool_input = payload.get("tool_input") or {}
    text = tool_input.get("text", "")
    if not isinstance(text, str) or not text:
        return 0

    if "[MEMORY" not in text and "[JOURNAL" not in text:
        return 0

    cleaned = _scrub(text)
    if cleaned == text:
        return 0

    _log(f"scrubbed {len(text) - len(cleaned)} chars from reply text")

    # `updatedInput` is a full replacement, not a partial merge. Build the
    # merged input so all original args (chat_id, reply_to, files) survive.
    merged = dict(tool_input)
    merged["text"] = cleaned

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": merged,
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
