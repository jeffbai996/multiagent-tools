"""Live infrastructure inventory.

Probes each configured host for the bits of running infra that tend to
rot in static docs: hook chains (`settings.json`), crontab entries,
systemd user units, launchd agents. Returns a structured dict per host
so the Flask UI can render a unified "what's wired up where" page.

Source of truth for hooks/crons/services stays in canonical locations
(settings.json, crontab, systemd files). This module READS, never
writes.

Transport abstraction:
  LocalTransport       — direct subprocess on the host running the server
  WrapperSshTransport  — invokes a user-provided wrapper like
                         `~/.local/bin/host-ssh "$@"` that handles SSH
                         and any guard wrapping. See class docstring.

Out of the box only LocalTransport runs. To probe other hosts, append
to the `hosts` list inside `gather()` with your own Transport instance.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any


# ─────────────────────────── transports ───────────────────────────


class Transport:
    """Run a shell command somewhere and return (rc, stdout, stderr)."""

    name: str = "abstract"

    def run(self, cmd: list[str], timeout: int = 8) -> tuple[int, str, str]:
        raise NotImplementedError

    def cat(self, path: str, timeout: int = 8) -> tuple[int, str]:
        rc, out, _ = self.run(["cat", path], timeout=timeout)
        return rc, out


class LocalTransport(Transport):
    name = "local"

    def run(self, cmd: list[str], timeout: int = 8) -> tuple[int, str, str]:
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except Exception as e:
            return 1, "", str(e)


class WrapperSshTransport(Transport):
    """Run commands through a wrapper script that handles SSH itself.

    Useful when you have a wrapper like `~/.local/bin/my-host-ssh "$@"`
    that does `ssh -i <key> user@host "$*"` (optionally through a
    restricted `command="..."` guard on the remote side). Pass the
    wrapper's name to the constructor; the wrapper's `$0 <cmd>` shape
    is the only assumed contract.

    Example:
        # Reach a remote host via a guarded wrapper installed at
        # ~/.local/bin/server-ssh:
        t = WrapperSshTransport("server-ssh")
        rc, out, err = t.run(["cat", "/etc/hostname"])
    """

    def __init__(self, wrapper: str, name: str | None = None) -> None:
        self.wrapper = wrapper
        self.name = name or wrapper

    def run(self, cmd: list[str], timeout: int = 8) -> tuple[int, str, str]:
        # Wrappers typically do `ssh ... "$*"` so individual args
        # concatenate space-separated; quote anything with whitespace.
        quoted = " ".join(_shell_quote(c) for c in cmd)
        try:
            p = subprocess.run(
                [self.wrapper, quoted],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except FileNotFoundError:
            return 127, "", f"{self.wrapper} not on PATH"
        except Exception as e:
            return 1, "", str(e)


# Back-compat alias — same shape as the old class name.
MacSshTransport = WrapperSshTransport


def _shell_quote(s: str) -> str:
    if not s or any(c in s for c in " \t\n\"'\\$`"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


# ─────────────────────────── probes ───────────────────────────


def probe_settings_hooks(t: Transport, paths: list[str]) -> dict[str, Any]:
    """Read each candidate settings.json path; return parsed `hooks` blocks
    keyed by path. Empty dict if no path was readable.
    """
    out: dict[str, Any] = {}
    for path in paths:
        rc, body = t.cat(path)
        if rc != 0 or not body.strip():
            continue
        try:
            cfg = json.loads(body)
        except json.JSONDecodeError as e:
            out[path] = {"_error": f"JSON parse error: {e}"}
            continue
        hooks = cfg.get("hooks", {})
        out[path] = hooks
    return out


# Match `* * * * * cmd...` style crontab lines. Comments and blank lines
# are skipped. We retain raw text so the UI can render verbatim.
_CRON_LINE_RE = re.compile(r"^\s*(?:@\S+|[\d*,\-\/]+\s+[\d*,\-\/]+\s+[\d*,\-\/]+\s+[\d*,\-\/]+\s+[\d*,\-\/]+)\s+\S")


def probe_crontab(t: Transport) -> list[dict[str, str]]:
    """Run `crontab -l` and return entries. Each entry has `schedule` and
    `command` columns split where possible, plus the raw line."""
    rc, body, err = t.run(["crontab", "-l"], timeout=5)
    if rc != 0:
        # rc 1 with "no crontab" stderr is normal — return empty.
        return []
    entries: list[dict[str, str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not _CRON_LINE_RE.match(line):
            continue
        # Split on first run of whitespace AFTER the schedule. Schedule
        # is either `@something` (single token) or 5 fields.
        if line.lstrip().startswith("@"):
            parts = line.split(None, 1)
        else:
            parts = line.split(None, 5)
        if len(parts) < 2:
            continue
        if line.lstrip().startswith("@"):
            schedule, command = parts[0], parts[1]
        else:
            # 5 schedule fields + command
            if len(parts) < 6:
                continue
            schedule = " ".join(parts[:5])
            command = parts[5]
        entries.append({"schedule": schedule, "command": command, "raw": line})
    return entries


def probe_systemd_user_units(t: Transport) -> list[dict[str, str]]:
    """List user-enabled systemd units (services, timers). Filters out
    auto-generated / static / disabled units to keep the noise out — only
    things the user has explicitly enabled show up.

    Linux only — returns empty on macOS where the binary is absent.
    """
    rc, body, _ = t.run(
        ["systemctl", "--user", "list-unit-files",
         "--type=service,timer", "--no-pager", "--no-legend"],
        timeout=6,
    )
    if rc != 0:
        return []
    units: list[dict[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        # Only surface enabled or linked units — that's what the user
        # actually wired up. Static/generated/disabled are stdlib noise.
        if state not in ("enabled", "linked", "linked-runtime", "enabled-runtime"):
            continue
        units.append({"name": name, "state": state})
    return units


def probe_launchd_user_agents(t: Transport, home: str) -> list[dict[str, str]]:
    """Mac launchd user agents (~/Library/LaunchAgents). Plist names only —
    state requires `launchctl list` which is heavier.

    `home` is the target host's $HOME (passed in because expanduser would
    resolve to the server's home, not Mac's).
    """
    rc, body, _ = t.run(
        ["ls", "-1", f"{home}/Library/LaunchAgents/"],
        timeout=4,
    )
    if rc != 0:
        return []
    agents: list[dict[str, str]] = []
    for line in body.splitlines():
        name = line.strip()
        if name and name.endswith(".plist"):
            agents.append({"name": name})
    return agents


# ─────────────────────────── orchestration ───────────────────────────


# Cache last probe result for ~30s to avoid hammering SSH on page reload.
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 30


def _cached(host_key: str, fn) -> dict[str, Any]:
    entry = _CACHE.get(host_key)
    if entry and time.time() - entry[0] < _CACHE_TTL_SEC:
        return entry[1]
    result = fn()
    _CACHE[host_key] = (time.time(), result)
    return result


def _probe_local() -> dict[str, Any]:
    """Probe the host this server is running on. Picks Linux vs Mac probes
    based on the platform.
    """
    import platform

    home = os.path.expanduser("~")
    transport = LocalTransport()
    is_mac = platform.system() == "Darwin"

    # Default settings.json candidate paths. Override with
    # MULTIAGENT_SETTINGS_PATHS (colon-separated). Useful when your agent
    # config lives somewhere non-standard.
    extra_paths_env = os.environ.get("MULTIAGENT_SETTINGS_PATHS", "").strip()
    if extra_paths_env:
        settings_paths = [
            os.path.expanduser(p) for p in extra_paths_env.split(":") if p.strip()
        ]
    else:
        # Reasonable defaults — Claude Code agent settings.
        settings_paths = [
            f"{home}/.claude/settings.json",
            f"{home}/.claude/settings.local.json",
        ]

    out: dict[str, Any] = {
        "host": platform.node() or ("mac" if is_mac else "linux"),
        "ok": True,
        "settings": probe_settings_hooks(transport, settings_paths),
        "crons": probe_crontab(transport),
        "probed_at": time.time(),
    }
    if is_mac:
        out["launchd"] = probe_launchd_user_agents(transport, home)
    else:
        out["systemd"] = probe_systemd_user_units(transport)
    return out


def gather() -> dict[str, Any]:
    """Return {hosts: [{...host info...}], cache_ttl_sec: N}.

    Out of the box we probe just localhost. To add other hosts, append to
    the `hosts` list — typically by spinning up a custom Transport that
    SSHes to the remote and runs the same probe functions.

    Example:
        from inventory import LocalTransport, MacSshTransport, _cached, ...
        # in your gather():
        hosts.append(_cached("server-2", lambda: _probe_remote(MacSshTransport(), home="/home/foo")))

    Cached for ~30s. Errors per-host don't fail the whole call.
    """
    hosts: list[dict[str, Any]] = []
    hosts.append(_cached("local", _probe_local))
    return {"hosts": hosts, "cache_ttl_sec": _CACHE_TTL_SEC}
