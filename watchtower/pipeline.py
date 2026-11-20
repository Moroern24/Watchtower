"""One deterministic read of a portal with a recipe → normalized tenders (+ documents),
and the commit of a validated snapshot. No LLM calls."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import normalize as N
from .db import DB, now_iso
from .documents import fetch_documents
from .fetcher import Cancelled, Fetcher, PortalError
from .recipe import Recipe, RawItem, doc_links, normalize_item, read_detail, read_list, stable_hash

log = logging.getLogger("watchtower.pipeline")


@dataclass
class ReadResult:
    tenders: list[dict] = field(default_factory=list)
    list_hashes: dict[str, str] = field(default_factory=dict)
    raw_items: list[RawItem] = field(default_factory=list)
    detail_errors: int = 0
    doc_blocked: dict[str, int] = field(default_factory=dict)
    doc_tenders_with_links: int = 0
    doc_tenders_with_files: int = 0
    skipped_large: int = 0


async def execute(db: DB, pid: str | None, recipe: Recipe, fetcher: Fetcher, steps: list[str],
                  max_items: int, with_documents: bool = True,
                  previous: dict[str, dict] | None = None) -> ReadResult:
    res = ReadResult()
    items = await read_list(recipe, fetcher, max_items, steps)
    res.raw_items = items
    previous = previous or {}
    reused = 0
    for item in items:
        tid = item.fields["id"]
        prev = previous.get(tid)
        res.list_hashes[tid] = item.list_hash
        if prev and prev.get("list_hash") == item.list_hash and prev.get("data"):
            data = json.loads(prev["data"]) if isinstance(prev["data"], str) else prev["data"]
            if data.get("_complete"):
                res.tenders.append(data)
                reused += 1
                continue
        try:
            await read_detail(recipe, fetcher, item)
        except Cancelled:
            raise
        except PortalError as e:
            res.detail_errors += 1
            steps.append(f"Detail failed for {tid}: {e.message}")
        t = normalize_item(recipe, item.fields)
        t["documents"] = []
        complete = True
        if with_documents:
            try:
                links, referer = await doc_links(recipe, fetcher, item)
            except Cancelled:
                raise
            except PortalError as e:
                links, referer = [], None
                steps.append(f"Document links failed for {tid}: {e.message}")
                complete = False
            if links:
                res.doc_tenders_with_links += 1
                dr = await fetch_documents(db, fetcher, links,
                                           recipe.documents.followSelector if recipe.documents
                                           else None, referer=referer)
                t["documents"] = dr.entries
                res.skipped_large += dr.skipped_large
                if dr.entries:
                    res.doc_tenders_with_files += 1
                if dr.blocked:
                    res.doc_blocked[dr.blocked] = res.doc_blocked.get(dr.blocked, 0) + 1
                    complete = False
                if dr.errors and not dr.entries:
                    complete = False
        t["_complete"] = complete and res.detail_errors == 0
        res.tenders.append(t)
    if reused:
        steps.append(f"{reused} unchanged tenders reused without re-fetching")
    return res


def public_tender(data: dict, last_changed_at: str) -> dict:
    out = {k: v for k, v in data.items() if not k.startswith("_")}
    for req in ("id", "url", "title", "buyerName", "publicationDate", "submissionDeadline",
                "status", "documents"):
        out.setdefault(req, [] if req == "documents" else None)
    out["lastChangedAt"] = last_changed_at
    return out


def content_hash(t: dict) -> str:
    return stable_hash({k: v for k, v in t.items()
                        if not k.startswith("_") and k != "lastChangedAt"})


def commit(db: DB, pid: str, recipe: Recipe, result: ReadResult) -> dict:
    """Write a validated snapshot. Returns counts."""
    now = now_iso()
    existing = db.get_tenders(pid)
    seen = set()
    new = changed = closed = 0
    for t in result.tenders:
        tid = t["id"]
        seen.add(tid)
        t = {k: v for k, v in t.items() if k != "lastChangedAt"}
        t["status"] = N.tender_status(t.get("_rawStatus"), t.get("submissionDeadline"),
                                      True, recipe.statusKeywords)
        h = content_hash(t)
        prev = existing.get(tid)
        if prev is None:
            new += 1
            changed_at = now
        elif prev["hash"] != h:
            changed += 1
            changed_at = now
        else:
            changed_at = prev["last_changed_at"]
        db.upsert_tender(pid, tid, {**t, "lastChangedAt": changed_at}, h,
                         result.list_hashes.get(tid, ""), changed_at, now, True)
    for tid, prev in existing.items():
        if tid in seen:
            continue
        data = json.loads(prev["data"])
        if data.get("status") != "closed" and data.get("status") != "withdrawn":
            data["status"] = "closed"
            data["lastChangedAt"] = now
            closed += 1
            db.upsert_tender(pid, tid, data, content_hash(data), prev["list_hash"] or "", now,
                             prev["last_seen_at"], False)
    return {"new": new, "changed": changed, "closedUnlisted": closed}


def refresh_deadline_status(db: DB, pid: str) -> int:
    """Close tenders whose deadline passed since the last read (cheap, no network)."""
    n = 0
    now = now_iso()
    for tid, row in db.get_tenders(pid).items():
        data = json.loads(row["data"])
        if data.get("status") == "open" and N.deadline_passed_grace(
                data.get("submissionDeadline")):
            data["status"] = "closed"
            data["lastChangedAt"] = now
            db.upsert_tender(pid, tid, data, content_hash(data), row["list_hash"] or "", now,
                             row["last_seen_at"], bool(row["listed"]))
            n += 1
    return n
