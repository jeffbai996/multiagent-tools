"""Pre-send tag handler: called by the Discord plugin before posting an
assistant message. Reads one JSON op from stdin, executes it against the
multiagent-tools, prints one JSON result to stdout.

Input shape:
  {"op": "memory_save",   "body": "...", "type": "feedback", "name": "",
                          "tags": [], "about": [], "bot": null}
  {"op": "memory_edit",   "id": 57, "body": "..."}
  {"op": "memory_delete", "id": 57}
  {"op": "journal_save",  "body": "...", "actor": "agent-1", "tags": []}
  {"op": "journal_delete","id": 12}

Output shape (all ops):
  {"ok": true, "id": 57, "type": "feedback", "summary": "first 240 chars..."}
  {"ok": false, "error": "..."}

Bot is whoever invoked the discord plugin — actor passed in from TS side.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store


def _summary(body: str, limit: int = 240) -> str:
    body = body.strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


def main() -> int:
    raw = sys.stdin.read()
    try:
        op_obj = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad input json: {e}"}))
        return 0

    op = op_obj.get("op", "")
    try:
        if op == "memory_save":
            entry = store.save_memory(
                op_obj.get("body", ""),
                type=op_obj.get("type", "feedback"),
                name=op_obj.get("name", ""),
                tags=op_obj.get("tags", []),
                about=op_obj.get("about", []),
                bot=op_obj.get("bot"),
            )
            print(json.dumps({
                "ok": True,
                "id": entry.get("id"),
                "type": entry.get("type", ""),
                "name": entry.get("name", ""),
                "summary": _summary(op_obj.get("body", "")),
            }))
        elif op == "memory_edit":
            ok = store.edit_memory(int(op_obj["id"]), op_obj.get("body", ""))
            print(json.dumps({
                "ok": bool(ok),
                "id": int(op_obj["id"]),
                "summary": _summary(op_obj.get("body", "")) if ok else "",
                "error": "" if ok else f"memory #{op_obj['id']} not found",
            }))
        elif op == "memory_delete":
            ok = store.remove_memory(int(op_obj["id"]))
            print(json.dumps({
                "ok": bool(ok),
                "id": int(op_obj["id"]),
                "error": "" if ok else f"memory #{op_obj['id']} not found",
            }))
        elif op == "journal_save":
            entry = store.add_journal(
                op_obj.get("body", ""),
                source=op_obj.get("source", "discord"),
                actor=op_obj.get("actor", "bot"),
                tags=op_obj.get("tags", []),
            )
            print(json.dumps({
                "ok": True,
                "id": entry.get("id"),
                "summary": _summary(op_obj.get("body", "")),
            }))
        elif op == "journal_delete":
            ok = store.remove_journal(int(op_obj["id"]))
            print(json.dumps({
                "ok": bool(ok),
                "id": int(op_obj["id"]),
                "error": "" if ok else f"journal #{op_obj['id']} not found",
            }))
        else:
            print(json.dumps({"ok": False, "error": f"unknown op: {op!r}"}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"crash: {type(e).__name__}: {e}"}))

    return 0


if __name__ == "__main__":
    sys.exit(main())
