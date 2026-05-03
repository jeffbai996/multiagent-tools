"""Digest helper — pulls recent Discord channel history for human review.

Pull-model digest: no LLM, no cron. The web UI calls fetch_window() on page
load, the user reads the messages, types a digest paragraph, and the route
saves it as a journal entry.

Discord token is loaded from `~/.config/multiagent-tools/env`
(`MULTIAGENT_DISCORD_TOKEN`). The bot user behind that token must (1) be a
member of every channel listed in DIGEST_CHANNELS, and (2) have the
Message Content intent enabled in the Discord developer portal — without
it, message bodies come back empty.

Channels are read from the same env file (MULTIAGENT_DIGEST_CHANNELS, a
comma-separated list of "name:id" pairs) so this module ships without
hardcoded server data.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import TypedDict

DISCORD_API = "https://discord.com/api/v10"

ENV_FILE = os.path.expanduser("~/.config/multiagent-tools/env")
DISCORD_TOKEN_VAR = "MULTIAGENT_DISCORD_TOKEN"
DIGEST_CHANNELS_VAR = "MULTIAGENT_DIGEST_CHANNELS"


def _read_env_var(name: str) -> str | None:
    """Look up `name` in os.environ, then in the env file."""
    if v := os.environ.get(name):
        return v
    if not os.path.exists(ENV_FILE):
        return None
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip()
                    return val.strip('"').strip("'")
    except OSError:
        return None
    return None


def _load_digest_channels() -> dict[str, str]:
    """Parse `MULTIAGENT_DIGEST_CHANNELS=name1:id1,name2:id2`."""
    raw = _read_env_var(DIGEST_CHANNELS_VAR)
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, channel_id = pair.split(":", 1)
        name = name.strip()
        channel_id = channel_id.strip()
        if name and channel_id:
            out[name] = channel_id
    return out


# Lazy load so the module imports cleanly without a config file.
DIGEST_CHANNELS: dict[str, str] = _load_digest_channels()

DEFAULT_HOURS = 24
HARD_MESSAGE_CAP = 200  # per channel — Discord paginates 100 max per request


class Message(TypedDict):
    channel: str
    channel_id: str
    id: str
    author: str
    author_id: str
    is_bot: bool
    content: str
    ts: str  # ISO 8601 from Discord


def _load_token() -> str | None:
    """Read MULTIAGENT_DISCORD_TOKEN from env or env file."""
    return _read_env_var(DISCORD_TOKEN_VAR)


def _fetch_channel(channel_id: str, after_ms: int, token: str) -> list[dict]:
    """Pull messages from one channel newer than after_ms (Discord snowflake).

    Discord's `after` parameter takes a snowflake ID, not a timestamp. We
    convert: snowflake = (unix_ms - DISCORD_EPOCH_MS) << 22.
    """
    DISCORD_EPOCH_MS = 1420070400000
    after_snowflake = (after_ms - DISCORD_EPOCH_MS) << 22

    out: list[dict] = []
    cursor = str(after_snowflake)
    fetched = 0
    while fetched < HARD_MESSAGE_CAP:
        params = urllib.parse.urlencode({"limit": "100", "after": cursor})
        url = f"{DISCORD_API}/channels/{channel_id}/messages?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "multiagent-tools digest (python urllib)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                batch = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"Discord API {e.code} for channel {channel_id}: {e.read()[:200]!r}"
            ) from e
        if not batch:
            break
        out.extend(batch)
        fetched += len(batch)
        # Discord returns newest-first when using `after`; pagination cursor
        # is the largest id we've seen so far.
        cursor = max(m["id"] for m in batch)
        if len(batch) < 100:
            break
    return out


def fetch_window(hours: int = DEFAULT_HOURS) -> list[Message]:
    """Return all messages from configured channels within the last `hours`.

    Sorted oldest → newest, channel-interleaved.
    Returns [] (not raise) if token is missing — caller should check and
    render an explanatory message instead of a stack trace.
    """
    token = _load_token()
    if not token:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_ms = int(cutoff.timestamp() * 1000)

    msgs: list[Message] = []
    for channel_name, channel_id in DIGEST_CHANNELS.items():
        try:
            raw = _fetch_channel(channel_id, cutoff_ms, token)
        except RuntimeError:
            # One channel failing shouldn't blank the whole page — skip it.
            # Debug visibility: we surface the partial result; the channel
            # just won't contribute messages this run.
            continue
        for m in raw:
            author = m.get("author", {}) or {}
            msgs.append({
                "channel": channel_name,
                "channel_id": channel_id,
                "id": m.get("id", ""),
                "author": author.get("username", "?"),
                "author_id": author.get("id", ""),
                "is_bot": bool(author.get("bot", False)),
                "content": m.get("content", "") or "",
                "ts": m.get("timestamp", ""),
            })

    msgs.sort(key=lambda m: m["ts"])
    return msgs


def _load_gemini_key() -> str | None:
    """Read GEMINI_API_KEY from env or env file."""
    return _read_env_var("GEMINI_API_KEY")


# gemini-3-flash-preview: cheap + fast, plenty for terse digest summaries.
# Pinned here rather than env-driven so summarize cost stays predictable
# regardless of what the conversational bots are using.
SUMMARIZE_MODEL = "gemini-3-flash-preview"

SUMMARIZE_SYSTEM = (
    "Summarize the following Discord chat messages into a tight digest "
    "paragraph. No voice, no persona — just dense factual recap. Surface: "
    "decisions made, problems hit, things shipped, open threads, and any "
    "named people / tickers / files / repos that show up. Skip pleasantries "
    "and bot status noise. ~200 words max. No preamble, no markdown headers, "
    "no bullets — one to three flowing paragraphs."
)


def summarize_messages(msgs: list[Message]) -> str:
    """Call gemini-3-flash-preview to summarize the digest window.

    Raises RuntimeError if the API key is missing or the request fails;
    callers (the Flask route) should catch and surface a friendly message.
    """
    api_key = _load_gemini_key()
    if not api_key:
        raise RuntimeError(
            f"GEMINI_API_KEY not found in env or {ENV_FILE}"
        )
    if not msgs:
        raise RuntimeError("no messages in window to summarize")

    # Build a compact transcript. Channel + author + content; bot messages
    # included because they often carry the actual decisions/results.
    lines: list[str] = []
    for m in msgs:
        ts = m["ts"][:16].replace("T", " ") if m["ts"] else ""
        author = m["author"] + ("[bot]" if m["is_bot"] else "")
        lines.append(f"[{ts}] #{m['channel']} {author}: {m['content']}")
    transcript = "\n".join(lines)

    body = {
        "systemInstruction": {"role": "system", "parts": [{"text": SUMMARIZE_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": transcript}]}],
        # gemini-3-flash-preview is a thinking model — token budget covers
        # both internal reasoning AND visible output. 1024 was too tight;
        # the model burned most of it on thinking and left only ~190 chars
        # of summary before stopping (observed 2026-05-01). 4096 is enough
        # headroom for typical 24h windows. thinkingLevel=low keeps cost
        # bounded — we don't need deep reasoning for a recap, just a summary.
        "generationConfig": {
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{SUMMARIZE_MODEL}:generateContent?key={api_key}"
    )
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"gemini API {e.code}: {e.read()[:300]!r}"
        ) from e
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"gemini returned no candidates: {payload!r}"[:300])
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str))
    return text.strip()


def stats(msgs: list[Message]) -> dict:
    """Quick counts for the page header — speakers, channel split, bot ratio."""
    by_channel: dict[str, int] = {}
    by_author: dict[str, int] = {}
    bot_count = 0
    for m in msgs:
        by_channel[m["channel"]] = by_channel.get(m["channel"], 0) + 1
        by_author[m["author"]] = by_author.get(m["author"], 0) + 1
        if m["is_bot"]:
            bot_count += 1
    return {
        "total": len(msgs),
        "by_channel": by_channel,
        "by_author": dict(sorted(by_author.items(), key=lambda kv: -kv[1])),
        "bot_count": bot_count,
        "human_count": len(msgs) - bot_count,
    }
