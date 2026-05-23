#!/usr/bin/env python3
"""UserPromptSubmit hook — resolve Discord <@USER_ID> mentions to names.

Fires on every turn. If the user prompt contains a Discord channel tag
(source="plugin:discord:discord"), scans for <@ID> and <@!ID> mention
patterns and injects a resolved-mentions block into the context so the
model knows who was addressed without relying on memory recall.

Output format (printed to stdout → injected as system-reminder):

  Discord mentions resolved:
    <@111111111111111111> → @Alice
    <@222222222222222222> → @Bob
  Addressed: @Alice, @Bob

If the message addresses this agent's own ID, an explicit note is added:
  WARN You (@this-agent) were mentioned in this message.

Exit 0 always — never block the turn.

Configuration:
  Roster: ~/.config/cc-discord-kit/discord_roster.json
          { "<user_id>": "<display name>", ... }
          Override path with CCDK_DISCORD_ROSTER.

  Own ID: CCDK_BOT_DISCORD_USER_ID  (the running agent's own Discord user_id)
          Falls back to CCDK_BOT env / hostname-derived self name if unset.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _roster_path() -> Path:
    explicit = os.environ.get("CCDK_DISCORD_ROSTER", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path("~/.config/cc-discord-kit/discord_roster.json").expanduser()


def _load_roster() -> dict[str, str]:
    path = _roster_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _own_id() -> str:
    """The Discord user_id of the agent running this hook (best-effort)."""
    explicit = os.environ.get("CCDK_BOT_DISCORD_USER_ID", "").strip()
    if explicit:
        return explicit
    return ""  # unknown — self-mention check will silently skip


def _own_name(roster: dict[str, str], own: str) -> str:
    if own and own in roster:
        return roster[own]
    return os.environ.get("CCDK_BOT", "").strip() or "this agent"


def main() -> int:
    payload = sys.stdin.read()

    if 'source="plugin:discord:discord"' not in payload:
        return 0

    ids_found = re.findall(r"<@!?(\d+)>", payload)
    if not ids_found:
        return 0

    seen: set[str] = set()
    unique_ids = [i for i in ids_found if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]

    roster = _load_roster()
    own = _own_id()
    own_name = _own_name(roster, own)

    lines = ["Discord mentions resolved:"]
    addressed_names: list[str] = []
    self_mentioned = False

    for uid in unique_ids:
        name = roster.get(uid, f"unknown ({uid})")
        lines.append(f"  <@{uid}> -> @{name}")
        addressed_names.append(f"@{name}")
        if own and uid == own:
            self_mentioned = True

    if addressed_names:
        lines.append(f"Addressed: {', '.join(addressed_names)}")
    if self_mentioned:
        lines.append(f"WARN You (@{own_name}) were mentioned in this message.")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
