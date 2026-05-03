"""Memory merge utilities — pure functions, no side effects.

Given two memories (winner + loser), produce:
  - a suggested combined text the user edits before saving
  - a regex-rewriter that updates `#<old>` and `[[memory:<old>]]` in any text
    so backlinks pointing at the loser get redirected to the winner

The actual merge orchestration (writing edits, deleting the loser, recording
history) lives in server.py and uses these pure functions plus history.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# Mirror rendering.py's patterns — same boundary rules so we rewrite
# exactly what the linkifier targets, no false positives like #fff colors.
_MEM_ID_PAT = re.compile(r"(?<![a-zA-Z0-9_/])#(\d{1,6})\b")
_LONG_MEM_PAT = re.compile(r"\[\[memory:(\d{1,6})\]\]")


def rewrite_refs(text: str, old_id: int, new_id: int) -> tuple[str, int]:
    """Rewrite `#<old_id>` and `[[memory:<old_id>]]` to point at new_id.

    Returns (rewritten_text, replacement_count).
    """
    if not text:
        return text, 0
    count = 0

    def short(m: re.Match) -> str:
        nonlocal count
        if int(m.group(1)) == old_id:
            count += 1
            return f"#{new_id}"
        return m.group(0)

    def long(m: re.Match) -> str:
        nonlocal count
        if int(m.group(1)) == old_id:
            count += 1
            return f"[[memory:{new_id}]]"
        return m.group(0)

    text = _LONG_MEM_PAT.sub(long, text)
    text = _MEM_ID_PAT.sub(short, text)
    return text, count


def suggest_merged_text(
    winner_text: str,
    loser_text: str,
    *,
    loser_id: int,
    loser_name: str = "",
) -> str:
    """Produce a deterministic combined text. User edits before saving.

    Empty texts collapse cleanly. Otherwise: winner first, then a divider
    with provenance, then the loser's text.
    """
    winner = (winner_text or "").rstrip()
    loser = (loser_text or "").rstrip()
    if not loser:
        return winner
    if not winner:
        return loser
    today = datetime.now(timezone.utc).date().isoformat()
    label = f"#{loser_id}"
    if loser_name:
        label = f"{label} {loser_name}"
    return (
        f"{winner}\n\n"
        f"---\n"
        f"_merged from {label} on {today}:_\n\n"
        f"{loser}"
    )
