"""Runs, state machine and scheduler (ARCHITECTURE.md §2, §9)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from . import prefs, validate
from .config import settings
from .db import DB, now_iso
from .fetcher import Cancelled, Fetcher, PortalError
from .learner import Learner
from .llm import LLM, Usage, has_key
from .pipeline import commit, execute, refresh_deadline_status
from .recipe import parse_recipe

log = logging.getLogger("watchtower.orchestrator")


def normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    port = p.port
    netloc = host if not port or (p.scheme, port) in (("http", 80), ("https", 443)) \
        else f"{host}:{port}"
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    frag = p.fragment if p.fragment.startswith("/") else ""
    return urlunsplit((p.scheme.lower(), netloc, path, p.query, frag))


def _older_than(iso: str | None, minutes: float) -> bool:
    if not iso:
        return True
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - t > timedelta(minutes=minutes)


class RunCtx:
    def __init__(self, db: DB, pid: str, rid: str):
        self.db, self.pid, self.rid = db, pid, rid
        self.steps: list[str] = []
        self.usage = Usage()
        self._last_flush = 0.0

    def log(self, msg: str) -> None:
        self.steps.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        if time.monotonic() - self._last_flush > 1.0:
            self.flush()

    def flush(self) -> None:
        self._last_flush = time.monotonic()
        self.db.update_run_steps(self.rid, self.steps)


class _StepSink(list):
    """list that mirrors appended steps into a RunCtx (learner/pipeline append to lists)."""

    def __init__(self, ctx: RunCtx):
        super().__init__()
        self.ctx = ctx

    def append(self, msg):  # noqa: D401
        super().append(msg)
        self.ctx.log(str(msg))


class Orchestrator:
    def __init__(self, db: DB, learner_cls: Callable = Learner, llm_available: Callable = has_key):
        self.db = db
        self.learner_cls = learner_cls
        self.llm_available = llm_available
        self.active: dict[str, tuple[str, asyncio.Task]] = {}
        self.sem = asyncio.Semaphore(settings.max_concurrent_runs)
        self._scheduler: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.db.close_open_runs()
        self._scheduler = asyncio.create_task(self._schedule_loop())

    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
        for _, task in list(self.active.values()):
            task.cancel()

    # ------------------------------------------------------------ API operations
    def add_portal(self, url: str) -> tuple[str, bool]:
        pid, created = self.db.create_portal(url.strip(), normalize_url(url))
        if created:
            self._launch(pid, "read")
        return pid, created

    def refresh(self, pid: str) -> str:
        if pid in self.active:
            return self.active[pid][0]
        return self._launch(pid, "read")

    def delete(self, pid: str) -> None:
        if pid in self.active:
            self.active[pid][1].cancel()
            self.active.pop(pid, None)
        self.db.purge_portal(pid)

    def active_run(self, pid: str) -> str | None:
        return self.active[pid][0] if pid in self.active else None

    # ------------------------------------------------------------ runs
    def _launch(self, pid: str, kind: str, problems: list[str] | None = None) -> str:
        rid = self.db.create_run(pid, kind)
        task = asyncio.create_task(self._guarded(pid, rid, kind, problems))
        self.active[pid] = (rid, task)
        return rid

    async def _guarded(self, pid: str, rid: str, kind: str, problems) -> None:
        ctx = RunCtx(self.db, pid, rid)
        try:
            async with self.sem:
                if self.db.is_deleted(pid):
                    raise Cancelled()
                if kind == "read":
                    follow = await self._read(ctx)
                else:
                    follow = await self._repair(ctx, problems or [])
        except (Cancelled, asyncio.CancelledError):
            self.db.finish_run(rid, "failed", "Cancelled (portal deleted)", ctx.steps,
                               ctx.usage.as_dict(), None)
            follow = None
        except Exception as e:  # noqa: BLE001 — never let a portal crash the service
            log.exception("run %s crashed", rid)
            ctx.log(f"Internal error: {type(e).__name__}: {e}")
            self.db.finish_run(rid, "failed", f"Internal error: {e}", ctx.steps,
                               ctx.usage.as_dict(), None)
            self._set_state(pid, "failed", f"Internal error during run: {e}")
            follow = None
        finally:
            if self.active.get(pid, (None,))[0] == rid:
                self.active.pop(pid, None)
        if follow and not self.db.is_deleted(pid):
            self._launch(pid, "repair", follow)

    def _set_state(self, pid: str, state: str, reason: str, **extra) -> None:
        if not self.db.is_deleted(pid):
            self.db.update_portal(pid, state=state, reason=reason[:500], **extra)

    def _fetcher(self, pid: str) -> Fetcher:
        return Fetcher(cancel_check=lambda: self.db.is_deleted(pid))

    async def _learn(self, ctx: RunCtx, portal: dict, fetcher: Fetcher, *, repair_of: dict | None,
                     problems: list[str]) -> dict:
        learner = self.learner_cls(self.db, fetcher, LLM(ctx.usage), _StepSink(ctx),
                                   settings.max_tenders)
        if repair_of is None:
            return await learner.learn(portal["url"])
        ids = list(self.db.get_tenders(portal["id"]).keys())[:8]
        return await learner.repair(portal["url"], repair_of, problems, ids)

    async def _read(self, ctx: RunCtx) -> list[str] | None:
        """Regular read (and the first attach). Returns problems if a repair should follow."""
        pid = ctx.pid
        portal = self.db.get_portal(pid)
        if portal is None:
            raise Cancelled()
        recipe_d = self.db.get_recipe(pid)
        attaching = recipe_d is None
        async with self._fetcher(pid) as fetcher:
            if attaching:
                self._set_state(pid, "integrating", "Learning how to read this portal")
                ctx.log(f"Attaching {portal['url']}")
                if not self.llm_available():
                    return self._fail(ctx, "Cannot learn a new portal: no model key configured "
                                      "(ANTHROPIC_API_KEY)")
                try:
                    recipe_d = await self._learn(ctx, portal, fetcher, repair_of=None, problems=[])
                except PortalError as e:
                    if e.kind == "cancelled":
                        raise Cancelled() from e
                    self.db.update_portal(pid, last_repair_at=now_iso(),
                                          hints=json.dumps({"blocked": e.kind == "blocked"}))
                    prefix = "Blocked" if e.kind == "blocked" else "Could not attach"
                    return self._fail(ctx, f"{prefix}: {e.message}")
                self.db.save_recipe(pid, recipe_d, "learn")
                self.db.update_portal(pid, last_repair_at=now_iso())
                ctx.log("Recipe learned; running the first full read")
            try:
                ok, problems, n = await self._read_with(ctx, fetcher, parse_recipe(recipe_d),
                                                        first=attaching)
            except PortalError as e:
                if e.kind == "cancelled":
                    raise Cancelled() from e
                ok, problems, n = False, [f"{e.kind}: {e.message}"], None
        self.db.update_portal(pid, last_read_at=now_iso())  # interval counts from the end
        if ok:
            self.db.finish_run(ctx.rid, "ok", None, ctx.steps, ctx.usage.as_dict(), n)
            return None
        reason = "; ".join(problems)[:400]
        self.db.finish_run(ctx.rid, "failed", reason, ctx.steps, ctx.usage.as_dict(), n)
        portal = self.db.get_portal(pid) or {}
        if portal.get("state") == "failed" and (json.loads(portal.get("hints") or "{}")
                                                .get("docsBlocked")):
            return None  # documents behind a login: not repairable by us
        if attaching or not portal.get("good_count"):
            self._set_state(pid, "failed", f"Learned recipe failed its checks: {reason}")
            return None
        self._set_state(pid, "healing", f"Read failed checks: {reason}")
        return problems

    def _fail(self, ctx: RunCtx, reason: str):
        ctx.log(reason)
        self.db.finish_run(ctx.rid, "failed", reason, ctx.steps, ctx.usage.as_dict(), None)
        self._set_state(ctx.pid, "failed", reason)
        return None

    async def _read_with(self, ctx: RunCtx, fetcher: Fetcher, recipe, first: bool,
                         commit_if_ok: bool = True, repair_mode: bool = False):
        """Execute + validate (+ commit). repair_mode: still compared with the last good read
        (count drops are failures, never auto-closing tenders), but a lost optional field is
        reported as a warning instead of blocking the repair."""
        pid = ctx.pid
        portal = self.db.get_portal(pid) or {}
        good_profile = json.loads(portal.get("good_profile") or "null")
        good_count = portal.get("good_count") or 0
        self.db.update_portal(pid, last_read_at=now_iso())
        res = await execute(self.db, pid, recipe, fetcher, _StepSink(ctx), settings.max_tenders,
                            previous=self.db.get_tenders(pid))
        tenders = res.tenders
        ok, problems, warnings = validate.check(
            tenders, None if first else good_profile, good_count, first,
            field_drift_is_warning=repair_mode)
        ctx.log(f"Read {len(tenders)} tenders; checks: "
                + ("passed" if ok else "; ".join(problems))
                + (f" (warnings: {'; '.join(warnings)})" if warnings else ""))
        if not ok:
            return False, problems, len(tenders)
        if not commit_if_ok:
            return True, [], len(tenders)
        if self.db.is_deleted(pid):
            raise Cancelled()
        stats = commit(self.db, pid, recipe, res)
        prof = validate.profile(tenders)
        n_open = sum(1 for t in self.db.tender_payloads(pid) if t.get("status") == "open")
        docs = sum(len(t.get("documents") or []) for t in tenders)
        ctx.log(f"Committed: {stats}")
        if res.doc_tenders_with_links and res.doc_blocked and res.doc_tenders_with_files == 0:
            kind = next(iter(res.doc_blocked))
            what = {"login": "require a login", "captcha": "require solving a captcha",
                    "forbidden": "are refused by the server (HTTP 403 — firewall, rate limit "
                                 "or access restriction)"}.get(kind, f"are blocked ({kind})")
            reason = (f"Tender list readable ({len(tenders)} tenders, {n_open} open) but the "
                      f"documents {what}; cannot deliver documents")
            self.db.update_portal(pid, state="failed", reason=reason,
                                  hints=json.dumps({"docsBlocked": kind}),
                                  good_count=len(tenders), good_profile=prof)
            ctx.log(reason)
            return False, [reason], len(tenders)
        extra = ""
        if res.skipped_large:
            extra += f"; {res.skipped_large} files over 250 MB skipped"
        if res.zip_truncated:
            extra += f"; {res.zip_truncated} ZIP archives only partly unpacked (size guard)"
        if warnings:
            extra += "; note: " + ", ".join(warnings)
        self.db.update_portal(pid, state="serving", hints=None,
                              reason=f"Delivering {len(tenders)} tenders ({n_open} open), "
                                     f"{docs} documents{extra}",
                              last_success_at=now_iso(), good_count=len(tenders),
                              good_profile=prof)
        return True, [], len(tenders)

    async def _repair(self, ctx: RunCtx, problems: list[str]) -> None:
        pid = ctx.pid
        portal = self.db.get_portal(pid)
        if portal is None:
            raise Cancelled()
        self._set_state(pid, "healing", "Repair running: " + "; ".join(problems)[:300])
        ctx.log("Repair started because: " + "; ".join(problems))
        current_v = portal.get("recipe_version") or 0
        async with self._fetcher(pid) as fetcher:
            # 1. retry (transient errors, half-loaded pages)
            ctx.log("Step 1: retry with the current recipe")
            await asyncio.sleep(2)
            if await self._try(ctx, fetcher, self.db.get_recipe(pid)):
                return self._repaired(ctx, "Recovered on retry (transient problem)")
            # 2. previous recipe versions
            for v in range(current_v - 1, max(0, current_v - 4), -1):
                old = self.db.get_recipe(pid, v)
                ctx.log(f"Step 2: try recipe version {v}")
                if old and await self._try(ctx, fetcher, old):
                    self.db.set_recipe_version(pid, v)
                    self.db.save_recipe(pid, old, "rollback")
                    return self._repaired(ctx, f"Recovered with earlier recipe v{v}")
            if current_v <= 1:
                ctx.log("Step 2: no earlier recipe versions to try")
            # 3. LLM repair
            if not self.llm_available():
                return self._repair_failed(ctx, "Portal changed and no model key is configured "
                                                "to re-learn it")
            hints = json.loads(portal.get("hints") or "{}")
            if hints.get("repairFailed") and not _older_than(portal.get("last_repair_at"),
                                                             prefs.get(self.db)["repairCooldownMin"]):
                return self._repair_failed(ctx, "Previous model repair failed recently; "
                                                "cooldown active, will retry later")
            ctx.log("Step 3: model repair with diagnostics")
            self.db.update_portal(pid, last_repair_at=now_iso())
            current = self.db.get_recipe(pid)
            try:
                new = await self._learn(ctx, portal, fetcher, repair_of=current,
                                        problems=problems)
            except PortalError as e:
                if e.kind == "cancelled":
                    raise Cancelled() from e
                prefix = "Blocked" if e.kind == "blocked" else "Repair failed"
                return self._repair_failed(ctx, f"{prefix}: {e.message}")
            same = json.dumps(new, sort_keys=True) == json.dumps(current, sort_keys=True)
            if same:
                ctx.log("The model found no change in how the portal is read")
            last_problems: list[str] = []
            if await self._try(ctx, fetcher, new, repair_mode=True, out_problems=last_problems):
                v = self.db.save_recipe(pid, new, "repair")
                return self._repaired(ctx, f"Re-learned the portal (recipe v{v})")
            if same:
                return self._repair_failed(
                    ctx, "The portal is read exactly as before but now shows a different picture ("
                    + "; ".join(last_problems or problems)
                    + "). Not auto-closing tenders; a person should confirm")
            return self._repair_failed(ctx, "Re-learned recipe still fails the checks: "
                                       + "; ".join(last_problems))

    async def _try(self, ctx: RunCtx, fetcher: Fetcher, recipe_d: dict | None,
                   repair_mode: bool = False, out_problems: list | None = None) -> bool:
        if not recipe_d:
            return False
        try:
            ok, problems, _ = await self._read_with(ctx, fetcher, parse_recipe(recipe_d), False,
                                                    repair_mode=repair_mode)
            if out_problems is not None:
                out_problems[:] = problems
            return ok
        except PortalError as e:
            if e.kind == "cancelled":
                raise Cancelled() from e
            ctx.log(f"Attempt failed: {e.message}")
            return False

    def _repaired(self, ctx: RunCtx, msg: str) -> None:
        ctx.log(msg)
        self.db.finish_run(ctx.rid, "ok", msg, ctx.steps, ctx.usage.as_dict(),
                           self.db.tender_count(ctx.pid))

    def _repair_failed(self, ctx: RunCtx, msg: str) -> None:
        ctx.log(msg)
        self.db.finish_run(ctx.rid, "failed", msg, ctx.steps, ctx.usage.as_dict(), None)
        n = self.db.tender_count(ctx.pid)
        self._set_state(ctx.pid, "failed",
                        f"{msg}. Still holding {n} tenders from the last correct read."
                        if n else msg, hints=json.dumps({"repairFailed": True}))

    # ------------------------------------------------------------ scheduler
    async def _schedule_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(settings.scheduler_tick_sec)

    async def _tick(self) -> None:
        cfg = prefs.get(self.db)                 # live: changes apply on the next tick
        interval, auto = cfg["checkIntervalMin"], cfg["autoCheck"]
        for p in self.db.list_portals():
            pid = p["id"]
            refresh_deadline_status(self.db, pid)
            if pid in self.active:
                continue
            hints = json.loads(p.get("hints") or "{}")
            if p["state"] == "integrating" or (p["state"] == "healing"):
                self._launch(pid, "read")  # unfinished attach/repair, e.g. after a restart
            elif not auto:
                continue                   # automatic checks paused by the operator
            elif p["state"] == "serving" and _older_than(p["last_read_at"], interval):
                self._launch(pid, "read")
            elif p["state"] == "failed" and not hints.get("blocked") and \
                    _older_than(p["last_read_at"], interval * 2) and \
                    (p.get("recipe_version") or _older_than(p.get("last_repair_at"),
                                                            cfg["repairCooldownMin"])):
                self._launch(pid, "read")
