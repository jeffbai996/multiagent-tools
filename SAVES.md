# Saving from a Discord turn

The canonical path for saving memories or journal entries during a Discord-originated bot turn is the `multiagent-tools` CLI with `--discord-chat-id` and `--discord-message-id` flags.

A `[MEMORY: ...]` / `[JOURNAL: ...]` tag protocol shipped earlier where bots emitted tags inline in their assistant text and a Stop hook scanned the transcript and wrote to the store. The tag protocol is no longer recommended — bots talking *about* the syntax triggered junk saves; tags inside fenced code blocks needed scrubbing; the tag-parsing layer was hard to make robust against agents that quote the syntax in prose.

The current flow is one explicit command, no transcript scanning, no scrubbing.

## How to save

```bash
multiagent-tools memory add \
  --type {user|feedback|project|reference} \
  --name "<short name>" \
  --tags "tag1,tag2" \
  --about "subject1,subject2" \
  --discord-chat-id "<chat_id from inbound channel tag>" \
  --discord-message-id "<message_id from inbound channel tag>" \
  "<body text>"
```

Same shape applies to `memory edit <id> "<new body>"`, `memory delete <id>`, `journal add "<text>"`, `journal delete <id>`.

The `--discord-chat-id` / `--discord-message-id` flags trigger the CLI to post a rendered confirmation card to the originating channel as a reply. Resolve both from the most recent inbound `<channel source="plugin:discord:discord" chat_id="X" message_id="Y" ...>` tag in the user message that triggered the save.

For terminal-only saves (no Discord context), omit the flags — the CLI's own `Saved #N` print to stdout is the confirmation.

## Card format

Every action posts a card with a bold prose header + a single fenced code block containing aligned meta key:value pairs and the body, separated by a horizontal rule:

```
💾 Memory #42 saved
```
type:  feedback
name:  Communication style
tags:  comm, voice
about: user
─────────────────────────
**Use direct, no glazing.**

Lead with insight, detail after.
```
```

Headers and emoji per action:
- 💾 Memory saved (`memory add`)
- ✏️ Memory edited (`memory edit`)
- 🗑️ Memory deleted (`memory delete`)
- 📓 Journal added (`journal add`)
- 🗑️ Journal deleted (`journal delete`)

The code-block surface is mobile-friendly and consistent across action types — markdown tables render unevenly on Discord mobile.

## Architecture

- **`store.py`** — JSON-backed memory + journal store
- **`cli.py`** — argparse CLI; `_post_card_if_discord` calls into `discord_card` after every successful mutating op when `--discord-chat-id` is set
- **`discord_card.py`** — shared module: `format_card(action) → str`, `post_action_card(action, chat_id, reply_to)`. Used by the CLI; the Stop hook also imports it as a fallback path
- **Token resolution** in `discord_card.read_bot_token()` chains through `$MULTIAGENT_DISCORD_TOKEN` → `$DISCORD_STATE_DIR/.env` → `$CLAUDE_PLUGIN_STATE_DIR/.env` → `$CLAUDE_CONFIG_DIR/channels/discord/.env` → `~/.claude/channels/discord/.env`. The `$DISCORD_STATE_DIR` priority covers multi-agent setups where each bot has its own state dir but shares `CLAUDE_CONFIG_DIR` — without it, cards post under the wrong agent identity

## Why this is the only path

Tag-based save protocols look elegant in design and rot fast in practice. The failure modes that recurred:

- **Tags in fenced code blocks** — bots demonstrating syntax in docs/examples accidentally triggered saves
- **Tags in inline-code spans** — same as above
- **Tags in plain prose** — discussing the protocol triggered saves
- **Bots drifting to bare CLI** — easier than literal tag emission for computed bodies, but skipped the whole confirmation pipeline
- **Tag scrubbing leaks** — `cc-scrub-tags` PreToolUse hook had edge cases where literal tag text leaked into Discord
- **Per-session hook attachment quirks** — Stop hooks occasionally weren't fired by Claude Code in the way the design assumed

Each fix tightened screws on a doomed design. The CLI-with-flags path is dumb on purpose: bots run a command, the command does exactly one thing, no implicit side-channels. When bots forget the flags the failure is loud (no card appears) instead of silent drift.

The `hooks/stop_hook.py` tag-parsing path is still present and will fire if you wire it into Claude Code's `Stop` hook, but the recommended setup is to leave it disconnected and use the CLI flow exclusively.
