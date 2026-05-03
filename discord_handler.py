"""Standalone Discord bot exposing multiagent-tools as slash commands.

Zero Claude tokens — this bot owns its own connection, calls store.py
directly, and replies in <500ms. Runs as a systemd user service.

Slash commands:
  /mem list  [type] [about] [bot] [all]
  /mem show  <id>
  /mem add   <text> [type] [name] [tags] [about] [bot]
  /mem search <term>
  /mem delete <id>
  /journal list [days]
  /journal show <id>
  /journal add  <text> [actor] [tags]
  /journal search <term>

Env vars (loaded from $HOME/.config/multiagent-tools/env):
  MULTIAGENT_DISCORD_TOKEN  — bot token
  MULTIAGENT_GUILD_IDS      — optional CSV of guild IDs for instant
                              per-server command sync. Without this, slash
                              commands sync globally (~1hr propagation).
  MULTIAGENT_DATA_DIR       — passed through to store.py (data files location)
"""

from __future__ import annotations

import logging
import os
import sys

import discord
from discord import app_commands

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("multiagent-discord")

TOKEN = os.environ.get("MULTIAGENT_DISCORD_TOKEN", "")
GUILD_IDS_RAW = os.environ.get("MULTIAGENT_GUILD_IDS", "")
GUILD_IDS = [int(g.strip()) for g in GUILD_IDS_RAW.split(",") if g.strip().isdigit()]


# ─────────────────────────── helpers ───────────────────────────


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _truncate(text: str, limit: int = 1900) -> str:
    """Discord messages cap at 2000. Code-block wrap costs 8 chars."""
    if len(text) <= limit:
        return text
    return text[: limit - 16] + "\n…(truncated)"


def _fmt_memory_list(entries: list[dict]) -> str:
    if not entries:
        return "(no memories)"
    lines = []
    for m in entries:
        ts = (m.get("ts") or "")[:10]
        about = ",".join(m.get("about") or [])
        bot = m.get("bot")
        tag_str = f"about:{about}" if about else ""
        if bot:
            tag_str = f"{tag_str} bot:{','.join(bot)}".strip()
        type_ = m.get("type", "")
        name = m.get("name") or m.get("text", "")[:60]
        if tag_str:
            lines.append(f"#{m['id']:>3} {type_:<9} {name}  ({tag_str})  {ts}")
        else:
            lines.append(f"#{m['id']:>3} {type_:<9} {name}  {ts}")
    return "\n".join(lines)


def _fmt_journal_list(entries: list[dict]) -> str:
    if not entries:
        return "(no journal entries)"
    lines = []
    for e in entries:
        ts = (e.get("ts") or "")[:10]
        actor = e.get("actor") or "-"
        text = (e.get("text") or "").replace("\n", " ")[:80]
        lines.append(f"#{e['id']:>3} {ts} {actor:<12} {text}")
    return "\n".join(lines)


def _fmt_memory_full(m: dict) -> str:
    parts = [
        f"=== Memory #{m['id']} ===",
        f"Type:  {m.get('type', '')}",
        f"Name:  {m.get('name', '')}",
        f"Tags:  {', '.join(m.get('tags', []))}",
    ]
    if m.get("about"):
        parts.append(f"About: {', '.join(m['about'])}")
    if m.get("bot"):
        parts.append(f"Bot:   {', '.join(m['bot'])}")
    parts.append(f"Saved: {m.get('ts', '')}")
    parts.append("")
    parts.append(m.get("text", ""))
    return "\n".join(parts)


def _fmt_journal_full(e: dict) -> str:
    parts = [
        f"=== Journal #{e['id']} ===",
        f"Source: {e.get('source', '')}",
        f"Actor:  {e.get('actor', '')}",
        f"Tags:   {', '.join(e.get('tags', []))}",
        f"Saved:  {e.get('ts', '')}",
        "",
        e.get("text", ""),
    ]
    return "\n".join(parts)


def _wrap(text: str) -> str:
    return f"```\n{_truncate(text)}\n```"


# ─────────────────────────── client + tree ───────────────────────────


class MultiagentClient(discord.Client):
    def __init__(self) -> None:
        # Slash commands don't require Message Content Intent — we only need
        # the default intents to receive interactions.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if GUILD_IDS:
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("synced commands to guild %s", gid)
        else:
            await self.tree.sync()
            log.info("synced commands globally (~1hr propagation)")


client = MultiagentClient()


# ─────────────────────────── /mem command group ───────────────────────────

mem_group = app_commands.Group(name="mem", description="multiagent-tools memories")


