"""Shared Discord card rendering + posting for multiagent-tools actions.

Both the Stop hook (when a bot emits a [MEMORY:]/[JOURNAL:] tag) and the
CLI (`multiagent-tools memory add|edit|delete` with --discord-* flags) post
the same rendered card to Discord. Sharing the format here keeps them
byte-for-byte consistent — drift between hook and CLI was the original
bug that motivated this module.

Format conventions:
  - Bold header in prose (emoji + verb + ID).
  - Single fenced code block below the header containing aligned meta
    key:value pairs and the body, separated by a horizontal rule. The
    code-block surface renders consistently on Discord mobile; markdown
    tables are unreliable on the same surface.

Failure modes for the poster: no token resolved → silent skip; HTTP
error → log + skip. The action already landed in the store; missing
visible confirmation is the worst case, never a corrupted write.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

CARD_BODY_LIMIT = 600


def _truncate_body(text: str, lim: int = CARD_BODY_LIMIT) -> str:
    if len(text) <= lim:
        return text
    return text[: lim - 1].rstrip() + "…"


def _render_card_block(meta: list[tuple[str, str]], body: str | None) -> str:
    """Render meta-pairs + optional body inside a fenced code block."""
    if not meta and not body:
        return ""
    pad = max((len(k) for k, _ in meta), default=0) + 2  # +2 for `: ` after key
    lines: list[str] = []
    for key, val in meta:
        lines.append(f"{(key + ':').ljust(pad)}{val}")
    if body:
        if lines:
            rule_width = max(len(line) for line in lines)
            lines.append("─" * min(rule_width, 50))
        lines.append(body)
    return "```\n" + "\n".join(lines) + "\n```"


def format_card(action: dict) -> str | None:
    """Render one action as a Discord-friendly card.

    `action` shape:
      {kind: 'memory_saved', entry: {...}}
      {kind: 'memory_edited', id: int, before: dict|None, after: dict|None}
      {kind: 'memory_deleted', before: dict|None}
      {kind: 'journal_added', entry: {...}}
      {kind: 'journal_deleted', before: dict|None}

    Returns None if the action has nothing renderable (e.g. delete of a
    missing id where `before` is None).
    """
    kind = action.get("kind")
    if kind == "memory_saved":
        e = action.get("entry") or {}
        if not e.get("id"):
            return None
        meta = [
            ("type", e.get("type", "?")),
            ("name", e.get("name", "") or "—"),
            ("tags", ", ".join(e.get("tags") or []) or "—"),
            ("about", ", ".join(e.get("about") or []) or "—"),
        ]
        body = _truncate_body(e.get("text", "")) or None
        return f"💾 **Memory #{e['id']} saved**\n" + _render_card_block(meta, body)
    if kind == "memory_edited":
        before = action.get("before") or {}
        after = action.get("after") or {}
        mid = action.get("id")
        meta = [
            ("type", after.get("type", before.get("type", "?"))),
            ("name", after.get("name", before.get("name", "")) or "—"),
            ("tags", ", ".join(after.get("tags") or before.get("tags") or []) or "—"),
            ("about", ", ".join(after.get("about") or before.get("about") or []) or "—"),
        ]
        body = _truncate_body(after.get("text", "")) or None
        return f"✏️ **Memory #{mid} edited**\n" + _render_card_block(meta, body)
    if kind == "memory_deleted":
        before = action.get("before") or {}
        if not before:
            return None
        meta = [
            ("type", before.get("type", "?")),
            ("name", before.get("name", "") or "—"),
        ]
        return f"🗑️ **Memory #{before.get('id', '?')} deleted**\n" + _render_card_block(meta, None)
    if kind == "journal_added":
        e = action.get("entry") or {}
        if not e.get("id"):
            return None
        meta = [
            ("tags", ", ".join(e.get("tags") or []) or "—"),
            ("actor", e.get("actor", "") or "—"),
        ]
        body = _truncate_body(e.get("text", "")) or None
        return f"📓 **Journal #{e['id']} added**\n" + _render_card_block(meta, body)
    if kind == "journal_deleted":
        before = action.get("before") or {}
        if not before:
            return None
        return f"🗑️ **Journal #{before.get('id','?')} deleted**"
    return None


def read_bot_token() -> str | None:
    """Read DISCORD_BOT_TOKEN.

    Resolution order:
      1. $MULTIAGENT_DISCORD_TOKEN — explicit token override
      2. $DISCORD_STATE_DIR/.env — multi-agent setups where each bot has
         its own state dir but shares CLAUDE_CONFIG_DIR. Priority over
         CLAUDE_CONFIG_DIR so per-bot overrides actually apply.
      3. $CLAUDE_PLUGIN_STATE_DIR/.env
      4. $CLAUDE_CONFIG_DIR/channels/discord/.env
      5. ~/.claude/channels/discord/.env (default agent)
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


def post_message(token: str, channel_id: str, content: str,
                 reply_to: str | None = None,
                 user_agent: str = "multiagent-tools-card (1.0)") -> tuple[bool, str]:
    """POST /channels/<id>/messages. Returns (ok, error_message_if_failed).

    Best-effort, single-shot, no retry — if the network's flaky, the user
    sees no card; the action already landed in the store.
    """
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
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return (200 <= resp.status < 300, "")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            err_body = ""
        return (False, f"HTTP {e.code}: {err_body!r}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def post_action_card(action: dict, chat_id: str,
                     reply_to: str | None = None,
                     user_agent: str = "multiagent-tools-card (1.0)") -> tuple[bool, str]:
    """Render and post a card for one action. Returns (ok, error).

    `ok=False, error=""` means the action wasn't renderable (e.g. delete
    of a missing id) — not actually a failure.
    """
    card = format_card(action)
    if not card:
        return (False, "")
    token = read_bot_token()
    if not token:
        return (False, "no DISCORD_BOT_TOKEN found")
    return post_message(token, chat_id, card, reply_to=reply_to, user_agent=user_agent)
