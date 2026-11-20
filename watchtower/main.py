"""HTTP API + dashboard on port 8080 (exact contract: PROJECT_INSTRUCTIONS.md §2)."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import browser
from .config import settings
from .db import DB
from .llm import has_key
from .orchestrator import Orchestrator
from .pipeline import public_tender

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).parent / "static"


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

    def D() -> DB:
        return state["db"]

    def O() -> Orchestrator:
        return state["orch"]

    def portal_entry(p: dict, full: bool = False) -> dict:
        e = {"id": p["id"], "url": p["url"], "state": p["state"], "reason": p["reason"],
             "tenderCount": D().tender_count(p["id"]), "lastSuccessAt": p["last_success_at"]}
        if full:
            e.update(lastReadAt=p["last_read_at"], createdAt=p["created_at"],
                     recipeVersion=p["recipe_version"], activeRunId=O().active_run(p["id"]),
                     modelUsage=D().portal_cost(p["id"]))
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
        return FileResponse(path, media_type=f["mime"] or "application/octet-stream",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/system")
    def system():
        portals = D().list_portals()
        cost = sum(D().portal_cost(p["id"])["costUsd"] for p in portals)
        return {"model": settings.model, "modelKeyConfigured": has_key(),
                "browserAvailable": browser.available(),
                "readIntervalMin": settings.read_interval_min, "totalModelCostUsd": round(cost, 4)}

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse((STATIC / "dashboard.html").read_text(encoding="utf-8"))

    @app.exception_handler(HTTPException)
    async def http_exc(request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
