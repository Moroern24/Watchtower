"""Polite async HTTP: at most one request per MIN_REQUEST_INTERVAL per host, for every
kind of traffic (pages, APIs, downloads). Supports cancellation on portal deletion."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .config import settings

log = logging.getLogger("watchtower.fetcher")


class PortalError(Exception):
    """kind: blocked | fetch | extract | validate | llm | cancelled"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class Cancelled(PortalError):
    def __init__(self):
        super().__init__("cancelled", "Portal was deleted")


@dataclass
class Page:
    url: str
    status: int
    headers: dict
    content: bytes
    elapsed: float = 0.0
    history: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").lower()

    @property
    def is_json(self) -> bool:
        if "json" in self.content_type:
            return True
        head = self.content[:200].lstrip()
        return head[:1] in (b"{", b"[") and "html" not in self.content_type

    @property
    def text(self) -> str:
        return decode_bytes(self.content, self.content_type)

    def json(self):
        import json
        return json.loads(self.text)


def decode_bytes(content: bytes, content_type: str = "") -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "")
    enc = m.group(1) if m else None
    if not enc:
        m2 = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", content[:4096], re.I)
        enc = m2.group(1).decode("ascii", "ignore") if m2 else None
    for e in [enc, "utf-8", "cp1252"]:
        if not e:
            continue
        try:
            return content.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


class HostLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def _key(self, url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    async def wait(self, url: str) -> asyncio.Lock:
        key = self._key(url)
        # locks are bound to an event loop; keep one per (loop, host)
        lock_key = f"{id(asyncio.get_running_loop())}|{key}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        await lock.acquire()
        try:
            delay = self._last.get(key, 0.0) + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        except BaseException:  # cancelled while waiting: never leave the host locked
            lock.release()
            raise
        return lock

    def done(self, url: str, lock: asyncio.Lock) -> None:
        self._last[self._key(url)] = time.monotonic()
        lock.release()


limiter = HostLimiter(settings.min_request_interval)


def _check_ssrf(url: str) -> None:
    if not settings.block_private_networks:
        return
    host = urlsplit(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise PortalError("fetch", f"Cannot resolve host {host}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise PortalError("fetch", f"Blocked private address {ip} for {host}")


class Fetcher:
    """One per run. `cancel_check()` returning True aborts before the next request."""

    def __init__(self, cancel_check: Callable[[], bool] | None = None,
                 client: httpx.AsyncClient | None = None):
        self.cancel_check = cancel_check or (lambda: False)
        self._own = client is None
        self.client = client or httpx.AsyncClient(
            follow_redirects=True, timeout=settings.http_timeout,
            headers={"User-Agent": settings.user_agent,
                     "Accept-Language": "de,en;q=0.8,fr;q=0.6,es;q=0.5,nl;q=0.5,pl;q=0.5"},
            verify=True)
        self.requests = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.close()

    async def close(self):
        if self._own:
            await self.client.aclose()

    def _guard(self, url: str) -> None:
        if self.cancel_check():
            raise Cancelled()
        if not url.startswith(("http://", "https://")):
            raise PortalError("fetch", f"Unsupported URL {url!r}")
        _check_ssrf(url)

    async def get(self, url: str, *, method: str = "GET", params: dict | None = None,
                  data=None, json_body=None, headers: dict | None = None,
                  max_bytes: int = 15 * 1024 * 1024) -> Page:
        self._guard(url)
        lock = await limiter.wait(url)
        t0 = time.monotonic()
        try:
            self.requests += 1
            req = self.client.build_request(method, url, params=params or None, data=data,
                                            json=json_body, headers=headers or None)
            resp = await self.client.send(req, stream=True)
            try:
                chunks, total = [], 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
            finally:
                await resp.aclose()
            return Page(url=str(resp.url), status=resp.status_code,
                        headers={k.lower(): v for k, v in resp.headers.items()},
                        content=body, elapsed=time.monotonic() - t0,
                        history=[str(h.url) for h in resp.history])
        except httpx.HTTPError as e:
            raise PortalError("fetch", f"{type(e).__name__} fetching {url}: {e}") from e
        finally:
            limiter.done(url, lock)

    async def download(self, url: str, dest_dir: Path, max_bytes: int,
                       headers: dict | None = None) -> dict:
        """Stream to a temp file. Returns dict(path, sha256, size, content_type,
        filename_hint, final_url, too_large, status, head(bytes))."""
        self._guard(url)
        lock = await limiter.wait(url)
        try:
            self.requests += 1
            async with self.client.stream("GET", url, headers=headers or None) as resp:
                info = {"status": resp.status_code, "final_url": str(resp.url),
                        "content_type": resp.headers.get("content-type", "").lower(),
                        "disposition": resp.headers.get("content-disposition", ""),
                        "too_large": False, "path": None, "sha256": None, "size": 0,
                        "head": b""}
                clen = resp.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    info["too_large"] = True
                    info["size"] = int(clen)
                    return info
                if resp.status_code >= 400:
                    return info
                dest_dir.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                fd = tempfile.NamedTemporaryFile(dir=dest_dir, delete=False, suffix=".part")
                size = 0
                try:
                    with fd:
                        async for chunk in resp.aiter_bytes(1 << 16):
                            if self.cancel_check():
                                raise Cancelled()
                            size += len(chunk)
                            if size > max_bytes:
                                info["too_large"] = True
                                break
                            if len(info["head"]) < 4096:
                                info["head"] += chunk[: 4096 - len(info["head"])]
                            h.update(chunk)
                            fd.write(chunk)
                except BaseException:
                    Path(fd.name).unlink(missing_ok=True)
                    raise
                if info["too_large"]:
                    Path(fd.name).unlink(missing_ok=True)
                    info["size"] = size
                    return info
                info.update(path=Path(fd.name), sha256=h.hexdigest(), size=size)
                return info
        except httpx.HTTPError as e:
            raise PortalError("fetch", f"{type(e).__name__} downloading {url}: {e}") from e
        finally:
            limiter.done(url, lock)
