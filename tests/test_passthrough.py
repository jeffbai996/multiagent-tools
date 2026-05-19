"""Tests for the Discord pass-through hook.

Covers parse (bash + slash), owner-id gate (env + file fallback), denylist,
path-traversal guard, closing-tag strip, native-slash fallthrough,
and built-in handler execution.

No Discord network calls — everything is exercised via parse_passthrough()
and run_slash() directly.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import discord_passthrough as dp  # noqa: E402


# Generic placeholder user_ids — not tied to any real Discord account.
OWNER = "111111111111111111"
WRONG = "999999999999999999"


def _make_prompt(body: str, user_id: str = OWNER) -> str:
    """Synthesize a Discord-tagged prompt as the plugin would emit it."""
    return (
        f'<channel source="plugin:discord:discord" '
        f'chat_id="222222222222222222" '
        f'message_id="333333333333333333" '
        f'user="testuser" user_id="{user_id}">{body}'
    )


@pytest.fixture(autouse=True)
def _force_owner(monkeypatch):
    """Ensure tests run with a known owner_id regardless of host env."""
    monkeypatch.setenv("MAT_OWNER_DISCORD_USER_ID", OWNER)
    yield


# ---------------------------- parse: bash ------------------------------------


def test_bang_parses_simple_command():
    r = dp.parse_passthrough(_make_prompt("!ls /tmp"))
    assert r is not None
    assert r["mode"] == "bash"
    assert r["cmd"] == "ls /tmp"
    assert r["timeout_s"] == dp.DEFAULT_TIMEOUT_S


def test_bang_with_timeout_override():
    r = dp.parse_passthrough(_make_prompt("!t=120 sleep 5"))
    assert r["mode"] == "bash"
    assert r["cmd"] == "sleep 5"
    assert r["timeout_s"] == 120


def test_bang_timeout_clamped_to_max():
    # MAX_TIMEOUT_S = 600; anything over is ignored (parser drops the t=N).
    r = dp.parse_passthrough(_make_prompt("!t=9999 sleep 5"))
    # Either the over-max value is rejected (kept default) OR clamped — assert
    # we never exceed MAX_TIMEOUT_S, never below 1.
    assert 1 <= r["timeout_s"] <= dp.MAX_TIMEOUT_S


def test_double_bang_strips_to_single():
    r = dp.parse_passthrough(_make_prompt("!!whoami"))
    assert r["mode"] == "bash"
    assert r["cmd"] == "whoami"


def test_empty_bang_returns_none():
    assert dp.parse_passthrough(_make_prompt("!")) is None
    assert dp.parse_passthrough(_make_prompt("!   ")) is None


# ---------------------------- parse: slash -----------------------------------


def test_slash_builtin_help_parses():
    r = dp.parse_passthrough(_make_prompt("/help"))
    assert r["mode"] == "slash"
    assert r["name"] == "help"
    assert r["args"] == ""


def test_slash_with_args():
    r = dp.parse_passthrough(_make_prompt("/log 50"))
    assert r["mode"] == "slash"
    assert r["name"] == "log"
    assert r["args"] == "50"


def test_slash_with_timeout_override():
    r = dp.parse_passthrough(_make_prompt("/log t=10 50"))
    assert r["mode"] == "slash"
    assert r["timeout_s"] == 10
    assert r["args"] == "50"


def test_slash_unknown_falls_through():
    # Unregistered /cmd must NOT be intercepted so native Claude slash commands
    # (/loop, /schedule, /compact) keep working.
    assert dp.parse_passthrough(_make_prompt("/loop 5m /foo")) is None
    assert dp.parse_passthrough(_make_prompt("/unknown")) is None
    assert dp.parse_passthrough(_make_prompt("/schedule daily")) is None


def test_slash_case_insensitive_name():
    # Names normalize to lowercase before dispatch.
    r = dp.parse_passthrough(_make_prompt("/HELP"))
    assert r is not None
    assert r["name"] == "help"


def test_empty_slash_returns_none():
    assert dp.parse_passthrough(_make_prompt("/")) is None
    assert dp.parse_passthrough(_make_prompt("/   ")) is None


# ---------------------------- owner gate -------------------------------------


def test_wrong_user_id_rejected():
    assert dp.parse_passthrough(_make_prompt("!ls", user_id=WRONG)) is None
    assert dp.parse_passthrough(_make_prompt("/help", user_id=WRONG)) is None


def test_no_channel_tag_returns_none():
    # Terminal-typed `!ls` (no Discord tag) must not trigger.
    assert dp.parse_passthrough("!ls") is None
    assert dp.parse_passthrough("/help") is None


def test_unconfigured_owner_fails_closed(monkeypatch, tmp_path):
    """With no env var and no owner_id file, parse must return None."""
    monkeypatch.delenv("MAT_OWNER_DISCORD_USER_ID", raising=False)
    monkeypatch.setattr(dp, "OWNER_ID_FILE", tmp_path / "nonexistent")
    assert dp.parse_passthrough(_make_prompt("!ls")) is None


def test_file_fallback_works_when_env_unset(monkeypatch, tmp_path):
    """When env is empty, the owner_id file should satisfy the gate."""
    monkeypatch.delenv("MAT_OWNER_DISCORD_USER_ID", raising=False)
    f = tmp_path / "owner_id"
    f.write_text(OWNER + "\n")
    monkeypatch.setattr(dp, "OWNER_ID_FILE", f)
    r = dp.parse_passthrough(_make_prompt("!ls"))
    assert r is not None
    assert r["cmd"] == "ls"


# ---------------------------- channel tag stripping --------------------------


def test_strip_open_tag_only():
    assert dp._strip_channel_tag(_make_prompt("!ls")) == "!ls"


def test_strip_close_tag_too():
    """Some plugin versions wrap in <channel>...</channel> — strip both."""
    assert dp._strip_channel_tag(_make_prompt("!ls") + "</channel>") == "!ls"


def test_strip_close_tag_with_newline():
    """Real-world case from the bug: !ls\\n</channel>."""
    text = _make_prompt("!ls") + "\n</channel>"
    assert dp._strip_channel_tag(text) == "!ls"
    # And the resulting bash cmd must be clean.
    r = dp.parse_passthrough(text)
    assert r["cmd"] == "ls"


def test_close_tag_in_middle_preserved():
    """A literal '</channel>' inside content shouldn't be stripped; only at end."""
    text = _make_prompt("!echo foo</channel>bar")
    # Closing-tag regex is anchored to end-of-string; middle occurrence stays.
    out = dp._strip_channel_tag(text)
    assert "</channel>bar" in out


