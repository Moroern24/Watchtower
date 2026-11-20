"""Shrink pages for LLM prompts: a DOM skeleton with classes/ids/hrefs and short texts,
repeated siblings collapsed. This is the main lever for model cost per portal."""
from __future__ import annotations

import json
import re

from lxml import etree

from .extract import el_text, parse_html

DROP = {"script", "style", "noscript", "svg", "iframe", "link", "meta", "template", "canvas",
        "video", "audio", "picture", "source", "object", "embed", "head"}
KEEP_ATTRS = ("id", "class", "href", "name", "type", "value", "role", "aria-label", "title",
              "rel", "action", "method", "for", "datetime", "colspan")
VOID = {"br", "hr", "img", "input"}


def _sig(el) -> str:
    return f"{el.tag}.{'.'.join(sorted((el.get('class') or '').split()[:3]))}"


def _attrs(el) -> str:
    parts = []
    for k in KEEP_ATTRS:
        v = el.get(k)
        if v is None:
            continue
        v = re.sub(r"\s+", " ", v).strip()
        if k == "class":
            v = " ".join(v.split()[:4])
        if len(v) > 140:
            v = v[:140] + "…"
        parts.append(f'{k}="{v}"')
    for k, v in el.attrib.items():
        if k.startswith("data-") and len(parts) < 10:
            parts.append(f'{k}="{v[:60]}"')
    return (" " + " ".join(parts)) if parts else ""


def _txt(s: str | None, limit: int) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _render(el, out: list[str], depth: int, budget: list[int], text_limit: int,
            keep_similar: int) -> None:
    if budget[0] <= 0 or not isinstance(el.tag, str):
        return
    tag = el.tag.lower()
    if tag in DROP:
        return
    if tag == "input" and (el.get("type") or "").lower() == "hidden" and \
            not re.search(r"token|viewstate|csrf", el.get("name") or "", re.I):
        return
    ind = "  " * min(depth, 12)
    line = f"{ind}<{tag}{_attrs(el)}>"
    t = _txt(el.text, text_limit)
    children = [c for c in el if isinstance(c.tag, str) and c.tag.lower() not in DROP]
    if not children:
        line += t + ("" if tag in VOID else f"</{tag}>")
        out.append(line)
        budget[0] -= len(line)
        tail = _txt(el.tail, text_limit)
        if tail:
            out.append(f"{ind}{tail}")
        return
    out.append(line + t)
    budget[0] -= len(line) + len(t)
    i = 0
    while i < len(children):
        c = children[i]
        sig = _sig(c)
        j = i
        while j < len(children) and _sig(children[j]) == sig:
            j += 1
        run = j - i
        shown = children[i:i + keep_similar] if run > keep_similar + 1 else children[i:j]
        for s in shown:
            _render(s, out, depth + 1, budget, text_limit, keep_similar)
        if len(shown) < run:
            out.append(f"{ind}  <!-- … {run - len(shown)} more similar <{sig}> elements -->")
        i = j
    out.append(f"{ind}</{tag}>")
    tail = _txt(el.tail, text_limit)
    if tail:
        out.append(f"{ind}{tail}")


def skeleton(html: str, url: str, max_chars: int = 45000, text_limit: int = 100,
             keep_similar: int = 3) -> str:
    doc = parse_html(html, url)
    for bad in doc.xpath("//comment()"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)
    body = doc.find("body")
    root = body if body is not None else doc
    out: list[str] = []
    _render(root, out, 0, [max_chars], text_limit, keep_similar)
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n<!-- truncated -->"
    title = doc.findtext(".//title") or ""
    return f"<!-- page: {url} | title: {_txt(title, 120)} -->\n{text}"


def visible_text(html: str, url: str, max_chars: int = 8000) -> str:
    doc = parse_html(html, url)
    for el in doc.xpath("//script|//style|//noscript|//svg|//head"):
        p = el.getparent()
        if p is not None:
            p.remove(el)
    lines = []
    for el in doc.iter("h1", "h2", "h3", "h4", "p", "li", "tr", "dt", "dd", "label", "td",
                       "div", "span", "a"):
        if len(el) == 0 or el.tag in ("tr", "p", "li", "dt", "dd", "h1", "h2", "h3"):
            t = el_text(el)
            if t and (not lines or lines[-1] != t):
                lines.append(t[:300])
    text = "\n".join(lines)
    return text[:max_chars]


def links(html: str, url: str, limit: int = 120) -> list[dict]:
    doc = parse_html(html, url)
    out, seen = [], set()
    for a in doc.iter("a"):
        href = a.get("href")
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        from urllib.parse import urljoin
        u = urljoin(doc.base_url, href)
        if u in seen:
            continue
        seen.add(u)
        out.append({"text": _txt(el_text(a), 80), "href": u})
        if len(out) >= limit:
            break
    return out


def json_sample(data, max_chars: int = 12000, list_keep: int = 3) -> str:
    def shrink(x, depth=0):
        if isinstance(x, list):
            head = [shrink(v, depth + 1) for v in x[:list_keep]]
            if len(x) > list_keep:
                head.append(f"… {len(x) - list_keep} more items")
            return head
        if isinstance(x, dict):
            return {k: shrink(v, depth + 1) for k, v in list(x.items())[:60]}
        if isinstance(x, str) and len(x) > 200:
            return x[:200] + "…"
        return x
    s = json.dumps(shrink(data), ensure_ascii=False, indent=1)
    return s[:max_chars]


def looks_like_js_app(html: str) -> bool:
    """Very little visible text but scripts → probably rendered client-side."""
    doc = parse_html(html, "http://x/")
    body_text = el_text(doc.find("body")) if doc.find("body") is not None else ""
    n_scripts = len(doc.xpath("//script"))
    n_links = len(doc.xpath("//a[@href]"))
    return (len(body_text or "") < 400 and n_scripts >= 1) or \
        bool(re.search(r'id="(app|root|__next|__nuxt)"', html) and n_links < 8)


def _unused(x):  # keep etree import used for type checkers
    return etree
