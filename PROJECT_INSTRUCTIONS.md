# PROJECT_INSTRUCTIONS.md — Watchtower

The main instructions for every AI agent and human working on this repo. Read this file in full before you change anything.
Related docs: `ARCHITECTURE.md` (components, data model, Recipe DSL), `TASKS.md` (task status), `DECISIONS.md` (ADR log), `CHANGELOG.md`.

---

## 1. Project Overview

**Name:** Watchtower (Patterno hackathon track "Build the Watchtower").

**Idea in plain words:** Governments publish contracts ("tenders") on thousands of separate websites ("portals"). Today a person at Patterno has to connect each portal by hand. When a portal changes its layout, its tenders can quietly stop arriving. Watchtower is a robot that:
1. **Attaches.** Given only a portal's web address, it works out on its own how to read the portal. It then delivers every open tender and the tender's documents.
2. **Notices.** It checks every read. It reports honestly when a portal stops delivering correctly, and when it cannot be fixed.
3. **Repairs.** It re-learns how to read a portal after the portal changes.

Nobody may help it while it runs.

**Goal:** win the challenge, which means correct delivery on portals the system has never seen, and production readiness.

**Users:**
- Patterno's ingestion pipeline uses the JSON API.
- An operator uses the dashboard every morning.
- Hackathon judges run the container against portals it has never seen.

**Main functions:**
- Add, list, delete and refresh portals through the REST API.
- An LLM learns a declarative **Recipe** per portal.
- Deterministic recipe reads use no LLM.
- Validation and drift detection after every read.
- Automatic repair.
- Document download with ZIP unpacking.
- Dashboard.

## 2. Product Requirements

### Functional (API contract — exact, do not deviate)
- `POST /portals {url}` returns **201** `{id}` immediately, then attaches in the background. The same URL (normalized) returns the same id.
- `GET /portals` returns **200** with a list of `{id,url,state,reason,tenderCount,lastSuccessAt|null}`. `GET /portals/{id}` returns one entry, or **404**.
- `DELETE /portals/{id}` returns **204**. All calls to that portal stop within 60 s: cancel its tasks and check `deleted` before each request.
- `POST /portals/{id}/refresh` returns **202** `{runId}`. If a run is already active, return its runId.
- `GET /portals/{id}/tenders[?changedSince=ISO]` returns **200** with a list of tenders. Only those with `lastChangedAt > changedSince`.
- `GET /portals/{id}/runs` returns **200** with a list of `{id,kind:read|repair,startedAt,finishedAt|null,result:ok|failed|null}`. The first attach is a `read`. We add extra fields `reason`, `steps`, `llmCalls`, `inputTokens`, `outputTokens`, `costUsd`, `tenderCount`; extra fields are allowed.
- `GET /files/{fileId}` returns the document bytes. `GET /` returns the dashboard.
- Every time is ISO 8601 with offset, for example `2026-12-09T10:00:00+01:00`. Unknown id returns 404. JSON everywhere except the dashboard and files.

**States:**
- `integrating`: attaching, nothing delivered yet.
- `serving`: delivering correctly.
- `healing`: broken, repair running; keep serving the last good tenders.
- `failed`: needs a person, with `reason`.
- Login or captcha that blocks delivery → `failed`, and `reason` says so.

**Tender fields:**
- Required: `id`, `url`, `title`, `buyerName`, `publicationDate` (YYYY-MM-DD), `submissionDeadline` (datetime with offset), `status` (open|closed|withdrawn), `documents` [{fileName,sizeBytes,sha256,path}], `lastChangedAt`. Send `null` when the portal does not show a value.
- Optional, included only if shown: `referenceNumber`, `description`, `procedureType` (open|restricted|negotiated|other), `contractNature` (works|services|supplies), `cpvCodes` (8 digits, no check digit), `placeOfPerformance` {postalCode,city,countryCode}.
- **A wrong value is worse than a missing one.** When in doubt, send null or omit the field.

**Status rules:**
- `closed` when the deadline has passed, or the tender has disappeared from the list.
- `withdrawn` when the portal marks it cancelled, detected by a keyword map.

**Documents:**
- Unpack ZIPs; list the inner files, not the zip.
- Skip any file over 250 MB, ZIP included.
- Deduplicate by sha256.

### Non-functional
- **Politeness:** at most **1 request per second per host**. This covers all traffic: list pages, detail pages, downloads and the browser. No logins, no captcha solving.
- **Regular reads make zero LLM calls.** The LLM is used only to learn, verify and repair.
- **Docker:**
  - One container on port 8080.
  - `docker build .` works on linux/amd64 and builds reasonably fast.
  - Model keys come from env vars.
