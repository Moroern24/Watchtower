"""HTTP API + dashboard on port 8080 (exact contract: PROJECT_INSTRUCTIONS.md §2)."""
from __future__ import annotations

import json
import re
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import browser
from .config import settings
from .db import DB
from . import llm, prefs
from .fetcher import PortalError, check_target
from .orchestrator import Orchestrator
from .pipeline import public_tender

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).parent / "static"

# Types a browser could execute or render as a page. Tender documents of these types are served
# as plain bytes, whatever the portal claimed.
ACTIVE_TYPES = ("html", "xhtml", "svg", "xml", "javascript", "ecmascript", "x-shockwave", "pdf+xml")
ACTIVE_EXT = (".html", ".htm", ".xhtml", ".svg", ".svgz", ".xml", ".xsl", ".js", ".mjs", ".swf")


def safe_media_type(mime: str | None, name: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if not m or any(t in m for t in ACTIVE_TYPES) or name.lower().endswith(ACTIVE_EXT):
        return "application/octet-stream"
    return m


def create_app(db: DB | None = None, orchestrator_factory=None) -> FastAPI:
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["db"] = db or DB(settings.db_path)
        state["orch"] = (orchestrator_factory or Orchestrator)(state["db"])
        await state["orch"].start()
        yield
        await state["orch"].stop()

    app = FastAPI(title="Watchtower", lifespan=lifespan)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        """CSRF guard: browsers send Origin on cross-site writes; API clients (curl, Patterno)
        send none and are unaffected. A write from another site's page is refused."""
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and origin != "null":
                if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
                    return JSONResponse({"error": "Cross-site request refused"}, status_code=403)
            elif request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "Cross-site request refused"}, status_code=403)
        return await call_next(request)

    def D() -> DB:
        return state["db"]

    def O() -> Orchestrator:
        return state["orch"]

    def portal_entry(p: dict, full: bool = False) -> dict:
        e = {"id": p["id"], "url": p["url"], "state": p["state"], "reason": p["reason"],
             "tenderCount": D().tender_count(p["id"]), "lastSuccessAt": p["last_success_at"]}
        if full:
            active = O().active_run(p["id"])
            e.update(openCount=D().open_count(p["id"]), lastReadAt=p["last_read_at"], createdAt=p["created_at"],
                     recipeVersion=p["recipe_version"], activeRunId=active,
                     nextCheckAt=prefs.next_check_at(p, prefs.get(D()), active is not None),
                     modelUsage=D().portal_cost(p["id"]), recentRuns=D().recent_runs(p["id"]))
        return e

    def get_or_404(pid: str) -> dict:
        p = D().get_portal(pid)
        if not p:
            raise HTTPException(404, "Unknown portal id")
        return p

    @app.post("/portals", status_code=201)
    async def add_portal(request: Request):
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = None
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str) or not url.strip() or " " in url.strip():
            return JSONResponse({"error": "Body must be {\"url\": \"…\"}"}, status_code=400)
        parts = urlsplit(url.strip() if "://" in url else "https://" + url.strip())
        try:
            host = parts.hostname or ""
            _ = parts.port  # raises ValueError for garbage like "javascript:alert(1)"
            host_ok = "." in host or host == "localhost" or ":" in host  # name, IPv4/IPv6
        except ValueError:
            host_ok = False
        if parts.scheme not in ("http", "https") or not host_ok:
            return JSONResponse({"error": "url must be an http(s) address with a host"},
                                status_code=400)
        try:
            await check_target(parts.geturl())     # e.g. cloud metadata addresses → 400
        except PortalError as e:
            return JSONResponse({"error": e.message}, status_code=400)
        pid, _ = O().add_portal(url)
        return {"id": pid}

    @app.get("/portals")
    def list_portals(full: bool = False):
        return [portal_entry(p, full) for p in D().list_portals()]

    @app.get("/portals/{pid}")
    def get_portal(pid: str, full: bool = False):
        return portal_entry(get_or_404(pid), full)

    @app.delete("/portals/{pid}", status_code=204)
    async def delete_portal(pid: str):
        get_or_404(pid)
        O().delete(pid)
        return Response(status_code=204)

    @app.post("/portals/{pid}/refresh", status_code=202)
    async def refresh(pid: str):
        get_or_404(pid)
        return {"runId": O().refresh(pid)}

    @app.get("/portals/{pid}/tenders")
    def tenders(pid: str, changedSince: str | None = None):
        get_or_404(pid)
        since = None
        if changedSince:
            try:
                since = datetime.fromisoformat(changedSince.replace("Z", "+00:00").replace(" ", "+"))
            except ValueError:
                return JSONResponse({"error": "changedSince must be ISO 8601"}, status_code=400)
        out = []
        for row in D().get_tenders(pid).values():
            changed = row["last_changed_at"]
            if since is not None:
                try:
                    c = datetime.fromisoformat(changed)
                    if since.tzinfo is None:
                        since = since.replace(tzinfo=c.tzinfo)
                    if c <= since:
                        continue
                except ValueError:
                    pass
            out.append(public_tender(json.loads(row["data"]), changed))
        out.sort(key=lambda t: t.get("lastChangedAt") or "", reverse=True)
        return out

    @app.get("/portals/{pid}/runs")
    def runs(pid: str, steps: bool = False):
        get_or_404(pid)
        out = []
        for r in D().list_runs(pid):
            e = {"id": r["id"], "kind": r["kind"], "startedAt": r["started_at"],
                 "finishedAt": r["finished_at"], "result": r["result"], "reason": r["reason"],
                 "tenderCount": r["tender_count"], "llmCalls": r["llm_calls"],
                 "inputTokens": r["input_tokens"], "outputTokens": r["output_tokens"],
                 "costUsd": r["cost_usd"]}
            if steps:
                e["steps"] = json.loads(r["steps"] or "[]")
            out.append(e)
        return out

    @app.get("/portals/{pid}/recipes")
    def recipes(pid: str):
        get_or_404(pid)
        return D().list_recipes(pid)

    @app.get("/files/{fid}")
    def file(fid: str):
        f = D().get_file(fid)
        path = settings.files_dir / f["sha256"] if f else None
        if not f or not path.exists():
            raise HTTPException(404, "Unknown file")
        name = (f.get("name") or fid).rsplit("/", 1)[-1]
        ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
        return FileResponse(path, media_type=safe_media_type(f["mime"], name),
                            headers={"X-Content-Type-Options": "nosniff",
                                     # a downloaded document may never run as a page on our origin
                                     "Content-Security-Policy": "default-src 'none'; sandbox",
                                     "Content-Disposition": f'attachment; filename="{ascii_name}"; '
                                                            f"filename*=UTF-8''{quote(name)}"})

    @app.get("/system")
    def system():
        portals = D().list_portals()
        cost = sum(D().portal_cost(p["id"])["costUsd"] for p in portals)
        st = llm.status()
        return {"model": st["model"], "llmProvider": st["provider"],
                "modelKeyConfigured": st["configured"], "modelProblem": st["problem"],
                "browserAvailable": browser.available(),
                "readIntervalMin": prefs.get(D())["checkIntervalMin"],
                "autoCheck": prefs.get(D())["autoCheck"], "totalModelCostUsd": round(cost, 4)}

    @app.get("/settings")
    def get_settings():
        return {**prefs.get(D()), "model": llm.status()}

    @app.put("/settings")
    async def put_settings(request: Request):
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = None
        try:
            new = prefs.update(D(), body)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {**new, "model": llm.status()}

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
        if (STATIC / "fonts" / "fonts.css").exists():   # self-hosted Inter / EB Garamond, no CDN
            html = html.replace("<!--FONTS-->", '<link rel="stylesheet" href="/static/fonts/fonts.css">', 1)
        return HTMLResponse(html, headers={"X-Content-Type-Options": "nosniff",
                                           "Referrer-Policy": "same-origin"})

    @app.get("/static/fonts/{name}")
    def font(name: str):
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.(woff2|css)", name):
            raise HTTPException(404, "Unknown file")
        path = STATIC / "fonts" / name
        if not path.is_file():
            raise HTTPException(404, "Unknown file")
        return FileResponse(path, media_type="text/css" if name.endswith(".css") else "font/woff2",
                            headers={"Cache-Control": "public, max-age=604800",
                                     "X-Content-Type-Options": "nosniff"})

    @app.exception_handler(HTTPException)
    async def http_exc(request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
