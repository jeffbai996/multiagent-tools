"""React hook: drop emoji reactions on Discord messages to signal agent state.

Called by multiple Claude Code hook events with different `mode` args:

  --mode received    UserPromptSubmit: 👀 on inbound Discord message
  --mode working     PreToolUse on long-running tools: 🤔 / 🔨 / 🔍 / 🤝
  --mode replied     PostToolUse on Discord reply tool: ✅
  --mode crosscheck  PostToolUse on Discord reply tool: 🔀 if outbound
                     chat_id doesn't match any inbound <channel> tag
                     from the current turn (cross-channel leak warn)
  --mode terminal    Stop: 🖥️ if Discord-origin and no reply was sent
  --mode memorized   Stop: 💾 if a memory/journal write happened this turn
  --mode compacted   PreCompact: 🗜️ on the most recent inbound
  --mode notified    External: 🔔 — stamped by notify_hook.py when a
                     system notification was mirrored to Discord

Each mode reads the relevant transcript context, decides whether a reaction
is warranted, and POSTs to the Discord REST API directly using the bot
token from `<CLAUDE_CONFIG_DIR>/channels/discord/.env`.

Hooks must be fast and silent — all reactions fire-and-forget with short
HTTP timeouts. Failures are logged and never propagate.

Env vars:
  CCDK_REACT_HOOK_LOG    override log path
                        (default ~/.local/state/cc-discord-kit/react_hook.log)
  CCDK_REACT_HOOK_STATE  override per-message idempotency state path
                        (default ~/.local/state/cc-discord-kit/react_hook_state.json)
  DISCORD_STATE_DIR     explicit override for the bot's channel state dir
                        (default <CLAUDE_CONFIG_DIR>/channels/discord)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time as _time
import traceback
import urllib.parse
import urllib.request
from typing import Any

def _state_root() -> str:
    """Shared root for log + state files. Created on demand."""
    state_dir = os.path.expanduser("~/.local/state/cc-discord-kit")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        pass
    return state_dir


def _resolve_log_path() -> str:
    explicit = os.environ.get("CCDK_REACT_HOOK_LOG")
    if explicit:
        return explicit
    return os.path.join(_state_root(), "react_hook.log")


LOG_PATH = _resolve_log_path()


def _resolve_state_path() -> str:
    explicit = os.environ.get("CCDK_REACT_HOOK_STATE")
    if explicit:
        return explicit
    return os.path.join(_state_root(), "react_hook_state.json")


STATE_PATH = _resolve_state_path()
# Drop entries older than this. One hour is well past any single turn but
# avoids unbounded growth if Discord state ever drifts from local memory.
STATE_TTL_SEC = 3600


def _load_state() -> dict:
    """Load the per-message reaction state. Returns {} on any error.

    Shape: { "<chat_id>:<msg_id>": {"applied": ["👀", "🤔", ...], "ts": float} }
    """
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    """Persist state to disk. Prunes stale entries before writing.

    Failures are silent — state is best-effort, the hook must never block.
    Atomic write (write-temp + rename) so concurrent writers from multiple
    agents sharing the state file don't corrupt it. Worst case under
    contention: a lost update means one duplicate PUT (one extra ping).

    Reserved top-level keys (start with `_`) escape TTL pruning — they're
    long-lived single-value entries (e.g. `_last_silent`) that don't need
    timestamps. Per-message reaction entries still age out at STATE_TTL_SEC.
    """
    try:
        cutoff = _time.time() - STATE_TTL_SEC
        pruned = {}
        for k, v in state.items():
            if k.startswith("_"):
                pruned[k] = v
            elif isinstance(v, dict) and v.get("ts", 0) > cutoff:
                pruned[k] = v
        tmp_path = f"{STATE_PATH}.tmp.{os.getpid()}"
        with open(tmp_path, "w") as f:
            json.dump(pruned, f)
        os.rename(tmp_path, STATE_PATH)
    except OSError:
        pass


_BOT_ID_CACHE: str | None = None


def _bot_id() -> str:
    """Stable identifier for the agent whose process is running this hook.

    Multiple agents on the same host (each with its own CLAUDE_CONFIG_DIR,
    e.g. ~/.claude, ~/.claude-second, ~/.claude-third) share STATE_PATH. Without
    partitioning, one agent's _mark_removed clobbers another's marks, and
    one agent's PUT gets skipped because the other already marked the emoji
    applied — even though to Discord they're distinct users and SHOULD react
    independently.

    Derived from the agent's CLAUDE config directory basename. state_dir
    typically ends in "<bot_root>/channels/discord", so we walk up two
    parents from there. Cached because it never changes within one
    process."""
    global _BOT_ID_CACHE
    if _BOT_ID_CACHE is not None:
        return _BOT_ID_CACHE
    state_dir = detect_discord_state_dir().rstrip("/")
    # state_dir = .../.claude/channels/discord  →  walk up to .claude
    bot_root = os.path.dirname(os.path.dirname(state_dir))
    _BOT_ID_CACHE = os.path.basename(bot_root) or "default"
    return _BOT_ID_CACHE


def _state_key(chat_id: str, msg_id: str) -> str:
    return f"{_bot_id()}:{chat_id}:{msg_id}"


def _has_applied(chat_id: str, msg_id: str, emoji: str) -> bool:
    """Has THIS bot already PUT this emoji on this message?"""
    state = _load_state()
    entry = state.get(_state_key(chat_id, msg_id))
    if not isinstance(entry, dict):
        return False
    return emoji in (entry.get("applied") or [])


def _mutate_state_locked(mutate_fn) -> None:
    """Read-modify-write STATE_PATH under an exclusive flock.

    Without locking, two bot processes can interleave their
    _load_state -> mutate -> _save_state cycles and one's mark gets dropped.
    flock serializes the critical section across processes; the atomic rename
    in _save_state still handles crash-safety on top."""
    lock_path = STATE_PATH + ".lock"
    try:
        with open(lock_path, "a+") as lf:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # locking unavailable — degrade to best-effort
            state = _load_state()
            mutate_fn(state)
            _save_state(state)
    except OSError:
        # Couldn't open the lock file — fall back to unlocked mutate.
        state = _load_state()
        mutate_fn(state)
        _save_state(state)


def _mark_applied(chat_id: str, msg_id: str, emoji: str) -> None:
    key = _state_key(chat_id, msg_id)

    def _mut(state: dict) -> None:
        entry = state.get(key) or {"applied": [], "ts": 0}
        if not isinstance(entry, dict):
            entry = {"applied": [], "ts": 0}
        applied = entry.get("applied") or []
        if emoji not in applied:
            applied.append(emoji)
        entry["applied"] = applied
        entry["ts"] = _time.time()
        state[key] = entry

    _mutate_state_locked(_mut)


def _last_silent_key(chat_id: str) -> str:
    """One slot per (bot, channel). Stores the most recent message_id where
    we ended a turn silently (parked 🖥️). Used to slide the 🖥️ forward
    instead of stamping every silent turn — keeps idle channels clean."""
    return f"{_bot_id()}:{chat_id}"


def _get_last_silent(chat_id: str) -> str | None:
    state = _load_state()
    table = state.get("_last_silent")
    if not isinstance(table, dict):
        return None
    val = table.get(_last_silent_key(chat_id))
    return val if isinstance(val, str) and val else None


def _set_last_silent(chat_id: str, msg_id: str) -> None:
    key = _last_silent_key(chat_id)

    def _mut(state: dict) -> None:
        table = state.get("_last_silent")
        if not isinstance(table, dict):
            table = {}
        table[key] = msg_id
        state["_last_silent"] = table

    _mutate_state_locked(_mut)


def _clear_last_silent(chat_id: str) -> None:
    """Drop the parked 🖥️ slot for this channel — called when a real reply
    happens (✅ takes over) or when we slide 🖥️ forward to a new message."""
    key = _last_silent_key(chat_id)

    def _mut(state: dict) -> None:
        table = state.get("_last_silent")
        if isinstance(table, dict) and key in table:
            del table[key]
            state["_last_silent"] = table

    _mutate_state_locked(_mut)


# ─── Last-replied slide-forward (same shape as last_silent) ───
# Per-(bot, channel) slot for the most recent message the bot replied to.
# When a new reply lands, DELETE ✅ from the previous parked message before
# PUTting on the new one. Net: at most one floating ✅ per channel at any
# time, on the most recent reply. Old ✅'s scroll out and don't need to
# linger as historical tombstones — once a reply is sent, the reply itself
# is the durable record; ✅ on every old reply just creates visual clutter.

def _last_replied_key(chat_id: str) -> str:
    return f"{_bot_id()}:{chat_id}"


def _get_last_replied(chat_id: str) -> str | None:
    state = _load_state()
    table = state.get("_last_replied")
    if not isinstance(table, dict):
        return None
    val = table.get(_last_replied_key(chat_id))
    return val if isinstance(val, str) and val else None


def _set_last_replied(chat_id: str, msg_id: str) -> None:
    key = _last_replied_key(chat_id)

    def _mut(state: dict) -> None:
        table = state.get("_last_replied")
        if not isinstance(table, dict):
            table = {}
        table[key] = msg_id
        state["_last_replied"] = table

    _mutate_state_locked(_mut)


def _clear_last_replied(chat_id: str) -> None:
    key = _last_replied_key(chat_id)

    def _mut(state: dict) -> None:
        table = state.get("_last_replied")
        if isinstance(table, dict) and key in table:
            del table[key]
            state["_last_replied"] = table

    _mutate_state_locked(_mut)


def _mark_removed(chat_id: str, msg_id: str, emoji: str) -> None:
    key = _state_key(chat_id, msg_id)

    def _mut(state: dict) -> None:
        entry = state.get(key)
        if not isinstance(entry, dict):
            return
        applied = entry.get("applied") or []
        if emoji in applied:
            applied = [e for e in applied if e != emoji]
            entry["applied"] = applied
            entry["ts"] = _time.time()
            state[key] = entry

    _mutate_state_locked(_mut)


# Map of state → unicode emoji
EMOJI = {
    "received":   "👀",
    "thinking":   "🤔",
    "editing":    "🔧",
    "researching": "🌐",
    "delegating": "🤖",
    "replied":    "✅",
    "terminal":   "🖥️",
    "memorized":  "💾",
    "compacted":  "📝",
    "notified":   "🔔",
    "errored":    "❌",
    "denied":     "⚠️",
    "crossposted": "🔀",
}

# Tool name → state. Order checked in CATEGORY_PRIORITY below; first match wins.
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
RESEARCH_TOOLS = {"WebFetch", "WebSearch", "mcp__plugin_context7_context7__query-docs"}
DELEGATE_TOOLS = {"Agent", "Task"}
THINKING_TOOLS = {"Bash"}  # everything else slow — fallback to thinking

# Discord plugin tool names (any of these = we replied)
DISCORD_REPLY_TOOLS = {
    "mcp__plugin_discord_discord__reply",
}

# Discord react tool — when called, the content emoji IS the bot's response,
# so terminal-mode 🖥️ should suppress (the react already conveys "I saw it
# and chose to express via reaction").
DISCORD_REACT_TOOLS = {
    "mcp__plugin_discord_discord__react",
}


def categorize_pretooluse(tool_name: str) -> str | None:
    """Return state name for a tool, or None if it shouldn't react.

    Priority: edit > delegate > research > thinking. Edit and delegate
    are more specific than thinking (Bash), so they win when both could
    apply. (No tool currently maps to multiple categories — but the
    explicit ordering keeps it future-proof.)
    """
    if tool_name in EDIT_TOOLS:
        return "editing"
    if tool_name in DELEGATE_TOOLS:
        return "delegating"
    if tool_name in RESEARCH_TOOLS:
        return "researching"
    if tool_name in THINKING_TOOLS:
        return "thinking"
    return None


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def detect_discord_state_dir() -> str:
    """Find the Discord plugin's state dir for this agent.

    Priority order (matches how the plugin itself resolves):
      1. DISCORD_STATE_DIR env (explicit per-agent override)
      2. CLAUDE_CONFIG_DIR/channels/discord
      3. ~/.claude/channels/discord  (default — resolves per machine via $HOME)
    """
    explicit = os.environ.get("DISCORD_STATE_DIR", "")
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.join(cfg, "channels", "discord")
    return os.path.expanduser("~/.claude/channels/discord")


def read_bot_token(state_dir: str) -> str | None:
    """Read DISCORD_BOT_TOKEN from the plugin's state dir .env."""
    env_path = os.path.join(state_dir, ".env")
    if not os.path.exists(env_path):
        log(f"no token file at {env_path}")
        return None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as e:
        log(f"failed to read {env_path}: {e}")
    return None


