# CHANGELOG

## [0.1.0] — 2026-11-20
- Planning: PROJECT_INSTRUCTIONS.md, ARCHITECTURE.md, DECISIONS.md, TASKS.md.

## [0.2.0] — 2026-11-20
- Implemented: rate-limited fetcher, Recipe DSL + executor, normalizers (dates/tz/CPV/enums/status),
  documents (multi-step hops, ZIP unpack, 250 MB cap, sha256 store), validation/drift,
  orchestrator (states, runs, repair ladder, scheduler, delete/cancel), API, dashboard,
  LLM learner (discover → list → detail → doc page → verify; repair with id continuity),
  Playwright XHR capture for JS apps, mock portals, Dockerfile, README.
- Fixed: per-host lock leaked when a request was cancelled during its politeness wait.
- Tests: 26 passing (no API key needed).

## [0.5.0] — 2026-11-20 — master–detail dashboard
- Dashboard redesigned after Better Stack / Uptime Kuma / Vercel status and Temporal run views:
  header verdict + compact add form, 5-card state strip (filters), master list with per-portal
  recent-runs strip, sticky detail panel with Runs timeline and Tenders tabs; auto-opens the most
  urgent portal; state colours scoped to badges; phone and tablet layouts.
- API: `GET /portals?full=true` entries carry `recentRuns` (dashboard only; the contract fields of
  `GET /portals` are unchanged).

## [0.4.0] — 2026-11-20 — operator dashboard
- Rebuilt `GET /`: state KPIs that filter, state legend, add-portal feedback (201 / existing /
  400), state chips + search, per-row Refresh/Details/Delete, detail panel with Runs, Tenders
  (status filter, portal-local deadlines, file sizes) and Recipe tabs; 404 and API-down
  handling; no DOM churn on auto-refresh when nothing changed; phone layout.
- Test: dashboard is served as one self-contained page wired to the API.

## [0.3.0] — 2026-11-20 — fixes from operator pass #1
- C1: a repaired recipe is checked against the last good read like any regular read. A sharp
  drop in tenders is never accepted by a repair; if the model returns the unchanged recipe the
  portal goes `failed` ("not auto-closing tenders") and keeps its open tenders.
  Also fixed: the model-repair cooldown never applied (state was already `healing`).
- C2: a login box or captcha widget on a download page no longer blocks documents; only a
  redirect to a login URL, or a login/captcha page with no file links, counts as a wall.
  Captcha detection no longer fires on a bare "/captcha" substring.
- C3: DELETE purges the portal with its recipes, runs and tenders; adding the URL again
  attaches from scratch with a new id.
- M1: optional fields (place of performance, procedure, nature, …) are part of the drift
  profile; after a repair a lost optional field is reported as a warning in the reason.
- M2: when the list has no identifier, the detail page's identifier is used; URL-hash ids
  remain only as a fallback and are reported as a warning.
- M4: "procédure adaptée" (MAPA) and similar national procedures map to `other`, not `open`.
- M5/M6: ZIP members over the limit and the archive size guard are counted and reported;
  HTTP 403 on documents is reported as "refused by the server", not as a login.
- M7: POST /portals answers 400 for anything that is not an http(s) URL with a host.
- M8: prompt-cache writes are costed at 1.25× input.
- Minor: file downloads carry `Content-Disposition` with the real name; ZIP entries keep their
  folder path; identical recipes are not saved as new versions; dashboard shows open/held and
  lists open tenders first; run log reports detail/document progress and skipped steps; read
  interval counts from the end of a read.
- Tests: 34 passing.
