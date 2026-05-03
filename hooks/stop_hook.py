"""Stop hook: scan the just-ended assistant turn for memory/journal tags.

Hook input (stdin JSON, per Claude Code Stop hook contract):
  {
    "hook_event_name": "Stop",
    "transcript_path": "/path/to/transcript.jsonl",
    ...
  }

We tail the transcript, find the most recent user + assistant messages.

Tags processed (only when user message contains a save-intent keyword):
  [MEMORY: text]                          → save_memory(text, type=feedback)
  [MEMORY type=project: text]             → save_memory(text, type=...)
  [MEMORY name="X" tags=a,b about=user: text]
                                          → save with metadata
  [MEMORY_EDIT: id | new text]            → edit_memory(id, new_text)
  [MEMORY_DELETE: id]                     → remove_memory(id)
  [JOURNAL: text]                         → add_journal(text, actor=<bot>)
  [JOURNAL_DELETE: id]                    → remove_journal(id)

Keyword-gate: tags are only honored when the most recent user message
contains an explicit save/recall verb. This prevents meta-discussion of
tag syntax from triggering real writes. To talk *about* the syntax without
firing it, use [MEMORY-EXAMPLE: ...] (anything matching MEMORY-EXAMPLE /
JOURNAL-EXAMPLE is stripped before scanning).

Exits 0 always; never blocks turn end.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import traceback

# This file lives in modules/multiagent-tools/hooks/, store.py lives one dir up.
_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _MODULE_DIR)
import store  # noqa: E402

LOG_PATH = os.path.join(store.DATA_DIR, "stop_hook.log")

# Agent identity. MULTIAGENT_BOT in env wins; otherwise derive from
# CLAUDE_CONFIG_DIR last path segment, falling back to hostname.
HOST = socket.gethostname()
_explicit = os.environ.get("MULTIAGENT_BOT", "").strip()
if _explicit:
    BOT_NAME = _explicit
else:
    _cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if _cfg:
        BOT_NAME = os.path.basename(_cfg.rstrip("/")) or HOST.lower() or "agent"
    else:
        BOT_NAME = HOST.lower() or "agent"


def log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


_DISCORD_REPLY_TOOLS = {"mcp__plugin_discord_discord__reply"}


def _extract_text(msg_obj: dict) -> str:
    """Pull text out of an assistant/user transcript entry.

    Reads `type:'text'` content blocks AND the `text` argument of Discord
    reply tool calls. The tool-arg path matters because bots sometimes emit
    [MEMORY:...] / [JOURNAL:...] tags only inside the Discord reply text
    (not in their terminal output) — without scanning tool_use args we'd
    silently drop those saves.
    """
    msg = msg_obj.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "tool_use" and c.get("name", "") in _DISCORD_REPLY_TOOLS:
                # Discord reply tool — pull the `text` arg so any tags inside
                # the user-facing message body get processed.
                arg_text = (c.get("input") or {}).get("text", "")
                if isinstance(arg_text, str) and arg_text:
                    parts.append(arg_text)
        return "\n".join(parts)
    return ""


def read_last_messages(transcript_path: str) -> tuple[str, str]:
    """Return (last_user_text, last_assistant_text) from the transcript jsonl."""
    if not transcript_path or not os.path.exists(transcript_path):
        return "", ""
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
    except OSError:
        return "", ""
    last_user = ""
    last_assistant = ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "assistant" and not last_assistant:
            last_assistant = _extract_text(obj)
        elif t == "user" and not last_user:
            last_user = _extract_text(obj)
        if last_user and last_assistant:
            break
    return last_user, last_assistant


def parse_kv_attrs(attr_str: str) -> dict:
    """Parse `key=value key2="quoted value" tags=a,b,c` style attrs."""
    out: dict = {}
    pattern = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s]+))')
    for m in pattern.finditer(attr_str):
        key, q_val, raw_val = m.group(1), m.group(2), m.group(3)
        out[key] = q_val if q_val is not None else raw_val
    return out


# ─────────── tag handlers ───────────

MEM_RE = re.compile(r"\[MEMORY(?:\s+([^:\]]+?))?:\s*(.+?)\]", re.DOTALL)
MEM_EDIT_RE = re.compile(r"\[MEMORY_EDIT:\s*(\d+)\s*\|\s*(.+?)\]", re.DOTALL)
MEM_DEL_RE = re.compile(r"\[MEMORY_DELETE:\s*(\d+)\]")
JOU_RE = re.compile(r"\[JOURNAL(?:\s+([^:\]]+?))?:\s*(.+?)\]", re.DOTALL)
JOU_DEL_RE = re.compile(r"\[JOURNAL_DELETE:\s*(\d+)\]")

SAVE_KEYWORDS_RE = re.compile(
    r"\b(remember|memori[sz]e|forget|"
    r"save\s+(?:this|that|it|to|the)|"
    r"note\s+(?:this|that|it|down|to)|"
    r"pin\s+(?:this|that|it)|"
    r"stash\s+(?:this|that|it)|"
    r"remind\s+me|"
    r"journal\s+(?:this|that|it|entry)|"
    r"add\s+(?:a\s+)?(?:memory|journal|note|entry)|"
    r"delete\s+(?:memory|#\d+|entry|journal)|"
    r"remove\s+(?:memory|#\d+|entry|journal)|"
    r"edit\s+(?:memory|#\d+|entry))\b",
    re.I,
)

EXAMPLE_RE = re.compile(
    r"\[(?:MEMORY|JOURNAL)-EXAMPLE(?:\s+[^:\]]+?)?:\s*.+?\]",
    re.DOTALL,
)


def user_asked_to_save(user_text: str) -> bool:
    """True iff the user's last message contains a save-intent verb."""
    if not user_text:
        return False
    return bool(SAVE_KEYWORDS_RE.search(user_text))