def _discord_reaction_call(
    method: str, token: str, channel_id: str, message_id: str, emoji: str,
    _retry: bool = True,
) -> bool:
    """PUT or DELETE on /channels/{ch}/messages/{msg}/reactions/{emoji}/@me.

    Honors Discord's Retry-After on 429. Single retry only — if it fails twice
    we give up so we don't block the hook.
    """
    encoded = urllib.parse.quote(emoji)
    url = (
        f"https://discord.com/api/v10/channels/{channel_id}"
        f"/messages/{message_id}/reactions/{encoded}/@me"
    )
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Length": "0",
            "User-Agent": "cc-discord-kit-react-hook (1.2)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:
            retry_after = e.headers.get("Retry-After", "0.5")
            try:
                wait = max(0.1, min(2.0, float(retry_after)))
            except ValueError:
                wait = 0.5
            log(f"react {method} 429 backoff {wait}s for {emoji!r}")
            _time.sleep(wait)
            return _discord_reaction_call(method, token, channel_id, message_id,
                                          emoji, _retry=False)
        log(f"react {method} HTTP {e.code} {channel_id}/{message_id} {emoji!r}")
        return False
    except Exception as e:
        log(f"react {method} failed {channel_id}/{message_id} {emoji!r}: {e}")
        return False


