"""Edit/delete history for the multiagent-tools.

Why this exists: web routes mutate memories.json and journal.json with no undo.
Every edit and delete done through the web UI gets a JSONL line in edits.jsonl
recording the prior state of the record (full snapshot in `before`) plus the
diff that was applied (`after`). That lets us:

  - show "this entry was edited N times" in the UI
  - revert any past edit (last-write-wins, no merge)
  - restore a deleted record from the trash view

Direct CLI / Discord / hook calls into store.py do NOT pass through here. That's
intentional — history capture is best-effort, scoped to the web surface where
fat-finger risk is highest.

File location matches store.py's data dir (DATA_DIR), so MULTIAGENT_DATA_DIR
overrides apply automatically. File: edits.jsonl, capped at 5000 lines via
truncate-and-rename after each append.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from store import (
    DATA_DIR,
    edit_journal,
    edit_memory,
    load_journal,
    load_memories,
    remove_journal,
    remove_memory,
    _journal,
    _memories,
)

log = logging.getLogger(__name__)

EDITS_FILE = os.path.join(DATA_DIR, "edits.jsonl")
MAX_HISTORY_LINES = 5000

VALID_KINDS = {"memory", "journal"}


# ─────────────────────────── core append + truncate ───────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_line(entry: dict) -> None:
    """Append one JSON line; if file exceeds cap, atomically truncate to last N."""
    os.makedirs(os.path.dirname(EDITS_FILE), exist_ok=True)
    try:
        with open(EDITS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("Failed to append history line to %s: %s", EDITS_FILE, e)
        return
    _maybe_truncate()


def _maybe_truncate() -> None:
    """If edits.jsonl > MAX_HISTORY_LINES, rewrite atomically with last N lines.

    Cheaper than counting on every call would be tracking line count in memory,
    but multiple processes write to this file (web + CLI later, maybe). Reading
    the whole file is fine at 5000 lines (~MB-scale).
    """
    try:
        with open(EDITS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= MAX_HISTORY_LINES:
        return
    keep = lines[-MAX_HISTORY_LINES:]
    tmp = EDITS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, EDITS_FILE)
    except OSError as e:
        log.warning("Failed to truncate %s: %s", EDITS_FILE, e)


# ─────────────────────────── recording ───────────────────────────


def record_edit(
    kind: str,
    entry_id: int,
    before_record: dict,
    after_fields: dict,
    *,
    actor: str = "web",
) -> None:
    """Log an edit. `before_record` = full prior record. `after_fields` = applied diff."""
    if kind not in VALID_KINDS:
        log.warning("record_edit: invalid kind %r", kind)
        return
    _append_line({
        "kind": kind,
        "id": int(entry_id),
        "ts": _now_iso(),
        "actor": actor,
        "before": before_record,
        "after": after_fields,
    })


def record_delete(
    kind: str,
    entry_id: int,
    before_record: dict,
    *,
    actor: str = "web",
) -> None:
    """Log a delete. `before_record` = full record so we can restore it."""
    if kind not in VALID_KINDS:
        log.warning("record_delete: invalid kind %r", kind)
        return
    _append_line({
        "kind": kind,
        "id": int(entry_id),
        "ts": _now_iso(),
        "actor": actor,
        "deleted": True,
        "before": before_record,
    })


# ─────────────────────────── reading ───────────────────────────


def _iter_history() -> list[dict]:
    """Read all history lines, oldest first. Bad lines skipped."""
    if not os.path.exists(EDITS_FILE):
        return []
    out: list[dict] = []
    try:
        with open(EDITS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log.warning("Failed to read %s: %s", EDITS_FILE, e)
    return out


def load_history(kind: str, entry_id: int) -> list[dict]:
    """All history entries for one record, oldest first."""
    return [
        h for h in _iter_history()
        if h.get("kind") == kind and h.get("id") == entry_id
    ]


def load_recent_deletes(limit: int = 50) -> list[dict]:
    """Most recent N deleted records (any kind), newest first. For trash view."""
    deletes = [h for h in _iter_history() if h.get("deleted")]
    deletes.reverse()
    return deletes[:limit]


# ─────────────────────────── revert / restore ───────────────────────────


def _record_to_edit_kwargs_memory(record: dict) -> dict:
    """Pull edit_memory kwargs out of a full memory record."""
    kwargs: dict[str, Any] = {
        "text": record.get("text", ""),
        "name": record.get("name", ""),
        "type": record.get("type"),
        "tags": list(record.get("tags", []) or []),
        "about": list(record.get("about", []) or []),
    }
    if "bot" in record:
        kwargs["bot"] = list(record.get("bot") or [])
    return kwargs


def _record_to_edit_kwargs_journal(record: dict) -> dict:
    """Pull edit_journal kwargs out of a full journal record."""
    return {
        "text": record.get("text", ""),
        "actor": record.get("actor", ""),
        "source": record.get("source", ""),
        "tags": list(record.get("tags", []) or []),
    }


def revert_edit(history_entry: dict) -> bool:
    """Restore the `before` snapshot of one history entry.

    Last-write-wins: if the record was edited again after this entry, those
    later changes are clobbered. That's fine — the user picked this snapshot.

    Returns True if the underlying store update succeeded.
    """
    kind = history_entry.get("kind")
    entry_id = history_entry.get("id")
    before = history_entry.get("before") or {}
    if not isinstance(entry_id, int) or kind not in VALID_KINDS:
        return False
    if kind == "memory":
        return edit_memory(entry_id, **_record_to_edit_kwargs_memory(before))
    return edit_journal(entry_id, **_record_to_edit_kwargs_journal(before))


def purge_history_entry(history_entry: dict) -> bool:
    """Permanently remove ONE history entry from edits.jsonl.

    Used by the trash UI's "delete forever" button: matches the entry by
    (kind, id, ts) and rewrites the file without it. The matched line is
    gone for good — restore_deleted can no longer find it.

    Atomic write (temp + rename) so concurrent appenders don't see a
    partial file. Returns True if a line was removed.
    """
    target_kind = history_entry.get("kind")
    target_id = history_entry.get("id")
    target_ts = history_entry.get("ts")
    if target_kind not in VALID_KINDS or not isinstance(target_id, int) or not target_ts:
        return False

    if not os.path.exists(EDITS_FILE):
        return False
    try:
        with open(EDITS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        log.warning("purge: failed to read %s: %s", EDITS_FILE, e)
        return False

    kept: list[str] = []
    removed = 0
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if (
            obj.get("kind") == target_kind
            and obj.get("id") == target_id
            and obj.get("ts") == target_ts
        ):
            removed += 1
            continue
        kept.append(line)

    if removed == 0:
        return False

    tmp = EDITS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, EDITS_FILE)
    except OSError as e:
        log.warning("purge: failed to rewrite %s: %s", EDITS_FILE, e)
        return False
    return True


def purge_all_deletes() -> int:
    """Permanently remove every delete-history entry from edits.jsonl.

    Powers the trash-page 'empty trash' button. Edit history (non-delete
    lines) is preserved — only entries with `deleted=True` are removed,
    matching exactly what load_recent_deletes() surfaces in the UI.

    Tombstones in memories.json / journal.json are NOT touched; this only
    wipes the recoverability path so restore_deleted() can no longer find
    these records. IDs stay reserved forever via tombstones, same as a
    single-row purge — monotonic ID allocation is preserved.

    Atomic write (temp + rename). Returns the count of removed lines.
    """
    if not os.path.exists(EDITS_FILE):
        return 0
    try:
        with open(EDITS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        log.warning("purge_all: failed to read %s: %s", EDITS_FILE, e)
        return 0

    kept: list[str] = []
    removed = 0
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if obj.get("deleted") is True:
            removed += 1
            continue
        kept.append(line)

    if removed == 0:
        return 0

    tmp = EDITS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, EDITS_FILE)
    except OSError as e:
        log.warning("purge_all: failed to rewrite %s: %s", EDITS_FILE, e)
        return 0
    return removed


def restore_deleted(history_entry: dict) -> dict | None:
    """Re-add a deleted record. Reuses original id if free, else assigns a new one
    and stamps `restored_from_id` for traceability.

    Returns the restored record, or None on failure.
    """
    if not history_entry.get("deleted"):
        return None
    kind = history_entry.get("kind")
    before = history_entry.get("before") or {}
    if kind not in VALID_KINDS or not before:
        return None

    store_obj = _memories if kind == "memory" else _journal
    entries = store_obj.load()
    existing_ids = {e.get("id") for e in entries}
    original_id = before.get("id")

    # Build fresh record from snapshot (drop ts so we re-stamp; keep everything else).
    new_record = {k: v for k, v in before.items() if k not in ("id", "ts")}
    new_record["ts"] = _now_iso()

    if isinstance(original_id, int) and original_id not in existing_ids:
        new_record["id"] = original_id
    else:
        new_record["id"] = store_obj.next_id(entries)
        if isinstance(original_id, int):
            new_record["restored_from_id"] = original_id

    entries.append(new_record)
    if len(entries) > store_obj.max_entries:
        entries = entries[-store_obj.max_entries:]
    store_obj.save(entries)
    return new_record


# ─────────────────────────── web wrappers ───────────────────────────


def _find_memory(memory_id: int) -> dict | None:
    """Return a DEEP COPY of the memory record. Critical: store.update() mutates
    dicts in place, and the cache hands out shared references. Without deepcopy
    our `before` snapshot would mutate alongside the live record."""
    for m in load_memories():
        if m.get("id") == memory_id:
            return copy.deepcopy(m)
    return None


def _find_journal(entry_id: int) -> dict | None:
    """Same deepcopy reasoning as _find_memory — see comment there."""
    for e in load_journal():
        if e.get("id") == entry_id:
            return copy.deepcopy(e)
    return None


def edit_memory_with_history(memory_id: int, *, actor: str = "web", **fields) -> bool:
    """Edit a memory, capturing prior state to history first.

    `fields` are passed straight to store.edit_memory. Only fields that store
    actually accepts (text, name, type, tags, about, bot) are forwarded; extras
    are dropped to avoid TypeError from the store layer.
    """
    before = _find_memory(memory_id)
    if before is None:
        return False
    allowed = {"text", "name", "type", "tags", "about", "bot", "pinned"}
    edit_fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not edit_fields:
        return False
    ok = edit_memory(memory_id, **edit_fields)
    if ok:
        # Snapshot before mutation. We took it before edit_memory ran, so it's
        # safe to log post-facto here.
        record_edit("memory", memory_id, before, edit_fields, actor=actor)
    return ok


def edit_journal_with_history(entry_id: int, *, actor: str = "web", **fields) -> bool:
    """Edit a journal entry, capturing prior state to history first.

    Naming snag: `actor` is both a history field (who did the edit) AND a journal
    record field (who pinned the moment). The kwarg `actor` here is the history
    one. To edit the journal's `actor` field, pass `entry_actor=`.
    """
    before = _find_journal(entry_id)
    if before is None:
        return False
    allowed = {"text", "actor", "source", "tags"}
    raw = dict(fields)
    if "entry_actor" in raw:
        raw["actor"] = raw.pop("entry_actor")
    edit_fields = {k: v for k, v in raw.items() if k in allowed and v is not None}
    if not edit_fields:
        return False
    ok = edit_journal(entry_id, **edit_fields)
    if ok:
        record_edit("journal", entry_id, before, edit_fields, actor=actor)
    return ok


def remove_memory_with_history(memory_id: int, *, actor: str = "web") -> bool:
    before = _find_memory(memory_id)
    if before is None:
        return False
    ok = remove_memory(memory_id)
    if ok:
        record_delete("memory", memory_id, before, actor=actor)
    return ok


def remove_journal_with_history(entry_id: int, *, actor: str = "web") -> bool:
    before = _find_journal(entry_id)
    if before is None:
        return False
    ok = remove_journal(entry_id)
    if ok:
        record_delete("journal", entry_id, before, actor=actor)
    return ok
