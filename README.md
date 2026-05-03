# multiagent-tools

A self-hosted shared brain for multi-agent setups: durable memory, journal, persona files, channel digest, and live infrastructure inventory across N hosts. Backing store is plain JSON. UI is a Flask web app with a `⌘K` command palette. Designed to be poked at over the local network or a private tunnel (e.g. tailscale), never the public internet.

Originally built to coordinate several Claude Code agents talking through Discord; the architecture works for any setup where multiple agent processes (LLMs, scripts, humans-in-the-loop) need a shared notebook.

## What's in here

- `store.py` — JSON-backed memory + journal store (Python lib). Memories are durable facts (cap 200), journal entries are pinned moments (cap 1000). Atomic writes, last-writer-wins.
- `cli.py` — local CLI: `multiagent-tools memory list|show|add|edit|delete|search` and same for `journal`/`persona`.
- `client.py` — HTTP-mode CLI: same commands, but talks to the Flask server when `MULTIAGENT_URL` is set. Lets agents on remote hosts use the store transparently.
- `server.py` — Flask web UI + JSON API on `127.0.0.1:<port>`. ⌘K palette; per-page editors; markdown rendering; pinning, trash, edit history, merge.
- `personas.py` — registry of "where each agent keeps its persona files." Loaded from `~/.config/multiagent-tools/agents.yaml` (see `agents.example.yaml`). Files in a configured git repo auto-commit on save.
- `digest.py` — pulls recent Discord channel history for human review (no LLM, no cron). Optional `/digest/summarize` endpoint hits Gemini if `GEMINI_API_KEY` is set.
- `inventory.py` — live read of hooks (`settings.json`), crontab, systemd user units, launchd agents across each configured host. Cached 30s. Source of truth stays in canonical files; this module never writes.
- `discord_handler.py` — Discord slash-command bot exposing `/mem` and `/journal`. Optional.
- `hooks/` — Claude Code hooks (`stop_hook.py`, `precompact_hook.py`, etc.) that scan transcripts for `[MEMORY:]` and `[JOURNAL:]` tags and call the store.

## Install

```bash
git clone https://github.com/<you>/multiagent-tools.git
cd multiagent-tools
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp agents.example.yaml ~/.config/multiagent-tools/agents.yaml
# Edit agents.yaml to point at your real persona-file paths.
```

Then either run the server:

```bash
python3 server.py
# Open http://127.0.0.1:5005
```

…or the CLI:

```bash
./cli.py memory list
./cli.py memory add "Use direct, no glazing" --type=feedback --name="comm style"
```

## Configuration

All env vars optional unless noted.

| Env var | Read by | Purpose |
| --- | --- | --- |
| `MULTIAGENT_DATA_DIR` | `store.py` | dir holding `memories.json` + `journal.json`. Default `~/.local/share/multiagent-tools/`. |
| `MULTIAGENT_AGENTS_FILE` | `personas.py` | path to `agents.yaml`. Default `~/.config/multiagent-tools/agents.yaml`. |
| `MULTIAGENT_URL` | `client.py` | when set, CLI runs in HTTP mode against this base URL instead of touching JSON files locally. |
| `MULTIAGENT_HOST` / `MULTIAGENT_PORT` | `server.py` | Flask bind. Default `127.0.0.1:5005`. **Don't bind to 0.0.0.0** — this is a personal store, not a public service. |
| `MULTIAGENT_URL_PREFIX` | `server.py` | for hosting under a path (e.g. `/multiagent-tools` behind a reverse proxy). |
| `MULTIAGENT_BOT` | `cli.py`, hooks | explicit agent identity. Otherwise auto-detected from `CLAUDE_CONFIG_DIR` last segment, then hostname. |
| `MULTIAGENT_DISCORD_TOKEN` | `digest.py`, `discord_handler.py` | bot token for the discord side. |
| `MULTIAGENT_GUILD_IDS` | `discord_handler.py` | optional CSV of Discord guild IDs for instant per-server slash command sync. Without this, slash commands sync globally (~1hr propagation). |
| `MULTIAGENT_DIGEST_CHANNELS` | `digest.py` | comma-separated `name:id` pairs for digest pull. |
| `MULTIAGENT_SETTINGS_PATHS` | `inventory.py` | optional CSV of extra Claude Code `settings.json` paths to probe for hook chains. |
| `GEMINI_API_KEY` | `digest.py` | enables the optional auto-summarize button on the digest page. |

The env file at `~/.config/multiagent-tools/env` is checked as a fallback for any of the above. Shell-style:

```
MULTIAGENT_DISCORD_TOKEN=...
MULTIAGENT_DIGEST_CHANNELS=general:111111111111111111,help:222222222222222222
GEMINI_API_KEY=...
```

## CLI

