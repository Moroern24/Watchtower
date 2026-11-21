# CHANGELOG

## Unreleased
- Configure Gemini through the provider-agnostic adapter with `GEMINI_API_KEY` and `LLM_MODEL`.
- Install libatomic1 before Playwright so the linux/amd64 Docker image builds and Chromium starts.
- Preserve the last good tender snapshot when document access is blocked, including during repair.
- Commit validated tender snapshots atomically, rolling back all changes on a storage error.
- Keep the French mock portal's deadline format intact on Windows.

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

## [0.10.0] — 2026-11-21 — security hardening
- Script links: only absolute http(s) tender URLs are kept and rendered as links.
- SSRF: cloud metadata and link-local addresses always refused, every redirect hop and the learning
  browser checked, `POST /portals` answers 400 for such targets.
- CSRF: cross-site writes (foreign `Origin` / `Sec-Fetch-Site: cross-site`) refused with 403.
- Documents: sandboxed downloads (`CSP: sandbox`); HTML/SVG/XML/JS served as octet-stream.
- Recipes: regexes length-capped, nested quantifiers rejected, input capped at 20k characters.
- Dashboard: no Google Fonts; self-hosted fonts from `watchtower/static/fonts/` when present.
- README → Security (measures and known limits); ADR-015; `tests/test_security.py` (35 tests).

## [0.9.0] — 2026-11-20 — operator settings
- Settings dialog: theme System / Dark / Light; automatic checks on/off with interval in minutes
  (presets 5/15/30/60); wait after a failed model repair; read-only model status.
- Runtime settings persisted in SQLite (`settings` table), validated, applied live by the scheduler;
  `GET`/`PUT /settings`; `nextCheckAt` per portal; footer and header show auto-check state.
- Fixed: the model-repair cooldown now reads the live setting.
- Tests: settings validation; scheduler pauses and resumes with the live setting.

## [0.8.0] — 2026-11-20 — any model provider
- Model access is provider-agnostic: Anthropic via SDK, plus one OpenAI-compatible adapter for
  OpenAI, Gemini, Groq, OpenRouter, DeepSeek, Mistral or any custom endpoint (e.g. Ollama).
  Provider picked from the key that is set; `LLM_MODEL`, `LLM_PROVIDER`, `LLM_BASE_URL`,
  `LLM_API_KEY`, `LLM_MAX_OUTPUT_TOKENS`, `PROMPT_PAGE_CHARS`.
- JSON mode with fallback, back-off on 429/5xx, `max_completion_tokens` fallback, clear errors.
- `.env` file support for local keys (git-ignored). `/system` reports provider, model and problem;
  the dashboard shows which model is in use or why none is.
- Tests: adapter against a fake OpenAI-compatible server.
- Dashboard header shows the Patterno mark and wordmark ("Patterno | Watchtower"); the mark is
  also the favicon and the page title is "Patterno Watchtower".

## [0.7.0] — 2026-11-20 — calmer dashboard
- Decluttered: one verdict in the header, state cards show only label + count, list grouped into
  Needs attention / Attaching / Working with reasons only for problems, row actions on hover,
  detail as callout + three facts + plain-language run history (logs on click), system info moved
  to the footer, "No model key" as a single tag.

## [0.6.0] — 2026-11-20 — Patterno look
- Dashboard restyled in Patterno's visual language (taken from patterno.de): navy `#0a0a1a` with
  indigo `#6366f1` accent, Inter + EB Garamond wordmark, pill header and buttons, uppercase micro
  labels, light-weight numerals; light theme from Patterno's light tokens with a remembered toggle.
  Layout and behaviour unchanged.

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