- **Persistence:** SQLite plus a file store under `DATA_DIR` (default `/data`).
- **Robustness:** never crash the API because of one portal. All portal errors are caught, logged into the run, and turned into a state.

### User scenarios
1. Judge posts 10 URLs.
   - Each goes `integrating` → `serving` with its tenders.
   - Some go `failed` with a reason such as "login required".
2. A portal changes its layout.
   - The next read fails validation and the portal goes `healing`. Old tenders are still served.
   - A repair run learns recipe v2 and the portal goes back to `serving`.
   - If repair fails, the portal goes to `failed` with a reason.
3. The operator opens `/` and sees, for each portal, its state, reason, counts, last success, runs, and LLM cost.

### Constraints
- 24 h hackathon; no logins or captcha solving; politeness limits; the LLM can be wrong, so always validate.

## 3. Technology Stack

| Layer | Technology | Why |
|---|---|---|
| Language | Python 3.12 | Best scraping ecosystem, official Anthropic SDK, fast to write |
| API server | FastAPI + uvicorn | Async, exact status codes, small |
| HTTP client | httpx (async) | HTTP/1.1+2, streaming downloads, cookies |
| HTML parsing | lxml + cssselect | Fast, CSS and XPath, tolerant of broken HTML |
| Dates | python-dateutil + zoneinfo/tzdata | Local time → offset; no LLM in the read path |
| LLM | Provider-agnostic adapter (Anthropic SDK or OpenAI-compatible Gemini API) | JSON output for recipes; used only at learn and repair time |
| JS portals | Playwright + Chromium (**optional** at runtime) | Captures the XHR/JSON behind single-page apps during learning. Regular reads hit the JSON API directly |
| Storage | SQLite (WAL) + content-addressed files | No external services, a single container |
| Dashboard | One static HTML file + vanilla JS polling the API | No build step |
| Tests | pytest + local mock-portal FastAPI app | Reproducible portals including the traps; no network |
| Deployment | Dockerfile (python:3.12-slim + playwright chromium) | Single image, port 8080 |

**Required:** everything except Playwright. Without Playwright the system still works for server-rendered and discoverable-JSON portals, and SPA learning degrades gracefully.

## 4. System Architecture (summary — full detail in ARCHITECTURE.md)

```
            ┌─────────── FastAPI (main.py) ────────────┐
 client ──► │ /portals  /runs  /tenders  /files  /     │
            └──────┬────────────────────────▲──────────┘
                   │ enqueue run            │ read state
            ┌──────▼──────────┐       ┌─────┴──────┐
            │ Orchestrator    │◄─────►│  Store     │ SQLite + files
            │ (scheduler.py)  │       └────────────┘
            └──┬────────┬─────┘
     learn/repair      regular read
   ┌───────▼─────┐  ┌──▼────────────┐
   │ Learner     │  │ Recipe        │──► Fetcher (per-host 1 rps)
   │ (LLM)       │──►│ Executor      │──► Documents (download/unzip)
   └─────────────┘  └──┬────────────┘
                       ▼
                   Validator (fill rates, invariants, drift)
```

Recipe = a declarative JSON spec that says where the list is, how to paginate, how to pick each field, where the documents are, and the date format and timezone. Learned by the LLM, executed deterministically, and versioned.

## 5. Project Structure

```
Dockerfile               # image; CMD runs uvicorn on 8080
requirements.txt
README.md                # one-page how-it-works, env vars, cost per portal
watchtower/
  main.py                # FastAPI routes + startup/shutdown of the orchestrator
  config.py              # env-var settings
  db.py                  # SQLite schema + data-access functions
  fetcher.py             # rate-limited async HTTP client (per host), size limits
  blockers.py            # captcha/login detection
  compact.py             # HTML → compact text/skeleton for LLM prompts
  recipe.py              # Recipe DSL (pydantic) + executor
  extract.py             # field-spec evaluation for HTML (css/label/regex) and JSON paths
  normalize.py           # dates/tz, CPV, enums, country, status
  validate.py            # validation, invariants, drift, fingerprint
  documents.py           # streaming download, sha256, safe unzip, file store
  llm.py                 # Anthropic wrapper, JSON output, usage/cost accounting
  learner.py             # discover → learn → verify → repair (LLM workflows)
  browser.py             # optional Playwright capture of XHR JSON
  orchestrator.py        # run queue, state machine, periodic scheduler
  static/dashboard.html  # operator dashboard
mockportals/server.py    # local test portals (SSR, SPA/JSON, multi-step, captcha, login, layout switch)
tests/                   # pytest suites
```

## 6. Agent Roles & Responsibilities