```bash
multiagent-tools memory list                          # all entries
multiagent-tools memory list --about user             # filter by subject
multiagent-tools memory list --type feedback          # filter by type
multiagent-tools memory show 42
multiagent-tools memory add "..." --type project --name "X" --tags a,b --about user

multiagent-tools journal list
multiagent-tools journal show 17
multiagent-tools journal add "..." --actor agent-1 --tags a,b

multiagent-tools persona show agent-1 persona.md      # print file contents
multiagent-tools persona edit agent-1 persona.md      # opens $EDITOR; saves on exit
multiagent-tools persona write agent-1 persona.md "<text>"  # write directly
```

Set `MULTIAGENT_URL=https://your-host:8443/` to run the same commands against a remote server.

## Web UI

`python3 server.py` then open `http://127.0.0.1:5005`. Pages:

| Path | What |
| --- | --- |
| `/` | memories index — search, filter by type/about/bot, pin/trash |
| `/journal` | journal entries timeline |
| `/personas` | per-agent persona file editor |
| `/digest` | recent Discord channel review (if configured) |
| `/inventory` | live hooks/crons/services across configured hosts |
| `/trash` | soft-deleted records, restore-able |

`⌘K` (mac) / `ctrl+K` (everywhere else) opens the command palette. Filter type-ahead, ↑↓ to navigate, ↵ to fire, esc to close.

## Memory schema

```json
{
  "id": 42,
  "type": "feedback",
  "name": "concise replies",
  "text": "...",
  "tags": ["communication"],
  "about": ["user"],
  "bot": null,
  "ts": "2026-05-01T20:00:00Z"
}
```

- `type` — one of `user`, `feedback`, `project`, `reference`. Used for color coding + filter.
- `name` — short title.
- `text` — the body. Markdown rendered in the web UI.
- `tags` — free-form labels.
- `about` — subjects the entry concerns (e.g. `["user"]`, `["domain-x"]`). Filterable.
- `bot` — if set (e.g. `["agent-1"]`), only that agent includes the entry in default views; others must pass `--all` to see it. Default null = visible to all agents.

Journal entries are similar but simpler — `id, ts, source, actor, text, tags, pinned`.

## Hooks (Claude Code agents)

The `hooks/` dir has Stop, PreCompact, UserPromptSubmit, and SessionStart hooks that:

- scan assistant turns for `[MEMORY: ...]` and `[JOURNAL: ...]` tags and route them to `store.add_memory` / `store.add_journal`
- inject the most recent N memory entries into UserPromptSubmit context
- write a "what was the last conversation about" snapshot before context compaction

Drop them into your Claude Code `settings.json` `hooks` block to enable. Tag format examples:

```
[MEMORY: prefer integration tests over unit tests for this codebase]
[MEMORY type=project name="rebrand" tags=infra: kicked off the rename ...]
[JOURNAL: hit 1,000 commits today]
```

**Save-intent gating.** The Stop hook only honors these tags when the most
recent user message contains a save-intent verb (`remember`, `save this`,
`note that`, `pin this`, `journal this`, `add memory`, etc.). This prevents
meta-discussion of tag syntax from triggering real writes. To talk *about*
the syntax without firing it, use `[MEMORY-EXAMPLE: ...]` /
`[JOURNAL-EXAMPLE: ...]` — anything matching the `-EXAMPLE` variant is
stripped before scanning.

## Discord bot

`discord_handler.py` is an optional standalone bot exposing `/mem` and
`/journal` slash commands. To set up:

1. Create a Discord application + bot at <https://discord.com/developers/applications>.
2. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`. The bot only needs the **default** intents — no Message Content Intent required.
3. Invite the bot to your server with the generated URL.
4. Set `MULTIAGENT_DISCORD_TOKEN=<token>` in `~/.config/multiagent-tools/env`.
5. Optionally set `MULTIAGENT_GUILD_IDS=<csv of guild IDs>` for instant slash-command sync (otherwise it's ~1hr global propagation).
6. Run `python3 discord_handler.py` (or enable the systemd unit installed by `install.sh`).

## Tests

```bash
pip install pytest
pytest tests/
```

Tests are fully isolated from your real data dir (`MULTIAGENT_DATA_DIR` is
set to a `tmp_path` in `conftest.py`) and do not touch the network.

## Inventory probes

The `/inventory` page uses a transport abstraction to read hook chains, crontab, and service lists from each host. Out of the box:

- `LocalTransport` — runs commands directly on the same host as the server.
- Custom transports — drop a class with `run(cmd, timeout) → (rc, stdout, stderr)` into `inventory.py` to reach other hosts. Common patterns: SSH-with-restricted-`command=` wrapper, `kubectl exec`, `docker exec`.

Source of truth (`settings.json`, `crontab`, `systemd` units) stays in its canonical location. This module just reads.

## License

MIT.
