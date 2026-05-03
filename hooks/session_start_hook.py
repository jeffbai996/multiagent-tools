"""SessionStart hook — inject full memory store once per session.

Output goes to stdout and is consumed by Claude Code as additional context
prepended to the session. Paid once per session boot, then cached for the
remainder of the session.

The full dump is ~33k tokens for 60 entries. Worth paying once for full
recall; too expensive to repeat every turn. UserPromptSubmit hook handles
per-turn refresh with a compact index instead.
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
        mem = store.format_memories_for_prompt(bot=bot)
        if mem:
            print(mem)
            print()
        jou = store.format_journal_for_prompt(days=30)
        if jou:
            print(jou)
        log(f"session_start bot={bot}: injected {len(mem)} mem + {len(jou)} jou chars")
    except Exception:
        log(f"session_start crashed:\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
