"""Download tender documents: follow HTML hops, unpack ZIPs, content-address by sha256."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from . import blockers
from .config import settings
from .db import DB
from .extract import el_text, parse_html, select
from .fetcher import Cancelled, Fetcher, PortalError, decode_bytes

log = logging.getLogger("watchtower.documents")

DOC_EXT = re.compile(r"\.(pdf|zip|docx?|xlsx?|odt|ods|rtf|txt|xml|gaeb|x8[0-9]|d8[0-9]|p8[0-9]|"
                     r"dwg|dxf|jpg|jpeg|png|tif|tiff|csv|7z|rar|pptx?|msg|eml|json|html?)$", re.I)
DL_WORDS = re.compile(r"download|herunterladen|télécharger|telecharger|descargar|downloaden|"
                      r"pobierz|scarica|unterlagen|dokument|document|dossier|pliego|anlage|"
                      r"\.pdf|\.zip|getfile|file\?|attachment|bijlage", re.I)


class DocResult:
    def __init__(self):
        self.entries: list[dict] = []
        self.blocked: str | None = None   # 'login' | 'captcha'
        self.errors: list[str] = []
        self.skipped_large = 0
        self.attempted = 0


def _filename(info: dict, url: str, hint: str | None) -> str:
    disp = info.get("disposition") or ""
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", disp, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r"filename\s*=\s*\"?([^\";]+)\"?", disp, re.I)
    if m:
        name = m.group(1).strip()
        try:
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        return name
    base = unquote(os.path.basename(urlsplit(info.get("final_url") or url).path))
    if hint and DOC_EXT.search(hint.strip()):
        return hint.strip()
    if base and "." in base:
        return base
    if hint:
        return re.sub(r"[\\/:*?\"<>|]+", "_", hint.strip())[:120]
    return base or "document"


def _store(db: DB, src: Path, sha: str, size: int, mime: str | None) -> str:
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.files_dir / sha
    if dest.exists():
        src.unlink(missing_ok=True)
    else:
        shutil.move(str(src), dest)
    return db.add_file(sha, size, mime)


def _unzip(db: DB, zpath: Path, depth: int = 0) -> list[dict]:
    out: list[dict] = []
    tmp_dir = settings.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size == 0:
                continue
            name = info.filename
            if info.flag_bits & 0x800 == 0:  # not utf-8 flagged: cp437 → try cp850/utf-8
                try:
                    name = name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            base = os.path.basename(name.replace("\\", "/"))
            if not base or base.startswith(".") or "__MACOSX" in name:
                continue
            if info.file_size > settings.max_file_bytes:
                continue
            total += info.file_size
            if total > 4 * settings.max_file_bytes:  # zip-bomb guard
                break
            h = hashlib.sha256()
            tmp = tmp_dir / f"z_{uuid.uuid4().hex}"
            size = 0
            with zf.open(info) as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > settings.max_file_bytes:
                        break
                    h.update(chunk)
                    dst.write(chunk)
            if size > settings.max_file_bytes:
                tmp.unlink(missing_ok=True)
                continue
            if depth == 0 and base.lower().endswith(".zip") and zipfile.is_zipfile(tmp):
                out.extend(_unzip(db, tmp, depth + 1))
                tmp.unlink(missing_ok=True)
                continue
            sha = h.hexdigest()
            fid = _store(db, tmp, sha, size, None)
            out.append({"fileName": base, "sizeBytes": size, "sha256": sha,
                        "path": f"/files/{fid}"})
    return out


def follow_links(html: str, base_url: str, follow_selector: str | None) -> list[tuple[str, str]]:
    doc = parse_html(html, base_url)
    links: list[tuple[str, str]] = []
    els = select(doc, follow_selector) if follow_selector else []
    if not els:
        els = [a for a in doc.iter("a") if a.get("href") and
               (DL_WORDS.search(a.get("href", "")) or DL_WORDS.search(el_text(a) or ""))]
    for a in els:
        href = a.get("href") or a.get("data-href")
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        links.append((urljoin(doc.base_url, href), el_text(a) or ""))
    m = re.search(r"<meta[^>]+http-equiv=[\"']?refresh[^>]+url=([^\"'>]+)", html, re.I)
    if m and not links:
        links.append((urljoin(base_url, m.group(1).strip()), ""))
    seen, uniq = set(), []
    for u, t in links:
        if u not in seen and u != base_url:
            seen.add(u)
            uniq.append((u, t))
    return uniq[:settings.max_docs_per_tender]


async def fetch_documents(db: DB, fetcher: Fetcher, links: list[tuple[str, str]],
                          follow_selector: str | None = None, use_cache: bool = True,
                          referer: str | None = None) -> DocResult:
    res = DocResult()
    seen_sha: set[str] = set()
    queue = [(u, n, 0) for u, n in links[: settings.max_docs_per_tender]]
    visited: set[str] = set()
    while queue:
        url, name, hop = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if use_cache:
            cached = db.doc_cache_get(url)
            if cached is not None:
                for e in cached:
                    if e["sha256"] not in seen_sha:
                        seen_sha.add(e["sha256"])
                        res.entries.append(e)
                continue
        res.attempted += 1
        try:
            info = await fetcher.download(url, settings.data_dir / "tmp", settings.max_file_bytes,
                                          headers={"Referer": referer} if referer else None)
        except Cancelled:
            raise
        except PortalError as e:
            res.errors.append(e.message)
            continue
        if info["too_large"]:
            res.skipped_large += 1
            continue
        if info["status"] >= 400 or not info["path"]:
            if info["status"] in (401, 403):
                res.blocked = res.blocked or "login"
            res.errors.append(f"HTTP {info['status']} for {url}")
            continue
        path: Path = info["path"]
        ctype = info["content_type"]
        is_html = ("html" in ctype or info["head"].lstrip()[:15].lower().startswith(
            (b"<!doctype html", b"<html"))) and not re.search(r"\.html?$", name or "", re.I)
        if is_html:
            html = decode_bytes(path.read_bytes(), ctype)
            path.unlink(missing_ok=True)
            block = blockers.classify_block(html, info["status"], info["final_url"])
            if block in ("login", "captcha"):
                res.blocked = res.blocked or block
                continue
            if hop < 2:
                for u, t in follow_links(html, info["final_url"], follow_selector):
                    queue.append((u, t, hop + 1))
            continue
        entries: list[dict]
        if zipfile.is_zipfile(path):
            try:
                entries = _unzip(db, path)
            except (zipfile.BadZipFile, OSError, RuntimeError) as e:
                res.errors.append(f"Bad zip {url}: {e}")
                entries = []
            path.unlink(missing_ok=True)
        else:
            fid = _store(db, path, info["sha256"], info["size"], ctype or None)
            entries = [{"fileName": _filename(info, url, name), "sizeBytes": info["size"],
                        "sha256": info["sha256"], "path": f"/files/{fid}"}]
        db.doc_cache_put(url, entries)
        for e in entries:
            if e["sha256"] not in seen_sha:
                seen_sha.add(e["sha256"])
                res.entries.append(e)
    return res
