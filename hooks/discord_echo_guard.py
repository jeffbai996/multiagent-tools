#!/usr/bin/env python3
"""Stop hook: block turn end when a Discord-originated message went unanswered.

The recurring failure mode this prevents: the agent sees a Discord <channel>
tag in the user message, drafts a response in the terminal, but forgets to
actually call mcp__plugin_discord_discord__reply (or react). To the user
in Discord this reads as silence — the agent looks broken or rude.

This is a Stop hook because we want to catch the failure at the latest
possible moment, *before* the assistant turn ends, and force the model to
send the reply before it's allowed to terminate.

Trigger: the current turn's user message contains a Discord channel tag
AND the current turn's assistant entries contain ZERO reply tool calls
AND ZERO react tool calls.

When triggered: exit 2 with stderr explaining the violation. Per Claude
Code's hook contract, exit 2 on Stop blocks the stop event and surfaces
stderr to the model, which then has a chance to send the reply before
trying to stop again.

Pass-through cases (exit 0):
  - Current turn's user message has no Discord channel tag (terminal use)
  - The turn called the reply tool (the obvious correct case)
  - The turn called the react tool (an explicit content react is itself a
    valid response)
  - Malformed input / missing transcript (never block on hook errors)
  - The "stop_hook_active" flag is set (the model has already been told and
    is retrying — let it through to avoid infinite loops if a tool is broken)

Module reuses parsers from react_hook.py — same file watches for the same
Discord tags, so any parsing change there propagates here automatically.

Env vars:
  CCDK_ECHO_GUARD_LOG  override log path
                      default ~/.local/state/cc-discord-kit/discord_echo_guard.log
"""

from __future__ import annotations

import json
import os
import sys

# Reuse parsers from react_hook so they stay in lockstep.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from react_hook import (  # type: ignore[import-not-found]
    _last_user_entry,
    parse_discord_origins,
    assistant_called_discord_reply,
    assistant_called_discord_react,
)


def _log_path() -> str:
    explicit = os.environ.get("CCDK_ECHO_GUARD_LOG")
    if explicit:
        return explicit
    state_dir = os.path.expanduser("~/.local/state/cc-discord-kit")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(state_dir, "discord_echo_guard.log")


LOG_PATH = _log_path()


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def _extract_user_text(user_entry: dict | None) -> str:
    """Get plain text from a transcript user entry."""
    if not user_entry:
        return ""
    msg = user_entry.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                txt = c.get("text", "")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("bad JSON input, passing through")
        return 0

    if payload.get("stop_hook_active"):
        log("stop_hook_active=true — pass-through to avoid loop")
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        log(f"no transcript_path or missing: {transcript_path!r} — pass")
        return 0

    user_entry = _last_user_entry(transcript_path)
    user_text = _extract_user_text(user_entry)
    origins = parse_discord_origins(user_text)
    if not origins:
        return 0

    if assistant_called_discord_reply(transcript_path):
        log(f"PASS reply origins={origins}")
        return 0

    if assistant_called_discord_react(transcript_path):
        log(f"PASS react origins={origins}")
        return 0

    last_chat, last_msg = origins[-1]
    msg = (
        "BLOCKED: This turn was triggered by a Discord message "
        f"(chat_id={last_chat}, message_id={last_msg}) but you did not call "
        "mcp__plugin_discord_discord__reply or mcp__plugin_discord_discord__react. "
        "Terminal-only responses are invisible to the Discord sender — they "
        "see silence.\n\n"
        "Required action before this turn can end:\n"
        f"  1. Call mcp__plugin_discord_discord__reply with chat_id={last_chat} "
        "and your response text\n"
        f"  2. Or, if a reaction is the appropriate response, call "
        f"mcp__plugin_discord_discord__react with chat_id={last_chat}, "
        f"message_id={last_msg}, and an emoji\n\n"
        "Then this Stop hook will pass and the turn can end normally."
    )
    log(f"BLOCK origins={origins} no_reply no_react")
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
