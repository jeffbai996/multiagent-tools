"""Thin HTTP client for vecgrep's /api/search endpoint.

Used by server.py to render semantic-search results inline in the memories
and journal index pages. Vecgrep returns chunk-level hits keyed by source
file path; we map those back to memory/journal entry IDs by parsing known
filename patterns.

Env:
  VECGREP_URL                  default http://127.0.0.1:8765
  VECGREP_CORPUS_MEMORIES      default multiagent-tools
  VECGREP_CORPUS_JOURNAL       default multiagent-tools
  VECGREP_TOP_K                default 10
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

VECGREP_URL = os.environ.get("VECGREP_URL", "http://127.0.0.1:8765").rstrip("/")
VECGREP_CORPUS_MEMORIES = os.environ.get("VECGREP_CORPUS_MEMORIES", "multiagent-tools")
VECGREP_CORPUS_JOURNAL = os.environ.get("VECGREP_CORPUS_JOURNAL", "multiagent-tools")
SEARCH_TIMEOUT_SEC = 5
TOP_K_DEFAULT = int(os.environ.get("VECGREP_TOP_K", "10"))

_FILENAME_RE = re.compile(
    r"^(?:(?P<kind>memory|journal)-(?P<eid>\d+)\.md|"
    r"(?P<eid2>\d{2,6})-[^.]*\.md)$"
)


class VecgrepUnavailable(Exception):
    """Raised when /api/search is unreachable or returns unusable data."""


def _post_search(query: str, corpus: str, top_k: int = 25) -> list[dict]:
    body = json.dumps({
        "query": query,
        "corpus": corpus,
        "top_k": top_k,
        "mode": "hybrid",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{VECGREP_URL}/api/search",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read())
            return data.get("hits", [])
    except urllib.error.URLError as e:
        raise VecgrepUnavailable(f"vecgrep at {VECGREP_URL} unreachable: {e}") from e
    except (json.JSONDecodeError, OSError, TimeoutError) as e:
        raise VecgrepUnavailable(f"vecgrep search failed: {e}") from e


def _hit_to_entry_id(hit: dict, want_kind: str | None = None) -> int | None:
    src = hit.get("source_id", "")
    if not src:
        return None
    base = os.path.basename(src)
    m = _FILENAME_RE.match(base)
    if not m:
        return None
    kind = m.group("kind")
    if want_kind and kind and kind != want_kind:
        return None
    raw_id = m.group("eid") or m.group("eid2")
    try:
        return int(raw_id)
    except (ValueError, TypeError):
        return None


def search_corpus_to_ids(
    query: str,
    corpus: str,
    top_k: int | None = None,
    want_kind: str | None = None,
) -> list[tuple[int, float]]:
    """Run vecgrep search and return (entry_id, similarity_pct) pairs.

    Dedupes repeated chunk hits for the same entry while preserving vecgrep's
    ranking order.
    """
    hits = _post_search(query, corpus, top_k=top_k or TOP_K_DEFAULT)
    seen: dict[int, float] = {}
    order: list[int] = []
    for h in hits:
        eid = _hit_to_entry_id(h, want_kind=want_kind)
        if eid is None:
            continue
        pct = float(h.get("similarity_pct", 0.0))
        if eid not in seen:
            seen[eid] = pct
            order.append(eid)
        else:
            seen[eid] = max(seen[eid], pct)
    return [(eid, seen[eid]) for eid in order]


def search_corpus_with_matches(
    query: str,
    corpus: str,
    top_k: int | None = None,
    want_kind: str | None = None,
) -> list[tuple[int, float, list[str]]]:
    """Like search_corpus_to_ids but also returns matched_by per entry.

    Returns (entry_id, similarity_pct, matched_by) triples in vecgrep's
    ranking order. matched_by is a list like ["vector"], ["bm25"], or
    ["bm25", "vector"] — drives the V/K/VK badges in the UI.
    """
    hits = _post_search(query, corpus, top_k=top_k or TOP_K_DEFAULT)
    seen_pct: dict[int, float] = {}
    seen_matched: dict[int, set[str]] = {}
    order: list[int] = []
    for h in hits:
        eid = _hit_to_entry_id(h, want_kind=want_kind)
        if eid is None:
            continue
        pct = float(h.get("similarity_pct", 0.0))
        matched = h.get("matched_by") or []
        if eid not in seen_pct:
            seen_pct[eid] = pct
            seen_matched[eid] = set(matched)
            order.append(eid)
        else:
            seen_pct[eid] = max(seen_pct[eid], pct)
            seen_matched[eid].update(matched)
    return [
        (eid, seen_pct[eid], sorted(seen_matched[eid]))
        for eid in order
    ]


def is_available() -> bool:
    try:
        req = urllib.request.Request(f"{VECGREP_URL}/api/config")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False
