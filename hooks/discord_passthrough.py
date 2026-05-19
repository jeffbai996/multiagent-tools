#!/usr/bin/env python3
"""UserPromptSubmit hook: Discord shell + slash pass-through.

When a Discord-origin message from Jeff starts with `!` or `/<known-cmd>`,
intercept it: execute on this host and reply directly to Discord, then BLOCK
the prompt so Claude never sees it (no token spend, no model turn).

Two prefix modes:

  !ls ~/repos              -> raw shell, run `ls ~/repos` via bash -lc
  !t=120 long-cmd          -> shell with 120s timeout (default 30s)
  /help                    -> built-in: list registered commands
  /log [N]                 -> built-in: tail passthrough.log
  /status                  -> built-in: uptime + disk + load
  /<name> arg1 arg2        -> file-registry: runs ~/repos/cc-context/commands/<name>.{sh,py}

Unmatched `/cmd` falls through to Claude (so Claude Code's native /loop,
/schedule, /compact etc. still work normally).

Trigger:
  - Inbound message has a <channel source="plugin:discord:discord" ...> tag
  - The tag's user_id matches CC_OWNER_DISCORD_USER_ID (env)
  - Body (after the tag) starts with `!` OR `/<known-cmd>`

Safety:
  - Sender gate: only Jeff's user_id (CLAUDE.md "approved" trust model)
  - Denylist on raw shell (`!`): rm -rf /, fork bomb, shutdown, mkfs, etc.
  - Slash commands skip the denylist (the script files themselves are the gate
    — you can't run anything that isn't already in the registry)
  - Timeout: hard wall, default 30s, max 600s

Output:
  - Wrapped in a code block in the Discord reply
  - Truncated to ~1900 chars inline; longer goes as a .txt attachment
  - Exit code shown only when nonzero
  - Stdout and stderr are merged

Logs: ~/.local/state/multiagent-tools/passthrough.log (override with MAT_PASSTHROUGH_LOG)
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shlex
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Owner's Discord user_id — the only sender allowed to run pass-through
# commands on this host. Resolved per-invocation (not at import time) so a
# running bot picks up changes without restart.
#
# Resolution order:
#   1. MAT_OWNER_DISCORD_USER_ID env var (preferred — works everywhere)
#   2. ~/.config/multiagent-tools/owner_id file (single line, just the user_id)
#
# Fails closed when neither is set — any other behavior would let arbitrary
# Discord users run shell on the host.
OWNER_ID_FILE = Path(
    os.environ.get("MAT_OWNER_ID_FILE")
    or Path.home() / ".config" / "multiagent-tools" / "owner_id"
)


def get_owner_id() -> str:
    """Return the configured owner Discord user_id, or empty string if unset."""
    env = os.environ.get("MAT_OWNER_DISCORD_USER_ID", "").strip()
    if env:
        return env
    try:
        if OWNER_ID_FILE.is_file():
            return OWNER_ID_FILE.read_text().strip().splitlines()[0].strip()
    except OSError:
        pass
    return ""

DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 600

INLINE_LIMIT = 1900  # Discord hard cap is 2000; reserve for code fence + cmd echo
ATTACHMENT_LIMIT = 8 * 1024 * 1024  # Discord default upload cap is 8MB for non-Nitro

# Log location — override with MAT_PASSTHROUGH_LOG env var.
LOG_PATH = Path(
    os.environ.get("MAT_PASSTHROUGH_LOG")
    or Path.home() / ".local" / "state" / "multiagent-tools" / "passthrough.log"
)

# Directory scanned for user-defined slash commands. Filename (minus .sh/.py)
# becomes the command name. `.sh` runs under `bash -lc`, `.py` under `python3`.
# Override with MAT_COMMANDS_DIR env var (defaults to ./commands next to repo root).
COMMANDS_DIR = Path(
    os.environ.get("MAT_COMMANDS_DIR")
    or Path(__file__).resolve().parent.parent / "commands"
)

# Persisted cwd across !cmd invocations. Each bash subprocess is short-lived,
# so we stash the post-command pwd here and read it back on the next call.
# Reset with `!cd` (no args) or `!cd ~`.
CWD_STATE_FILE = Path(
    os.environ.get("MAT_CWD_STATE_FILE")
    or Path.home() / ".cache" / "multiagent-tools" / "passthrough_cwd"
)


def _read_persisted_cwd() -> str:
    """Return the persisted cwd, or $HOME if missing/invalid."""
    home = str(Path.home())
    try:
        if CWD_STATE_FILE.is_file():
            cwd = CWD_STATE_FILE.read_text().strip().splitlines()[0].strip()
            if cwd and Path(cwd).is_dir():
                return cwd
    except OSError:
        pass
    return home


def _write_persisted_cwd(cwd: str) -> None:
    try:
        CWD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CWD_STATE_FILE.write_text(cwd + "\n")
    except OSError:
        pass

# Same tag pattern the other hooks use, but capture user_id too.
CHANNEL_TAG_RE = re.compile(
    r'<channel\s+source="(?:plugin:discord:discord|discord)"\s+'
    r'chat_id="(\d+)"\s+message_id="(\d+)"'
    r'(?:\s+user="[^"]*")?'
    r'\s+user_id="(\d+)"',
    re.IGNORECASE,
)

# Denylist: regex patterns that block execution outright. Match against the
# raw command string. These are the obvious foot-guns Jeff explicitly asked
# to gate; not an exhaustive sandbox.
DENYLIST = [
    # rm -rf targeting / (root), $HOME, ~, * — but NOT /subdir/...
    # The target must be the WHOLE word, so we require end-of-string or whitespace
    # immediately after the dangerous target.
    (r"\brm\s+(-[a-zA-Z]*[rRfF][a-zA-Z]*\s+)+(/(\s|$)|\$HOME(\s|$)|~(\s|$)|\*(\s|$))",
     "rm -rf on / or HOME root"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bshutdown\b", "shutdown"),
    (r"\breboot\b", "reboot"),
    (r"\bhalt\b", "halt"),
    (r"\bmkfs(\.\w+)?\b", "mkfs"),
    (r"\bdd\s+.*\bof=/dev/", "dd to /dev/"),
    (r"\bsudo\b", "sudo (use a non-elevated alternative)"),
    (r"\bgit\s+push\s+(-f|--force|--force-with-lease)\s+.*\b(main|master)\b", "force push to main/master"),
    (r">\s*/dev/sd[a-z]", "redirect to raw block device"),
]


def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except OSError:
        pass


def _detect_state_dir() -> str:
    explicit = os.environ.get("DISCORD_STATE_DIR", "")
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.join(cfg, "channels", "discord")
    return os.path.expanduser("~/.claude/channels/discord")


def _read_token(state_dir: str) -> str | None:
    env_path = os.path.join(state_dir, ".env")
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _strip_channel_tag(text: str) -> str:
    """Remove the <channel ...> opening AND any </channel> closing tag.

    Some Discord plugin versions emit a closing tag; without stripping it,
    the trailing `</channel>` gets passed to bash and triggers a syntax error.
    """
    out = re.sub(
        r'<channel\s+source="(?:plugin:discord:discord|discord)"[^>]*>\s*',
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    out = re.sub(r'\s*</channel>\s*$', "", out, flags=re.IGNORECASE)
    return out.strip()


def parse_passthrough(prompt: str) -> dict | None:
    """Detect a pass-through prompt. Returns a dict describing the dispatch, or None.

    Return shape:
      {
        "chat_id": str, "msg_id": str, "timeout_s": int,
        "mode": "bash" | "slash",
        # bash:
        "cmd": str,
        # slash:
        "name": str, "args": str,
      }
    """
    m = CHANNEL_TAG_RE.search(prompt)
    if not m:
        return None
    chat_id, msg_id, user_id = m.group(1), m.group(2), m.group(3)
    owner_id = get_owner_id()
    if not owner_id:
        # Fail closed — neither env var nor owner_id file is set.
        log("owner_id not configured (env MAT_OWNER_DISCORD_USER_ID or ~/.config/multiagent-tools/owner_id) — refusing")
        return None
    if user_id != owner_id:
        return None

    body = _strip_channel_tag(prompt).strip()
    if not body:
        return None

    timeout = DEFAULT_TIMEOUT_S

    # --- bang/bash mode ---
    if body.startswith("!"):
        body = body.lstrip("!").lstrip()
        if not body:
            return None

        # Optional t=N prefix for timeout override (legacy syntax `!t=N cmd`).
        tmatch = re.match(r"^t=(\d+)\s+(.+)$", body, re.DOTALL)
        if tmatch:
            try:
                t = int(tmatch.group(1))
                if 1 <= t <= MAX_TIMEOUT_S:
                    timeout = t
                    body = tmatch.group(2).strip()
            except ValueError:
                pass

        if not body:
            return None
        return {
            "chat_id": chat_id, "msg_id": msg_id, "timeout_s": timeout,
            "mode": "bash", "cmd": body,
        }

    # --- slash mode ---
    if body.startswith("/"):
        # Split into first-token + rest. Names are case-insensitive, kebab-case.
        rest = body[1:].lstrip()
        if not rest:
            return None
        parts = rest.split(None, 1)
        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Optional t=N flag at start of args (e.g. /sync t=120 origin main)
        tmatch = re.match(r"^t=(\d+)\s+(.+)$", args, re.DOTALL)
        if tmatch:
            try:
                t = int(tmatch.group(1))
                if 1 <= t <= MAX_TIMEOUT_S:
                    timeout = t
                    args = tmatch.group(2).strip()
            except ValueError:
                pass
        elif args:
            args = args.strip()

        # Only intercept if it's actually a registered command — otherwise let
        # Claude handle native slash commands (/loop, /schedule, /compact, etc).
        if not is_registered_slash(name):
            return None

        return {
            "chat_id": chat_id, "msg_id": msg_id, "timeout_s": timeout,
            "mode": "slash", "name": name, "args": args,
        }

    return None


# ---------------------------- slash dispatch ---------------------------------


def is_registered_slash(name: str) -> bool:
    """Is `name` a built-in handler or a file in COMMANDS_DIR?"""
    if name in BUILTIN_SLASH:
        return True
    return _resolve_slash_file(name) is not None


def _resolve_slash_file(name: str) -> Path | None:
    """Find ~/repos/cc-context/commands/<name>.{sh,py}. Returns Path or None."""
    if not COMMANDS_DIR.is_dir():
        return None
    # Guard against path traversal — name must be a bare identifier.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        return None
    for ext in (".sh", ".py"):
        p = COMMANDS_DIR / f"{name}{ext}"
        if p.is_file():
            return p
    return None


def run_slash(name: str, args: str, timeout_s: int) -> tuple[str, int, bool]:
    """Dispatch a /slash command. Returns (output, exit_code, timed_out)."""
    handler = BUILTIN_SLASH.get(name)
    if handler is not None:
        try:
            out, code = handler(args)
            return out, code, False
        except Exception as exc:
            return f"<handler error: {exc}>", 1, False

    script = _resolve_slash_file(name)
    if script is None:
        # Shouldn't happen — parse_passthrough already gated on is_registered_slash.
        return f"<no such command: {name}>", 127, False

    if script.suffix == ".py":
        cmd_argv = [sys.executable, str(script)]
    else:
        cmd_argv = ["bash", str(script)]

    if args:
        try:
            cmd_argv.extend(shlex.split(args))
        except ValueError as exc:
            return f"<arg parse error: {exc}>", 1, False

    try:
        proc = subprocess.run(
            cmd_argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(Path.home()),
        )
        out = proc.stdout
        if proc.stderr:
            if out and not out.endswith("\n"):
                out += "\n"
            out += proc.stderr
        return out, proc.returncode, False
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        if e.stderr:
            s = e.stderr if isinstance(e.stderr, str) else e.stderr.decode("utf-8", "replace")
            if partial and not partial.endswith("\n"):
                partial += "\n"
            partial += s
        return partial, 124, True
    except Exception as exc:
        return f"<exec error: {exc}>", 1, False


# ---- built-in slash handlers (no file required) ----------------------------


def _slash_help(_args: str) -> tuple[str, int]:
    lines = ["built-in slash commands:"]
    for n in sorted(BUILTIN_SLASH):
        lines.append(f"  /{n}")
    if COMMANDS_DIR.is_dir():
        file_cmds = sorted(
            p.stem for p in COMMANDS_DIR.iterdir()
            if p.is_file() and p.suffix in (".sh", ".py")
        )
        if file_cmds:
            lines.append("")
            lines.append("file-registry commands:")
            for n in file_cmds:
                lines.append(f"  /{n}")
    lines.append("")
    lines.append("raw shell: !<cmd>   timeout override: !t=N <cmd> or /name t=N <args>")
    return "\n".join(lines), 0


def _slash_log(args: str) -> tuple[str, int]:
    n = 20
    if args.strip():
        try:
            n = max(1, min(200, int(args.strip().split()[0])))
        except ValueError:
            pass
    if not LOG_PATH.exists():
        return "(no log file yet)", 0
    try:
        with open(LOG_PATH) as f:
            lines = f.readlines()
        tail = "".join(lines[-n:])
        return tail or "(empty)", 0
    except OSError as e:
        return f"<read error: {e}>", 1


def _slash_status(_args: str) -> tuple[str, int]:
    try:
        proc = subprocess.run(
            ["bash", "-lc", "echo HOST: $(hostname); echo; echo UPTIME:; uptime; echo; echo DISK:; df -h ~ | tail -1; echo; echo LOAD: $(cat /proc/loadavg 2>/dev/null || echo n/a)"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.stdout + (proc.stderr if proc.returncode else ""), proc.returncode
    except Exception as exc:
        return f"<status error: {exc}>", 1


BUILTIN_SLASH: dict[str, "callable"] = {
    "help": _slash_help,
    "commands": _slash_help,
    "log": _slash_log,
    "status": _slash_status,
}


def check_denylist(cmd: str) -> str | None:
    """Returns the denylist label if cmd matches a forbidden pattern."""
    for pattern, label in DENYLIST:
        if re.search(pattern, cmd):
            return label
    return None


def run_command(cmd: str, timeout_s: int) -> tuple[str, int, bool]:
    """Run cmd via `bash -lc` in the persisted cwd. Updates persisted cwd
    from the post-command pwd (so `cd foo` carries to the next invocation).

    Returns (output, exit_code, timed_out).
    """
    start_cwd = _read_persisted_cwd()
    home = str(Path.home())
    sentinel = "__MAT_PASSTHROUGH_CWD__"
    wrapped = f"{cmd}\n_mat_rc=$?\nprintf '\\n%s%s\\n' '{sentinel}' \"$(pwd)\"\nexit $_mat_rc"

    try:
        proc = subprocess.run(
            ["bash", "-lc", wrapped],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=start_cwd,
        )
        out = proc.stdout
        if proc.stderr:
            if out and not out.endswith("\n"):
                out += "\n"
            out += proc.stderr

        new_cwd = start_cwd
        if sentinel in out:
            idx = out.rfind(sentinel)
            new_cwd_line = out[idx + len(sentinel):].split("\n", 1)[0].strip()
            if new_cwd_line and Path(new_cwd_line).is_dir():
                new_cwd = new_cwd_line
            out = out[:idx].rstrip("\n")
        if new_cwd != start_cwd or start_cwd != home:
            _write_persisted_cwd(new_cwd)

        return out, proc.returncode, False
    except subprocess.TimeoutExpired as e:
        partial = ""
        if e.stdout:
            partial += e.stdout if isinstance(e.stdout, str) else e.stdout.decode("utf-8", "replace")
        if e.stderr:
            s = e.stderr if isinstance(e.stderr, str) else e.stderr.decode("utf-8", "replace")
            if partial and not partial.endswith("\n"):
                partial += "\n"
            partial += s
        return partial, 124, True
    except Exception as exc:
        return f"<exec error: {exc}>", 1, False


def format_inline(cmd: str, output: str, exit_code: int, timed_out: bool, timeout_s: int) -> str:
    """Format short output for inline Discord reply (≤2000 chars)."""
    # Use `$` echo prefix for raw shell; for /slash commands the line already
    # starts with `/`, so no prefix needed. For bash, prepend cwd hint if we're
    # not at $HOME — gives the user spatial awareness across multi-step sessions.
    if cmd.startswith("/"):
        cmd_line = cmd
    else:
        cwd = _read_persisted_cwd()
        home = str(Path.home())
        if cwd != home:
            disp = "~" + cwd[len(home):] if cwd.startswith(home + "/") else cwd
            cmd_line = f"{disp} $ {cmd}"
        else:
            cmd_line = f"$ {cmd}"
    # Reserve room for fences + cmd echo + exit line
    fence_overhead = len("```\n\n```")  # opening + closing fence + newlines
    exit_line = ""
    if timed_out:
        exit_line = f"\n[timed out after {timeout_s}s]"
    elif exit_code != 0:
        exit_line = f"\n[exit {exit_code}]"

    body_budget = INLINE_LIMIT - len(cmd_line) - fence_overhead - len(exit_line) - 2
    if body_budget < 100:
        body_budget = 100

    body = output.rstrip()
    truncated = False
    if len(body) > body_budget:
        body = body[:body_budget].rstrip()
        truncated = True

    parts = ["```", cmd_line, body if body else "(no output)"]
    if truncated:
        parts.append("[output truncated, see attachment]")
    parts.append("```")
    if exit_line:
        parts.append(exit_line.lstrip("\n"))
    return "\n".join(parts)


def _multipart_body(boundary: str, payload_json: str, filename: str, file_bytes: bytes, file_mime: str) -> bytes:
    """Build a multipart/form-data body for Discord message-with-attachment POST."""
    crlf = b"\r\n"
    parts = []
    # JSON payload part
    parts.append(f"--{boundary}".encode())
    parts.append(b'Content-Disposition: form-data; name="payload_json"')
    parts.append(b"Content-Type: application/json")
    parts.append(b"")
    parts.append(payload_json.encode("utf-8"))
    # File part
    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"'.encode("utf-8")
    )
    parts.append(f"Content-Type: {file_mime}".encode("utf-8"))
    parts.append(b"")
    parts.append(file_bytes)
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    return crlf.join(parts)


def _discord_post_message(token: str, channel_id: str, content: str, reply_to: str | None = None) -> bool:
    """POST a plain message to a channel."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload: dict = {"content": content, "allowed_mentions": {"parse": []}}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "multiagent-tools-passthrough/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        log(f"post HTTP {e.code} channel={channel_id}: {e.read()[:300]!r}")
        return False
    except Exception as e:
        log(f"post failed channel={channel_id}: {e}")
        return False


