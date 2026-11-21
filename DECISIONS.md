# DECISIONS.md — Architecture Decision Log

Each entry: context → decision → consequences.

### ADR-001 Declarative Recipe DSL, no generated code (2026-11-20)
- **Context:** The LLM must learn how a portal is read. Generated Python is more expressive, but it is a security risk and hard to validate or diff.
- **Decision:** The LLM emits a JSON recipe that is validated against a pydantic schema and executed by our own code.
- **Consequences:** The recipe is safe, cheap and diffable. The DSL is the ceiling on what we can express, so it is extended (labels, regex, JSON paths, doc page hops) to cover the common portal patterns. A sandboxed-code escape hatch was considered and deferred.

### ADR-002 LLM only at learn, verify and repair time (2026-11-20)
- **Decision:** Regular reads make 0 model calls.
- **Consequences:** Model cost is limited to setup and repairs. Notice must therefore be deterministic (validation + drift).

### ADR-003 Deterministic normalization (2026-11-20)
- **Decision:** The LLM says where a value is and its format or timezone. Code converts it. A value that cannot be parsed becomes null.
- **Why:** The spec says a wrong value is worse than a missing one.

### ADR-004 SQLite + local file store in one container (2026-11-20)
- **Decision:** Use SQLite in WAL mode with a single process.
- **Consequences:** No external services, so the build stays simple. Horizontal scale would later need Postgres and object storage. The interfaces in `db.py` are kept narrow for that reason.

### ADR-005 Playwright for learning only, optional (2026-11-20)
- **Decision:** The browser is used during learning to find the JSON API behind JS apps. Regular reads call that API over plain HTTP. A recipe flag `render` allows browser reads as a last resort.
- **Consequences:** The image is bigger (Chromium). The code still runs without Playwright installed.

### ADR-006 Snapshot commit after validation (2026-11-20)
- **Decision:** Tenders are written only from validated reads, so healing keeps serving the last good data.

### ADR-007 Repair ladder (2026-11-20)
- **Decision:** The repair steps are, in order: retry → previous recipe versions → LLM patch → LLM fresh learn → failed.
- **Consequences:** Cheap steps come first. LLM repairs are rate-limited per portal (`REPAIR_COOLDOWN_MIN`) to cap cost.

### ADR-008 Model choice (2026-11-20)
- **Decision:** The default is `claude-opus-5-5`, overridable with `WATCHTOWER_MODEL`. A recipe error costs far more (wrong data, re-repairs) than the tokens saved by using a smaller model.
- **Consequences:** Operators can switch to a cheaper model with the env var.

### ADR-010 A repair may never accept fewer tenders on its own (2026-11-20)
- **Context:** operator pass #1 found that a repair re-validated with basic checks only, so a
  real drop (or broken pagination) closed open tenders under a green state.
- **Decision:** repaired recipes face the same drift checks as regular reads. A count drop is
  always a failure; if the model returns the unchanged recipe, the portal is `failed` with
  "not auto-closing tenders" and keeps serving its last correct tenders. Losing an optional
  field after a repair is accepted (missing beats wrong) but reported in the reason.

### ADR-011 DELETE purges; re-adding attaches from scratch (2026-11-20)
- **Decision:** deleting a portal removes its recipes, runs and tenders. Adding the same URL
  later gives a new id and a fresh attach — the operator's way to force a re-learn.

### ADR-012 Walls are judged by outcome, not by page chrome (2026-11-20)
- **Decision:** a login form or captcha widget on a page does not block documents while the
  page offers file links. Blocked = redirected to a login URL, or a wall page with nothing to
  follow. HTTP 403 is reported as "refused by server", not as a login.

### ADR-013 Dashboard follows Patterno's visual language (2026-11-20)
- **Decision:** colours, type and controls follow patterno.de (navy + indigo, Inter, EB Garamond
  wordmark, pills). Dark is the default, as on the brand site.
- **Update (0.8.0):** at the team's request the header carries the Patterno mark and wordmark
  ("Patterno | Watchtower", mark taken from patterno.de's own inline SVG) and the mark is the
  favicon, because Watchtower is built for Patterno's production. Confirm brand use with the
  organisers before publishing the repository or video beyond the hackathon.
- **Consequences:** the operator console feels like part of the Patterno product. Web fonts load
  from Google Fonts; without internet the system fonts take over and nothing breaks.

### ADR-014 Any model provider, one adapter (2026-11-20)
- **Context:** the brief allows any model; the team needs to test without an Anthropic key.
- **Decision:** Anthropic stays on its SDK; every other provider goes through one OpenAI-compatible
  HTTP adapter (OpenAI, Gemini, Groq, OpenRouter, DeepSeek, Mistral, Ollama, …). The provider is
  chosen from whichever key is set; non-Anthropic models must be named explicitly (`LLM_MODEL`) so
  we never guess a model id. A `.env` file is read for local development.
- **Consequences:** no new dependency (uses httpx). Recipe quality depends on the model chosen —
  small or free models may need more fix rounds or fail on hard portals.

### ADR-015 Security hardening after review (2026-11-21)
- **Context:** a review found a script-link XSS (portal `javascript:` URLs reached the dashboard),
  SSRF gaps (metadata addresses reachable, redirects unchecked), CSRF on `POST /portals`, active
  document types served inline-capable, unbounded model-written regexes and Google Fonts (GDPR).
- **Decision:** only `http(s)` URLs leave the system; metadata/link-local addresses are always
  refused (first request, redirects, browser), private networks remain a switch; cross-site writes
  are refused by `Origin`/`Sec-Fetch-Site`; documents are sandboxed downloads and active types are
  `application/octet-stream`; recipe regexes are vetted; fonts are self-hosted or system fonts.
- **Kept as known limits:** no authentication (contract), no global disk quota, Chromium without
  sandbox while learning, partial DNS-rebinding coverage — documented in README → Security.
- **Tests:** `tests/test_security.py`.

### ADR-009 No LLM-written Python sandbox, no captcha handling (2026-11-20)
- **Decision:** Skipped, following the brainstorm and the rules. Captchas and logins are detected and reported, never solved.

### ADR-016 Keep a validated snapshot intact when writes fail (2026-11-21)
- **Decision:** Tender snapshot writes run in one SQLite transaction. If document delivery is
  blocked during a read or repair, keep the previously validated snapshot and report `failed`.
- **Consequences:** Operators retain the last correct tenders until a complete read succeeds.
