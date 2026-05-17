"""SessionStart hook — inject shared memories once per session.

Split dump strategy:
- feedback type: full body (behavioral rules need to be pre-loaded)
- all other types: index only (titles + tags; look up body on demand)
- journal: last 10 days

Paid once per session boot, then cached for the remainder of the session.
UserPromptSubmit hook handles per-turn refresh with compact index + 1d journal.
"""

from __future__ import annotations

import os
import sys
import traceback

_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _MODULE_DIR)
import store  # noqa: E402

LOG_PATH = os.path.join(store.DATA_DIR, "boot_hook.log")


def _detect_bot() -> str | None:
    """Override with MULTIAGENT_BOT, else derive from CLAUDE_CONFIG_DIR."""
    explicit = os.environ.get("MULTIAGENT_BOT", "").strip()
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.basename(cfg.rstrip("/")) or None
    return None


def log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def main() -> int:
    try:
        bot = _detect_bot()
        mem_full = store.format_memories_for_prompt(bot=bot, types=["feedback"])
        mem_idx = store.format_memories_index(bot=bot, exclude_types=["feedback"])
        if mem_full:
            print(mem_full)
            print()
        if mem_idx:
            print(mem_idx)
            print()
        jou = store.format_journal_for_prompt(days=10)
        if jou:
            print(jou)
        log(f"session_start bot={bot}: injected {len(mem_full)} full + {len(mem_idx)} idx + {len(jou)} jou chars")
    except Exception:
        log(f"session_start crashed:\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
