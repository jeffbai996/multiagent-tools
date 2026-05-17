"""Discord channel transcript archiver with rolling retention.

Polls configured digest channels via the same Discord REST path used by
digest.py, stores per-channel per-day JSONL files, and writes markdown shadow
files that external search tools can index.

Env:
  MULTIAGENT_TRANSCRIPTS_DIR             storage dir
  MULTIAGENT_TRANSCRIPTS_POLL_INTERVAL   seconds between --watch polls
  MULTIAGENT_TRANSCRIPTS_RETENTION_DAYS  prune older than this, default 365
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from digest import DIGEST_CHANNELS, _fetch_channel, _load_token  # noqa: E402

log = logging.getLogger(__name__)

TRANSCRIPTS_DIR = Path(os.environ.get(
    "MULTIAGENT_TRANSCRIPTS_DIR",
    os.path.expanduser("~/.local/share/multiagent-tools/transcripts"),
))
STATE_FILE = TRANSCRIPTS_DIR / "state.json"
POLL_INTERVAL_SEC = int(os.environ.get("MULTIAGENT_TRANSCRIPTS_POLL_INTERVAL", "300"))
RETENTION_DAYS = int(os.environ.get("MULTIAGENT_TRANSCRIPTS_RETENTION_DAYS", "365"))
DISCORD_EPOCH_MS = 1420070400000
_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(jsonl|md)$")


def _load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("state file unreadable, starting fresh")
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def _initial_cursor_ms() -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)


def _channel_dir(channel_name: str) -> Path:
    return TRANSCRIPTS_DIR / channel_name


def _jsonl_path(channel_name: str, day: str) -> Path:
    return _channel_dir(channel_name) / f"{day}.jsonl"


def _md_path(channel_name: str, day: str) -> Path:
    return _channel_dir(channel_name) / f"{day}.md"


def _ts_to_day(iso_ts: str) -> str:
    return iso_ts[:10]


def _append_messages(channel_name: str, msgs: list[dict]) -> set[str]:
    touched_days: set[str] = set()
    if not msgs:
        return touched_days

    by_day: dict[str, list[dict]] = {}
    for m in msgs:
        by_day.setdefault(_ts_to_day(m["ts"]), []).append(m)

    _channel_dir(channel_name).mkdir(parents=True, exist_ok=True)
    for day, day_msgs in by_day.items():
        jsonl = _jsonl_path(channel_name, day)
        existing_ids: set[str] = set()
        if jsonl.exists():
            for line in jsonl.read_text().splitlines():
                try:
                    existing_ids.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    pass

        new = [m for m in day_msgs if m.get("id") not in existing_ids]
        if not new:
            continue
        with jsonl.open("a") as f:
            for m in new:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        touched_days.add(day)
    return touched_days


def _rebuild_md(channel_name: str, day: str) -> None:
    jsonl = _jsonl_path(channel_name, day)
    md = _md_path(channel_name, day)
    if not jsonl.exists():
        md.unlink(missing_ok=True)
        return

    msgs: list[dict] = []
    for line in jsonl.read_text().splitlines():
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    msgs.sort(key=lambda m: m.get("ts", ""))

    parts = [
        "---",
        f'channel: "{channel_name}"',
        f"date: {day}",
        f"messages: {len(msgs)}",
        "---",
        "",
        f"# #{channel_name} - {day}",
        "",
    ]
    for m in msgs:
        time_part = (m.get("ts", "")[11:16] or "??:??")
        author = m.get("author", "?")
        bot_marker = " [bot]" if m.get("is_bot") else ""
        content = m.get("content", "") or "(empty)"
        parts.append(f"**{author}{bot_marker}** . {time_part}")
        for line in content.splitlines() or [""]:
            parts.append(f"> {line}")
        parts.append("")
    md.write_text("\n".join(parts))


def _prune_old(retention_days: int = RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).date()
    removed = 0
    if not TRANSCRIPTS_DIR.exists():
        return 0
    for channel_dir in TRANSCRIPTS_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
        for f in channel_dir.iterdir():
            m = _DAY_RE.match(f.name)
            if not m:
                continue
            try:
                day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
    return removed


def _poll_once(state: dict[str, str], token: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for channel_name, channel_id in DIGEST_CHANNELS.items():
        last_id = state.get(channel_id)
        after_ms = ((int(last_id) >> 22) + DISCORD_EPOCH_MS) if last_id else _initial_cursor_ms()
        try:
            raw = _fetch_channel(channel_id, after_ms, token)
        except RuntimeError as e:
            log.warning("channel %s fetch failed: %s", channel_name, e)
            counts[channel_name] = 0
            continue

        msgs = []
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

        if msgs:
            touched = _append_messages(channel_name, msgs)
            for day in touched:
                _rebuild_md(channel_name, day)
            state[channel_id] = max(m["id"] for m in msgs)
        counts[channel_name] = len(msgs)
    return counts


def read_window(start_date: str, end_date: str,
                channel_filter: list[str] | None = None) -> list[dict]:
    if not TRANSCRIPTS_DIR.exists():
        return []
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return []
    if sd > ed:
        sd, ed = ed, sd

    target_channels = channel_filter or list(DIGEST_CHANNELS.keys())
    out: list[dict] = []
    for channel_name in target_channels:
        channel_dir = _channel_dir(channel_name)
        if not channel_dir.exists():
            continue
        for f in sorted(channel_dir.glob("*.jsonl")):
            m = _DAY_RE.match(f.name)
            if not m:
                continue
            try:
                day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < sd or day > ed:
                continue
            for line in f.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    out.sort(key=lambda m: m.get("ts", ""))
    return out


def archive_stats() -> dict:
    stats: dict = {
        "channels": {},
        "earliest_day": None,
        "latest_day": None,
        "total_messages": 0,
    }
    if not TRANSCRIPTS_DIR.exists():
        return stats
    all_days: set[str] = set()
    for channel_name in DIGEST_CHANNELS:
        channel_dir = _channel_dir(channel_name)
        if not channel_dir.exists():
            stats["channels"][channel_name] = {"days": 0, "messages": 0}
            continue
        days = 0
        msgs = 0
        for f in channel_dir.glob("*.jsonl"):
            m = _DAY_RE.match(f.name)
            if not m:
                continue
            days += 1
            all_days.add(m.group(1))
            try:
                msgs += sum(1 for _ in f.open())
            except OSError:
                pass
        stats["channels"][channel_name] = {"days": days, "messages": msgs}
        stats["total_messages"] += msgs
    if all_days:
        sorted_days = sorted(all_days)
        stats["earliest_day"] = sorted_days[0]
        stats["latest_day"] = sorted_days[-1]
    return stats


def _reindex_all() -> int:
    count = 0
    if not TRANSCRIPTS_DIR.exists():
        return 0
    for channel_dir in TRANSCRIPTS_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
        for f in channel_dir.glob("*.jsonl"):
            _rebuild_md(channel_dir.name, f.stem)
            count += 1
    return count


def run_once() -> dict[str, int]:
    token = _load_token()
    if not token:
        log.error("no Discord token; cannot poll")
        return {}
    state = _load_state()
    counts = _poll_once(state, token)
    _save_state(state)
    pruned = _prune_old()
    if pruned:
        log.info("pruned %d transcript files older than %d days", pruned, RETENTION_DAYS)
    return counts


def run_forever(interval_sec: int = POLL_INTERVAL_SEC) -> None:
    log.info("transcript poller starting (interval=%ds)", interval_sec)
    while True:
        try:
            counts = run_once()
            total = sum(counts.values())
            if total:
                log.info("poll added %d messages", total)
        except Exception:
            log.exception("poll cycle crashed")
        time.sleep(interval_sec)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Discord transcript archiver")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--reindex", action="store_true")
    p.add_argument("--prune-only", action="store_true")
    args = p.parse_args(argv)

    if args.reindex:
        print(f"rebuilt { _reindex_all() } markdown shadows")
        return 0
    if args.prune_only:
        print(f"pruned { _prune_old() } files older than {RETENTION_DAYS} days")
        return 0
    if args.watch:
        run_forever()
        return 0
    print(json.dumps(run_once(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
