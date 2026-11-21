"""Recipe DSL (ARCHITECTURE.md §4) and its deterministic executor. No LLM calls here."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from typing import Any, Literal, Optional
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit, parse_qsl

from pydantic import BaseModel, Field, ValidationError

from . import normalize as N
from .extract import SpecError, eval_spec, json_path, parse_html, select
from .fetcher import Fetcher, Page, PortalError

log = logging.getLogger("watchtower.recipe")

TENDER_FIELDS = ["id", "url", "title", "buyerName", "publicationDate", "submissionDeadline",
                 "status", "referenceNumber", "description", "procedureType", "contractNature",
                 "cpvCodes", "postalCode", "city", "countryCode"]


class FieldSpec(BaseModel):
    css: Optional[str] = None
    xpath: Optional[str] = None
    attr: Optional[str] = None
    label: Optional[str] = None
    path: Optional[str] = None
    regex: Optional[str] = None
    const: Optional[Any] = None
    all: bool = False
    template: Optional[str] = None


class Pagination(BaseModel):
    type: Literal["none", "next_link", "page_param", "offset_param"] = "none"
    param: Optional[str] = None
    start: int = 1
    step: int = 1
    maxPages: int = 30
    nextSelector: Optional[str] = None
    inBody: bool = False


class ListSpec(BaseModel):
    url: str
    method: Literal["GET", "POST"] = "GET"
    params: dict[str, Any] = Field(default_factory=dict)
    body: Optional[Any] = None
    bodyType: Literal["json", "form"] = "json"
    headers: dict[str, str] = Field(default_factory=dict)
    items: str
    pagination: Pagination = Field(default_factory=Pagination)
    fields: dict[str, FieldSpec] = Field(default_factory=dict)


class DetailSpec(BaseModel):
    kind: Literal["html", "json"] = "html"
    urlTemplate: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, FieldSpec] = Field(default_factory=dict)


class DocSpec(BaseModel):
    pageLink: Optional[FieldSpec] = None
    links: Optional[str] = None
    items: Optional[str] = None
    urlPath: Optional[str] = None
    namePath: Optional[str] = None
    urlTemplate: Optional[str] = None
    followSelector: Optional[str] = None


class Recipe(BaseModel):
    kind: Literal["html", "json"] = "html"
    list: ListSpec
    detail: Optional[DetailSpec] = None
    documents: Optional[DocSpec] = None
    dateFormats: list[str] = Field(default_factory=list)
    dayFirst: bool = True
    timezone: Optional[str] = "Europe/Berlin"
    countryCode: Optional[str] = None
    statusKeywords: dict[str, list[str]] = Field(default_factory=dict)
    notes: Optional[str] = None


def parse_recipe(data: dict) -> Recipe:
    try:
        r = Recipe.model_validate(data)
    except ValidationError as e:
        raise PortalError("llm", f"Recipe does not match schema: {e.errors()[:3]}") from e
    # drop unknown field names (prompt-injection / hallucination guard)
    # ("_name" helper fields are allowed: usable in templates, never emitted)
    def ok(k: str) -> bool:
        return k in TENDER_FIELDS or (k.startswith("_") and len(k) < 30)
    r.list.fields = {k: v for k, v in r.list.fields.items() if ok(k)}
    if r.detail:
        r.detail.fields = {k: v for k, v in r.detail.fields.items() if ok(k)}
    if not r.list.url.startswith(("http://", "https://")):
        raise PortalError("llm", "Recipe list.url must be absolute http(s)")
    return r


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:20]


# ------------------------------------------------------------------ list reading

class RawItem:
    __slots__ = ("fields", "scope", "is_json", "base_url", "list_hash", "detail_scope",
                 "detail_url", "detail_is_json", "synthetic_id")

    def __init__(self, fields, scope, is_json, base_url):
        self.fields: dict = fields
        self.scope = scope
        self.is_json = is_json
        self.base_url = base_url
        self.list_hash = stable_hash(fields)
        self.detail_scope = None
        self.detail_url: str | None = None
        self.detail_is_json = False
        self.synthetic_id = False


def _with_param(url: str, param: str, value) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    q.append((param, str(value)))
    return urlunsplit(parts._replace(query=urlencode(q)))


def _fill_template(tpl: str, fields: dict) -> str:
    out = tpl
    for k, v in fields.items():
        if isinstance(v, (str, int, float)):
            out = out.replace("{" + k + "}", str(v))
    return out


async def fetch_list_page(spec: ListSpec, fetcher: Fetcher, page_no: int | None,
                          url_override: str | None = None) -> Page:
    url = url_override or spec.url
    params = dict(spec.params)
    body = copy.deepcopy(spec.body)
    pg = spec.pagination
    if page_no is not None and pg.param and not url_override:
        if pg.inBody and isinstance(body, dict):
            body[pg.param] = page_no
        elif pg.inBody and spec.bodyType == "form":
            body = dict(body or {})
            body[pg.param] = page_no
        else:
            url = _with_param(url, pg.param, page_no)
    kwargs: dict = {"method": spec.method, "params": params or None,
                    "headers": spec.headers or None}
    if spec.method == "POST":
        if spec.bodyType == "form":
            kwargs["data"] = body
        else:
            kwargs["json_body"] = body
    page = await fetcher.get(url, **kwargs)
    if page.status >= 400:
        raise PortalError("fetch", f"List page HTTP {page.status} at {url}")
    return page


def items_from_page(recipe: Recipe, page: Page) -> tuple[list[RawItem], Any]:
    spec = recipe.list
    if recipe.kind == "json":
        try:
            data = page.json()
        except ValueError as e:
            raise PortalError("extract", f"List response is not JSON ({page.content_type})") from e
        arr = json_path(data, spec.items)
        if isinstance(arr, dict):
            arr = list(arr.values())
        if not isinstance(arr, list):
            return [], data
        out = []
        for it in arr:
            f = {k: eval_spec(v.model_dump(exclude_none=True), it, is_json=True,
                              base_url=page.url) for k, v in spec.fields.items()}
            out.append(RawItem(f, it, True, page.url))
        return out, data
    doc = parse_html(page.text, page.url)
    try:
        els = select(doc, spec.items)
    except SpecError as e:
        raise PortalError("extract", str(e)) from e
    out = []
    for el in els:
        try:
            f = {k: eval_spec(v.model_dump(exclude_none=True), el, base_url=doc.base_url)
                 for k, v in spec.fields.items()}
        except SpecError as e:
            raise PortalError("extract", str(e)) from e
        if not any(v for v in f.values()):
            continue  # header/blank rows
        out.append(RawItem(f, el, False, doc.base_url))
    return out, doc


def _ensure_id(item: RawItem) -> str | None:
    f = item.fields
    tid = N.clean_text(f.get("id")) if f.get("id") is not None else None
    if not tid:
        item.synthetic_id = True
        basis = f.get("url") or f.get("referenceNumber") or f.get("title")
        if not basis:
            return None
        tid = "h" + hashlib.sha1(str(basis).encode()).hexdigest()[:16]
    f["id"] = str(tid)
    return f["id"]


async def read_list(recipe: Recipe, fetcher: Fetcher, max_items: int,
                    steps: list[str] | None = None) -> list[RawItem]:
    spec = recipe.list
    pg = spec.pagination
    seen: dict[str, RawItem] = {}
    next_url: str | None = None
    for i in range(max(1, pg.maxPages if pg.type != "none" else 1)):
        page_no = None
        if pg.type == "page_param":
            page_no = pg.start + i * pg.step
        elif pg.type == "offset_param":
            page_no = pg.start + i * pg.step
        if pg.type == "next_link" and i > 0 and not next_url:
            break
        page = await fetch_list_page(spec, fetcher, page_no,
                                     next_url if pg.type == "next_link" else None)
        items, root = items_from_page(recipe, page)
        new = 0
        for it in items:
            tid = _ensure_id(it)
            if tid and tid not in seen:
                seen[tid] = it
                new += 1
        if steps is not None:
            steps.append(f"List page {i + 1}: {len(items)} rows, {new} new")
        if new == 0 or len(seen) >= max_items:
            break
        if pg.type == "next_link":
            next_url = None
            if pg.nextSelector:
                if recipe.kind == "json":
                    nu = json_path(root, pg.nextSelector)
                    next_url = urljoin(page.url, str(nu)) if nu else None
                else:
                    els = select(root, pg.nextSelector)
                    href = els[0].get("href") if els else None
                    next_url = urljoin(root.base_url, href) if href else None
            if not next_url or next_url == page.url:
                break
        elif pg.type == "none":
            break
    return list(seen.values())[:max_items]


# ------------------------------------------------------------------ detail + documents

async def read_detail(recipe: Recipe, fetcher: Fetcher, item: RawItem) -> None:
    d = recipe.detail
    if not d:
        return
    url = _fill_template(d.urlTemplate, item.fields) if d.urlTemplate else item.fields.get("url")
    if not url:
        return
    url = urljoin(item.base_url, url)
    page = await fetcher.get(url, headers=d.headers or None)
    if page.status >= 400:
        raise PortalError("fetch", f"Detail page HTTP {page.status} at {url}")
    item.detail_url = page.url
    if d.kind == "json" or (page.is_json and d.kind != "html"):
        data = page.json()
        item.detail_scope, item.detail_is_json = data, True
        vals = {k: eval_spec(v.model_dump(exclude_none=True), data, is_json=True,
                             base_url=page.url) for k, v in d.fields.items()}
    else:
        doc = parse_html(page.text, page.url)
        item.detail_scope, item.detail_is_json = doc, False
        vals = {k: eval_spec(v.model_dump(exclude_none=True), doc, base_url=doc.base_url)
                for k, v in d.fields.items()}
    for k, v in vals.items():
        if v not in (None, "", []):
            if k == "id":
                # identity comes from the list; the detail page may only supply a real
                # identifier where the list had none (instead of a URL hash)
                if not item.synthetic_id:
                    continue
                v = N.clean_text(v)
                if not v:
                    continue
                item.synthetic_id = False
            item.fields[k] = v


async def doc_links(recipe: Recipe, fetcher: Fetcher, item: RawItem) -> tuple[list, str | None]:
    """Return ([(url, name)], referer)."""
    ds = recipe.documents
    if not ds:
        return [], None
    scope, is_json, base = item.scope, item.is_json, item.base_url
    if item.detail_scope is not None:
        scope, is_json, base = item.detail_scope, item.detail_is_json, item.detail_url or base
    if ds.urlTemplate:
        return [(urljoin(base, _fill_template(ds.urlTemplate, item.fields)), "")], base
    if ds.pageLink:
        link = eval_spec(ds.pageLink.model_dump(exclude_none=True), scope, is_json=is_json,
                         base_url=base)
        if isinstance(link, list):
            link = link[0] if link else None
        if link:
            page = await fetcher.get(urljoin(base, str(link)))
            if page.status < 400:
                if page.is_json:
                    scope, is_json = page.json(), True
                else:
                    scope, is_json = parse_html(page.text, page.url), False
                base = page.url
    out: list[tuple[str, str]] = []
    if is_json and ds.items:
        arr = json_path(scope, ds.items) or []
        if isinstance(arr, dict):
            arr = [arr]
        for it in arr if isinstance(arr, list) else []:
            u = json_path(it, ds.urlPath) if ds.urlPath else (it if isinstance(it, str) else None)
            n = json_path(it, ds.namePath) if ds.namePath else ""
            if u:
                out.append((urljoin(base, str(u)), str(n or "")))
    elif not is_json and ds.links:
        try:
            for a in select(scope, ds.links):
                href = a.get("href") or a.get("data-href") or a.get("data-url")
                if href and not href.startswith(("javascript:", "mailto:", "#")):
                    from .extract import el_text
                    out.append((urljoin(scope.getroottree().getroot().base_url or base, href),
                                el_text(a) or ""))
        except SpecError as e:
            raise PortalError("extract", str(e)) from e
    seen, uniq = set(), []
    for u, n in out:
        if u not in seen:
            seen.add(u)
            uniq.append((u, n))
    return uniq, base


# ------------------------------------------------------------------ normalization

def normalize_item(recipe: Recipe, f: dict) -> dict:
    tz, fm, dfirst = recipe.timezone, recipe.dateFormats, recipe.dayFirst
    url = f.get("url")
    t: dict[str, Any] = {
        "id": N.clean_text(f.get("id")),
        "url": N.http_url(url),
        "title": N.clean_text(f.get("title")),
        "buyerName": N.clean_text(f.get("buyerName")),
        "publicationDate": N.to_date(f.get("publicationDate"), fm, tz, dfirst),
        "submissionDeadline": N.to_datetime(f.get("submissionDeadline"), fm, tz, dfirst),
        "_rawStatus": N.clean_text(f.get("status")),
    }
    if t["title"] and len(t["title"]) > 1000:
        t["title"] = t["title"][:1000]
    for key in ("buyerName",):
        if N.looks_like_date_or_number(t[key]):
            t[key] = None
    opt = {
        "referenceNumber": N.clean_text(f.get("referenceNumber")),
        "description": N.clean_text(f.get("description")),
        "procedureType": N.procedure_type(f.get("procedureType")),
        "contractNature": N.contract_nature(f.get("contractNature")),
        "cpvCodes": N.cpv_codes(f.get("cpvCodes")),
    }
    if opt["description"] and len(opt["description"]) > 5000:
        opt["description"] = opt["description"][:5000]
    for k, v in opt.items():
        if v:
            t[k] = v
    pc, city = N.postal_code(f.get("postalCode")), N.clean_text(f.get("city"))
    cc = N.country_code(f.get("countryCode"))
    if city and pc and city.startswith(pc):
        city = city[len(pc):].strip(" ,") or None
    if pc or city or cc:
        t["placeOfPerformance"] = {"postalCode": pc, "city": city,
                                   "countryCode": cc or N.country_code(recipe.countryCode)}
    return t


def recipe_json(recipe: Recipe) -> dict:
    return recipe.model_dump(exclude_none=True)