| Agent | Tasks | Skills / tools | Owns files | Delivers |
|---|---|---|---|---|
| Lead / Architect (main session) | Planning, docs, integration, reviews | Plan agent, `code-review`, `simplify` | `*.md`, `main.py`, `orchestrator.py` | Working integrated system |
| Extraction Engineer | Recipe DSL, executor, normalizers | `run`, pytest | `recipe.py`, `extract.py`, `normalize.py` | Deterministic reads + tests |
| LLM Engineer | Prompts, learner, repair, cost | Provider-agnostic adapter | `llm.py`, `learner.py`, `compact.py`, `browser.py` | Recipe learning with JSON output |
| Reliability Engineer | Fetcher, rate limits, documents, validation | pytest | `fetcher.py`, `documents.py`, `validate.py`, `blockers.py` | Polite, safe I/O; notice logic |
| QA Engineer | Mock portals, contract tests | pytest, `run` | `mockportals/`, `tests/` | Green test suite |
| UX Engineer | Dashboard | `built-in-browser` for checks | `static/dashboard.html` | Clear operator view |
| Security reviewer | SSRF, unzip, XSS, prompt injection | `security-review` | review only | Findings |

Rules for agents:
- Only edit the files you own. Coordinate interface changes through this file and `ARCHITECTURE.md`.
- Never edit the same file in parallel.

## 7. Development Workflow
- Order of work follows §9. Run the tests (`pytest -q`) before marking a task done in `TASKS.md`.
- **Git:**
  - Small commits with imperative messages (`Add recipe executor pagination`).
  - Never commit secrets or `data/`.
- **Code style:**
  - PEP 8, type hints, `from __future__ import annotations`.
  - Functions under ~60 lines.
  - Modules are snake_case, classes are PascalCase, constants are UPPER_CASE.
  - JSON API field names are camelCase, exactly as in the spec.
- **Errors:**
  - Portal-level errors raise `PortalError(kind, message)`. Kinds: `blocked`, `fetch`, `extract`, `validate`, `llm`.
  - The orchestrator turns them into run results and portal state.
  - Never let an exception escape a background task.
- **Logging:**
  - Use `logging` with logger names per module.
  - Each run keeps a human-readable `steps` log, which is shown on the dashboard.
  - Never log API keys.
- **Docs:** update `CHANGELOG.md` per iteration, `DECISIONS.md` for every architectural choice, and `TASKS.md` statuses.

## 8. Quality Standards
- **Tests:**
  - Unit tests for normalizers and extract.
  - Integration tests that run the recipe executor against the mock portals.
  - API contract tests (status codes, idempotency, refresh runId, delete).
  - The LLM is mocked in tests through recorded or hand-written recipes. Live Gemini calls require `GEMINI_API_KEY`.
- **Security:**
  - Portal content is untrusted. LLM output must validate against the Recipe schema.
  - Never execute generated code.
  - Safe unzip: path traversal, size and ratio limits.
  - Files are served by opaque id.
  - The dashboard escapes all strings.
  - Optional SSRF guard via `BLOCK_PRIVATE_NETWORKS=1`.
- **Performance:**
  - Regular reads cost 0 LLM calls.
  - Detail pages and documents are fetched only for new or changed tenders. Unchanged documents are not re-downloaded.
- **Definition of Done:**
  - Code implemented and tests green.
  - Behaviour matches the §2 contract.
  - Docs updated.
  - No secrets in the repo.

## 9. Implementation Roadmap
See `TASKS.md` for live status. Phases:
1. **Foundations:** config, db, fetcher, normalize, extract, recipe executor (+ tests).
2. **Mock portals:** SSR, SPA/JSON, multi-step docs, captcha-but-docs-open, login, layout switch, withdrawn/disappearing tender.
3. **API + orchestrator:** state machine, runs, scheduler, delete/cancel, documents.
4. **LLM learner:** discover, learn, verify, repair; cost accounting; browser capture.
5. **Validation and notice:** fill rates, invariants, drift and fingerprint; previous-recipe rollback before an LLM repair.
6. **Dashboard.**
7. **Docker + README + end-to-end check.**

## 10. Important Decisions (details in DECISIONS.md)
1. A declarative Recipe DSL instead of LLM-generated code. It is safe, auditable, diffable and cheap. No code execution.
2. The LLM is used only for learn, verify and repair. Regular reads are deterministic.
3. All field normalization is deterministic code. The LLM only says where a value is and in which format.
4. SQLite plus a local file store, with a single container and no external services.
5. Playwright is used only for learning on JS apps, and is optional.
6. Healing serves the last good snapshot. A new snapshot replaces the old one only after validation passes.
7. Repair order: (a) retry; (b) re-validate with the previous recipe versions; (c) LLM repair with diagnostics; (d) LLM fresh learn; then fail.