def _attr_list(attrs: dict, key: str) -> list[str]:
    val = attrs.get(key, "")
    return [v.strip() for v in val.split(",") if v.strip()]


def process_text(text: str) -> dict:
    counts = {"saved": 0, "edited": 0, "deleted": 0,
              "journaled": 0, "journal_deleted": 0}

    text = EXAMPLE_RE.sub("", text)

    for m in MEM_RE.finditer(text):
        attr_str, body = m.group(1) or "", m.group(2).strip()
        if not body:
            continue
        attrs = parse_kv_attrs(attr_str)
        store.save_memory(
            body,
            type=attrs.get("type", "feedback"),
            name=attrs.get("name", ""),
            tags=_attr_list(attrs, "tags"),
            about=_attr_list(attrs, "about"),
            bot=_attr_list(attrs, "bot") or None,
        )
        counts["saved"] += 1

    for m in MEM_EDIT_RE.finditer(text):
        mid, new = int(m.group(1)), m.group(2).strip()
        if store.edit_memory(mid, new):
            counts["edited"] += 1

    for m in MEM_DEL_RE.finditer(text):
        if store.remove_memory(int(m.group(1))):
            counts["deleted"] += 1

    for m in JOU_RE.finditer(text):
        attr_str, body = m.group(1) or "", m.group(2).strip()
        if not body:
            continue
        attrs = parse_kv_attrs(attr_str)
        tags = _attr_list(attrs, "tags")
        store.add_journal(body, source=attrs.get("source", f"hook:{HOST}"),
                          actor=attrs.get("actor", BOT_NAME), tags=tags)
        counts["journaled"] += 1

    for m in JOU_DEL_RE.finditer(text):
        if store.remove_journal(int(m.group(1))):
            counts["journal_deleted"] += 1

    return counts


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload: dict = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log(f"bad JSON input: {raw[:200]!r}")
        return 0

    transcript = payload.get("transcript_path", "")
    user_text, assistant_text = read_last_messages(transcript)
    if not assistant_text:
        return 0

    if not user_asked_to_save(user_text):
        if MEM_RE.search(assistant_text) or JOU_RE.search(assistant_text) \
                or MEM_DEL_RE.search(assistant_text) or MEM_EDIT_RE.search(assistant_text) \
                or JOU_DEL_RE.search(assistant_text):
            log(f"gated bot={BOT_NAME} (no save-intent in user msg) "
                f"user={user_text[:80]!r}")
        return 0

    try:
        counts = process_text(assistant_text)
    except Exception:
        log(f"process_text crashed:\n{traceback.format_exc()}")
        return 0

    if any(counts.values()):
        log(f"{payload.get('hook_event_name', '?')} bot={BOT_NAME} {counts}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