def _discord_post_with_attachment(
    token: str,
    channel_id: str,
    content: str,
    file_bytes: bytes,
    filename: str,
    reply_to: str | None = None,
) -> bool:
    """POST a message with a file attachment via multipart/form-data."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload: dict = {
        "content": content,
        "allowed_mentions": {"parse": []},
        "attachments": [{"id": 0, "filename": filename}],
    }
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}

    boundary = "----cc-passthrough-" + uuid.uuid4().hex
    mime, _ = mimetypes.guess_type(filename)
    body = _multipart_body(boundary, json.dumps(payload), filename, file_bytes, mime or "text/plain")
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "multiagent-tools-passthrough/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        log(f"post-attach HTTP {e.code} channel={channel_id}: {e.read()[:300]!r}")
        return False
    except Exception as e:
        log(f"post-attach failed channel={channel_id}: {e}")
        return False


def _emit_block(reason: str) -> None:
    """Tell Claude Code to drop this prompt entirely."""
    print(json.dumps({"decision": "block", "reason": reason}))


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log(f"bad JSON input: {raw[:200]!r}")
        return 0

    prompt = payload.get("prompt", "") or ""
    if not prompt:
        return 0

    parsed = parse_passthrough(prompt)
    if parsed is None:
        return 0
    chat_id = parsed["chat_id"]
    msg_id = parsed["msg_id"]
    timeout_s = parsed["timeout_s"]
    mode = parsed["mode"]

    state_dir = _detect_state_dir()
    token = _read_token(state_dir)
    if not token:
        log(f"no token at {state_dir}/.env; cannot reply, passing prompt through")
        return 0  # let Claude handle it normally so Jeff at least sees something

    if mode == "bash":
        cmd = parsed["cmd"]
        # Denylist check (raw shell only — slash commands are gated by registration).
        deny_label = check_denylist(cmd)
        if deny_label:
            log(f"DENY chat={chat_id} cmd={cmd!r} reason={deny_label}")
            msg = f"```\n$ {cmd}\n[blocked: {deny_label}]\n```"
            _discord_post_message(token, chat_id, msg, reply_to=msg_id)
            _emit_block(f"pass-through denied: {deny_label}")
            return 0
        log(f"EXEC chat={chat_id} timeout={timeout_s} cmd={cmd!r}")
        output, exit_code, timed_out = run_command(cmd, timeout_s)
        log(f"DONE chat={chat_id} exit={exit_code} timed_out={timed_out} bytes={len(output)}")
        echo_line = cmd
    else:  # slash
        name = parsed["name"]
        args = parsed["args"]
        log(f"SLASH chat={chat_id} timeout={timeout_s} name={name!r} args={args!r}")
        output, exit_code, timed_out = run_slash(name, args, timeout_s)
        log(f"DONE chat={chat_id} exit={exit_code} timed_out={timed_out} bytes={len(output)}")
        echo_line = f"/{name}" + (f" {args}" if args else "")
        # Reuse `cmd` var name for the formatter — it's just the echoed first line.
        cmd = echo_line

    # Decide inline vs attachment based on size
    full_text = output.rstrip()
    inline_msg = format_inline(cmd, output, exit_code, timed_out, timeout_s)

    posted = False
    # If output is large enough that inline truncated, attach the full thing
    if len(full_text) > INLINE_LIMIT - 200:
        attach_bytes = full_text.encode("utf-8", "replace")
        if len(attach_bytes) > ATTACHMENT_LIMIT:
            attach_bytes = attach_bytes[:ATTACHMENT_LIMIT - 100] + b"\n[output exceeds 8MB, truncated]\n"
        # Use a stable, descriptive name with a short hash for uniqueness
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", cmd[:30]).strip("-") or "output"
        filename = f"passthrough-{slug}-{uuid.uuid4().hex[:6]}.txt"
        posted = _discord_post_with_attachment(
            token, chat_id, inline_msg, attach_bytes, filename, reply_to=msg_id
        )

    if not posted:
        posted = _discord_post_message(token, chat_id, inline_msg, reply_to=msg_id)

    if not posted:
        log(f"FAILED to post reply chat={chat_id} cmd={cmd!r}")
        # Don't block — let Claude see the prompt and react
        return 0

    _emit_block("pass-through executed; Discord already replied")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log(f"crash:\n{traceback.format_exc()}")
        # Never fail the hook — just pass through
        sys.exit(0)