# ---------------------------- denylist ---------------------------------------


@pytest.mark.parametrize("cmd", [
    "sudo rm -rf /",
    "rm -rf /",
    "rm -rf $HOME",
    "rm -rf ~",
    "rm -rf *",
    ":(){ :|:& };:",
    "shutdown -h now",
    "reboot",
    "halt",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda",
    "sudo apt install foo",
    "git push --force origin main",
    "git push -f origin master",
    "echo x > /dev/sda",
])
def test_denylist_blocks(cmd):
    assert dp.check_denylist(cmd) is not None, f"denylist missed: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "ls /tmp",
    "rm -rf /home/jbai/some-temp-dir",  # specific subdir, not root
    "git push origin feature/x",
    "echo hello",
    "cat /etc/hostname",
])
def test_denylist_allows(cmd):
    assert dp.check_denylist(cmd) is None, f"denylist false-positive: {cmd!r}"


# ---------------------------- slash dispatch & file registry -----------------


def test_help_lists_builtins():
    out, code, _ = dp.run_slash("help", "", 5)
    assert code == 0
    assert "/help" in out
    assert "/log" in out
    assert "/status" in out
    assert "/commands" in out


def test_log_returns_recent_lines(tmp_path, monkeypatch):
    log = tmp_path / "passthrough.log"
    log.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    monkeypatch.setattr(dp, "LOG_PATH", log)
    out, code, _ = dp.run_slash("log", "5", 5)
    assert code == 0
    # Last 5 of lines 0..49 are 45..49.
    assert "line 49" in out
    assert "line 45" in out
    assert "line 44" not in out


def test_status_returns_zero():
    out, code, _ = dp.run_slash("status", "", 10)
    assert code == 0
    # Should mention at least hostname (HOST: prefix in handler).
    assert "HOST:" in out or "UPTIME:" in out


def test_path_traversal_blocked():
    """Slash names must match the safe regex — no .., slashes, or uppercase."""
    assert dp._resolve_slash_file("../etc/passwd") is None
    assert dp._resolve_slash_file("foo/bar") is None
    assert dp._resolve_slash_file("BOTS") is None  # case-sensitive
    assert dp._resolve_slash_file("") is None
    assert dp._resolve_slash_file(".hidden") is None


