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


# How many recent user messages the save-intent gate scans. Bumped from 1 →
# 5 because real save flows often span turns: "save our address" (turn N) →
# "1955 129th Ave" (turn N+1) → assistant emits [MEMORY:] (turn N+1's reply,
# Stop fires here). With N=1 the gate only sees the address-only msg and
# blocks. N=5 catches the "save" verb up to a few exchanges back.
GATE_USER_LOOKBACK = 5


def read_last_messages(transcript_path: str,
                       user_lookback: int = GATE_USER_LOOKBACK
                       ) -> tuple[str, str, str]:
    """Return (last_user_text, last_assistant_text, recent_user_window).

    `recent_user_window` joins the most recent `user_lookback` user messages
    (oldest-first within the window) for save-intent gate scanning. Keeping
    `last_user_text` separate so the Discord-origin parser still resolves the
    LATEST user message's <channel> tag — that's the right card target even
    when the save verb fired in an earlier turn.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return "", "", ""
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
    except OSError:
        return "", "", ""
    last_user = ""
    last_assistant = ""
    user_window: list[str] = []
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "assistant" and not last_assistant:
            last_assistant = _extract_text(obj)
        elif t == "user":
            text = _extract_text(obj)
            if text:
                if not last_user:
                    last_user = text
                if len(user_window) < user_lookback:
                    user_window.append(text)
        if last_assistant and len(user_window) >= user_lookback:
            break
    return last_user, last_assistant, "\n".join(reversed(user_window))


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
    # Bare-verb gate matching ticker-tape (chat.py:1054) — any of these words
    # in the user's last message is enough. The earlier verb+noun-adjacency
    # variant kept gating valid requests like "delete that memory" or "nuke
    # memory 88" because of intervening words. Bare-verb is permissive but
    # the EXAMPLE_RE pre-strip and the [MEMORY:]/[JOURNAL:] tag form keep the
    # false-positive rate effectively zero in practice.
    r"\b(remember|memori[sz]e|save|memo|memory|forget|"
    r"delete|remove|nuke|edit|note|remind|journal|pin|stash)\b",
    re.I,
)

EXAMPLE_RE = re.compile(
    r"\[(?:MEMORY|JOURNAL)-EXAMPLE(?:\s+[^:\]]+?)?:\s*.+?\]",
    re.DOTALL,
)

# Fenced code blocks (``` ... ```) — remove the ENTIRE block including fences.
# Bots use these for syntax demos, command examples, snippet replays — none
# of them should trigger real saves. Non-greedy so multiple blocks don't
# collapse into one. Tolerates language tags after the opening fence.
FENCED_CODE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n.*?```", re.DOTALL)

# Inline-code spans (`x`). Single-backtick pairs only.
INLINE_CODE_RE = re.compile(r"`[^`\n]+?`")


def user_asked_to_save(user_text: str) -> bool:
    """True iff the user's last message contains a save-intent verb."""
    if not user_text:
        return False
    return bool(SAVE_KEYWORDS_RE.search(user_text))


def _attr_list(attrs: dict, key: str) -> list[str]:
    val = attrs.get(key, "")
    return [v.strip() for v in val.split(",") if v.strip()]


