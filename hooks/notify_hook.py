"""Notification hook: mirror Claude Code system notifications to Discord.

Hook input (stdin JSON, per Claude Code Notification hook contract):
  {
    "hook_event_name": "Notification",
    "session_id": "...",
    "transcript_path": "...",
    "cwd": "...",
    "notification_type": "permission_prompt" | "idle_prompt" | "auth_success"
                         | "elicitation_dialog" | "elicitation_complete"
                         | "elicitation_response"
  }

What this does:
  1. Filters to actionable notification types (permission_prompt, idle_prompt,
     elicitation_dialog). Auth and elicitation completes are noise.
  2. Picks a target channel:
       - NOTIFY_CHANNEL_ID env var if set
       - else: most recent Discord-origin chat_id from the transcript
       - else: skip (terminal-only session, nothing to mirror to)
  3. Posts a short message via the Discord Bot REST API.
  4. Best-effort drops a 🔔 reaction on the most recent Discord-origin
     message via cc-react-hook --mode notified.

Exits 0 always; never blocks Claude Code. Failures log to notify_hook.log.

Notification hooks have no decision control — exit code is ignored — so we
can't gate anything. Side effect only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

# Notification types we actually want to forward. Everything else is noise.
# idle_prompt removed 2026-04-29 — bots are idle most of the time, 🌙 was clutter.
ACTIONABLE_TYPES = {"permission_prompt", "elicitation_dialog"}

_STATE_DIR = os.path.expanduser("~/.local/state/cc-discord-kit")
try:
    os.makedirs(_STATE_DIR, exist_ok=True)
except OSError:
    pass
LOG_PATH = os.environ.get(
    "CCDK_NOTIFY_HOOK_LOG", os.path.join(_STATE_DIR, "notify_hook.log")
)

# Same Discord-origin tag pattern react_hook uses.
CHANNEL_TAG_RE = re.compile(
    r'<channel\s+source="(?:plugin:discord:discord|discord)"\s+'
    r'chat_id="(\d+)"\s+message_id="(\d+)"',
    re.IGNORECASE,
)

# Agent identity. CCDK_BOT in env wins; otherwise derive from
# CLAUDE_CONFIG_DIR basename or fall back to "agent".
def _agent_name() -> str:
    explicit = os.environ.get("CCDK_BOT", "").strip()
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").rstrip("/")
    if cfg:
        return os.path.basename(cfg).lstrip(".") or "agent"
    return "agent"

BOT_NAME = _agent_name()
HOST = socket.gethostname()


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def _detect_state_dir() -> str:
    """Same priority as react_hook.detect_discord_state_dir."""
    explicit = os.environ.get("DISCORD_STATE_DIR", "")
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.join(cfg, "channels", "discord")
    return os.path.expanduser("~/.claude/channels/discord")


def _read_token(state_dir: str) -> str | None:
    env_path = os.path.join(state_dir, ".env")
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _last_user_text(transcript_path: str) -> str:
    """Tail the transcript and return the most recent real user-message text."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
            text = "\n".join(parts)
            if text:
                return text
    return ""


def _origin_from_transcript(transcript_path: str) -> tuple[str, str] | None:
    """Find (chat_id, msg_id) of the most recent Discord-origin user message."""
    user_text = _last_user_text(transcript_path)
    if not user_text:
        return None
    m = CHANNEL_TAG_RE.search(user_text)
    if not m:
        return None
    return m.group(1), m.group(2)


def _discord_post(token: str, channel_id: str, content: str) -> bool:
    """POST a new message to a channel. Returns True on 2xx."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "cc-discord-kit-notify-hook (1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        log(f"post HTTP {e.code} channel={channel_id}: {e.read()[:200]!r}")
        return False
    except Exception as e:
        log(f"post failed channel={channel_id}: {e}")
        return False


def _drop_notified_reaction(payload: dict) -> None:
    """Best-effort: invoke react_hook --mode notified to drop a 🔔.

    Resolves the script path via CCDK_REACT_HOOK_BIN env or falls back to
    `python3 <hooks_dir>/react_hook.py`. Silent on failure.
    """
    bin_path = os.environ.get("CCDK_REACT_HOOK_BIN", "").strip()
    if bin_path:
        cmd = [bin_path, "--mode", "notified"]
    else:
        cmd = [
            sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "react_hook.py"),
            "--mode", "notified",
        ]
    try:
        subprocess.run(
            cmd,
            input=json.dumps(payload).encode("utf-8"),
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _format_message(notification_type: str, payload: dict) -> str:
    """Short, actionable Discord message. No code block (paginate-guard friendly)."""
    cwd = payload.get("cwd", "")
    cwd_short = os.path.basename(cwd.rstrip("/")) if cwd else ""
    session = (payload.get("session_id", "") or "")[:8]
    label = {
        "permission_prompt": "🔔 Permission needed",
        "idle_prompt":       "🌙 Idle prompt",
        "elicitation_dialog": "❓ MCP elicitation",
    }.get(notification_type, f"🔔 {notification_type}")
    parts = [f"**{label}** — {BOT_NAME}@{HOST}"]
    if cwd_short:
        parts.append(f"(in `{cwd_short}`, session `{session}`)")
    return " ".join(parts)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload: dict = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log(f"bad JSON input: {raw[:200]!r}")
        return 0

    notification_type = payload.get("notification_type", "")
    if notification_type not in ACTIONABLE_TYPES:
        log(f"skip type={notification_type!r} (not actionable)")
        return 0

    state_dir = _detect_state_dir()
    token = _read_token(state_dir)
    if not token:
        log(f"no token at {state_dir} — skipping")
        return 0

    # Channel resolution: env override > transcript origin > skip.
    channel_id = os.environ.get("NOTIFY_CHANNEL_ID", "").strip()
    if not channel_id:
        origin = _origin_from_transcript(payload.get("transcript_path", ""))
        if origin:
            channel_id = origin[0]

    if not channel_id:
        log(f"no channel target type={notification_type} — skipping")
        return 0

    try:
        msg = _format_message(notification_type, payload)
    except Exception:
        log(f"format crash:\n{traceback.format_exc()}")
        return 0

    if os.environ.get("NOTIFY_DRY_RUN") == "1":
        log(f"DRY type={notification_type} channel={channel_id} msg={msg!r}")
        return 0

    ok = _discord_post(token, channel_id, msg)
    log(f"type={notification_type} channel={channel_id} posted={ok}")
    if ok:
        _drop_notified_reaction(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
