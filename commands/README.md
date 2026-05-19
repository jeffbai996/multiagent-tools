# multiagent-tools slash commands

Files in this directory are dispatchable as Discord `/slash` commands via the
discord-passthrough hook (`hooks/discord_passthrough.py`).

## How it works

- Filename minus extension = command name. `git-pull.sh` → `/git-pull`
- `.sh` files run via `bash <script> <args...>`
- `.py` files run via `python3 <script> <args...>`
- Args after the command name are tokenized with `shlex.split` and passed as argv
- Working directory is `$HOME`
- Default timeout 30s; override per-call with `/cmd t=120 ...`

## Adding a command

```bash
cat > commands/uptime.sh <<'EOF'
#!/usr/bin/env bash
# /uptime — system uptime
uptime
EOF
chmod +x commands/uptime.sh
```

Next message from Discord: `/uptime` runs that on the host and replies inline.

## Built-ins (not files — hardcoded in the hook)

- `/help`, `/commands` — list registered commands
- `/log [N]` — tail the passthrough.log
- `/status` — uptime + disk + load

## Conventions

- Names: lowercase, kebab-case (`[a-z0-9][a-z0-9_-]*`)
- Keep output under ~1900 chars when possible; larger goes as a .txt attachment
- Exit nonzero on failure — the wrapper shows `[exit N]` to Discord
- Stdout + stderr both reach Discord; merge order may not be perfect

## Unmatched `/cmd`

If you type `/something` and no file/builtin exists, the hook does NOT intercept
— the message falls through to Claude normally. This preserves native Claude
Code slash commands (`/loop`, `/schedule`, `/compact`, etc.).

## Where this directory lives

By default the hook looks for commands at `<repo-root>/commands/`. Override
with the `MAT_COMMANDS_DIR` environment variable if you keep your scripts
elsewhere.
