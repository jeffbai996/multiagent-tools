"""Tests for merger.py: rewrite_refs + suggest_merged_text."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import merger  # noqa: E402


# ─────────────────────────── rewrite_refs ───────────────────────────


def test_rewrite_short_form():
    out, n = merger.rewrite_refs("see #5", 5, 99)
    assert out == "see #99"
    assert n == 1


def test_rewrite_long_form():
    out, n = merger.rewrite_refs("see [[memory:5]]", 5, 99)
    assert out == "see [[memory:99]]"
    assert n == 1


def test_rewrite_both_forms():
    out, n = merger.rewrite_refs("[[memory:5]] and #5", 5, 99)
    assert out == "[[memory:99]] and #99"
    assert n == 2


def test_boundary_does_not_match_longer_id():
    """`#5` should NOT match `#50` — partial-prefix is the classic regex pitfall."""
    out, n = merger.rewrite_refs("see #5 and #50", 5, 99)
    assert out == "see #99 and #50"
    assert n == 1


def test_color_codes_unchanged():
    """`#fff` and similar non-numeric should not get matched."""
    out, n = merger.rewrite_refs("color #fff is #f0c674", 5, 99)
    assert out == "color #fff is #f0c674"
    assert n == 0


def test_other_ids_unchanged():
    """Only the target id is rewritten; other id refs stay put."""
    out, n = merger.rewrite_refs("links: #3, #5, #7, #11", 5, 99)
    assert out == "links: #3, #99, #7, #11"
    assert n == 1


def test_empty_text():
    out, n = merger.rewrite_refs("", 5, 99)
    assert out == ""
    assert n == 0


def test_no_matches():
    out, n = merger.rewrite_refs("no refs here at all", 5, 99)
    assert out == "no refs here at all"
    assert n == 0


def test_multiple_occurrences_of_target():
    out, n = merger.rewrite_refs("see #5 then #5 again then [[memory:5]]", 5, 99)
    assert out == "see #99 then #99 again then [[memory:99]]"
    assert n == 3


def test_url_path_does_not_match():
    """Patterns adjacent to letters/slashes shouldn't trigger (mirrors rendering.py)."""
    out, n = merger.rewrite_refs("link to /api/memory/5 stays", 5, 99)
    assert out == "link to /api/memory/5 stays"
    assert n == 0


# ─────────────────────────── suggest_merged_text ───────────────────────────


def test_suggest_combines_winner_and_loser():
    out = merger.suggest_merged_text("hello winner", "hello loser",
                                     loser_id=5, loser_name="L name")
    assert "hello winner" in out
    assert "hello loser" in out
    assert "---" in out
    assert "#5 L name" in out


def test_suggest_empty_loser_returns_winner():
    out = merger.suggest_merged_text("just winner", "",
                                     loser_id=5, loser_name="L")
    assert out == "just winner"


def test_suggest_empty_winner_returns_loser():
    out = merger.suggest_merged_text("", "just loser",
                                     loser_id=5, loser_name="L")
    assert out == "just loser"


def test_suggest_both_empty():
    out = merger.suggest_merged_text("", "", loser_id=5, loser_name="L")
    assert out == ""


def test_suggest_handles_no_loser_name():
    out = merger.suggest_merged_text("w", "l", loser_id=42, loser_name="")
    assert "#42" in out
    # No trailing space when name empty
    assert "#42 " not in out or "#42 on" in out


def test_suggest_strips_trailing_whitespace():
    out = merger.suggest_merged_text("winner  \n\n", "loser  \n",
                                     loser_id=1, loser_name="")
    # Winner block ends cleanly before the divider, loser ends cleanly at EOF
    assert "winner  \n\n\n---" not in out
    assert out.endswith("loser")
