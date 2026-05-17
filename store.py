"""Shared memory + journal store across multiple agents.

Single source of truth that any number of agents (Claude Code instances,
Discord bots, helper scripts, web UI) can read and write to. Writes are
atomic per-call (last-writer-wins, no locking — collisions are rare
given typical message cadence).

Two stores:
  memories.json  — durable facts (cap 200), always loaded into prompt
  journal.json   — pinned moments (cap 1000), recent slice loaded into prompt

Data location is parametrized via the MULTIAGENT_DATA_DIR env var.
Default is `~/.local/share/multiagent-tools/`.

Schema (memory entry):
  id, ts, type, name, text, tags, about?, bot?
    about: list[str]  — subject(s) the memory is about
    bot:   list[str] | null  — if set, only that agent sees in default views
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def _resolve_data_dir() -> str:
    """Return the directory housing memories.json + journal.json."""
    explicit = os.environ.get("MULTIAGENT_DATA_DIR", "")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.expanduser("~/.local/share/multiagent-tools")


DATA_DIR = _resolve_data_dir()
MEMORIES_FILE = os.path.join(DATA_DIR, "memories.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "journal.json")

MEMORIES_CAP = 200
JOURNAL_CAP = 1000

VALID_TYPES = {"user", "feedback", "project", "reference"}

# Strip leading rendered-header lines that agents sometimes paste back when
# round-tripping content through `multiagent-tools memory show`. This keeps
# metadata displays from compounding into the stored body across edits.
_RENDERED_HEADER_RE = re.compile(
    r'^(About: [^\n]*\n+Saved: [^\n]*\n+)+', re.MULTILINE
)


def _strip_rendered_header(text: str) -> str:
    return _RENDERED_HEADER_RE.sub('', text.lstrip())


class JsonStore:
    """Persistent list-of-dicts store with auto-incrementing IDs.

    Cache uses mtime-based invalidation so external writes (e.g. backfill
    scripts, another process) are picked up on the next load. Without this,
    a long-running Flask server holds stale data forever.
    """

    def __init__(self, file_path: str, max_entries: int) -> None:
        self._file_path = file_path
        self.max_entries = max_entries
        self._cache: list[dict] | None = None
        self._cache_mtime: float = 0.0

    def _invalidate(self) -> None:
        self._cache = None
        self._cache_mtime = 0.0

    def _file_mtime(self) -> float:
        try:
            return os.path.getmtime(self._file_path)
        except OSError:
            return 0.0

    def _load_raw(self) -> list[dict]:
        """Load all entries including soft-deleted tombstones. Used for ID allocation."""
        current_mtime = self._file_mtime()
        if self._cache is not None and current_mtime == self._cache_mtime:
            return list(self._cache)
        if not os.path.exists(self._file_path):
            return []
        try:
            with open(self._file_path, "r") as f:
                data = json.load(f)
            result = data if isinstance(data, list) else []
            self._cache = result
            self._cache_mtime = current_mtime
            return list(result)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load %s: %s", self._file_path, e)
            return []

    def load(self) -> list[dict]:
        """Load live entries — tombstones (deleted=True) filtered out."""
        return [e for e in self._load_raw() if not e.get("deleted")]

    def save(self, entries: list[dict]) -> None:
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
        try:
            tmp = self._file_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._file_path)
            self._cache = entries
            self._cache_mtime = self._file_mtime()
        except OSError as e:
            log.warning("Failed to save %s: %s", self._file_path, e)
            self._invalidate()

    def next_id(self, entries: list[dict] | None = None) -> int:
        """Monotonic ID allocation: max over ALL entries including tombstones, +1.

        Ignores any `entries` arg passed by older callers — always reads raw to
        guarantee monotonicity. Deleted IDs are never reused.
        """
        raw = self._load_raw()
        if not raw:
            return 1
        return max(e.get("id", 0) for e in raw) + 1

    def add(self, extra_fields: dict) -> dict:
        raw = self._load_raw()
        entry = {
            "id": self.next_id(),
            "ts": datetime.now(timezone.utc).isoformat(),
            **extra_fields,
        }
        raw.append(entry)
        # Cap counts only live entries; tombstones don't count toward the cap.
        live_count = sum(1 for e in raw if not e.get("deleted"))
        if live_count > self.max_entries:
            # Drop oldest live entries until we're under the cap, but keep
            # tombstones (they're cheap and preserve ID monotonicity).
            excess = live_count - self.max_entries
            kept = []
            dropped = 0
            for e in raw:
                if dropped < excess and not e.get("deleted"):
                    dropped += 1
                    continue
                kept.append(e)
            raw = kept
        self.save(raw)
        return entry

    def update(self, entry_id: int, fields: dict) -> bool:
        raw = self._load_raw()
        for e in raw:
            if e.get("id") == entry_id and not e.get("deleted"):
                e.update(fields)
                self.save(raw)
                return True
        return False

    def remove(self, entry_id: int) -> bool:
        """Soft-delete: mark tombstone, keep ID reserved forever."""
        raw = self._load_raw()
        for e in raw:
            if e.get("id") == entry_id and not e.get("deleted"):
                e["deleted"] = True
                e["deleted_ts"] = datetime.now(timezone.utc).isoformat()
                self.save(raw)
                return True
        return False

    def clear(self) -> int:
        """Soft-delete everything live. Tombstones remain to preserve ID monotonicity."""
        raw = self._load_raw()
        count = 0
        ts = datetime.now(timezone.utc).isoformat()
        for e in raw:
            if not e.get("deleted"):
                e["deleted"] = True
                e["deleted_ts"] = ts
                count += 1
        self.save(raw)
        return count


# ─────────────────────────── memories ───────────────────────────

_memories = JsonStore(MEMORIES_FILE, MEMORIES_CAP)


def load_memories() -> list[dict]:
    return _memories.load()


def load_memories_raw() -> list[dict]:
    """Return all memories including tombstones (entries with deleted=True).

    Use only for debugging — most callers want load_memories(), which filters
    tombstones the same way the rest of the read path does.
    """
    return _memories._load_raw()


def save_memory(text: str, *, type: str = "feedback", name: str = "",
                tags: list[str] | None = None,
                about: list[str] | None = None,
                bot: list[str] | None = None) -> dict:
    """Save a durable memory. Type: user|feedback|project|reference.

    about: free-form subject labels. Defaults to [].
    bot:   if set, only matching bots include this in default views. Default null.
    """
    if type not in VALID_TYPES:
        type = "feedback"
    fields: dict = {
        "type": type,
        "name": name.strip(),
        "tags": list(tags) if tags else [],
        "text": _strip_rendered_header(text.strip()),
        "about": list(about) if about else [],
    }
    if bot is not None:
        fields["bot"] = list(bot)
    return _memories.add(fields)


def edit_memory(memory_id: int, text: str | None = None, *,
                name: str | None = None, type: str | None = None,
                tags: list[str] | None = None,
                about: list[str] | None = None,
                bot: list[str] | None = None,
                pinned: bool | None = None) -> bool:
    fields: dict = {}
    if text is not None:
        fields["text"] = _strip_rendered_header(text.strip())
    if name is not None:
        fields["name"] = name.strip()
    if type is not None and type in VALID_TYPES:
        fields["type"] = type
    if tags is not None:
        fields["tags"] = list(tags)
    if about is not None:
        fields["about"] = list(about)
    if bot is not None:
        fields["bot"] = list(bot)
    if pinned is not None:
        fields["pinned"] = bool(pinned)
    if not fields:
        return False
    return _memories.update(memory_id, fields)


def remove_memory(memory_id: int) -> bool:
    return _memories.remove(memory_id)


def search_memories(term: str) -> list[dict]:
    term_lower = term.lower()
    return [
        m for m in load_memories()
        if term_lower in m.get("text", "").lower()
        or term_lower in m.get("name", "").lower()
        or any(term_lower in t.lower() for t in m.get("tags", []))
        or any(term_lower in a.lower() for a in m.get("about", []))
    ]


def filter_memories(entries: list[dict] | None = None, *,
                    type: str | None = None,
                    about: list[str] | None = None,
                    bot: str | None = None,
                    show_all: bool = False) -> list[dict]:
    """Filter memories by type / about / bot.

    about: list of labels; entry matches if ANY of its `about` labels are in
           the filter list (OR semantics). Empty filter = no about filtering.
    bot:   the calling bot's name. Default view hides entries with `bot` set
           UNLESS the calling bot is in that list. show_all=True bypasses.
    """
    if entries is None:
        entries = load_memories()
    out = []
    for m in entries:
        if type and m.get("type") != type:
            continue
        if about:
            entry_about = m.get("about", []) or []
            if not any(label in entry_about for label in about):
                continue
        if not show_all:
            entry_bot = m.get("bot")
            if entry_bot:
                if not bot or bot not in entry_bot:
                    continue
        out.append(m)
    return out


def format_memories_for_prompt(*, bot: str | None = None,
                               types: list[str] | None = None,
                               exclude_types: list[str] | None = None) -> str:
    """Full memory dump — for SessionStart hooks (cached, paid once per session).

    If `bot` is provided, hides entries with a `bot` field that doesn't include
    the caller. Shared entries (no `bot` field) are always shown.
    `types` restricts to only those types; `exclude_types` drops those types.
    """
    entries = filter_memories(bot=bot)
    if types is not None:
        entries = [m for m in entries if m.get("type", "feedback") in types]
    if exclude_types is not None:
        entries = [m for m in entries if m.get("type", "feedback") not in exclude_types]
    if not entries:
        return ""
    lines = ["MEMORIES (durable facts shared across agents):"]
    by_type: dict[str, list[dict]] = {}
    for m in entries:
        by_type.setdefault(m.get("type", "feedback"), []).append(m)
    for t in ("user", "project", "feedback", "reference"):
        if t not in by_type:
            continue
        lines.append(f"\n[{t.upper()}]")
        for m in by_type[t]:
            header = f"#{m['id']}"
            if m.get("name"):
                header += f" {m['name']}"
            extras = []
            if m.get("tags"):
                extras.append(', '.join(m['tags']))
            if m.get("about"):
                extras.append("about: " + ', '.join(m['about']))
            if extras:
                header += f" ({' | '.join(extras)})"
            lines.append(f"- {header}")
            lines.append(f"  {m['text']}")
    return "\n".join(lines)


def format_memories_index(*, bot: str | None = None,
                          types: list[str] | None = None,
                          exclude_types: list[str] | None = None) -> str:
    """Compact index — name + type + tags only, no body. ~800 tokens for 60 entries.
    For UserPromptSubmit hooks where the full dump is too expensive every turn.
    Bot can `multiagent-tools memory show <id>` to read any specific entry in full.
    `types` restricts to only those types; `exclude_types` drops those types.
    """
    entries = filter_memories(bot=bot)
    if types is not None:
        entries = [m for m in entries if m.get("type", "feedback") in types]
    if exclude_types is not None:
        entries = [m for m in entries if m.get("type", "feedback") not in exclude_types]
    if not entries:
        return ""
    lines = [
        "MEMORIES INDEX — names + tags only.",
        "Run `multiagent-tools memory show <id>` to read full text of any entry.",
    ]
    by_type: dict[str, list[dict]] = {}
    for m in entries:
        by_type.setdefault(m.get("type", "feedback"), []).append(m)
    for t in ("user", "project", "feedback", "reference"):
        if t not in by_type:
            continue
        lines.append(f"\n[{t.upper()}]")
        for m in by_type[t]:
            head = f"#{m['id']} {m.get('name', '')}"
            tags = m.get("tags", [])
            if tags:
                head += f" ({','.join(tags[:3])})"
            about = m.get("about", [])
            if about:
                head += f" [{','.join(about)}]"
            lines.append(f"  {head}")
    return "\n".join(lines)


# ─────────────────────────── journal ───────────────────────────

_journal = JsonStore(JOURNAL_FILE, JOURNAL_CAP)


def load_journal() -> list[dict]:
    return _journal.load()


def load_journal_raw() -> list[dict]:
    """Return all journal entries including tombstones. Debugging only."""
    return _journal._load_raw()


def add_journal(text: str, *, source: str = "", actor: str = "",
                tags: list[str] | None = None) -> dict:
    """Pin a moment. source = 'discord:my-channel' / 'cli' / etc.
    actor = agent or user name.
    """
    return _journal.add({
        "source": source,
        "actor": actor,
        "tags": list(tags) if tags else [],
        "text": text.strip(),
    })


def remove_journal(entry_id: int) -> bool:
    return _journal.remove(entry_id)


def edit_journal(entry_id: int, text: str | None = None, *,
                 actor: str | None = None,
                 source: str | None = None,
                 tags: list[str] | None = None) -> bool:
    fields: dict = {}
    if text is not None:
        fields["text"] = text.strip()
    if actor is not None:
        fields["actor"] = actor.strip()
    if source is not None:
        fields["source"] = source.strip()
    if tags is not None:
        fields["tags"] = list(tags)
    if not fields:
        return False
    return _journal.update(entry_id, fields)


def search_journal(term: str) -> list[dict]:
    term_lower = term.lower()
    return [
        e for e in load_journal()
        if term_lower in e.get("text", "").lower()
        or any(term_lower in t.lower() for t in e.get("tags", []))
    ]


def journal_recent(days: int = 7) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for e in load_journal():
        try:
            ts = datetime.fromisoformat(e.get("ts", "").replace("Z", "+00:00"))
            if ts >= cutoff:
                out.append(e)
        except (ValueError, TypeError):
            continue
    return out


def format_journal_for_prompt(days: int = 7) -> str:
    entries = journal_recent(days)
    if not entries:
        return ""
    lines = [f"JOURNAL (pinned moments, last {days} days):"]
    for e in entries:
        ts = e.get("ts", "")[:10]
        actor = e.get("actor", "")
        src = e.get("source", "")
        head = f"#{e['id']} [{ts}"
        if actor:
            head += f" by {actor}"
        if src:
            head += f" via {src}"
        head += "]"
        lines.append(f"- {head}")
        lines.append(f"  {e['text']}")
    return "\n".join(lines)