def discord_react(token: str, channel_id: str, message_id: str, emoji: str) -> bool:
    """PUT a reaction. Idempotent: skips the HTTP call if local state shows
    we already PUT this emoji on this message. Prevents notification spam
    when many tools fire in quick succession (each PreToolUse would otherwise
    re-PUT the same 🤔 / 🔧 etc and Discord pings the user on every retry)."""
    if _has_applied(channel_id, message_id, emoji):
        return True  # already applied — pretend success
    ok = _discord_reaction_call("PUT", token, channel_id, message_id, emoji)
    if ok:
        _mark_applied(channel_id, message_id, emoji)
    return ok


def discord_unreact(token: str, channel_id: str, message_id: str, emoji: str) -> bool:
    """Remove the bot's own reaction. 404 is fine (idempotent — wasn't there).
    Skips the HTTP call entirely if local state shows the emoji isn't applied."""
    if not _has_applied(channel_id, message_id, emoji):
        return True  # already absent — nothing to do
    ok = _discord_reaction_call("DELETE", token, channel_id, message_id, emoji)
    # Mark removed regardless — if DELETE failed we'll retry on next event;
    # if Discord disagrees we just drift one cycle, never duplicate-notify.
    _mark_removed(channel_id, message_id, emoji)
    return ok


# ─── Transcript scanning ───

def _extract_text(msg_obj: dict) -> str:
    """Pull text out of an assistant/user transcript entry.

    Reads `type:'text'` content blocks AND the `text` argument of Discord
    reply tool calls. The tool-arg path matters because bots sometimes emit
    only via Discord reply (e.g. brief acknowledgments where the assistant
    composes nothing in terminal text and goes straight to the reply tool).
    Without scanning tool_use args, parse_discord_origin would miss the
    inbound channel tag in the user message and drop the 💾 / ✅ react.
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
            elif c.get("type") == "tool_use" and c.get("name", "") in DISCORD_REPLY_TOOLS:
                arg_text = (c.get("input") or {}).get("text", "")
                if isinstance(arg_text, str) and arg_text:
                    parts.append(arg_text)
        return "\n".join(parts)
    return ""


def _last_user_entry(transcript_path: str) -> dict | None:
    """Return the most recent REAL user entry (text content, not a tool_result)."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        # Skip tool_result-only entries — those are tool replies, not real prompts.
        if isinstance(content, list):
            kinds = {c.get("type") for c in content if isinstance(c, dict)}
            if kinds and kinds.issubset({"tool_result"}):
                continue
        return obj
    return None


