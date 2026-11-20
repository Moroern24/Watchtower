"""Evaluate FieldSpecs (see ARCHITECTURE.md §4) against HTML elements or JSON items."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

import lxml.html
from lxml import etree
from lxml.cssselect import CSSSelector, SelectorError

from .normalize import clean_text

URL_ATTRS = {"href", "src", "data-href", "data-url", "action", "data-src", "data-link"}
LABEL_TAGS = ("dt", "th", "td", "label", "span", "strong", "b", "div", "p", "li", "h3", "h4",
              "h5", "em", "small")

_css_cache: dict[str, CSSSelector] = {}


class SpecError(ValueError):
    pass


def parse_html(text: str, base_url: str) -> lxml.html.HtmlElement:
    if not text or not text.strip():
        text = "<html></html>"
    raw = text.encode("utf-8", "replace")
    parser = lxml.html.HTMLParser(encoding="utf-8")
    try:
        doc = lxml.html.document_fromstring(raw, parser=parser, base_url=base_url)
    except (etree.ParserError, ValueError):
        return lxml.html.document_fromstring("<html></html>", base_url=base_url)
    base = doc.find(".//base[@href]")
    if base is not None and base.get("href"):
        doc = lxml.html.document_fromstring(raw, parser=parser,
                                            base_url=urljoin(base_url, base.get("href")))
    return doc


def select(scope, selector: str) -> list:
    """CSS selector, or XPath when it starts with '/', './' or '('."""
    if not selector:
        return []
    sel = selector.strip()
    try:
        if sel.startswith(("/", "./", "(")):
            res = scope.xpath(sel)
            return [r for r in res if isinstance(r, etree._Element)]
        if sel not in _css_cache:
            _css_cache[sel] = CSSSelector(sel, translator="html")
        return _css_cache[sel](scope)
    except (SelectorError, etree.XPathError, Exception) as e:  # noqa: BLE001
        raise SpecError(f"Bad selector {selector!r}: {e}") from e


def el_text(el) -> str | None:
    if el is None:
        return None
    if isinstance(el, str):
        return clean_text(el)
    # treat <br> as whitespace
    parts = []
    for t in el.itertext():
        parts.append(t)
    return clean_text(" ".join(parts))


def _base(scope) -> str:
    root = scope.getroottree().getroot() if hasattr(scope, "getroottree") else None
    return getattr(root, "base_url", None) or ""


def _attr_value(el, attr: str | None, base: str):
    attr = attr or "text"
    if attr == "text":
        return el_text(el)
    if attr == "html":
        return etree.tostring(el, encoding="unicode")
    val = el.get(attr)
    if val is None and attr == "href":
        # allow selecting a container: take its first link
        a = el.find(".//a[@href]")
        val = a.get("href") if a is not None else None
    if val is None:
        return None
    val = val.strip()
    if attr in URL_ATTRS and val and not val.lower().startswith(("javascript:", "mailto:", "#")):
        return urljoin(base, val)
    return val


def _norm_label(s: str) -> str:
    return re.sub(r"[\s:：*]+$", "", re.sub(r"\s+", " ", s).strip().lower())


def find_by_label(scope, label: str) -> str | None:
    target = _norm_label(label)
    if not target:
        return None
    best = None
    for el in scope.iter(*LABEL_TAGS):
        txt = el_text(el)
        if not txt or len(txt) > len(target) + 25:
            continue
        nt = _norm_label(txt)
        if nt == target or (nt.startswith(target) and len(nt) <= len(target) + 3):
            best = el
            break
    if best is not None:
        return _value_after_label(best, target)
    # inline "Label: value" inside a single element
    for el in scope.iter(*LABEL_TAGS):
        txt = el_text(el)
        if not txt or len(txt) > 400:
            continue
        low = txt.lower()
        if low.startswith(target):
            rest = txt[len(target):].lstrip(" :：-–\t")
            if rest and len(el) <= 3:
                return clean_text(rest)
    return None


def _value_after_label(el, target: str) -> str | None:
    tag = el.tag
    nxt = el.getnext()
    while nxt is not None and not isinstance(nxt.tag, str):
        nxt = nxt.getnext()
    if tag in ("dt", "th", "td") and nxt is not None:
        return el_text(nxt)
    if nxt is not None and el_text(nxt):
        return el_text(nxt)
    # label's tail text, e.g. <b>Buyer:</b> City of X
    if el.tail and clean_text(el.tail):
        return clean_text(el.tail)
    parent = el.getparent()
    if parent is not None:
        # th in a row whose value cell is the next td; or <div><span>L</span><span>V</span></div>
        ptxt = el_text(parent) or ""
        own = el_text(el) or ""
        rest = ptxt[len(own):].strip(" :：-–") if ptxt.startswith(own) else ""
        if rest:
            return clean_text(rest)
        pn = parent.getnext()
        if pn is not None and isinstance(pn.tag, str):
            return el_text(pn)
    return None


# ------------------------------------------------------------------ JSON paths

_TOKEN = re.compile(r"[^.\[\]]+|\[\d+\]|\[\*\]")


def json_path(data: Any, path: str | None):
    if path is None or path in ("", "$", "."):
        return data
    path = path[2:] if path.startswith("$.") else path
    cur = [data]
    multi = False
    for tok in _TOKEN.findall(path):
        nxt = []
        for c in cur:
            if tok == "[*]" or tok == "*":
                multi = True
                if isinstance(c, list):
                    nxt.extend(c)
                elif isinstance(c, dict):
                    nxt.extend(c.values())
            elif tok.startswith("["):
                i = int(tok[1:-1])
                if isinstance(c, list) and -len(c) <= i < len(c):
                    nxt.append(c[i])
            elif isinstance(c, dict):
                if tok in c:
                    nxt.append(c[tok])
            elif isinstance(c, list):  # implicit map over lists
                multi = True
                for it in c:
                    if isinstance(it, dict) and tok in it:
                        nxt.append(it[tok])
        cur = nxt
    if multi:
        return cur
    return cur[0] if cur else None


# ------------------------------------------------------------------ FieldSpec

def apply_regex(value, pattern: str | None):
    if not pattern or value is None:
        return value
    if isinstance(value, list):
        out = [apply_regex(v, pattern) for v in value]
        return [v for v in out if v is not None]
    try:
        m = re.search(pattern, str(value), re.I | re.S)
    except re.error as e:
        raise SpecError(f"Bad regex {pattern!r}: {e}") from e
    if not m:
        return None
    if m.groups():
        g = next((g for g in m.groups() if g is not None), None)
        return clean_text(g)
    return clean_text(m.group(0))


def eval_spec(spec: dict | None, scope, *, is_json: bool = False, base_url: str = ""):
    """Evaluate a FieldSpec. scope = lxml element (html) or JSON value."""
    if not spec:
        return None
    if isinstance(spec, str):  # shorthand: css / path
        spec = {"path": spec} if is_json else {"css": spec}
    if "const" in spec and spec["const"] is not None:
        return spec["const"]
    want_all = bool(spec.get("all"))
    value: Any = None
    if is_json:
        if spec.get("path") is not None:
            value = json_path(scope, spec["path"])
            if isinstance(value, (dict,)):
                value = None
            if isinstance(value, list) and not want_all:
                value = [v for v in value if v not in (None, "")]
                value = value[0] if len(value) == 1 else (value or None)
            if isinstance(value, str):
                value = value.strip() or None
            if value is not None and spec.get("attr") == "url":
                value = urljoin(base_url, str(value))
    else:
        base = base_url or _base(scope)
        if spec.get("label"):
            value = find_by_label(scope, spec["label"])
        elif spec.get("css") or spec.get("xpath"):
            els = select(scope, spec.get("css") or spec.get("xpath"))
            if want_all:
                value = [v for v in (_attr_value(e, spec.get("attr"), base) for e in els) if v]
            elif els:
                value = _attr_value(els[0], spec.get("attr"), base)
        elif spec.get("attr"):  # attribute of the scope element itself
            value = _attr_value(scope, spec.get("attr"), base)
    value = apply_regex(value, spec.get("regex"))
    if spec.get("template") and value is not None:
        value = spec["template"].replace("{value}", str(value))
        if base_url:
            value = urljoin(base_url, value)
    return value