@mem_group.command(name="list", description="list memories with optional filters")
@app_commands.describe(
    type="filter by type",
    about="comma-separated subject labels (OR semantics)",
    bot="filter to entries scoped to a single bot",
    show_all="include bot-scoped entries from other bots",
)
@app_commands.choices(type=[
    app_commands.Choice(name="user", value="user"),
    app_commands.Choice(name="feedback", value="feedback"),
    app_commands.Choice(name="project", value="project"),
    app_commands.Choice(name="reference", value="reference"),
])
async def mem_list(
    interaction: discord.Interaction,
    type: app_commands.Choice[str] | None = None,
    about: str | None = None,
    bot: str | None = None,
    show_all: bool = False,
) -> None:
    entries = store.filter_memories(
        type=type.value if type else None,
        about=_parse_csv(about) or None,
        bot=bot,
        show_all=show_all,
    )
    await interaction.response.send_message(_wrap(_fmt_memory_list(entries)))


@mem_group.command(name="show", description="show a memory by id")
async def mem_show(interaction: discord.Interaction, id: int) -> None:
    m = next((x for x in store.load_memories() if x.get("id") == id), None)
    if not m:
        await interaction.response.send_message(f"Memory #{id} not found", ephemeral=True)
        return
    await interaction.response.send_message(_wrap(_fmt_memory_full(m)))


@mem_group.command(name="add", description="save a new memory")
@app_commands.describe(
    text="memory body",
    type="entry type (default feedback)",
    name="short title",
    tags="comma-separated tags",
    about="comma-separated subject labels (e.g. user,project)",
    bot="comma-separated bot scope (leave blank for shared)",
)
@app_commands.choices(type=[
    app_commands.Choice(name="user", value="user"),
    app_commands.Choice(name="feedback", value="feedback"),
    app_commands.Choice(name="project", value="project"),
    app_commands.Choice(name="reference", value="reference"),
])
async def mem_add(
    interaction: discord.Interaction,
    text: str,
    type: app_commands.Choice[str] | None = None,
    name: str | None = None,
    tags: str | None = None,
    about: str | None = None,
    bot: str | None = None,
) -> None:
    bot_list = _parse_csv(bot) if bot else None
    m = store.save_memory(
        text,
        type=type.value if type else "feedback",
        name=name or "",
        tags=_parse_csv(tags),
        about=_parse_csv(about),
        bot=bot_list,
    )
    await interaction.response.send_message(
        f"Saved #{m['id']} ({m.get('type')}): {m.get('name', '')}"
    )


@mem_group.command(name="search", description="search memories by term")
async def mem_search(interaction: discord.Interaction, term: str) -> None:
    results = store.search_memories(term)
    await interaction.response.send_message(_wrap(_fmt_memory_list(results)))


@mem_group.command(name="delete", description="delete a memory by id")
async def mem_delete(interaction: discord.Interaction, id: int) -> None:
    ok = store.remove_memory(id)
    if ok:
        await interaction.response.send_message(f"Deleted #{id}")
    else:
        await interaction.response.send_message(f"Memory #{id} not found", ephemeral=True)


# ─────────────────────────── /journal command group ───────────────────────────

jou_group = app_commands.Group(name="journal", description="multiagent-tools journal")


@jou_group.command(name="list", description="list journal entries")
@app_commands.describe(days="filter to last N days (0 = all)")
async def jou_list(interaction: discord.Interaction, days: int = 0) -> None:
    entries = store.journal_recent(days) if days else store.load_journal()
    await interaction.response.send_message(_wrap(_fmt_journal_list(entries)))


@jou_group.command(name="show", description="show a journal entry by id")
async def jou_show(interaction: discord.Interaction, id: int) -> None:
    e = next((x for x in store.load_journal() if x.get("id") == id), None)
    if not e:
        await interaction.response.send_message(f"Journal #{id} not found", ephemeral=True)
        return
    await interaction.response.send_message(_wrap(_fmt_journal_full(e)))


@jou_group.command(name="add", description="pin a journal entry")
@app_commands.describe(
    text="entry body",
    actor="who/what created it (default: discord-handler)",
    tags="comma-separated tags",
)
async def jou_add(
    interaction: discord.Interaction,
    text: str,
    actor: str | None = None,
    tags: str | None = None,
) -> None:
    e = store.add_journal(
        text,
        source="discord:slash",
        actor=actor or "discord-handler",
        tags=_parse_csv(tags),
    )
    await interaction.response.send_message(f"Pinned #{e['id']}")


@jou_group.command(name="search", description="search journal by term")
async def jou_search(interaction: discord.Interaction, term: str) -> None:
    results = store.search_journal(term)
    await interaction.response.send_message(_wrap(_fmt_journal_list(results)))


@jou_group.command(name="delete", description="delete a journal entry by id")
async def jou_delete(interaction: discord.Interaction, id: int) -> None:
    ok = store.remove_journal(id)
    if ok:
        await interaction.response.send_message(f"Deleted #{id}")
    else:
        await interaction.response.send_message(f"Journal #{id} not found", ephemeral=True)


client.tree.add_command(mem_group)
client.tree.add_command(jou_group)


@client.event
async def on_ready() -> None:
    log.info("logged in as %s (id %s)", client.user, client.user.id if client.user else "?")


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    if not TOKEN:
        log.error("MULTIAGENT_DISCORD_TOKEN not set in env")
        return 1
    client.run(TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
