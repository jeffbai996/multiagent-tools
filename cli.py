#!/usr/bin/env python3
"""multiagent-tools CLI. Manages memories.json + journal.json from the shell.

Usage:
  multiagent-tools memory list [--type TYPE] [--about LABEL]... [--bot NAME] [--all]
  multiagent-tools memory show <id>
  multiagent-tools memory add <text> [--type TYPE] [--name NAME] [--tags a,b,c]
                                [--about a,b] [--bot a,b]
  multiagent-tools memory edit <id> <text>
  multiagent-tools memory delete <id>
  multiagent-tools memory search <term> [--about LABEL]... [--bot NAME] [--all]

  multiagent-tools journal list [--days N]
  multiagent-tools journal show <id>
  multiagent-tools journal add <text> [--source SRC] [--actor A] [--tags a,b,c]
  multiagent-tools journal delete <id>
  multiagent-tools journal search <term>

  multiagent-tools persona list
  multiagent-tools persona show <bot> <slot>
  multiagent-tools persona edit <bot> <slot>           # opens $EDITOR
  multiagent-tools persona write <bot> <slot> <text>   # write directly

When MULTIAGENT_URL is set in env, this CLI shells out to client.py and
talks to the multiagent-tools HTTP server at that URL instead of touching local
files. Output format is identical either way.

Run with no args for help.
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata


def _maybe_proxy() -> None:
    """If MULTIAGENT_URL is set, hand off to client.py and exit."""
    url = os.environ.get("MULTIAGENT_URL", "").strip()
    if not url:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    try:
        import client  # type: ignore
    except ImportError:
        # client.py missing — fall through to local mode.
        return
    sys.exit(client.main(sys.argv[1:], base_url=url))


_maybe_proxy()

# Local mode: import store after the proxy check.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import personas  # noqa: E402


# ─────────────────────────── display helpers ───────────────────────────


def _char_width(ch: str) -> int:
    # East Asian Wide and Fullwidth chars render in 2 terminal cells.
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _disp_width(text: str) -> int:
    return sum(_char_width(c) for c in text)


def _pad_disp(text: str, width: int) -> str:
    """Truncate text to fit `width` display cells, then pad to exactly width."""
    text = text.replace("\n", " ").strip()
    if _disp_width(text) <= width:
        return text + " " * (width - _disp_width(text))
    # Truncate with ellipsis. Reserve 3 cells for "...".
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
    """Best-effort agent name. Override with MULTIAGENT_BOT in env.

    Fallback: derive from CLAUDE_CONFIG_DIR last path segment if set
    (e.g. ~/.claude-alt → "claude-alt"). Works for any naming scheme.
    """
    explicit = os.environ.get("MULTIAGENT_BOT", "").strip()
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.basename(cfg.rstrip("/")) or None
    return None


def cmd_memory(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "list":
        entries = store.filter_memories(
            type=args.type,
            about=args.about or None,
            bot=_detect_calling_bot(),
            show_all=bool(args.all),
        )
        _print_memory_list(entries)
        return 0
    if sub == "show":
        m = next((x for x in store.load_memories() if x["id"] == args.id), None)
        if not m:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        _print_memory_full(m)
        return 0
    if sub == "add":
        tags = _parse_csv(args.tags)
        about = _parse_csv(args.about)
        bot_list = _parse_csv(args.bot) if args.bot else None
        m = store.save_memory(args.text, type=args.type or "feedback",
                              name=args.name or "", tags=tags,
                              about=about, bot=bot_list)
        print(f"Saved #{m['id']}: {m.get('name', '')}")
        return 0
    if sub == "edit":
        ok = store.edit_memory(args.id, args.text)
        if not ok:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Updated #{args.id}")
        return 0
    if sub == "delete":
        ok = store.remove_memory(args.id)
        if not ok:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        return 0
    if sub == "search":
        results = store.search_memories(args.term)
        # Apply about/bot filters to search results too.
        results = store.filter_memories(
            results,
            about=args.about or None,
            bot=_detect_calling_bot(),
            show_all=bool(args.all),
        )
        _print_memory_list(results)
        return 0
    return 2


def cmd_journal(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "list":
        entries = (store.journal_recent(args.days)
                   if args.days else store.load_journal())
        _print_journal_list(entries)
        return 0
    if sub == "show":
        e = next((x for x in store.load_journal() if x["id"] == args.id), None)
        if not e:
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        _print_journal_full(e)
        return 0
    if sub == "add":
        tags = _parse_csv(args.tags)
        e = store.add_journal(args.text, source=args.source or "cli",
                              actor=args.actor or "", tags=tags)
        print(f"Pinned #{e['id']}")
        return 0
    if sub == "delete":
        ok = store.remove_journal(args.id)
        if not ok:
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        return 0
    if sub == "search":
        results = store.search_journal(args.term)
        _print_journal_list(results)
        return 0
    return 2


def cmd_persona(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "list":
        for bot in personas.list_bots():
            print(bot)
            for s in personas.get_files(bot):
                print(f"  {s['slot']:<12} [{s['mode']}]  {s['path']}")
        return 0
    if sub == "show":
        try:
            data = personas.read_slot(args.bot, args.slot)
        except KeyError:
            print(f"unknown bot/slot: {args.bot}/{args.slot}", file=sys.stderr)
            return 1
        sys.stdout.write(data["text"])
        return 0
    if sub == "write":
        try:
            result = personas.write_slot(args.bot, args.slot, args.text)
        except KeyError:
            print(f"unknown bot/slot: {args.bot}/{args.slot}", file=sys.stderr)
            return 1
        msg = f"wrote {result['path']}"
        if result["mode"] == "git":
            if result["committed"]:
                msg += f" (committed {result['sha']})"
            elif result["error"]:
                msg += f" (commit failed: {result['error']})"
        print(msg)
        return 0
    if sub == "edit":
        import subprocess, tempfile
        try:
            data = personas.read_slot(args.bot, args.slot)
        except KeyError:
            print(f"unknown bot/slot: {args.bot}/{args.slot}", file=sys.stderr)
            return 1
        editor = os.environ.get("EDITOR", "vim")
        suffix = "." + args.slot.rsplit(".", 1)[-1] if "." in args.slot else ".md"
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
            tmp.write(data["text"])
            tmp_path = tmp.name
        try:
            subprocess.run([editor, tmp_path], check=True)
            with open(tmp_path, "r", encoding="utf-8") as f:
                new_text = f.read()
        finally:
            os.unlink(tmp_path)
        if new_text == data["text"]:
            print("no changes")
            return 0
        result = personas.write_slot(args.bot, args.slot, new_text)
        msg = f"wrote {result['path']}"
        if result["mode"] == "git":
            if result["committed"]:
                msg += f" (committed {result['sha']})"
            elif result["error"]:
                msg += f" (commit failed: {result['error']})"
        print(msg)
        return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="multiagent-tools",
                                description="Shared memory + journal for multi-agent setups")
    top = p.add_subparsers(dest="cmd", required=True)

    # memory
    mem = top.add_parser("memory", help="manage durable memories")
    msub = mem.add_subparsers(dest="sub", required=True)

    m_list = msub.add_parser("list")
    m_list.add_argument("--type", choices=sorted(store.VALID_TYPES))
    m_list.add_argument("--about", action="append", default=[],
                        help="filter by subject label (repeatable, OR semantics)")
    m_list.add_argument("--all", action="store_true",
                        help="include bot-scoped entries from other bots")

    m_show = msub.add_parser("show")
    m_show.add_argument("id", type=int)

    m_add = msub.add_parser("add")
    m_add.add_argument("text")
    m_add.add_argument("--type", choices=sorted(store.VALID_TYPES))
    m_add.add_argument("--name", default="")
    m_add.add_argument("--tags", default="", help="comma-separated")
    m_add.add_argument("--about", default="", help="comma-separated subject labels")
    m_add.add_argument("--bot", default="",
                       help="comma-separated bot names; default unset = shared across all agents")

    m_edit = msub.add_parser("edit")
    m_edit.add_argument("id", type=int)
    m_edit.add_argument("text")

    m_del = msub.add_parser("delete")
    m_del.add_argument("id", type=int)

    m_search = msub.add_parser("search")
    m_search.add_argument("term")
    m_search.add_argument("--about", action="append", default=[])
    m_search.add_argument("--all", action="store_true")

    # journal
    jou = top.add_parser("journal", help="manage pinned moments")
    jsub = jou.add_subparsers(dest="sub", required=True)

    j_list = jsub.add_parser("list")
    j_list.add_argument("--days", type=int, default=0,
                        help="filter to last N days (0 = all)")

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

    # persona
    per = top.add_parser("persona", help="manage per-bot persona files")
    psub = per.add_subparsers(dest="sub", required=True)

    psub.add_parser("list")

    p_show = psub.add_parser("show")
    p_show.add_argument("bot")
    p_show.add_argument("slot")

    p_edit = psub.add_parser("edit")
    p_edit.add_argument("bot")
    p_edit.add_argument("slot")

    p_write = psub.add_parser("write")
    p_write.add_argument("bot")
    p_write.add_argument("slot")
    p_write.add_argument("text")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "memory":
        return cmd_memory(args)
    if args.cmd == "journal":
        return cmd_journal(args)
    if args.cmd == "persona":
        return cmd_persona(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
