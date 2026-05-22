#!/usr/bin/env python3
"""UserPromptSubmit hook: inject current wall-clock time into context.

Claude Code injects `currentDate` (a date string) once at session start, but
in long-running agent sessions that value goes stale fast — the agent ends
up confidently reporting yesterday's date because that's what its system
context was frozen at. This hook prints a short timestamp line on every
UserPromptSubmit; Claude Code routes UserPromptSubmit hook stdout into the
model's context for that turn, so the agent always knows what time it is
*now*, not what time the session started.

Output is one line: `[wall clock: 2026-05-01 18:04 PT (Fri)]`. Local time
zone abbreviation comes from %Z. Silent on any error — never blocks the
prompt, never crashes the chain.
"""

from __future__ import annotations

import datetime
import sys
import traceback


def _now_line() -> str:
    now = datetime.datetime.now().astimezone()
    # %Z gives the local TZ name (e.g. PDT, PST, EST). On macOS / Linux
    # this resolves correctly without a TZ env var.
    return f"[wall clock: {now.strftime('%Y-%m-%d %H:%M %Z')} ({now.strftime('%a')})]"


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        sys.stdout.write(_now_line() + "\n")
    except Exception:
        sys.stderr.write(f"inject_time crash: {traceback.format_exc()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
