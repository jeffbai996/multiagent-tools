"""Per-agent persona file registry.

Personas (CLAUDE.md, persona.md, system_prompt.md, whatever) are
versioned text files scattered across the filesystem because each
agent's runtime expects them in a particular place. This module is the
single source of truth for "where does agent X keep file Y," plus
atomic read/write with optional git commit for files that live inside
a tracked repo.

The agent registry loads from `~/.config/multiagent-tools/agents.yaml`
(or the path in `MULTIAGENT_AGENTS_FILE`). See `agents.example.yaml` in
the repo root for the schema.

Paths in the registry are literal — never accept user-supplied path
components, since these endpoints can write outside the data dir.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Literal, TypedDict

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - guidance shown if missing
    yaml = None  # type: ignore

Mode = Literal["git", "plain"]


class Slot(TypedDict):
    slot: str
    path: str
    mode: Mode


# Default config search path: env var, then XDG-style fallback.
def _config_path() -> str:
    explicit = os.environ.get("MULTIAGENT_AGENTS_FILE")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.expanduser("~/.config/multiagent-tools/agents.yaml")


# Cache parsed config so we don't reread on every endpoint call.
_BOTS_CACHE: dict[str, list[tuple[str, str, Mode]]] | None = None
_GIT_REPO_CACHE: str | None = None


def _load_config() -> tuple[dict[str, list[tuple[str, str, Mode]]], str]:
    """Load agent registry from yaml. Returns (BOTS dict, git_repo_path).

    Schema:
      git_repo: /path/to/git/repo  # used to scope mode=git commits
      agents:
        agent-1:
          - { slot: CLAUDE.md, path: /abs/path, mode: git }
          - { slot: persona.md, path: /abs/path, mode: plain }
        agent-2:
          - { slot: persona.md, path: /abs/path, mode: plain }
    """
    global _BOTS_CACHE, _GIT_REPO_CACHE
    if _BOTS_CACHE is not None:
        return _BOTS_CACHE, _GIT_REPO_CACHE or ""

    path = _config_path()
    if not os.path.exists(path):
        # Empty registry — endpoints will return 404/empty list.
        _BOTS_CACHE = {}
        _GIT_REPO_CACHE = ""
        return _BOTS_CACHE, ""

    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to parse agents.yaml; pip install pyyaml"
        )

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    git_repo = os.path.expanduser(str(data.get("git_repo", "")))
    bots: dict[str, list[tuple[str, str, Mode]]] = {}
    for name, slots in (data.get("agents") or {}).items():
        slot_tuples: list[tuple[str, str, Mode]] = []
        for s in slots or []:
            slot = str(s["slot"])
            slot_path = os.path.expanduser(str(s["path"]))
            mode: Mode = "git" if s.get("mode") == "git" else "plain"
            slot_tuples.append((slot, slot_path, mode))
        bots[str(name)] = slot_tuples

    _BOTS_CACHE = bots
    _GIT_REPO_CACHE = git_repo
    return bots, git_repo


def reset_cache() -> None:
    """Test hook + manual reload after editing agents.yaml."""
    global _BOTS_CACHE, _GIT_REPO_CACHE
    _BOTS_CACHE = None
    _GIT_REPO_CACHE = None


def _bots() -> dict[str, list[tuple[str, str, Mode]]]:
    bots, _ = _load_config()
    return bots


def _git_repo() -> str:
    _, repo = _load_config()
    return repo


def list_bots() -> list[str]:
    return list(_bots().keys())


def get_files(bot: str) -> list[Slot]:
    bots = _bots()
    if bot not in bots:
        raise KeyError(bot)
    return [{"slot": s, "path": p, "mode": m} for s, p, m in bots[bot]]


def _resolve(bot: str, slot: str) -> tuple[str, Mode]:
    bots = _bots()
    if bot not in bots:
        raise KeyError(bot)
    for s, p, m in bots[bot]:
        if s == slot:
            return p, m
    raise KeyError(f"{bot}/{slot}")


def read_slot(bot: str, slot: str) -> dict:
    """Return {bot, slot, path, mode, text, mtime}. text is "" if file missing."""
    path, mode = _resolve(bot, slot)
    text = ""
    mtime: float | None = None
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        mtime = os.path.getmtime(path)
    return {
        "bot": bot,
        "slot": slot,
        "path": path,
        "mode": mode,
        "text": text,
        "mtime": mtime,
    }


def write_slot(bot: str, slot: str, text: str) -> dict:
    """Atomic write. If mode=git, also commits in the configured git_repo.

    Returns {ok, path, mode, committed, sha, error}. The file write
    succeeds independently of the commit — if git fails, the new file
    is still on disk and `committed: false` is returned with the error.
    """
    path, mode = _resolve(bot, slot)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)

    result: dict = {
        "ok": True,
        "path": path,
        "mode": mode,
        "committed": False,
        "sha": None,
        "error": None,
    }

    if mode == "git":
        repo = _git_repo()
        if not repo:
            result["error"] = "mode=git but no git_repo configured in agents.yaml"
            return result
        try:
            sha = _git_commit(repo, path, f"personas: update {bot} {slot}")
            result["committed"] = True
            result["sha"] = sha
        except subprocess.CalledProcessError as e:
            result["error"] = (e.stderr or e.stdout or str(e)).strip()

    return result


def _git_commit(repo: str, path: str, message: str) -> str:
    """git add <path> && git commit -m <message>; return short SHA."""
    rel = os.path.relpath(path, repo)
    if rel.startswith(".."):
        raise ValueError(f"{path} is not under {repo}")

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    run("git", "add", "--", rel)
    # Skip if nothing staged (e.g. saved identical content).
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", rel],
        cwd=repo,
    )
    if diff.returncode == 0:
        sha = run("git", "rev-parse", "--short", "HEAD").stdout.strip()
        return sha
    run("git", "commit", "-m", message, "--", rel)
    return run("git", "rev-parse", "--short", "HEAD").stdout.strip()