def process_text(text: str) -> tuple[dict, list[dict]]:
    """Run all tag handlers, returning (counts, actions).

    `actions` is the per-event payload list used by the Discord card poster:
      {kind: 'memory_saved', entry: {...}}        — full saved entry
      {kind: 'memory_edited', id: int, before: dict|None, after: dict|None}
      {kind: 'memory_deleted', before: dict|None}  — captured before removal
      {kind: 'journal_added', entry: {...}}
      {kind: 'journal_deleted', before: dict|None}

    For deletes we look up the entry BEFORE calling remove so the card can
    show what was actually removed (id alone is too cryptic).
    """
    counts = {"saved": 0, "edited": 0, "deleted": 0,
              "journaled": 0, "journal_deleted": 0}
    actions: list[dict] = []

    # Strip code-fenced blocks + inline-code spans BEFORE example markers.
    # Bots discussing the tag syntax (e.g. "use the [MEMORY: text] form")
    # almost always do so inside backticks, and a literal example body like
    # `...` would otherwise create a junk memory. Real saves should be in
    # plain prose; the [MEMORY-EXAMPLE: ...] escape hatch covers the rare
    # case where a bot needs to discuss syntax outside code formatting.
    text = FENCED_CODE_RE.sub("", text)
    text = INLINE_CODE_RE.sub("", text)
    text = EXAMPLE_RE.sub("", text)

    for m in MEM_RE.finditer(text):
        attr_str, body = m.group(1) or "", m.group(2).strip()
        if not body:
            continue
        attrs = parse_kv_attrs(attr_str)
        entry = store.save_memory(
            body,
            type=attrs.get("type", "feedback"),
            name=attrs.get("name", ""),
            tags=_attr_list(attrs, "tags"),
            about=_attr_list(attrs, "about"),
            bot=_attr_list(attrs, "bot") or None,
        )
        counts["saved"] += 1
        actions.append({"kind": "memory_saved", "entry": entry})

    for m in MEM_EDIT_RE.finditer(text):
        mid, new = int(m.group(1)), m.group(2).strip()
        before = _find_memory(mid)
        if store.edit_memory(mid, new):
            counts["edited"] += 1
            actions.append({
                "kind": "memory_edited",
                "id": mid,
                "before": before,
                "after": _find_memory(mid),
            })

    for m in MEM_DEL_RE.finditer(text):
        mid = int(m.group(1))
        before = _find_memory(mid)
        if store.remove_memory(mid):
            counts["deleted"] += 1
            actions.append({"kind": "memory_deleted", "before": before})

    for m in JOU_RE.finditer(text):
        attr_str, body = m.group(1) or "", m.group(2).strip()
        if not body:
            continue
        attrs = parse_kv_attrs(attr_str)
        tags = _attr_list(attrs, "tags")
        entry = store.add_journal(body, source=attrs.get("source", f"hook:{HOST}"),
                                  actor=attrs.get("actor", BOT_NAME), tags=tags)
        counts["journaled"] += 1
        actions.append({"kind": "journal_added", "entry": entry})

    for m in JOU_DEL_RE.finditer(text):
        jid = int(m.group(1))
        before = _find_journal(jid)
        if store.remove_journal(jid):
            counts["journal_deleted"] += 1
            actions.append({"kind": "journal_deleted", "before": before})

    return counts, actions


def _find_memory(mid: int) -> dict | None:
    """Lookup memory by id from the live store. Returns None on miss."""
    try:
        for m in store.load_memories():
            if m.get("id") == mid:
                return m
    except Exception:
        return None
    return None


def _find_journal(jid: int) -> dict | None:
    try:
        for j in store.load_journal():
            if j.get("id") == jid:
                return j
    except Exception:
        return None
    return None


# ─────────── Discord card poster ───────────
#
# When a save/edit/delete/journal action fires, post a rendered confirmation
# card to the Discord channel where the user requested it. Replaces the bot's
# in-reply rendering, which was unreliable (depended on the bot remembering
# to render). Failure modes (no Discord origin, no token, HTTP error) all
# silently log + skip — the action already landed in the store.
#
# Multiagent-tools agnostic about which bot. Token resolution order:
#   $MULTIAGENT_DISCORD_TOKEN   — explicit override
#   $CLAUDE_PLUGIN_STATE_DIR/.env DISCORD_BOT_TOKEN
#   $CLAUDE_CONFIG_DIR/channels/discord/.env DISCORD_BOT_TOKEN
#   ~/.claude/channels/discord/.env DISCORD_BOT_TOKEN

