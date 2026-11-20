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
