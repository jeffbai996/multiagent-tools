#!/usr/bin/env python3
"""Surface the agent's between-tool prose ("narration") to Discord.

This is NOT the LLM's extended-thinking trace — it's the plaintext
assistant text blocks that appear between tool calls in the session
transcript. Visible in the Claude Code terminal; the Discord sender
doesn't see them, which makes the agent look silent during long tasks.

This module is invoked in two phases of the hook lifecycle:

  PostToolUse  (--mode watch)     — after each tool call, pick up any
                                    new assistant text blocks that
                                    appeared in the transcript since
                                    the last firing and post / edit a
                                    narrate placeholder in Discord.
  Stop         (--mode finalize)  — at turn end, either delete the
                                    placeholder (collapse mode: real reply
                                    already landed, narration just
                                    collapses away) or convert it to a
                                    persistent quoted prefix (always
                                    mode).

Per-channel mode lives in <bot_root>/channels/discord/narrate.json:
  { "1234567890": "collapse" | "always" | "never" }
(Legacy "auto" is accepted as an alias for "collapse" and migrated on
read.) Default is "never" for every channel — opt-in.

Per-turn state lives in ~/.local/state/cc-discord-kit/narrate_state.json
keyed by "{bot_id}:{turn_id}":
  { chat_id, placeholder_msg_id, last_transcript_line,
    buffer, mode, finalized }

Hook input contract (stdin JSON, same shape Claude Code gives all hooks):
  { transcript_path, stop_hook_active, ... }

We reuse parsers from react_hook.py — same file watches the same
transcript so the two hooks stay in lockstep.

Env vars:
  CCDK_NARRATE_STATE  override state path
                     (default ~/.local/state/cc-discord-kit/narrate_state.json)
  CCDK_NARRATE_LOG    override log path
                     (default ~/.local/state/cc-discord-kit/narrate.log)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any

# Reuse the existing transcript / Discord-tag parsers.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from react_hook import (  # type: ignore[import-not-found]
    _bot_id,
    detect_discord_state_dir,
    read_bot_token,
    parse_discord_origins,
    _last_user_entry,
    assistant_called_discord_reply,
    count_discord_replies,
)

# Discord 2000-char per-message hard limit; leave headroom for our prefix.
DISCORD_LIMIT = 2000
# Bold+italic for the live "narrating…" marker so it's visually distinct
# from the body even at a glance. Always-mode marker stays plain bold —
# it's a header, not a status.
NARRATE_PREFIX_AUTO = "🧠 ***Narrating…***\n"
NARRATE_PREFIX_ALWAYS = "🧠 **Narration**\n"


def _blockquote(text: str) -> str:
    """Wrap text in Discord's multi-line blockquote (`>>>`).

    Discord's `>>>` quote primitive applies to everything from that token
    to end-of-message — no per-line `> ` prefix and no visible `>` glyph
    on blank lines. The single-line `> ` primitive we tried first turned
    blank-line separators into visible bare `>` characters, which made the
    narration look like every paragraph break had a stray glyph.

    Strip trailing whitespace from the text so the quote block doesn't
    end with empty lines that some clients render with extra padding."""
    return ">>> " + text.rstrip()



STATE_DIR = os.path.expanduser("~/.local/state/cc-discord-kit")
try:
    os.makedirs(STATE_DIR, exist_ok=True)
except OSError:
    pass
STATE_PATH = os.environ.get(
    "CCDK_NARRATE_STATE", os.path.join(STATE_DIR, "narrate_state.json")
)
LOG_PATH = os.environ.get(
    "CCDK_NARRATE_LOG", os.path.join(STATE_DIR, "narrate.log")
)


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-channel mode config
# ---------------------------------------------------------------------------

def _channel_tools_mode(state_dir: str, chat_id: str) -> str:
    """Resolve the channel's `tools` mode from sibling tools.json.

    Used at narrate finalize time to decide whether tool messages
    should be deleted (collapse) or kept (ticker/diffs/full).
    Returns 'off' on any missing-file or parse error."""
    path = os.path.join(state_dir, "tools.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "off"
    if not isinstance(data, dict):
        return "off"
    val = data.get(chat_id, "off")
    return val if isinstance(val, str) else "off"


def _narrate_config_path() -> str:
    """<bot_root>/channels/discord/narrate.json — sibling to access.json."""
    state_dir = detect_discord_state_dir()
    return os.path.join(state_dir, "narrate.json")


def channel_mode(chat_id: str) -> str:
    """Resolve the per-channel narrate mode. Default 'never' (opt-in)."""
    path = _narrate_config_path()
    if not os.path.exists(path):
        return "never"
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "never"
    val = cfg.get(chat_id, "never")
    # 'auto' is the legacy alias for 'collapse' — accept it for backwards
    # compatibility with narrate.json files that pre-date the rename
    if val == "auto":
        val = "collapse"
    if val not in ("never", "collapse", "always"):
        log(f"invalid mode {val!r} for chat {chat_id} — defaulting to never")
        return "never"
    return val


# ---------------------------------------------------------------------------
# Per-turn state
# ---------------------------------------------------------------------------

import fcntl
from contextlib import contextmanager

_LOCK_PATH = STATE_PATH + ".lock"


@contextmanager
def _state_lock():
    """Exclusive file lock around read-modify-write of narrate_state.json.

    PostToolUse fires after every tool call, so multi-tool turns
    (Bash + Read + Edit clusters) trigger many concurrent invocations
    of this script. Without locking, two concurrent watchers can both
    read state showing 'no placeholder_msg_id', both decide to create
    one, and we end up with duplicate placeholders for the same turn.

    fcntl.flock() blocks the second process until the first commits.
    The lock auto-releases on the file descriptor close.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(_LOCK_PATH, "a")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.rename(tmp, STATE_PATH)
    except OSError as e:
        log(f"state save failed: {e}")