_CHANNEL_TAG_RE = re.compile(
    r'<channel\s+source=["\'](?:plugin:discord:discord|discord)["\']'
    r'[^>]*?chat_id=["\']([^"\']+)["\']'
    r'[^>]*?message_id=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _parse_discord_origin(user_text: str) -> tuple[str, str] | None:
    """Last <channel> tag in user_text → (chat_id, message_id), or None.

    Last == newest in the user msg (plugin batches multiple inbound messages
    into one user_text in document order). The latest is the message the
    user actually meant when they said "save that".
    """
    if not user_text:
        return None
    matches = list(_CHANNEL_TAG_RE.finditer(user_text))
    if not matches:
        return None
    m = matches[-1]
    return m.group(1), m.group(2)


def _read_bot_token() -> str | None:
    """Read DISCORD_BOT_TOKEN.

    Resolution order:
      1. $MULTIAGENT_DISCORD_TOKEN — explicit token override
      2. $DISCORD_STATE_DIR/.env — multi-agent setups where each bot has
         its own state dir but shares CLAUDE_CONFIG_DIR. Takes priority
         over CLAUDE_CONFIG_DIR for that reason.
      3. $CLAUDE_PLUGIN_STATE_DIR/.env
      4. $CLAUDE_CONFIG_DIR/channels/discord/.env
      5. ~/.claude/channels/discord/.env
    """
    explicit = os.environ.get("MULTIAGENT_DISCORD_TOKEN", "").strip()
    if explicit:
        return explicit

    env_path: str | None = None
    state_dir = os.environ.get("DISCORD_STATE_DIR", "")
    if state_dir:
        env_path = os.path.join(state_dir, ".env")
    else:
        plugin_dir = os.environ.get("CLAUDE_PLUGIN_STATE_DIR", "")
        if plugin_dir:
            env_path = os.path.join(plugin_dir, ".env")
        elif os.environ.get("CLAUDE_CONFIG_DIR"):
            env_path = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "channels", "discord", ".env")
        else:
            env_path = os.path.expanduser("~/.claude/channels/discord/.env")

    if not env_path or not os.path.exists(env_path):
        return None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _truncate_body(text: str, lim: int = 600) -> str:
    """Trim long bodies for the card. Cards are visible-confirmation, not full
    reproduction — link to the web UI for full content."""
    if len(text) <= lim:
        return text
    return text[: lim - 1].rstrip() + "…"


