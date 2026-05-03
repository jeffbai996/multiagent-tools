"""History module tests.

Each test uses the `fresh_store` fixture from conftest.py, which reloads
store + history with a temp DATA_DIR. That keeps tests fully isolated and
prevents touching real multiagent-tools data.
"""

from __future__ import annotations

import json
import os


def test_record_edit_then_load_history(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("original text", name="t1")

    before = dict(mem)
    history.record_edit("memory", mem["id"], before, {"text": "new text"}, actor="web")

    entries = history.load_history("memory", mem["id"])
    assert len(entries) == 1
    assert entries[0]["kind"] == "memory"
    assert entries[0]["id"] == mem["id"]
    assert entries[0]["actor"] == "web"
    assert entries[0]["before"] == before
    assert entries[0]["after"] == {"text": "new text"}
    assert "deleted" not in entries[0]


def test_record_delete_then_restore(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("kill me", name="trash", tags=["foo"])
    mem_id = mem["id"]

    # Capture full record, delete, then restore from the history log.
    history.record_delete("memory", mem_id, dict(mem), actor="web")
    assert store.remove_memory(mem_id) is True
    assert all(m["id"] != mem_id for m in store.load_memories())

    deletes = history.load_recent_deletes()
    assert len(deletes) == 1
    restored = history.restore_deleted(deletes[0])
    assert restored is not None
    # ID slot should be free (nothing else added), so original id reused.
    assert restored["id"] == mem_id
    assert restored["text"] == "kill me"
    assert restored["name"] == "trash"
    assert "restored_from_id" not in restored


def test_restore_deleted_collision_assigns_new_id(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("first", name="a")
    mem_id = mem["id"]

    history.record_delete("memory", mem_id, dict(mem), actor="web")
    store.remove_memory(mem_id)

    # Add a new memory — next_id is monotonic on max(id)+1, so this lands on
    # mem_id+1, not the freed slot. To force a collision we manually re-add
    # with the original id.
    store._memories.save(store.load_memories() + [{
        "id": mem_id, "ts": "2026-01-01T00:00:00+00:00",
        "type": "feedback", "name": "squatter", "tags": [],
        "text": "took the slot", "about": [],
    }])

    deletes = history.load_recent_deletes()
    restored = history.restore_deleted(deletes[0])
    assert restored is not None
    assert restored["id"] != mem_id
    assert restored.get("restored_from_id") == mem_id
    assert restored["text"] == "first"


def test_revert_edit_memory_restores_prior_text(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("v1", name="evolving")
    mem_id = mem["id"]

    # Use the wrapper so history is captured automatically.
    assert history.edit_memory_with_history(mem_id, text="v2") is True
    cur = [m for m in store.load_memories() if m["id"] == mem_id][0]
    assert cur["text"] == "v2"

    hist = history.load_history("memory", mem_id)
    assert len(hist) == 1
    assert history.revert_edit(hist[0]) is True

    after_revert = [m for m in store.load_memories() if m["id"] == mem_id][0]
    assert after_revert["text"] == "v1"
    assert after_revert["name"] == "evolving"


def test_two_edits_revert_first_is_last_write_wins(fresh_store):
    """Reverting the first edit's snapshot wins, even though a later edit happened.

    This documents the deliberate choice: revert applies one snapshot, ignoring
    subsequent edits. Simple, predictable, no merge logic.
    """
    store, history = fresh_store
    mem = store.save_memory("v1", name="x")
    mem_id = mem["id"]

    history.edit_memory_with_history(mem_id, text="v2")
    history.edit_memory_with_history(mem_id, text="v3")

    hist = history.load_history("memory", mem_id)
    assert len(hist) == 2
    # First history entry's `before` is v1.
    assert hist[0]["before"]["text"] == "v1"
    # Reverting the first one rewinds straight to v1 (clobbering v2 and v3).
    assert history.revert_edit(hist[0]) is True
    cur = [m for m in store.load_memories() if m["id"] == mem_id][0]
    assert cur["text"] == "v1"


def test_edit_journal_with_history(fresh_store):
    store, history = fresh_store
    j = store.add_journal("first cut", actor="agent-1", source="cli")
    jid = j["id"]

    assert history.edit_journal_with_history(jid, text="second cut") is True
    cur = [e for e in store.load_journal() if e["id"] == jid][0]
    assert cur["text"] == "second cut"

    hist = history.load_history("journal", jid)
    assert len(hist) == 1
    assert hist[0]["before"]["text"] == "first cut"


def test_remove_memory_with_history_then_restore(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("delete me cleanly", name="rm")
    mem_id = mem["id"]

    assert history.remove_memory_with_history(mem_id) is True
    assert all(m["id"] != mem_id for m in store.load_memories())

    deletes = history.load_recent_deletes()
    assert len(deletes) == 1
    assert deletes[0]["kind"] == "memory"
    restored = history.restore_deleted(deletes[0])
    assert restored is not None
    assert restored["text"] == "delete me cleanly"


def test_load_recent_deletes_newest_first(fresh_store):
    store, history = fresh_store
    a = store.save_memory("a")
    b = store.save_memory("b")
    history.remove_memory_with_history(a["id"])
    history.remove_memory_with_history(b["id"])

    deletes = history.load_recent_deletes(limit=10)
    assert len(deletes) == 2
    assert deletes[0]["before"]["text"] == "b"  # newest first
    assert deletes[1]["before"]["text"] == "a"


def test_truncation_after_5000_lines(fresh_store):
    store, history = fresh_store

    # Write 5005 fake history lines directly via record_edit. Bypass the
    # store-level wrappers since we don't care about the real records here.
    fake_before = {"id": 1, "text": "x", "name": "", "tags": [], "type": "feedback", "about": []}
    for i in range(5005):
        history.record_edit("memory", 1, fake_before, {"text": f"v{i}"}, actor="test")

    # File exists and is capped at MAX_HISTORY_LINES.
    with open(history.EDITS_FILE) as f:
        lines = f.readlines()
    assert len(lines) == history.MAX_HISTORY_LINES == 5000

    # The latest line must still be present (truncation keeps tail).
    last = json.loads(lines[-1])
    assert last["after"] == {"text": "v5004"}
    # And the earliest preserved line must be v5 (we wrote v0..v5004; truncation
    # keeps the last 5000 → starts at v5).
    first = json.loads(lines[0])
    assert first["after"] == {"text": "v5"}


def test_revert_edit_journal(fresh_store):
    store, history = fresh_store
    j = store.add_journal("orig", actor="agent-2", source="discord:general")
    jid = j["id"]

    history.edit_journal_with_history(jid, text="changed")
    hist = history.load_history("journal", jid)
    assert history.revert_edit(hist[0]) is True

    cur = [e for e in store.load_journal() if e["id"] == jid][0]
    assert cur["text"] == "orig"


def test_edit_with_no_valid_fields_returns_false(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("hello")
    # nonsense_field is not in the allow-list → no edit, no history.
    assert history.edit_memory_with_history(mem["id"], nonsense_field="x") is False
    assert history.load_history("memory", mem["id"]) == []


def test_edit_missing_record_returns_false(fresh_store):
    store, history = fresh_store
    assert history.edit_memory_with_history(99999, text="ghost") is False
    assert history.remove_memory_with_history(99999) is False
    # No history written for failed lookups.
    assert not os.path.exists(history.EDITS_FILE) or history.load_history("memory", 99999) == []


def test_load_history_filters_by_kind_and_id(fresh_store):
    store, history = fresh_store
    m1 = store.save_memory("m1")
    m2 = store.save_memory("m2")
    j1 = store.add_journal("j1", actor="x")

    history.edit_memory_with_history(m1["id"], text="m1.v2")
    history.edit_memory_with_history(m2["id"], text="m2.v2")
    history.edit_journal_with_history(j1["id"], text="j1.v2")

    h_m1 = history.load_history("memory", m1["id"])
    h_m2 = history.load_history("memory", m2["id"])
    h_j1 = history.load_history("journal", j1["id"])

    assert len(h_m1) == 1 and h_m1[0]["before"]["text"] == "m1"
    assert len(h_m2) == 1 and h_m2[0]["before"]["text"] == "m2"
    assert len(h_j1) == 1 and h_j1[0]["before"]["text"] == "j1"