def _turn_key(transcript_path: str) -> str | None:
    """Key state by (bot, current user turn timestamp).

    Falls back to None if no user entry found in transcript — we then skip,
    since there's no Discord turn to narrate.
    """
    user = _last_user_entry(transcript_path)
    if not user:
        return None
    ts = user.get("timestamp") or user.get("ts") or ""
    if not ts:
        return None
    return f"{_bot_id()}:{ts}"


def _get_turn(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    val = state.get(key)
    return val if isinstance(val, dict) else None


# ---------------------------------------------------------------------------
# Discord HTTP — send / edit / delete a message
# ---------------------------------------------------------------------------

def _discord_request(
    method: str, url: str, token: str, body: dict | None = None,
    _retry: bool = True,
) -> dict | None:
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "cc-discord-kit-narrate (0.1)",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method in ("POST", "PATCH"):
        # POST/PATCH with empty body still needs the length header
        data = b""
        headers["Content-Length"] = "0"

    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if 200 <= resp.status < 300:
                raw = resp.read()
                return json.loads(raw) if raw else {}
            log(f"{method} {url} → HTTP {resp.status}")
            return None
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:
            retry_after = e.headers.get("Retry-After", "1")
            try:
                wait = max(0.1, min(3.0, float(retry_after)))
            except ValueError:
                wait = 1.0
            log(f"{method} 429 backoff {wait}s")
            time.sleep(wait)
            return _discord_request(method, url, token, body, _retry=False)
        log(f"{method} {url} HTTP {e.code} body={e.read()[:200]!r}")
        return None
    except Exception as e:
        log(f"{method} {url} failed: {e}")
        return None


def discord_send_message(token: str, chat_id: str, content: str) -> str | None:
    """POST /channels/{id}/messages. Returns the new message_id or None."""
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages"
    body = {"content": content[:DISCORD_LIMIT], "allowed_mentions": {"parse": []}}
    resp = _discord_request("POST", url, token, body)
    return resp.get("id") if resp else None


def discord_edit_message(token: str, chat_id: str, msg_id: str, content: str) -> bool:
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages/{msg_id}"
    body = {"content": content[:DISCORD_LIMIT]}
    return _discord_request("PATCH", url, token, body) is not None


def discord_delete_message(token: str, chat_id: str, msg_id: str) -> bool:
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages/{msg_id}"
    return _discord_request("DELETE", url, token) is not None


# ---------------------------------------------------------------------------
# Transcript walker — pick up new assistant text since last_seen
# ---------------------------------------------------------------------------

def _byte_offset_after_current_user_turn(
    transcript_path: str, turn_ts: str,
) -> int:
    """Return the byte position just after the user-entry line matching turn_ts.

    Critical for first-run turn init: we only want to narrate text blocks
    that come AFTER the inbound message we're responding to. Initializing
    last_byte_offset at 0 would replay the entire session's history.

    Falls back to end-of-file (not 0) if the matching user entry isn't
    found — safer to narrate nothing than to flood with history.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return 0
    try:
        offset = 0
        with open(transcript_path) as f:
            while True:
                line = f.readline()
                if not line:
                    break
                # The newline that .readline() included is also the file
                # separator we care about — offset = end-of-this-line
                next_offset = offset + len(line.encode("utf-8"))
                stripped = line.strip()
                if stripped:
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        obj = {}
                    if (obj.get("type") == "user"
                            and (obj.get("timestamp") or obj.get("ts")) == turn_ts):
                        return next_offset
                offset = next_offset
    except OSError as e:
        log(f"user-turn search failed: {e}")
        return size
    # Not found — fall back to end-of-file so we narrate nothing
    # rather than flooding history.
    return size


def _new_text_blocks_since(
    transcript_path: str, last_byte_offset: int,
) -> tuple[list[str], int]:
    """Read the transcript starting at last_byte_offset.

    Return (list_of_new_text_block_contents, new_byte_offset). Text blocks
    are returned in document order — earliest first.

    We track by byte offset rather than line index because the file is
    append-only JSONL and offset comparison is trivially monotonic.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], last_byte_offset
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return [], last_byte_offset
    if size <= last_byte_offset:
        return [], last_byte_offset

    new_texts: list[str] = []
    try:
        with open(transcript_path) as f:
            f.seek(last_byte_offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                for c in content:
                    if (isinstance(c, dict)
                            and c.get("type") == "text"
                            and isinstance(c.get("text"), str)):
                        cleaned = _clean_narrate_text(c["text"])
                        if cleaned:
                            new_texts.append(cleaned)
    except OSError as e:
        log(f"transcript read failed: {e}")
        return [], last_byte_offset

    return new_texts, size


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_narrate_text(text: str) -> str:
    """Strip HTML tags, neutralize triple-backtick fences, trim whitespace.

    The model sometimes emits raw HTML in terminal-targeted prose (<br>,
    <span style=...>, etc.) for formatting affordances that exist in
    its own output channel but render as literal tags on Discord. We
    can't fully prevent emission, so we sanitize at the narrate boundary.

    Triple-backtick fences inside the prose break out of Discord's `>>>`
    multi-line blockquote (a fenced code block always wins over the
    quote primitive), which turns the narration into a "salad" of
    quoted + unquoted fragments. Replace them with a zero-width-joiner
    between the backticks so the run no longer matches Discord's fence
    regex but still reads as triple-backtick text visually.

    Returns an empty string if the text is empty after stripping —
    callers should skip empty results.
    """
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub("", text)
    # Neutralize ``` runs — a triple-backtick inside Discord's >>>
    # blockquote always wins and opens a fenced code block, which
    # turns the narration into alternating quoted + unquoted fragments
    # ("salad"). Insert a zero-width space between each backtick run
    # so the regex doesn't fire; visually the run still reads as ```
    # at any normal zoom level.
    cleaned = cleaned.replace("```", "`​`​`")
    # Rewrite line-start "- " / "* " / "+ " markdown list markers to a
    # bullet glyph. Inside Discord's >>> blockquote those dashes get
    # rendered as bulleted list items, which makes brainstorm prose
    # ("- option A\n- option B") visually conflate with diff lines or
    # restructure the layout in ways that don't match the prose intent.
    cleaned = re.sub(r"^[\-\*\+] ", "• ", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()
    return cleaned


# ---------------------------------------------------------------------------
# Phase handlers
# ---------------------------------------------------------------------------

def handle_watch(payload: dict) -> int:
    """PostToolUse: surface new assistant text blocks for this turn."""
    transcript_path = payload.get("transcript_path") or ""
    if not transcript_path:
        return 0

    user = _last_user_entry(transcript_path)
    user_text = _extract_user_text(user)
    origins = parse_discord_origins(user_text)
    if not origins:
        # Not a Discord-origin turn — narration not applicable
        return 0
    chat_id, _ = origins[-1]
    mode = channel_mode(chat_id)
    if mode == "never":
        return 0

    turn_key = _turn_key(transcript_path)
    if not turn_key:
        return 0
    turn_ts = turn_key.split(":", 1)[1] if ":" in turn_key else ""

    # Serialize the entire read-modify-write of narrate_state.json so
    # concurrent PostToolUse hooks (one per tool call) can't both create
    # a placeholder for the same turn. flock blocks the second invocation
    # until the first commits its placeholder_msg_id back to state.
    with _state_lock():
        return _handle_watch_locked(
            payload, transcript_path, chat_id, mode, turn_key, turn_ts,
        )


def _handle_watch_locked(
    payload: dict, transcript_path: str, chat_id: str, mode: str,
    turn_key: str, turn_ts: str,
) -> int:
    """Body of handle_watch executed under _state_lock()."""
    state = _load_state()
    turn = _get_turn(state, turn_key)
    if turn is None:
        # First firing for this turn — initialize offset at the byte
        # position just after the inbound user message. NEVER 0 — that
        # would replay the entire session history.
        initial_offset = _byte_offset_after_current_user_turn(
            transcript_path, turn_ts
        )
        turn = {
            "chat_id": chat_id,
            # Current segment's live placeholder. Reset to None each
            # time we detect a mid-turn reply landed (segment rotation).
            "placeholder_msg_id": None,
            "last_byte_offset": initial_offset,
            "buffer": "",
            "mode": mode,
            "finalized": False,
            # How many discord-reply tool calls had landed when the
            # current segment's placeholder was created. When the live
            # count exceeds this, a reply landed mid-segment and we
            # seal the current placeholder and start a new one below.
            "replies_at_create": 0,
            # Trail of sealed placeholders within this turn — collapse
            # mode deletes all of them at Stop, always-mode keeps them as
            # 🧠 Narration headers above their respective replies.
            "sealed_placeholders": [],
        }
    if turn.get("finalized"):
        # Already wrapped up — nothing more to do for this turn
        return 0

    new_texts, new_offset = _new_text_blocks_since(
        transcript_path, turn.get("last_byte_offset", 0)
    )

    # Segment rotation: if a reply landed since the current segment's
    # placeholder was created, seal the current placeholder (so it
    # stays positioned above its triggering reply) and reset state for
    # a brand-new segment below the reply.
    current_reply_count = count_discord_replies(transcript_path)
    if (
        turn.get("placeholder_msg_id")
        and current_reply_count > turn.get("replies_at_create", 0)
    ):
        _seal_segment(turn)
        log(f"rotated narrate segment for turn {turn_key} (reply count {current_reply_count})")

    if not new_texts and turn.get("placeholder_msg_id"):
        # Nothing new to add — keep state coherent and exit
        turn["last_byte_offset"] = new_offset
        state[turn_key] = turn
        _save_state(state)
        return 0
    if not new_texts:
        # Nothing yet — don't create an empty placeholder
        return 0

    # Race guard: if a reply has landed AFTER we'd want to create a
    # fresh placeholder for the current segment, decide based on mode.
    # Posting now would land AFTER that reply (Discord positions by
    # post time, never reorders on edit).
    #
    # Collapse mode: suppress. A placeholder posted below the reply will
    # be deleted at Stop anyway — that's just a visible flash of "the
    # agent is talking to itself" with no payoff.
    #
    # Always mode: post anyway. A "Narration" header rendered BELOW the
    # reply is degraded (the convention is above) but at least visible;
    # silently dropping the prose is worse. This case fires routinely when
    # the model emits text BEFORE the reply tool in a single turn ("on
    # it, let me look into X" followed by a Discord reply) — the prose
    # then has nowhere to land if we suppress.
    #
    # Edits to an existing placeholder are still fine in both modes —
    # only a brand-new send after the latest reply was at risk.
    if (
        not turn.get("placeholder_msg_id")
        and current_reply_count > turn.get("replies_at_create", 0)
    ):
        if mode == "collapse":
            turn["last_byte_offset"] = new_offset
            state[turn_key] = turn
            _save_state(state)
            log(f"narrate suppressed for {turn_key}: reply already landed (collapse mode)")
            return 0
        log(f"narrate posting below reply for {turn_key}: prose-then-reply turn (always mode)")

    state_dir = detect_discord_state_dir()
    token = read_bot_token(state_dir)
    if not token:
        log(f"no bot token at {state_dir} — skipping narrate")
        return 0

    # Append new text to buffer with paragraph breaks between blocks
    incoming = "\n\n".join(new_texts)
    candidate_buffer = (turn["buffer"] + "\n\n" + incoming
                        if turn["buffer"] else incoming)
    candidate_content = NARRATE_PREFIX_AUTO + _blockquote(candidate_buffer)

    # Length guard: if appending this batch would push the placeholder
    # over Discord's 2000-char cap, the edit would silently truncate
    # mid-blockquote — looks like raw text leaks past the quote bar.
    # Auto-rotate: seal the current segment (it stays sized as it was
    # before this batch), reset state, and start a fresh placeholder
    # with just the new text. Same mechanism as reply-triggered
    # rotation, just driven by length instead of a reply landing.
    if (
        turn.get("placeholder_msg_id")
        and len(candidate_content) > DISCORD_LIMIT
    ):
        _seal_segment(turn)
        log(f"rotated narrate segment for turn {turn_key} (length cap)")
        turn["buffer"] = incoming
    else:
        turn["buffer"] = candidate_buffer
    turn["last_byte_offset"] = new_offset

    # Both modes show the bold+italic "Narrating…" status marker while the
    # turn is mid-flight. The transition happens at Stop:
    #   collapse → placeholder is deleted (status marker disappears with it)
    #   always → handle_finalize swaps to the plain bold "Narration" header
    # so the live word reads as "this is still happening" and the settled
    # word reads as "this was narration".
    content = NARRATE_PREFIX_AUTO + _blockquote(turn["buffer"])

    if turn.get("placeholder_msg_id"):
        if discord_edit_message(token, chat_id, turn["placeholder_msg_id"], content):
            log(f"edited narrate placeholder for turn {turn_key}")
    else:
        msg_id = discord_send_message(token, chat_id, content)
        if msg_id:
            turn["placeholder_msg_id"] = msg_id
            turn["replies_at_create"] = current_reply_count
            log(f"created narrate placeholder {msg_id} for turn {turn_key}")
        else:
            log(f"narrate placeholder send failed for {turn_key}")

    state[turn_key] = turn
    _save_state(state)
    return 0


def _seal_segment(turn: dict) -> None:
    """Seal the current segment's placeholder and reset for a new one.

    Called when a mid-turn reply lands. The current placeholder stays
    where it is in the channel (above its triggering reply) and is
    appended to sealed_placeholders for Stop-time finalization. Buffer
    and live id reset so the next narrate text creates a fresh
    placeholder below the reply.
    """
    msg_id = turn.get("placeholder_msg_id")
    if msg_id:
        turn.setdefault("sealed_placeholders", []).append({
            "msg_id": msg_id,
            "buffer": turn.get("buffer", ""),
        })
    turn["placeholder_msg_id"] = None
    turn["buffer"] = ""


def handle_finalize(payload: dict) -> int:
    """Stop: clean up the narrate placeholders for this turn.

    Each turn may now contain multiple segment placeholders if a
    mid-turn reply triggered rotation. Auto-mode deletes them all
    (the placeholder is purely ephemeral). Always-mode edits each
    one to the settled 🧠 Narration header so they read as section
    headers above their respective replies.
    """
    transcript_path = payload.get("transcript_path") or ""
    with _state_lock():
        return _handle_finalize_locked(payload, transcript_path)


def _handle_finalize_locked(payload: dict, transcript_path: str) -> int:
    """Body of handle_finalize executed under _state_lock()."""
    turn_key = _turn_key(transcript_path)
    if not turn_key:
        return 0
    state = _load_state()
    turn = _get_turn(state, turn_key)
    if not turn or turn.get("finalized"):
        return 0
    chat_id = turn.get("chat_id")
    mode = turn.get("mode", "never")

    # Collect everything to finalize: the live segment + all sealed
    # segments from mid-turn rotations.
    segments: list[dict] = list(turn.get("sealed_placeholders", []))
    live_msg_id = turn.get("placeholder_msg_id")
    if live_msg_id:
        segments.append({
            "msg_id": live_msg_id,
            "buffer": turn.get("buffer", ""),
        })

    if (segments or turn.get("tool_msg_id") or turn.get("sealed_tool_messages")) and chat_id:
        state_dir = detect_discord_state_dir()
        token = read_bot_token(state_dir)
        if token:
            for seg in segments:
                seg_msg_id = seg.get("msg_id")
                if not seg_msg_id:
                    continue
                if mode == "collapse":
                    if discord_delete_message(token, chat_id, seg_msg_id):
                        log(f"deleted narrate placeholder {seg_msg_id} (collapse, turn {turn_key})")
                elif mode == "always":
                    content = NARRATE_PREFIX_ALWAYS + _blockquote(seg.get("buffer", ""))
                    if discord_edit_message(token, chat_id, seg_msg_id, content):
                        log(f"finalized narrate placeholder {seg_msg_id} (always, turn {turn_key})")

            # Finalize tool-trace messages.
            # Behavior depends on the channel's `tools` mode:
            #   collapse → delete the tool message entirely (symmetric
            #              with narrate collapse mode)
            #   otherwise → swap the live "Tool trace…" prefix to the
            #               settled "Tool trace" version, keep the body
            # Covers both the in-flight tool_msg_id and any
            # sealed_tool_messages from rotations.
            tools_mode = _channel_tools_mode(state_dir, chat_id)
            tool_msgs: list[dict] = list(turn.get("sealed_tool_messages", []))
            if turn.get("tool_msg_id"):
                tool_msgs.append({
                    "msg_id": turn["tool_msg_id"],
                    "buffer": turn.get("tool_buffer", ""),
                })
            for tm in tool_msgs:
                tm_id = tm.get("msg_id")
                if not tm_id:
                    continue
                if tools_mode == "collapse":
                    if discord_delete_message(token, chat_id, tm_id):
                        log(f"deleted tool message {tm_id} (collapse, turn {turn_key})")
                    continue
                buf = tm.get("buffer", "")
                final_content = "🔧 **Tool trace**\n"
                if buf:
                    final_content += "```diff\n" + buf + "\n```"
                if discord_edit_message(token, chat_id, tm_id, final_content):
                    log(f"finalized tool message {tm_id} (turn {turn_key})")

    turn["finalized"] = True
    state[turn_key] = turn

    # Garbage-collect old finalized turns (older than 24h) to keep state bounded
    cutoff = time.time() - 86400
    pruned = {
        k: v for k, v in state.items()
        if not (isinstance(v, dict) and v.get("finalized")
                and isinstance(v.get("ts"), (int, float)) and v["ts"] < cutoff)
    }
    pruned[turn_key] = {**turn, "ts": time.time()}
    _save_state(pruned)
    return 0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_user_text(user_entry: dict | None) -> str:
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("watch", "finalize"), required=True)
    args = ap.parse_args()

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("bad JSON input, passing through")
        return 0

    if args.mode == "watch":
        return handle_watch(payload)
    return handle_finalize(payload)


if __name__ == "__main__":
    sys.exit(main())
