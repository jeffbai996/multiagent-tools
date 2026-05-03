"""Persona registry tests.

We point MULTIAGENT_AGENTS_FILE at a tmp YAML so personas.py never reads
the user's real ~/.config/multiagent-tools/agents.yaml.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


def _import_personas():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    if "personas" in sys.modules:
        del sys.modules["personas"]
    import personas  # noqa: E402
    return personas


@pytest.fixture
def fresh_personas(tmp_path, monkeypatch):
    plain = tmp_path / "plain.md"
    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(textwrap.dedent(f"""
        agents:
          testbot:
            - {{ slot: plain.md, path: {plain}, mode: plain }}
    """).strip() + "\n")
    monkeypatch.setenv("MULTIAGENT_AGENTS_FILE", str(agents_file))
    p = _import_personas()
    p.reset_cache()
    return p, plain


@pytest.fixture
def git_personas(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    tracked = repo / "tracked.md"
    tracked.write_text("seed\n")
    subprocess.run(["git", "add", "tracked.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(textwrap.dedent(f"""
        git_repo: {repo}
        agents:
          gitbot:
            - {{ slot: tracked.md, path: {tracked}, mode: git }}
    """).strip() + "\n")
    monkeypatch.setenv("MULTIAGENT_AGENTS_FILE", str(agents_file))
    p = _import_personas()
    p.reset_cache()
    return p, repo, tracked


def test_list_bots_and_get_files(fresh_personas):
    personas, _ = fresh_personas
    assert personas.list_bots() == ["testbot"]
    files = personas.get_files("testbot")
    assert len(files) == 1
    assert files[0]["slot"] == "plain.md"
    assert files[0]["mode"] == "plain"


def test_read_missing_file_returns_empty(fresh_personas):
    personas, plain = fresh_personas
    assert not plain.exists()
    data = personas.read_slot("testbot", "plain.md")
    assert data["text"] == ""
    assert data["mtime"] is None
    assert data["mode"] == "plain"


def test_write_then_read_roundtrip(fresh_personas):
    personas, plain = fresh_personas
    result = personas.write_slot("testbot", "plain.md", "hello world\n")
    assert result["ok"] is True
    assert result["committed"] is False
    assert plain.read_text() == "hello world\n"

    data = personas.read_slot("testbot", "plain.md")
    assert data["text"] == "hello world\n"
    assert data["mtime"] is not None


def test_write_atomic_on_existing_file(fresh_personas):
    personas, plain = fresh_personas
    plain.write_text("original\n")
    personas.write_slot("testbot", "plain.md", "replaced\n")
    assert plain.read_text() == "replaced\n"
    # No leftover .tmp.
    assert not (plain.parent / "plain.md.tmp").exists()


def test_unknown_bot_raises(fresh_personas):
    personas, _ = fresh_personas
    with pytest.raises(KeyError):
        personas.read_slot("nope", "plain.md")
    with pytest.raises(KeyError):
        personas.read_slot("testbot", "nope.md")


def test_git_write_commits(git_personas):
    personas, repo, tracked = git_personas
    result = personas.write_slot("gitbot", "tracked.md", "edited\n")
    assert result["ok"] is True
    assert result["committed"] is True
    assert result["sha"]
    assert result["error"] is None

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert log.stdout.strip() == "personas: update gitbot tracked.md"


def test_git_write_idempotent(git_personas):
    """Writing identical content shouldn't create an empty commit."""
    personas, repo, tracked = git_personas
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    result = personas.write_slot("gitbot", "tracked.md", "seed\n")
    assert result["committed"] is True  # path was clean → returns existing sha
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert before == after
