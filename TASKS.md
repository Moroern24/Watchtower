# TASKS.md

Status: ☐ todo · ◐ in progress · ☑ done (tests green)

| ID | Task | Pri | Depends | Agent | Done when | Status |
|---|---|---|---|---|---|---|
| T01 | Docs: PROJECT_INSTRUCTIONS, ARCHITECTURE, DECISIONS, TASKS, CHANGELOG | P0 | – | Lead | Files exist and are consistent | ☑ |
| T02 | config.py, db.py schema + DAO | P0 | T01 | Lead | Unit tests pass | ☑ |
| T03 | fetcher.py per-host rate limit, streaming, cancellation | P0 | T02 | Reliability | Spacing test ≥1 s per host | ☑ |
| T04 | normalize.py: dates/tz, CPV, enums, country, status | P0 | – | Extraction | Unit tests pass (DST, formats) | ☑ |
| T05 | extract.py + recipe.py executor (html/json, pagination, label, regex) | P0 | T03,T04 | Extraction | Executor reads mock SSR + SPA with hand recipes | ☑ |
| T06 | documents.py download, HTML hops, sha256, safe unzip, 250 MB cap | P0 | T03 | Reliability | Mock ZIP unpacked, multi-step resolved | ☑ |
| T07 | Mock portals: ssr, spa, multi, captcha, login, notice, layout v2 | P0 | – | QA | Served by `mockportals/server.py` | ☑ |
| T08 | validate.py: fill rates, invariants, drift | P0 | T04 | Reliability | Detects layout switch & column swap | ☑ |
| T09 | orchestrator.py: runs, states, scheduler, refresh, delete | P0 | T05–T08 | Lead | Contract tests pass with fake learner | ☑ |
| T10 | main.py API contract | P0 | T09 | Lead | Contract tests pass | ☑ |
| T11 | llm.py + learner.py (discover, learn, verify, repair) | P0 | T05 | LLM | Live run on mocks learns a valid recipe (needs key) | ◐ plumbing tested with a scripted model; live run pending an API key |
| T12 | blockers.py captcha/login detection | P0 | – | Reliability | login mock → failed; captcha mock → serving | ☑ |
| T13 | browser.py XHR capture (optional) | P1 | T11 | LLM | SPA mock: JSON API found | ☑ |
| T14 | Dashboard | P1 | T10 | UX | Shows states, reasons, runs, cost | ☑ |
| T15 | Dockerfile + README | P0 | T10 | Lead | Image builds; README one-pager | ◐ written; `docker build` not run (no Docker on dev machine) |
| T16 | End-to-end verification in browser | P1 | all | QA | Dashboard checked visually | ☑ (demo learner) |
| T18 | Operator pass #1 fixes (C1–C3, M1, M2, M4–M8, minors) | P0 | T09 | Lead | Regression tests for each | ☑ |
| T20 | Operator dashboard v2 (filters, actions, details, empty/error states) | P1 | T14 | UX | All flows checked in browser | ☑ |
| T19 | M3 assumed deadline time flag; M9 "list ok, docs blocked" state | P2 | – | Lead | Decided with Patterno contract | ☐ |
| T17 | Real-portal test set (≥4 countries, 3 software pkgs) | P1 | T11 | QA | Live results logged | ☐ (needs API key) |
