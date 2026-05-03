"""Markdown rendering + backlink detection for memory/journal bodies.

Two transforms run before the markdown engine:
  1. Memory IDs (`#42`) and journal IDs (`j#42`) are linkified.
  2. Inline `[[memory:42]]` / `[[journal:42]]` long form also linkified.

Output is sanitized via bleach so user content can't inject scripts.
"""
from __future__ import annotations

import re
from typing import Iterable

import bleach
import markdown
from markupsafe import Markup

# What html bleach permits in rendered output.
_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "hr", "table", "thead", "tbody", "tr", "th", "td",
    "del", "ins", "sup", "sub", "mark", "span",
}
_ALLOWED_ATTRS = {
    "a": ["href", "title", "class"],
    "span": ["class"],
    "code": ["class"],
}

# Detect `#42` only when it stands as a token, so `#42` in prose
# becomes a link but #fragment-id-with-text and `#fff` color codes don't.
_MEM_ID_RE = re.compile(r"(?<![a-zA-Z0-9_/])#(\d{1,6})\b")
_JNL_ID_RE = re.compile(r"\bj#(\d{1,6})\b")
_LONG_RE = re.compile(r"\[\[(memory|journal):(\d{1,6})\]\]")


def _linkify_refs(text: str, url_prefix: str) -> str:
    """Replace #42, j#42, [[memory:42]], [[journal:42]] with markdown links."""
    def mem_repr(m: re.Match) -> str:
        nid = m.group(1)
        return f"[#{nid}]({url_prefix}/memory/{nid})"

    def jnl_repr(m: re.Match) -> str:
        nid = m.group(1)
        return f"[j#{nid}]({url_prefix}/journal/{nid})"

    def long_repr(m: re.Match) -> str:
        kind, nid = m.group(1), m.group(2)
        if kind == "memory":
            return f"[#{nid}]({url_prefix}/memory/{nid})"
        return f"[j#{nid}]({url_prefix}/journal/{nid})"

    text = _LONG_RE.sub(long_repr, text)
    text = _JNL_ID_RE.sub(jnl_repr, text)
    text = _MEM_ID_RE.sub(mem_repr, text)
    return text


def render_body(text: str, url_prefix: str = "") -> Markup:
    """Render memory/journal body as sanitized HTML."""
    if not text:
        return Markup("")
    linked = _linkify_refs(text, url_prefix.rstrip("/"))
    html = markdown.markdown(
        linked,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        output_format="html5",
    )
    cleaned = bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    return Markup(cleaned)


def extract_refs(text: str) -> tuple[set[int], set[int]]:
    """Return (memory_ids, journal_ids) referenced by `text`."""
    if not text:
        return set(), set()
    mems = {int(m.group(1)) for m in _MEM_ID_RE.finditer(text)}
    jnls = {int(m.group(1)) for m in _JNL_ID_RE.finditer(text)}
    for m in _LONG_RE.finditer(text):
        kind, nid = m.group(1), int(m.group(2))
        (mems if kind == "memory" else jnls).add(nid)
    return mems, jnls


def find_backlinks(target_id: int, target_kind: str,
                   memories: Iterable[dict], journal: Iterable[dict]) -> dict:
    """Return {'memories': [{id, name, type}], 'journal': [{id, ts}]} that
    reference target_id. target_kind is 'memory' or 'journal'."""
    out_mems: list[dict] = []
    out_jnls: list[dict] = []
    for m in memories:
        if m.get("id") == target_id and target_kind == "memory":
            continue
        mems_refd, jnls_refd = extract_refs(m.get("text", ""))
        refs = mems_refd if target_kind == "memory" else jnls_refd
        if target_id in refs:
            out_mems.append({
                "id": m.get("id"),
                "name": m.get("name") or "",
                "type": m.get("type") or "",
            })
    for e in journal:
        if e.get("id") == target_id and target_kind == "journal":
            continue
        mems_refd, jnls_refd = extract_refs(e.get("text", ""))
        refs = mems_refd if target_kind == "memory" else jnls_refd
        if target_id in refs:
            out_jnls.append({
                "id": e.get("id"),
                "ts": e.get("ts") or "",
                "actor": e.get("actor") or "",
            })
    return {"memories": out_mems, "journal": out_jnls}
