"""HTTP-mode CLI client. Activated when MULTIAGENT_URL env var is set.

cli.py defers to this module instead of loading store.py directly. We mirror
the local CLI's argument shape and output format byte-for-byte so users
(humans and tools) can't tell which mode they're in.

Talks to the Flask server at MULTIAGENT_URL via /api/memory and /api/journal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


# ─────────────────────────── http helpers ───────────────────────────


def _get(base: str, path: str, params: dict | None = None) -> dict:
    qs = "?" + urllib.parse.urlencode(params, doseq=True) if params else ""
    return _request(base + path + qs, method="GET")


def _post(base: str, path: str, body: dict) -> dict:
    return _request(base + path, method="POST", body=body)


def _put(base: str, path: str, body: dict) -> dict:
    return _request(base + path, method="PUT", body=body)


def _delete(base: str, path: str) -> dict:
    return _request(base + path, method="DELETE")


def _request(url: str, *, method: str, body: dict | None = None) -> dict:
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_str = ""
        try:
            body_str = e.read().decode("utf-8")
            parsed = json.loads(body_str)
            return parsed if isinstance(parsed, dict) else {"ok": False, "error": body_str}
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}: {body_str or e.reason}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"connection failed: {e.reason}"}


# ─────────────────────────── display (mirror cli.py) ───────────────────────────


def _char_width(ch: str) -> int:
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _disp_width(text: str) -> int:
    return sum(_char_width(c) for c in text)


def _pad_disp(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    if _disp_width(text) <= width:
        return text + " " * (width - _disp_width(text))
    out = []
    used = 0
    for ch in text:
        cw = _char_width(ch)
        if used + cw > width - 3:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "..." + " " * (width - used - 3)


def _print_memory_list(entries: list[dict]) -> None:
    if not entries:
        print("(no memories)")
        return
    print(f"{_pad_disp('ID', 5)}{_pad_disp('TYPE', 11)}"
          f"{_pad_disp('NAME', 38)}{_pad_disp('ABOUT', 12)}"
          f"{_pad_disp('DATE', 12)}")
    print("-" * 78)
    for m in entries:
        ts = m.get("ts", "")[:10]
        about = ",".join(m.get("about", []) or [])
        bot = m.get("bot")
        name = m.get("name", "")
        if bot:
            name = f"{name} [bot:{','.join(bot)}]"
        print(f"{_pad_disp('#' + str(m['id']), 5)}"
              f"{_pad_disp(m.get('type', ''), 11)}"
              f"{_pad_disp(name, 38)}"
              f"{_pad_disp(about, 12)}"
              f"{_pad_disp(ts, 12)}")


def _print_journal_list(entries: list[dict]) -> None:
    if not entries:
        print("(no journal entries)")
        return
    print(f"{_pad_disp('ID', 5)}{_pad_disp('DATE', 12)}"
          f"{_pad_disp('ACTOR', 12)}{_pad_disp('TEXT', 50)}")
    print("-" * 79)
    for e in entries:
        ts = e.get("ts", "")[:10]
        actor = e.get("actor", "") or "-"
        print(f"{_pad_disp('#' + str(e['id']), 5)}"
              f"{_pad_disp(ts, 12)}"
              f"{_pad_disp(actor, 12)}"
              f"{_pad_disp(e.get('text', ''), 50)}")


def _print_memory_full(m: dict) -> None:
    print(f"=== Memory #{m['id']} ===")
    print(f"Type:  {m.get('type', '')}")
    print(f"Name:  {m.get('name', '')}")
    print(f"Tags:  {', '.join(m.get('tags', []))}")
    if m.get("about"):
        print(f"About: {', '.join(m.get('about', []))}")
    if m.get("bot"):
        print(f"Bot:   {', '.join(m.get('bot', []))}")
    print(f"Saved: {m.get('ts', '')}")
    print()
    print(m.get("text", ""))


def _print_journal_full(e: dict) -> None:
    print(f"=== Journal #{e['id']} ===")
    print(f"Source: {e.get('source', '')}")
    print(f"Actor:  {e.get('actor', '')}")
    print(f"Tags:   {', '.join(e.get('tags', []))}")
    print(f"Saved:  {e.get('ts', '')}")
    print()
    print(e.get("text", ""))


# ─────────────────────────── command handlers ───────────────────────────


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _detect_calling_bot() -> str | None:
    """Best-effort agent name detection. Override with MULTIAGENT_BOT in env."""
    explicit = os.environ.get("MULTIAGENT_BOT", "").strip()
    if explicit:
        return explicit
    # Fallback: derive from CLAUDE_CONFIG_DIR last path segment if set,
    # which is conventional for Claude Code agents living in alt config
    # directories like ~/.claude-foo. Works for any naming scheme.
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.basename(cfg.rstrip("/")) or None
    return None


def cmd_memory(args: argparse.Namespace, base: str) -> int:
    sub = args.sub
    if sub == "list":
        params: dict = {}
        if args.type:
            params["type"] = args.type
        if args.about:
            params["about"] = args.about
        bot = _detect_calling_bot()
        if bot:
            params["bot"] = bot
        if args.all:
            params["all"] = "1"
        resp = _get(base, "/api/memory", params)
        if not resp.get("ok"):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        _print_memory_list(resp.get("memories", []))
        return 0
    if sub == "show":
        resp = _get(base, f"/api/memory/{args.id}")
        if not resp.get("ok"):
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        _print_memory_full(resp.get("memory", {}))
        return 0
    if sub == "add":
        body: dict = {
            "text": args.text,
            "type": args.type or "feedback",
            "name": args.name or "",
            "tags": _parse_csv(args.tags),
            "about": _parse_csv(args.about),
        }
        bot_list = _parse_csv(args.bot) if args.bot else None
        if bot_list is not None:
            body["bot"] = bot_list
        resp = _post(base, "/api/memory", body)
        if not resp.get("ok"):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        m = resp.get("memory", {})
        print(f"Saved #{m.get('id')}: {m.get('name', '')}")
        return 0
    if sub == "edit":
        resp = _put(base, f"/api/memory/{args.id}", {"text": args.text})
        if not resp.get("ok"):
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Updated #{args.id}")
        return 0
    if sub == "delete":
        resp = _delete(base, f"/api/memory/{args.id}")
        if not resp.get("ok"):
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        return 0
    if sub == "search":
        params = {"q": args.term}
        if args.about:
            params["about"] = args.about
        bot = _detect_calling_bot()
        if bot:
            params["bot"] = bot
        if args.all:
            params["all"] = "1"
        resp = _get(base, "/api/memory", params)
        if not resp.get("ok"):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        _print_memory_list(resp.get("memories", []))
        return 0
    return 2


def cmd_journal(args: argparse.Namespace, base: str) -> int:
    sub = args.sub
    if sub == "list":
        params = {}
        if args.days:
            params["days"] = args.days
        resp = _get(base, "/api/journal", params)
        if not resp.get("ok"):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        _print_journal_list(resp.get("entries", []))
        return 0
    if sub == "show":
        resp = _get(base, f"/api/journal/{args.id}")
        if not resp.get("ok"):
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        _print_journal_full(resp.get("entry", {}))
        return 0
    if sub == "add":
        body = {
            "text": args.text,
            "source": args.source or "cli",
            "actor": args.actor or "",
            "tags": _parse_csv(args.tags),
        }
        resp = _post(base, "/api/journal", body)
        if not resp.get("ok"):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        e = resp.get("entry", {})
        print(f"Pinned #{e.get('id')}")
        return 0
    if sub == "delete":
        resp = _delete(base, f"/api/journal/{args.id}")
        if not resp.get("ok"):
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        return 0
    if sub == "search":
        resp = _get(base, "/api/journal", {"q": args.term})
        if not resp.get("ok"):
            print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
            return 1
        _print_journal_list(resp.get("entries", []))
        return 0
    return 2


def cmd_persona(args: argparse.Namespace, base: str) -> int:
    sub = args.sub
    if sub == "list":
        resp = _get(base, "/api/personas")
        for bot, slots in resp.items():
            print(bot)
            for s in slots:
                print(f"  {s['slot']:<12} [{s['mode']}]  {s['path']}")
        return 0
    if sub == "show":
        try:
            resp = _get(base, f"/api/personas/{args.bot}/{args.slot}")
        except urllib.error.HTTPError as e:
            print(f"unknown bot/slot: {args.bot}/{args.slot}", file=sys.stderr)
            return 1
        if not resp.get("ok", True):
            print(f"error: {resp.get('error')}", file=sys.stderr)
            return 1
        sys.stdout.write(resp.get("text", ""))
        return 0
    if sub == "write":
        resp = _put(base, f"/api/personas/{args.bot}/{args.slot}", {"text": args.text})
        if not resp.get("ok"):
            print(f"error: {resp.get('error')}", file=sys.stderr)
            return 1
        msg = f"wrote {resp['path']}"
        if resp["mode"] == "git":
            if resp.get("committed"):
                msg += f" (committed {resp.get('sha')})"
            elif resp.get("error"):
                msg += f" (commit failed: {resp['error']})"
        print(msg)
        return 0
    if sub == "edit":
        import subprocess, tempfile
        try:
            resp = _get(base, f"/api/personas/{args.bot}/{args.slot}")
        except urllib.error.HTTPError:
            print(f"unknown bot/slot: {args.bot}/{args.slot}", file=sys.stderr)
            return 1
        original = resp.get("text", "")
        editor = os.environ.get("EDITOR", "vim")
        suffix = "." + args.slot.rsplit(".", 1)[-1] if "." in args.slot else ".md"
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
            tmp.write(original)
            tmp_path = tmp.name
        try:
            subprocess.run([editor, tmp_path], check=True)
            with open(tmp_path, "r", encoding="utf-8") as f:
                new_text = f.read()
        finally:
            os.unlink(tmp_path)
        if new_text == original:
            print("no changes")
            return 0
        result = _put(base, f"/api/personas/{args.bot}/{args.slot}", {"text": new_text})
        if not result.get("ok"):
            print(f"error: {result.get('error')}", file=sys.stderr)
            return 1
        msg = f"wrote {result['path']}"
        if result["mode"] == "git":
            if result.get("committed"):
                msg += f" (committed {result.get('sha')})"
            elif result.get("error"):
                msg += f" (commit failed: {result['error']})"
        print(msg)
        return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="multiagent-tools",
                                description="Shared memory + journal (HTTP mode)")
    top = p.add_subparsers(dest="cmd", required=True)

    mem = top.add_parser("memory")
    msub = mem.add_subparsers(dest="sub", required=True)

    m_list = msub.add_parser("list")
    m_list.add_argument("--type", choices=["user", "feedback", "project", "reference"])
    m_list.add_argument("--about", action="append", default=[])
    m_list.add_argument("--all", action="store_true")

    m_show = msub.add_parser("show")
    m_show.add_argument("id", type=int)

    m_add = msub.add_parser("add")
    m_add.add_argument("text")
    m_add.add_argument("--type", choices=["user", "feedback", "project", "reference"])
    m_add.add_argument("--name", default="")
    m_add.add_argument("--tags", default="")
    m_add.add_argument("--about", default="")
    m_add.add_argument("--bot", default="")

    m_edit = msub.add_parser("edit")
    m_edit.add_argument("id", type=int)
    m_edit.add_argument("text")

    m_del = msub.add_parser("delete")
    m_del.add_argument("id", type=int)

    m_search = msub.add_parser("search")
    m_search.add_argument("term")
    m_search.add_argument("--about", action="append", default=[])
    m_search.add_argument("--all", action="store_true")

    jou = top.add_parser("journal")
    jsub = jou.add_subparsers(dest="sub", required=True)

    j_list = jsub.add_parser("list")
    j_list.add_argument("--days", type=int, default=0)

    j_show = jsub.add_parser("show")
    j_show.add_argument("id", type=int)

    j_add = jsub.add_parser("add")
    j_add.add_argument("text")
    j_add.add_argument("--source", default="cli")
    j_add.add_argument("--actor", default="")
    j_add.add_argument("--tags", default="")

    j_del = jsub.add_parser("delete")
    j_del.add_argument("id", type=int)

    j_search = jsub.add_parser("search")
    j_search.add_argument("term")

    per = top.add_parser("persona")
    psub = per.add_subparsers(dest="sub", required=True)
    psub.add_parser("list")
    p_show = psub.add_parser("show")
    p_show.add_argument("bot"); p_show.add_argument("slot")
    p_edit = psub.add_parser("edit")
    p_edit.add_argument("bot"); p_edit.add_argument("slot")
    p_write = psub.add_parser("write")
    p_write.add_argument("bot"); p_write.add_argument("slot"); p_write.add_argument("text")

    return p


def main(argv: list[str], *, base_url: str) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base = base_url.rstrip("/")
    if args.cmd == "memory":
        return cmd_memory(args, base)
    if args.cmd == "journal":
        return cmd_journal(args, base)
    if args.cmd == "persona":
        return cmd_persona(args, base)
    parser.print_help()
    return 2


if __name__ == "__main__":
    url = os.environ.get("MULTIAGENT_URL", "")
    if not url:
        print("error: MULTIAGENT_URL not set", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1:], base_url=url))
