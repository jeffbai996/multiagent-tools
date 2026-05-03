"""PreCompact hook: snapshot recent transcript activity to the journal
before Claude Code truncates context.

Hook input (stdin JSON, per Claude Code PreCompact hook contract):
  {
    "hook_event_name": "PreCompact",
    "session_id": "...",
    "transcript_path": "/path/to/transcript.jsonl",
    "cwd": "...",
    ...
  }

What this does:
  1. Reads the (about-to-be-compacted) transcript jsonl.
  2. Slices the recent window — last N user/assistant turns and the tools
     they involved.
  3. Builds a heuristic, no-LLM summary: user prompt openings, tool calls,
     file paths touched.
  4. Writes one journal entry via add_journal with
     source="precompact:<bot>", actor=<bot>, tags=["compact", session_short].

Fires for both manual /compact AND auto-compaction (the harness matcher
distinguishes them; we treat both identically — the breadcrumb is valuable
either way).

Exits 0 always; never blocks compaction.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any

# Routing:
#   - MULTIAGENT_URL set → POST to <URL>/api/journal (HTTP-mode caller)
#   - else                → import store.py and write directly (local-mode caller)
MULTIAGENT_URL = os.environ.get("MULTIAGENT_URL", "").rstrip("/")

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PARENT = os.path.dirname(_HERE)

store = None
if not MULTIAGENT_URL:
    sys.path.insert(0, _MODULE_PARENT)
    import store  # noqa: E402

LOG_PATH = os.path.join(_HERE, "precompact_hook.log")
# Fall back to a writeable spot if the script lives in a read-only location
if not os.access(_HERE, os.W_OK):
    LOG_PATH = os.path.expanduser("~/.local/share/multiagent-tools/precompact_hook.log")

# Bot identity. Set MULTIAGENT_BOT in env for explicit naming. Otherwise:
# 1) derive from CLAUDE_CONFIG_DIR last path segment (e.g. ~/.claude-alt → "claude-alt")
# 2) fall back to hostname.
HOST = socket.gethostname()
explicit = os.environ.get("MULTIAGENT_BOT", "").strip()
if explicit:
    BOT_NAME = explicit
else:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        BOT_NAME = os.path.basename(cfg.rstrip("/")) or HOST.lower() or "agent"
    else:
        BOT_NAME = HOST.lower() or "agent"

# How many recent transcript entries to consider for the summary.
WINDOW_LINES = 60
# Cap on individual snippet length so the journal entry stays readable.
SNIPPET_LIMIT = 240


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def _extract_text(msg_obj: dict) -> str:
    msg = msg_obj.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)
    return ""


def _extract_tool_uses(msg_obj: dict) -> list[dict]:
    """Return list of {name, input} for tool_use blocks in an assistant entry."""
    msg = msg_obj.get("message", {})
    content = msg.get("content", [])
    if not isinstance(content, list):
        return []
    out = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_use":
            out.append({"name": c.get("name", ""), "input": c.get("input", {})})
    return out


def read_recent(transcript_path: str, n: int = WINDOW_LINES) -> list[dict]:
    """Last n parseable jsonl entries, oldest-first."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
    except OSError:
        return []
    parsed = []
    for line in lines[-n:]:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


def _truncate(s: str, lim: int = SNIPPET_LIMIT) -> str:
    s = s.strip().replace("\n", " ")
    if len(s) <= lim:
        return s
    return s[: lim - 1] + "…"


def build_summary(entries: list[dict]) -> str:
    """Heuristic, no-LLM summary of the recent window.

    Goal: a *breadcrumb*, not a session log. Output target ≤ 500 chars.
    Bash commands and git shas removed — those live in shell history and
    `git log` and were duplicating information that already had a home.
    """
    user_prompts: list[str] = []
    files_touched: set[str] = set()
    discord_replies = 0
    assistant_text_chunks: list[str] = []

    for entry in entries:
        t = entry.get("type")
        if t == "user":
            text = _extract_text(entry)
            if text:
                user_prompts.append(_truncate(text, 80))
            continue
        if t != "assistant":
            continue
        for tu in _extract_tool_uses(entry):
            name = tu["name"]
            inp = tu.get("input") or {}
            if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                fp = inp.get("file_path") or inp.get("notebook_path") or ""
                if fp:
                    files_touched.add(fp)
            elif name.startswith("mcp__plugin_discord_discord__reply"):
                discord_replies += 1
        text = _extract_text(entry)
        if text:
            assistant_text_chunks.append(_truncate(text, 160))

    parts: list[str] = []
    if user_prompts:
        parts.append("Last prompts:")
        for p in user_prompts[-3:]:
            parts.append(f"  • {p}")
    if files_touched:
        parts.append(f"Files written ({len(files_touched)}):")
        # Show basenames only — full paths blew up the line count.
        seen_basenames: list[str] = []
        for fp in sorted(files_touched):
            base = os.path.basename(fp) or fp
            if base not in seen_basenames:
                seen_basenames.append(base)
        for base in seen_basenames[:6]:
            parts.append(f"  • {base}")
        if len(seen_basenames) > 6:
            parts.append(f"  …+{len(seen_basenames) - 6} more")
    if discord_replies:
        parts.append(f"Discord replies sent: {discord_replies}")
    if not parts and assistant_text_chunks:
        parts.append("Assistant tail:")
        parts.append(f"  > {assistant_text_chunks[-1]}")

    return "\n".join(parts) if parts else "(no recoverable activity in window)"


def _add_journal_http(text: str, source: str, actor: str, tags: list[str]) -> None:
    """POST to <MULTIAGENT_URL>/api/journal. Best-effort: log + swallow errors."""
    url = f"{MULTIAGENT_URL}/api/journal"
    body = json.dumps({
        "text": text, "source": source, "actor": actor, "tags": tags,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "cc-precompact-hook (multiagent-tools, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if not (200 <= resp.status < 300):
                log(f"http POST {url} status={resp.status}")
    except urllib.error.HTTPError as e:
        log(f"http POST {url} HTTP {e.code}: {e.read()[:200]!r}")
    except Exception as e:
        log(f"http POST {url} failed: {e}")


def _add_journal(text: str, source: str, actor: str, tags: list[str]) -> None:
    """Route through HTTP if MULTIAGENT_URL is set, else direct import."""
    if MULTIAGENT_URL:
        _add_journal_http(text, source, actor, tags)
    else:
        store.add_journal(text, source=source, actor=actor, tags=tags)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload: dict = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log(f"bad JSON input: {raw[:200]!r}")
        return 0

    transcript = payload.get("transcript_path", "")
    session_id = payload.get("session_id", "")
    session_short = (session_id[:8] if session_id else "?")
    matcher = payload.get("hook_event_name", "PreCompact")  # informational

    entries = read_recent(transcript)
    if not entries:
        log(f"no entries readable from {transcript!r}")
        return 0

    try:
        summary = build_summary(entries)
    except Exception:
        log(f"build_summary crashed:\n{traceback.format_exc()}")
        return 0

    text = (
        f"Pre-compaction snapshot — bot={BOT_NAME} host={HOST} "
        f"session={session_short}\n\n{summary}"
    )

    try:
        _add_journal(
            text,
            source=f"precompact:{BOT_NAME}",
            actor=BOT_NAME,
            tags=["compact", f"session:{session_short}"],
        )
    except Exception:
        log(f"add_journal crashed:\n{traceback.format_exc()}")
        return 0

    log(
        f"{matcher} bot={BOT_NAME} session={session_short} "
        f"entries={len(entries)} chars={len(text)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