# Discord channel tag patterns. The plugin emits:
#   <channel source="discord" chat_id="..." message_id="..." user="..." ts="...">
# Both quoted forms (single + double) seen in the wild.
CHANNEL_TAG_RE = re.compile(
    r'<channel\s+source=["\'](?:plugin:discord:discord|discord)["\']'
    r'[^>]*?chat_id=["\']([^"\']+)["\']'
    r'[^>]*?message_id=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def parse_discord_origins(user_text: str) -> list[tuple[str, str]]:
    """Return ALL Discord origins in the user text, in document order.

    A single user_text can contain multiple <channel> tags when the plugin
    batches several Discord messages between assistant turns (e.g. two
    people speaking before the bot responds). Each tag is one inbound
    message that may need its own reaction.
    """
    if not user_text:
        return []
    return [(m.group(1), m.group(2)) for m in CHANNEL_TAG_RE.finditer(user_text)]


def parse_discord_origin(user_text: str) -> tuple[str, str] | None:
    """Backwards-compat singular: return the LAST origin if any.

    Document-order last == chronologically newest == best target for
    handlers that only stamp one message (e.g. 💾/🔔/💤 additive states
    where stamping every inbound message would be excessive).
    """
    origins = parse_discord_origins(user_text)
    return origins[-1] if origins else None


def _walk_turn(
    transcript_path: str, *, skip_real_users: int = 0
) -> tuple[list[dict], list[dict]]:
    """Return (assistant_entries, user_tool_result_entries) for one turn.

    skip_real_users=0 → the turn after the most recent real user message.
    skip_real_users=1 → the previous turn (used by retroactive-finalize when
                       the latest user message is the NEW one we just received
                       and the assistant turn we want to inspect is the one
                       that came right before it).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], []
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return [], []

    assistant_entries: list[dict] = []
    user_tool_results: list[dict] = []
    real_users_seen = 0
    # When skip=0 we want the trailing assistant entries (the most recent
    # turn at Stop time). When skip>0 we wait until we've passed `skip`
    # real user messages first.
    started = (skip_real_users == 0)
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")

        if t == "user":
            msg = obj.get("message", {})
            content = msg.get("content", [])
            has_text = False
            has_tool_result = False
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            has_text = True
                        elif c.get("type") == "tool_result":
                            has_tool_result = True
            elif isinstance(content, str) and content.strip():
                has_text = True

            is_real = has_text and not has_tool_result
            if is_real:
                real_users_seen += 1
                if started:
                    # We were collecting; this user message bounds the turn we want.
                    break
                if real_users_seen >= skip_real_users:
                    # Either we're now past the skip count (skip>0 case) or
                    # we never had skip to begin with (handled at init).
                    started = True
                continue
            else:
                # tool_result follow-up — only collect if we're inside the turn
                if started:
                    user_tool_results.append(obj)
                continue

        if t == "assistant" and started:
            assistant_entries.append(obj)

    return assistant_entries, user_tool_results


def _walk_current_turn(transcript_path: str) -> tuple[list[dict], list[dict]]:
    """Most recent assistant turn (back-compat alias)."""
    return _walk_turn(transcript_path, skip_real_users=0)


def assistant_called_discord_reply(transcript_path: str) -> bool:
    """Did this turn's assistant call mcp__plugin_discord_discord__reply?"""
    return count_discord_replies(transcript_path) > 0


def count_discord_replies(transcript_path: str) -> int:
    """How many times has this turn's assistant called the Discord reply tool?

    Used by narrate.py to detect mid-turn replies and rotate the
    placeholder so subsequent narrate prose lands BELOW each reply
    instead of being edited invisibly into an earlier placeholder
    above it.
    """
    assistant_entries, _ = _walk_current_turn(transcript_path)
    count = 0
    for obj in assistant_entries:
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                if c.get("name", "") in DISCORD_REPLY_TOOLS:
                    count += 1
    return count


def assistant_called_discord_react(transcript_path: str) -> bool:
    """Did this turn's assistant call mcp__plugin_discord_discord__react?

    A content react (🎉, 🦆, 😂, etc.) is itself a response — terminal-mode
    🖥️ should suppress in that case to avoid stacking redundant emoji.
    """
    assistant_entries, _ = _walk_current_turn(transcript_path)
    for obj in assistant_entries:
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                if c.get("name", "") in DISCORD_REACT_TOOLS:
                    return True
    return False


def _echo_guard_recently_blocked(origins: list[tuple[str, str]]) -> bool:
    """Detect whether cc-discord-echo-guard just blocked this turn.

    When echo-guard exits 2, the harness still runs the rest of the Stop
    chain. Without this check, --mode terminal stamps a premature 🖥️ which
    then collides with the ✅ that lands when the model retries with the
    reply tool call. We tail echo-guard's log for a BLOCK entry on one of
    our inbound msg_ids within the last 60s.

    Log line format: 'BLOCK origins=[(...,...)] ...' with the msg_id
    embedded; we substring-match rather than parse for resilience.

    Override the log location with CCDK_ECHO_GUARD_LOG.
    """
    log_path = os.environ.get("CCDK_ECHO_GUARD_LOG") or os.path.join(
        _state_root(), "discord_echo_guard.log"
    )
    if not os.path.exists(log_path):
        return False
    try:
        mtime = os.path.getmtime(log_path)
    except OSError:
        return False
    if _time.time() - mtime > 60.0:
        return False
    msg_ids = {msg_id for _, msg_id in origins}
    try:
        with open(log_path) as f:
            tail = f.readlines()[-20:]
    except OSError:
        return False
    for line in reversed(tail):
        if not line.startswith("BLOCK "):
            continue
        if any(mid in line for mid in msg_ids):
            return True
    return False


# Permission-denial responses look like:
#   "Permission to use Edit has been denied because Claude Code is running in"
PERM_DENIED_RE = re.compile(
    r"Permission to use \w+ has been denied|"
    r"permission denied because|"
    r"don't ask mode",
    re.IGNORECASE,
)


def turn_had_errors(transcript_path: str) -> tuple[bool, bool]:
    """Return (had_any_error, had_permission_denial) for the current turn.

    Scans tool_result blocks in the user-message follow-ups for is_error=true
    and for permission-denial text patterns.
    """
    _, user_tool_results = _walk_current_turn(transcript_path)
    any_error = False
    perm_denied = False
    for obj in user_tool_results:
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            if c.get("is_error"):
                any_error = True
            # Scan content text for permission-denial signature
            inner = c.get("content", "")
            if isinstance(inner, list):
                inner = " ".join(
                    item.get("text", "") for item in inner
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            if isinstance(inner, str) and PERM_DENIED_RE.search(inner):
                perm_denied = True
                any_error = True  # permission denial is also an error
    return any_error, perm_denied


# ─── Mode handlers ───

def _previous_user_origins(transcript_path: str) -> list[tuple[str, str]]:
    """Find the SECOND-most-recent real user entry (not a tool_result) and
    return ALL its Discord origins. Used to retroactively finalize the
    previous turn in --channels mode where Stop hooks don't fire reliably.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return []
    real_user_count = 0
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            kinds = {c.get("type") for c in content if isinstance(c, dict)}
            if kinds == {"tool_result"}:
                continue
        real_user_count += 1
        if real_user_count == 2:
            return parse_discord_origins(_extract_text(obj))
    return []


def _previous_user_origin(transcript_path: str) -> tuple[str, str] | None:
    """Backwards-compat singular: last origin from the previous user entry."""
    origins = _previous_user_origins(transcript_path)
    return origins[-1] if origins else None


def _retroactive_finalize_previous_turn(transcript_path: str) -> None:
    """If the previous turn never got a final-state emoji (e.g. --channels
    mode where Stop hooks don't fire reliably), retroactively post the
    correct final emoji on the prior message when the next one arrives.

    Best-effort: if Stop already fired correctly we'll repost the same emoji
    here, and Discord dedupes (no harm).

    CRITICAL: at UserPromptSubmit time, the new user message is already in
    the transcript. We must skip ONE real user message to find the previous
    turn's assistant entries. _walk_turn(skip_real_users=1) handles this.
    """
    origins = _previous_user_origins(transcript_path)
    if not origins:
        return

    # Walk the PREVIOUS turn explicitly.
    assistant_entries, user_tool_results = _walk_turn(
        transcript_path, skip_real_users=1
    )

    replied = False
    reacted = False
    for obj in assistant_entries:
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                name = c.get("name", "")
                if name in DISCORD_REPLY_TOOLS:
                    replied = True
                elif name in DISCORD_REACT_TOOLS:
                    reacted = True
        if replied:
            break  # reply is dominant — no need to keep scanning

    had_error = False
    perm_denied = False
    for obj in user_tool_results:
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            if c.get("is_error"):
                had_error = True
            inner = c.get("content", "")
            if isinstance(inner, list):
                inner = " ".join(
                    item.get("text", "") for item in inner
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            if isinstance(inner, str) and PERM_DENIED_RE.search(inner):
                perm_denied = True
                had_error = True

    state_dir = detect_discord_state_dir()
    token = read_bot_token(state_dir)

    def _drop_parked_silent_retro(chat_id: str) -> None:
        if not token:
            return
        prior = _get_last_silent(chat_id)
        if prior:
            discord_unreact(token, chat_id, prior, EMOJI["terminal"])
            _clear_last_silent(chat_id)

    cleaned_channels: set[str] = set()

    def _clean_channel(chat_id: str) -> None:
        if chat_id not in cleaned_channels:
            _drop_parked_silent_retro(chat_id)
            cleaned_channels.add(chat_id)

    if perm_denied:
        for chat_id, msg_id in origins:
            _clean_channel(chat_id)
            _do_react(chat_id, msg_id, EMOJI["denied"], "retro-denied")
    elif had_error and not replied:
        for chat_id, msg_id in origins:
            _clean_channel(chat_id)
            _do_react(chat_id, msg_id, EMOJI["errored"], "retro-errored")
    elif replied:
        for chat_id, msg_id in origins:
            _clean_channel(chat_id)
            _slide_replied(chat_id, msg_id, "retro-replied")
    elif reacted:
        # Content react was the response — suppress 🖥️ on every inbound
        # msg, just clean transients.
        for chat_id, msg_id in origins:
            _clean_channel(chat_id)
            if token:
                for prev_mode in ALL_TRANSIENTS + ["notified"]:
                    discord_unreact(token, chat_id, msg_id, EMOJI[prev_mode])
        log(f"retro-terminal-suppressed-by-react origins={len(origins)}")
    else:
        # Silent turn (retroactive path) — slide 🖥️ forward. One parked 🖥️
        # per channel, on the LAST inbound in each channel for this turn.
        last_per_channel: dict[str, str] = {}
        for chat_id, msg_id in origins:
            last_per_channel[chat_id] = msg_id
        for chat_id, msg_id in last_per_channel.items():
            prior = _get_last_silent(chat_id)
            if token and prior and prior != msg_id:
                discord_unreact(token, chat_id, prior, EMOJI["terminal"])
            _do_react(chat_id, msg_id, EMOJI["terminal"], "retro-terminal")
            _set_last_silent(chat_id, msg_id)


def handle_received(payload: dict) -> int:
    """UserPromptSubmit: 👀 on the inbound message.

    Also: retroactively finalize the previous turn's reaction if Stop hook
    didn't get to it (happens in --channels mode where Stop doesn't fire)."""
    transcript = payload.get("transcript_path", "")
    # Best-effort: finalize previous turn before reacting to new one
    try:
        _retroactive_finalize_previous_turn(transcript)
    except Exception as e:
        log(f"retro-finalize crash: {e}")

    user_text = payload.get("prompt", "") or payload.get("user_message", "")
    if not user_text:
        last = _last_user_entry(transcript)
        if last:
            user_text = _extract_text(last)
    origins = parse_discord_origins(user_text)
    if not origins:
        return 0
    # Stamp 👀 on EVERY inbound message in this turn — when several people
    # speak before the bot replies, each gets its own ack.
    for chat_id, msg_id in origins:
        _do_react(chat_id, msg_id, EMOJI["received"], "received")
    return 0


def handle_working(payload: dict) -> int:
    """PreToolUse: emit category-specific emoji for the tool firing.

    Maps tools to states: editing/researching/delegating/thinking.
    Fast tools (Read, Glob, Grep) return None → no react.
    """
    tool_name = payload.get("tool_name", "")
    state = categorize_pretooluse(tool_name)
    if state is None:
        return 0
    transcript = payload.get("transcript_path", "")
    last = _last_user_entry(transcript)
    if not last:
        return 0
    user_text = _extract_text(last)
    origins = parse_discord_origins(user_text)
    if not origins:
        return 0
    # Stamp the working-state emoji on every inbound msg in the turn.
    for chat_id, msg_id in origins:
        _do_react(chat_id, msg_id, EMOJI[state], state)
    return 0


def _slide_replied(chat_id: str, msg_id: str, mode: str) -> int:
    """Stamp ✅ on msg_id, DELETE ✅ from the previous parked reply (if any
    and different), and update the parked-replied slot. Net effect: at most
    one floating ✅ per channel at any time, on the most recent reply.

    Same shape as the silent-🖥️ slide. Doesn't lose meaningful info because
    the reply itself is the durable record — the ✅ tombstone on old
    messages added nothing the conversation didn't already show.
    """
    state_dir = detect_discord_state_dir()
    token = read_bot_token(state_dir)
    prior = _get_last_replied(chat_id)
    if token and prior and prior != msg_id:
        discord_unreact(token, chat_id, prior, EMOJI["replied"])
    rc = _do_react(chat_id, msg_id, EMOJI["replied"], mode)
    _set_last_replied(chat_id, msg_id)
    return rc


def handle_replied(payload: dict) -> int:
    """PostToolUse on Discord reply tool: ✅ on the original inbound message.

    Sliding behavior: only the most recent replied message in this channel
    keeps ✅. Older ✅'s get cleared as new replies land.
    """
    tool_name = payload.get("tool_name", "")
    if tool_name not in DISCORD_REPLY_TOOLS:
        return 0
    transcript = payload.get("transcript_path", "")
    last = _last_user_entry(transcript)
    if not last:
        return 0
    user_text = _extract_text(last)
    origins = parse_discord_origins(user_text)
    if not origins:
        return 0
    # Slide ✅ through every inbound msg in the turn. _slide_replied unstamps
    # the prior parked ✅ on each call, so within a channel only the LAST
    # inbound ends up parked — which matches the existing single-msg
    # contract. Across different channels each parks independently.
    for chat_id, msg_id in origins:
        _slide_replied(chat_id, msg_id, "replied")
    return 0


def handle_crosscheck(payload: dict) -> int:
    """PostToolUse on Discord reply tool: react 🔀 on the bot's just-sent
    message if the outbound chat_id doesn't match any inbound <channel>
    tag from the current user turn. Warn-only, no block.

    Catches cross-channel leaks where the model meant to reply to one
    channel but typed (or session-residue) ended up sending to another.
    """
    tool_name = payload.get("tool_name", "")
    if tool_name not in DISCORD_REPLY_TOOLS:
        return 0

    tool_input = payload.get("tool_input") or {}
    out_chat_id = tool_input.get("chat_id")
    if not out_chat_id or not isinstance(out_chat_id, str):
        return 0

    # The Discord plugin returns the posted message_id in the tool_response.
    # We need it to react on the bot's own message.
    tool_response = payload.get("tool_response") or {}
    out_msg_id: str | None = None
    if isinstance(tool_response, dict):
        # Plugin response shape: {"content":"sent (id: 1234567890)", ...}
        # OR sometimes the id directly under a key. Try a few.
        for key in ("message_id", "id", "msg_id"):
            v = tool_response.get(key)
            if isinstance(v, str) and v.isdigit():
                out_msg_id = v
                break
        if not out_msg_id:
            content = tool_response.get("content") or tool_response.get("text") or ""
            if isinstance(content, str):
                m = re.search(r"\(id:\s*(\d+)\)", content)
                if m:
                    out_msg_id = m.group(1)

    transcript = payload.get("transcript_path", "")
    if not transcript:
        return 0

    # Collect every inbound Discord origin from the CURRENT user turn.
    # If the outbound chat_id matches any of them, it's a legitimate reply
    # (even if multi-channel — operator may explicitly direct cross-posts).
    last = _last_user_entry(transcript)
    if not last:
        return 0
    user_text = _extract_text(last)
    inbound_origins = parse_discord_origins(user_text)
    inbound_chat_ids = {cid for cid, _ in inbound_origins}

    if out_chat_id in inbound_chat_ids:
        return 0  # no mismatch — legitimate reply

    # Mismatch: chat_id wasn't in any inbound tag this turn. React 🔀 on
    # the just-posted message so the user sees the cross-post indicator.
    if not out_msg_id:
        log(f"crosscheck: chat_id {out_chat_id} not in inbound {inbound_chat_ids}, but couldn't extract out_msg_id from response — skipping react")
        return 0
    log(f"crosscheck: cross-post detected, reacting 🔀 on {out_chat_id}/{out_msg_id} (inbound was {inbound_chat_ids})")
    return _do_react(out_chat_id, out_msg_id, EMOJI["crossposted"], "crossposted")


def handle_terminal(payload: dict) -> int:
    """Stop: emit the final-state emoji.

    Priority (most-specific first):
      ⚠️ permission denied  — needs your attention
      ❌ errored            — tool error this turn
      ✅ replied            — Discord reply was sent
      (suppressed)          — only a content react was sent (react IS the response)
      🖥️ terminal           — Discord-origin but no reply / react sent

    Sliding-🖥️ behavior: in chatty channels (e.g. #fam where the bot lurks
    while the household talks), every silent turn used to leave a 🖥️ on its
    own message — turning the channel into a wall of monitor emoji. Now we
    keep at most ONE 🖥️ per channel: when a new silent turn lands, remove
    🖥️ from the previous parked message before stamping the new one. ✅ /
    🔧 / 👀 / etc. keep their per-message lifecycle as before.
    """
    bot = os.environ.get("CLAUDE_CONFIG_DIR", "default")
    transcript = payload.get("transcript_path", "")
    last = _last_user_entry(transcript)
    if not last:
        log(f"terminal: no last user entry (bot_cfg={bot})")
        return 0
    user_text = _extract_text(last)
    origins = parse_discord_origins(user_text)
    if not origins:
        log(f"terminal: no discord origin in last user msg (bot_cfg={bot})")
        return 0

    had_error, perm_denied = turn_had_errors(transcript)
    replied = assistant_called_discord_reply(transcript)
    reacted = assistant_called_discord_react(transcript)

    state_dir = detect_discord_state_dir()
    token = read_bot_token(state_dir)

    # Defensive: if cc-discord-echo-guard JUST BLOCKED this turn (within last
    # 60s, matching one of our inbound msg_ids), the model is about to retry
    # with a reply tool call. Stamping 🖥️ now would be premature — it'd
    # collide with the ✅ that lands on the retry. Skip the stamp; the next
    # Stop pass (after the reply tool fires) will handle the final state.
    if (not replied) and (not reacted) and _echo_guard_recently_blocked(origins):
        log(f"terminal-skipped-pending-echo-guard-retry origins={len(origins)}")
        return 0

    # Whenever this turn produced ANY visible response (reply, content react,
    # error, denial), the channel's parked 🖥️ should be dropped — the
    # response is what the user sees, no need for the "I'm here" indicator.
    def _drop_parked_silent(chat_id: str) -> None:
        if not token:
            return
        prior = _get_last_silent(chat_id)
        if prior:
            discord_unreact(token, chat_id, prior, EMOJI["terminal"])
            _clear_last_silent(chat_id)

    # Apply the chosen final state to every inbound msg in the turn.
    # _drop_parked_silent is per-channel so we dedupe channels we've already
    # cleaned to avoid redundant unreact calls.
    cleaned_channels: set[str] = set()

    if perm_denied:
        for chat_id, msg_id in origins:
            if chat_id not in cleaned_channels:
                _drop_parked_silent(chat_id)
                cleaned_channels.add(chat_id)
            _do_react(chat_id, msg_id, EMOJI["denied"], "denied")
        return 0
    if had_error and not replied:
        for chat_id, msg_id in origins:
            if chat_id not in cleaned_channels:
                _drop_parked_silent(chat_id)
                cleaned_channels.add(chat_id)
            _do_react(chat_id, msg_id, EMOJI["errored"], "errored")
        return 0
    if replied:
        for chat_id, msg_id in origins:
            if chat_id not in cleaned_channels:
                _drop_parked_silent(chat_id)
                cleaned_channels.add(chat_id)
            _slide_replied(chat_id, msg_id, "replied")
        return 0
    if reacted:
        # Content react IS the response — suppress 🖥️, just clean transients
        # off every inbound msg.
        for chat_id, msg_id in origins:
            if chat_id not in cleaned_channels:
                _drop_parked_silent(chat_id)
                cleaned_channels.add(chat_id)
            if token:
                for prev_mode in ALL_TRANSIENTS + ["notified"]:
                    discord_unreact(token, chat_id, msg_id, EMOJI[prev_mode])
        log(f"terminal-suppressed-by-react origins={len(origins)}")
        return 0

    # Silent turn — slide 🖥️ forward. 🖥️ is a "I'm parked here, no real
    # response" indicator; we keep at most ONE per channel, stamped on the
    # most recent silent inbound. So per channel we only stamp the LAST
    # origin, and clear any prior parked 🖥️ on a different message.
    last_per_channel: dict[str, str] = {}
    for chat_id, msg_id in origins:
        last_per_channel[chat_id] = msg_id
    for chat_id, msg_id in last_per_channel.items():
        prior = _get_last_silent(chat_id)
        if token and prior and prior != msg_id:
            discord_unreact(token, chat_id, prior, EMOJI["terminal"])
        _do_react(chat_id, msg_id, EMOJI["terminal"], "terminal")
        _set_last_silent(chat_id, msg_id)
    return 0


def handle_memorized(payload: dict) -> int:
    """Stop (after stop_hook ran): 💾 if a memory/journal entry was written
    in this turn. Additive — does not clean up other reactions.

    The stop_hook.log entries don't have timestamps; we use the file's
    *modification time* as the rough write time and require it within the
    last 30 seconds (a typical Stop hook chain completes in well under that).
    """
    transcript = payload.get("transcript_path", "")
    last = _last_user_entry(transcript)
    if not last:
        return 0
    user_text = _extract_text(last)
    origin = parse_discord_origin(user_text)
    if not origin:
        return 0
    chat_id, msg_id = origin

    # stop_hook writes its events to this path when memory/journal writes
    # happen. If the file doesn't exist, the Stop hook isn't wired up here
    # and there's nothing to read. Override with CCDK_STOP_HOOK_LOG.
    stop_log = os.environ.get("CCDK_STOP_HOOK_LOG") or os.path.join(
        _state_root(), "stop_hook.log"
    )
    if not os.path.exists(stop_log):
        return 0
    try:
        mtime = os.path.getmtime(stop_log)
    except OSError:
        return 0
    if _time.time() - mtime > 30:
        # Last write to stop_hook.log was too long ago to be from this turn.
        return 0
    try:
        with open(stop_log) as f:
            lines = f.readlines()
    except OSError:
        return 0
    if not lines:
        return 0
    last_line = lines[-1]
    if "saved" not in last_line and "journaled" not in last_line:
        return 0
    if re.search(r"['\"](?:saved|edited|deleted|journaled|journal_deleted)['\"]:\s*[1-9]", last_line):
        return _do_react(chat_id, msg_id, EMOJI["memorized"], "memorized")
    return 0


# Lifecycle: each state cleans up listed predecessors before adding itself.
# Final states (replied, terminal, errored, denied) clean up ALL transients.
# 💾 memorized is additive — fires alongside the final state, doesn't replace.
ALL_TRANSIENTS = ["received", "thinking", "editing", "researching", "delegating"]

# 🔔 notified is a permission-pending indicator; once the permission gets granted
# (signaled by ANY subsequent tool firing or final state), the bell should clear.
# So every other state in the lifecycle treats notified as a predecessor.
TRANSIENT_PREDECESSORS = {
    "received":    ["notified"],
    "thinking":    ["received", "notified"],
    "editing":     ["received", "thinking", "notified"],
    "researching": ["received", "thinking", "notified"],
    "delegating":  ["received", "thinking", "notified"],
    "replied":     ALL_TRANSIENTS + ["notified"],
    "terminal":    ALL_TRANSIENTS + ["notified"],
    "errored":     ALL_TRANSIENTS + ["notified"],
    "denied":      ALL_TRANSIENTS + ["notified"],
    "memorized":   [],  # additive — sits alongside whatever final state exists
    "compacted":   [],  # additive — fires from PreCompact, can stack with anything
    "notified":    [],  # additive when first stamped; cleared by next state event
}


def _do_react(chat_id: str, msg_id: str, emoji: str, mode: str) -> int:
    state_dir = detect_discord_state_dir()
    token = read_bot_token(state_dir)
    if not token:
        log(f"no token, skipping {mode} react (state_dir={state_dir})")
        return 0

    # Clean up transient predecessors first (fire-and-forget; 404 ok).
    for prev_mode in TRANSIENT_PREDECESSORS.get(mode, []):
        prev_emoji = EMOJI[prev_mode]
        discord_unreact(token, chat_id, msg_id, prev_emoji)

    ok = discord_react(token, chat_id, msg_id, emoji)
    log(f"{mode}={ok} chat={chat_id} msg={msg_id} emoji={emoji} state_dir={state_dir}")
    return 0


def handle_compacted(payload: dict) -> int:
    """PreCompact: 💤 on the latest Discord-origin message in the transcript.

    Called by precompact_hook.py after it writes its journal snapshot.
    Best-effort — if there's no Discord origin in the recent window
    (e.g. terminal-only session), we just no-op.
    """
    transcript = payload.get("transcript_path", "")
    last = _last_user_entry(transcript)
    if not last:
        return 0
    user_text = _extract_text(last)
    origin = parse_discord_origin(user_text)
    if not origin:
        return 0
    chat_id, msg_id = origin
    return _do_react(chat_id, msg_id, EMOJI["compacted"], "compacted")


def handle_notified(payload: dict) -> int:
    """Notification: 🔔 on the latest Discord-origin message in the transcript.

    Called by notify_hook.py after it posts the notification mirror to
    Discord. Same shape as handle_compacted/handle_memorized — additive.
    """
    transcript = payload.get("transcript_path", "")
    last = _last_user_entry(transcript)
    if not last:
        return 0
    user_text = _extract_text(last)
    origin = parse_discord_origin(user_text)
    if not origin:
        return 0
    chat_id, msg_id = origin
    return _do_react(chat_id, msg_id, EMOJI["notified"], "notified")


HANDLERS = {
    "received":   handle_received,
    "working":    handle_working,
    "replied":    handle_replied,
    "crosscheck": handle_crosscheck,
    "terminal":   handle_terminal,
    "memorized":  handle_memorized,
    "compacted":  handle_compacted,
    "notified":   handle_notified,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=list(HANDLERS.keys()))
    args = parser.parse_args()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log(f"bad JSON input mode={args.mode}: {raw[:200]!r}")
        return 0

    try:
        return HANDLERS[args.mode](payload)
    except Exception:
        log(f"handler crash mode={args.mode}:\n{traceback.format_exc()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