def _italicize_body(text: str) -> str:
    """Wrap each non-empty line in `*...*` so the body renders italic on
    Discord. Plain italic asterisk avoids the bare `>` glyphs the blockquote
    variant left dangling on mobile when paragraphs had blank-line breaks.
    Empty lines pass through as paragraph breaks.

    Pre-existing `**bold**` markdown in the body composes naturally with the
    outer italic — e.g. `**1955 ...**` becomes `***1955 ...***` which Discord
    renders as bold-italic.
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append("")
        else:
            out.append(f"*{stripped}*")
    return "\n".join(out)


def _format_card(action: dict) -> str | None:
    """Render one action as a Discord-friendly card. Returns None if the
    action has no entry to render (e.g. delete of a missing id).

    Format conventions: header with emoji + bold; meta line with italics +
    inline-code values; body in italics (no blockquote — `>` looked broken
    on Discord mobile when bodies had paragraph breaks).
    """
    kind = action.get("kind")
    if kind == "memory_saved":
        e = action.get("entry") or {}
        if not e.get("id"):
            return None
        tags = ", ".join(e.get("tags") or []) or "—"
        about = ", ".join(e.get("about") or []) or "—"
        body = _italicize_body(_truncate_body(e.get("text", "")))
        return (
            f"💾 **Memory #{e['id']} saved**\n"
            f"*type:* `{e.get('type','?')}` · "
            f"*name:* `{e.get('name','—') or '—'}` · "
            f"*tags:* `{tags}` · *about:* `{about}`\n\n"
            f"{body}"
        )
    if kind == "memory_edited":
        before = action.get("before") or {}
        after = action.get("after") or {}
        mid = action.get("id")
        body = _italicize_body(_truncate_body(after.get("text", "")))
        return (
            f"✏️ **Memory #{mid} edited**\n"
            f"*name:* `{after.get('name', before.get('name', '—')) or '—'}`\n\n"
            f"{body}"
        )
    if kind == "memory_deleted":
        before = action.get("before") or {}
        if not before:
            return None
        return (
            f"🗑️ **Memory #{before.get('id', '?')} deleted**\n"
            f"*was:* `{before.get('type','?')}` · `{before.get('name','—') or '—'}`"
        )
    if kind == "journal_added":
        e = action.get("entry") or {}
        if not e.get("id"):
            return None
        tags = ", ".join(e.get("tags") or []) or "—"
        body = _italicize_body(_truncate_body(e.get("text", "")))
        return (
            f"📓 **Journal #{e['id']} added**\n"
            f"*tags:* `{tags}`\n\n"
            f"{body}"
        )
    if kind == "journal_deleted":
        before = action.get("before") or {}
        if not before:
            return None
        return f"🗑️ **Journal #{before.get('id','?')} deleted**"
    return None


def _post_discord_message(token: str, channel_id: str, content: str,
                          reply_to: str | None = None) -> bool:
    """POST /channels/<id>/messages. Best-effort, single-shot, no retry — if
    the network's flaky, the user sees no card; the action already landed in
    the store. The log carries the failure for triage."""
    import urllib.error
    import urllib.request
    body: dict = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }
    if reply_to:
        body["message_reference"] = {
            "message_id": reply_to,
            "fail_if_not_exists": False,
        }
    data = json.dumps(body).encode("utf-8")
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "multiagent-stop-hook (cards, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            err_body = ""
        log(f"discord POST {channel_id} HTTP {e.code}: {err_body!r}")
        return False
    except Exception as e:
        log(f"discord POST {channel_id} failed: {e}")
        return False


def post_action_cards(actions: list[dict], user_text: str) -> int:
    """For each action with renderable content, post a card to the Discord
    channel the user requested in. Returns count posted."""
    if not actions:
        return 0
    origin = _parse_discord_origin(user_text)
    if not origin:
        # Stop hook fires for all assistant turns; only Discord-originated
        # save requests get cards. Terminal saves are silent (the CLI itself
        # already prints "Saved #N").
        return 0
    chat_id, msg_id = origin
    token = _read_bot_token()
    if not token:
        log("discord card skipped: no DISCORD_BOT_TOKEN found")
        return 0
    posted = 0
    for action in actions:
        card = _format_card(action)
        if not card:
            continue
        if _post_discord_message(token, chat_id, card, reply_to=msg_id):
            posted += 1
    return posted


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload: dict = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log(f"bad JSON input: {raw[:200]!r}")
        return 0

    transcript = payload.get("transcript_path", "")
    user_text, assistant_text, user_window = read_last_messages(transcript)
    if not assistant_text:
        return 0

    # Gate scans the last GATE_USER_LOOKBACK user messages joined, not just
    # the latest. user_text stays the latest for Discord-origin parsing —
    # that's the right card target.
    if not user_asked_to_save(user_window):
        if MEM_RE.search(assistant_text) or JOU_RE.search(assistant_text) \
                or MEM_DEL_RE.search(assistant_text) or MEM_EDIT_RE.search(assistant_text) \
                or JOU_DEL_RE.search(assistant_text):
            log(f"gated bot={BOT_NAME} (no save-intent in user window N={GATE_USER_LOOKBACK}) "
                f"latest_user={user_text[:80]!r}")
        return 0

    try:
        counts, actions = process_text(assistant_text)
    except Exception:
        log(f"process_text crashed:\n{traceback.format_exc()}")
        return 0

    if any(counts.values()):
        log(f"{payload.get('hook_event_name', '?')} bot={BOT_NAME} {counts}")

    # Post Discord confirmation cards for each action when the request came
    # from a Discord channel. Best-effort — never block the hook on this.
    try:
        posted = post_action_cards(actions, user_text)
        if posted:
            log(f"posted {posted} discord card{'s' if posted != 1 else ''}")
    except Exception:
        log(f"post_action_cards crashed:\n{traceback.format_exc()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
