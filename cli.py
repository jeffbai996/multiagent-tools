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
  multiagent-tools journal edit <id> [<text>] [--actor A] [--source SRC] [--tags a,b,c]
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
import history  # noqa: E402
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
        if m.get("deleted"):
            name = f"[DEL] {name}"
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
        text = e.get("text", "")
        if e.get("deleted"):
            text = f"[DEL] {text}"
        print(f"{_pad_disp('#' + str(e['id']), 5)}"
              f"{_pad_disp(ts, 12)}"
              f"{_pad_disp(actor, 12)}"
              f"{_pad_disp(text, 50)}")


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


def _resolve_discord_origin_from_transcript() -> tuple[str, str] | None:
    """Auto-detect Discord chat/message IDs by reading the active Claude
    Code transcript.

    Used as a fallback when explicit --discord-chat-id / --discord-message-id
    flags weren't passed but we're running inside a Claude Code session that
    originated from a Discord-routed user turn.

    Looks at the most-recently-modified .jsonl under
    ~/.claude/projects/<cwd-encoded>/, tails it from the end, and pulls
    the latest <channel source="plugin:discord:discord" ...> tag. The
    "latest real user prompt" is the message the user actually meant when
    they asked the assistant to do something.

    Returns None outside a Claude Code session, when no transcript is
    found, or when the latest user content has no Discord channel tag.
    """
    if os.environ.get("CLAUDECODE") != "1":
        return None
    projects_root = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_root):
        return None
    candidates: list[tuple[str, float]] = []
    try:
        for proj in os.listdir(projects_root):
            sub = os.path.join(projects_root, proj)
            if not os.path.isdir(sub):
                continue
            for name in os.listdir(sub):
                if name.endswith(".jsonl"):
                    p = os.path.join(sub, name)
                    try:
                        candidates.append((p, os.path.getmtime(p)))
                    except OSError:
                        continue
    except OSError:
        return None
    if not candidates:
        return None
    # Don't trust transcripts that haven't been touched in a long while —
    # otherwise a stale transcript from a closed session could resurface
    # an old Discord chat_id and post the card to the wrong channel.
    import time as _time
    transcript, mtime = max(candidates, key=lambda x: x[1])
    if _time.time() - mtime > 600:  # 10min staleness window
        return None

    import json
    import re
    tag_re = re.compile(
        r'<channel\s+source=["\'](?:plugin:discord:discord|discord)["\']'
        r'[^>]*?chat_id=["\']([^"\']+)["\']'
        r'[^>]*?message_id=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    try:
        with open(transcript, "r") as f:
            lines = f.readlines()
    except OSError:
        return None

    # Walk backward to find the latest *real* user prompt (skipping
    # tool_result entries — type:user but tool output, not user words).
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts: list[str] = []
            is_real_prompt = False
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    continue
                if c.get("type") == "text" and isinstance(c.get("text"), str):
                    text_parts.append(c["text"])
                    is_real_prompt = True
            if not is_real_prompt:
                continue
            text = "\n".join(text_parts)
        else:
            continue
        if not text:
            continue
        matches = list(tag_re.finditer(text))
        if matches:
            m = matches[-1]
            return m.group(1), m.group(2)
        # Latest real prompt had no Discord tag — terminal-turn signal.
        # Don't walk back into older Discord history.
        return None
    return None


def _post_card_if_discord(action: dict, args: argparse.Namespace) -> None:
    """Post a confirmation card to Discord if Discord context is available.

    Resolves the target channel in priority order:
      1. Explicit --discord-chat-id / --discord-message-id flags
      2. (auto) Latest <channel> tag in the active Claude Code transcript
         when running inside a Claude Code session (CLAUDECODE=1)
      3. (fallback) Calling agent's `discord_home_channel` from agents.yaml
         when CLAUDECODE=1 and no transcript tag matched — covers self-
         initiated mutations that aren't tied to a specific Discord turn

    With no Discord context resolvable, this is a no-op — the CLI's own
    `Saved #N` print is the terminal-only confirmation. The CLAUDECODE
    gate is what keeps human terminal use silent.
    """
    chat_id = getattr(args, "discord_chat_id", None) or ""
    msg_id = getattr(args, "discord_message_id", None) or None

    if not chat_id:
        auto = _resolve_discord_origin_from_transcript()
        if auto is not None:
            chat_id, msg_id = auto
        elif os.environ.get("CLAUDECODE") == "1":
            # Agent invoking the CLI but the current turn isn't Discord-
            # routed (self-initiated cleanup, scheduled task, etc). Post
            # to that agent's home channel if agents.yaml declares one.
            bot = _detect_calling_bot()
            meta = personas.get_agent_meta(bot)
            chat_id = str(meta.get("discord_home_channel") or "")
            msg_id = None

    if not chat_id:
        return

    try:
        import discord_card
        ok, err = discord_card.post_action_card(
            action, chat_id, reply_to=msg_id,
            user_agent="multiagent-cli (1.0)",
        )
        if not ok and err:
            print(f"[card post failed] {err}", file=sys.stderr)
    except Exception as e:
        print(f"[card post crashed] {type(e).__name__}: {e}", file=sys.stderr)


def _find_memory(mid: int) -> dict | None:
    return next((x for x in store.load_memories() if x.get("id") == mid), None)


def _find_journal(jid: int) -> dict | None:
    return next((x for x in store.load_journal() if x.get("id") == jid), None)


def cmd_memory(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "list":
        base = store.load_memories_raw() if getattr(args, "include_deleted", False) \
            else store.load_memories()
        entries = store.filter_memories(
            entries=base,
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
        if getattr(args, "body_only", False):
            sys.stdout.write(m.get("text", ""))
            if not m.get("text", "").endswith("\n"):
                sys.stdout.write("\n")
        else:
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
        _post_card_if_discord({"kind": "memory_saved", "entry": m}, args)
        return 0
    if sub == "edit":
        before = _find_memory(args.id)
        ok = store.edit_memory(args.id, args.text)
        if not ok:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Updated #{args.id}")
        _post_card_if_discord(
            {"kind": "memory_edited", "id": args.id,
             "before": before, "after": _find_memory(args.id)},
            args,
        )
        return 0
    if sub == "delete":
        before = _find_memory(args.id)
        ok = history.remove_memory_with_history(args.id, actor="cli")
        if not ok:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        _post_card_if_discord({"kind": "memory_deleted", "before": before}, args)
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
        if getattr(args, "include_deleted", False):
            entries = store.load_journal_raw()
            if args.days:
                # Manual day-window filter on raw set; journal_recent() already filters.
                from datetime import datetime, timedelta, timezone
                cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
                entries = [
                    e for e in entries
                    if datetime.fromisoformat(e.get("ts", "1970-01-01T00:00:00+00:00")) >= cutoff
                ]
        else:
            entries = (store.journal_recent(args.days)
                       if args.days else store.load_journal())
        _print_journal_list(entries)
        return 0
    if sub == "show":
        e = next((x for x in store.load_journal() if x["id"] == args.id), None)
        if not e:
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        if getattr(args, "body_only", False):
            sys.stdout.write(e.get("text", ""))
            if not e.get("text", "").endswith("\n"):
                sys.stdout.write("\n")
        else:
            _print_journal_full(e)
        return 0
    if sub == "add":
        tags = _parse_csv(args.tags)
        e = store.add_journal(args.text, source=args.source or "cli",
                              actor=args.actor or "", tags=tags)
        print(f"Pinned #{e['id']}")
        _post_card_if_discord({"kind": "journal_added", "entry": e}, args)
        return 0
    if sub == "edit":
        before = _find_journal(args.id)
        if not before:
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        kwargs = {}
        if args.text is not None:
            kwargs["text"] = args.text
        if args.actor is not None:
            kwargs["actor"] = args.actor
        if args.source is not None:
            kwargs["source"] = args.source
        if args.tags is not None:
            kwargs["tags"] = _parse_csv(args.tags)
        if not kwargs:
            print("nothing to edit (pass text or --actor/--source/--tags)",
                  file=sys.stderr)
            return 2
        ok = store.edit_journal(args.id, **kwargs)
        if not ok:
            print(f"Journal #{args.id} edit failed", file=sys.stderr)
            return 1
        after = _find_journal(args.id)
        print(f"Edited #{args.id}")
        _post_card_if_discord(
            {"kind": "journal_edited", "id": args.id,
             "before": before, "after": after},
            args,
        )
        return 0
    if sub == "delete":
        before = _find_journal(args.id)
        ok = history.remove_journal_with_history(args.id, actor="cli")
        if not ok:
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        _post_card_if_discord({"kind": "journal_deleted", "before": before}, args)
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


def _add_discord_flags(parser: argparse.ArgumentParser) -> None:
    """Add --discord-chat-id / --discord-message-id to a subcommand parser.

    When --discord-chat-id is set, after a successful op the CLI posts a
    rendered card to that channel (replying to --discord-message-id when
    provided). Without these flags the CLI is silent on Discord and just
    prints `Saved #N` to stdout — backwards-compatible with terminal use.

    Bots passing these from a Discord-originated request resolve them from
    the inbound `<channel>` tag's `chat_id` and `message_id` attributes.
    """
    parser.add_argument("--discord-chat-id", default="",
                        help="post a confirmation card to this Discord channel after the op")
    parser.add_argument("--discord-message-id", default="",
                        help="reply-to message ID for the card (requires --discord-chat-id)")


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
    m_list.add_argument("--include-deleted", action="store_true",
                        help="include tombstoned entries (debugging)")

    m_show = msub.add_parser("show")
    m_show.add_argument("id", type=int)
    m_show.add_argument("--body-only", action="store_true",
                        help="print only the body text, no rendered metadata")

    m_add = msub.add_parser("add")
    m_add.add_argument("text")
    m_add.add_argument("--type", choices=sorted(store.VALID_TYPES))
    m_add.add_argument("--name", default="")
    m_add.add_argument("--tags", default="", help="comma-separated")
    m_add.add_argument("--about", default="", help="comma-separated subject labels")
    m_add.add_argument("--bot", default="",
                       help="comma-separated bot names; default unset = shared across all agents")
    _add_discord_flags(m_add)

    m_edit = msub.add_parser("edit")
    m_edit.add_argument("id", type=int)
    m_edit.add_argument("text")
    _add_discord_flags(m_edit)

    m_del = msub.add_parser("delete")
    m_del.add_argument("id", type=int)
    _add_discord_flags(m_del)

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
    j_list.add_argument("--include-deleted", action="store_true",
                        help="include tombstoned entries (debugging)")

    j_show = jsub.add_parser("show")
    j_show.add_argument("id", type=int)
    j_show.add_argument("--body-only", action="store_true",
                        help="print only the body text, no rendered metadata")

    j_add = jsub.add_parser("add")
    j_add.add_argument("text")
    j_add.add_argument("--source", default="cli")
    j_add.add_argument("--actor", default="")
    j_add.add_argument("--tags", default="")
    _add_discord_flags(j_add)

    j_edit = jsub.add_parser("edit")
    j_edit.add_argument("id", type=int)
    j_edit.add_argument("text", nargs="?", default=None,
                        help="new body text (omit to keep current text)")
    j_edit.add_argument("--actor", default=None,
                        help="overwrite the entry's actor field")
    j_edit.add_argument("--source", default=None,
                        help="overwrite the entry's source field")
    j_edit.add_argument("--tags", default=None,
                        help="comma-separated tags (overwrites)")
    _add_discord_flags(j_edit)

    j_del = jsub.add_parser("delete")
    j_del.add_argument("id", type=int)
    _add_discord_flags(j_del)

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