def test_file_registry_resolves_sh(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "COMMANDS_DIR", tmp_path)
    script = tmp_path / "echo-test.sh"
    script.write_text('#!/usr/bin/env bash\necho "hello $1"\n')
    script.chmod(0o755)
    out, code, _ = dp.run_slash("echo-test", "world", 5)
    assert code == 0
    assert "hello world" in out


def test_file_registry_resolves_py(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "COMMANDS_DIR", tmp_path)
    script = tmp_path / "py-test.py"
    script.write_text("import sys\nprint('args:', sys.argv[1:])\n")
    out, code, _ = dp.run_slash("py-test", "a b c", 5)
    assert code == 0
    assert "args:" in out
    assert "a" in out and "b" in out and "c" in out


def test_slash_unknown_via_run_slash_returns_127(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "COMMANDS_DIR", tmp_path)
    out, code, _ = dp.run_slash("nonexistent-cmd", "", 5)
    assert code == 127
    assert "no such command" in out


# ---------------------------- formatter --------------------------------------


def test_format_inline_uses_dollar_for_bash():
    msg = dp.format_inline("ls /tmp", "file1\nfile2", 0, False, 30)
    assert "$ ls /tmp" in msg
    assert "file1" in msg


def test_format_inline_no_dollar_for_slash():
    # Slash echo already begins with '/' — no '$' prefix added.
    msg = dp.format_inline("/help", "foo", 0, False, 30)
    assert "$ /help" not in msg
    assert "/help" in msg


def test_format_inline_shows_nonzero_exit():
    msg = dp.format_inline("false", "", 1, False, 30)
    assert "[exit 1]" in msg


def test_format_inline_shows_timeout():
    msg = dp.format_inline("sleep 100", "partial", 124, True, 30)
    assert "[timed out" in msg


def test_format_inline_truncates_long_output():
    huge = "x" * 5000
    msg = dp.format_inline("dump", huge, 0, False, 30)
    assert "[output truncated" in msg
    assert len(msg) <= dp.INLINE_LIMIT + 50  # small overhead allowance


# ---------------------------- persistent cwd ---------------------------------


def test_run_command_persists_cd(tmp_path, monkeypatch):
    """`!cd <dir>` should carry to the next `!cmd` invocation."""
    state = tmp_path / "cwd"
    monkeypatch.setattr(dp, "CWD_STATE_FILE", state)
    target = tmp_path / "subdir"
    target.mkdir()

    out, code, _ = dp.run_command(f"cd {target}", 5)
    assert code == 0
    assert state.read_text().strip() == str(target)

    out2, code2, _ = dp.run_command("pwd", 5)
    assert code2 == 0
    assert str(target) in out2


def test_run_command_resets_when_command_changes_cwd_back(tmp_path, monkeypatch):
    state = tmp_path / "cwd"
    state.write_text("/tmp\n")
    monkeypatch.setattr(dp, "CWD_STATE_FILE", state)
    home = str(Path.home())

    out, code, _ = dp.run_command(f"cd {home}", 5)
    assert code == 0
    assert state.read_text().strip() == home


def test_run_command_sentinel_stripped_from_output(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "CWD_STATE_FILE", tmp_path / "cwd")
    out, code, _ = dp.run_command("echo hello", 5)
    assert "hello" in out
    assert "__MAT_PASSTHROUGH_CWD__" not in out


def test_format_inline_shows_cwd_when_not_home(tmp_path, monkeypatch):
    state = tmp_path / "cwd"
    # Use tmp_path itself — guaranteed to exist and not be $HOME.
    state.write_text(str(tmp_path) + "\n")
    monkeypatch.setattr(dp, "CWD_STATE_FILE", state)

    msg = dp.format_inline("ls", "foo", 0, False, 30)
    # Path won't start with $HOME, so it'll render as absolute.
    assert str(tmp_path) in msg
    assert "$ ls" in msg


def test_format_inline_no_cwd_prefix_when_at_home(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "CWD_STATE_FILE", tmp_path / "nonexistent")
    msg = dp.format_inline("ls", "foo", 0, False, 30)
    assert msg.split("\n", 2)[1] == "$ ls"
