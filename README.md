# Watchtower

Watchtower connects public-tender portals on its own, watches them and repairs itself when a portal changes. One container serves the API and the operator dashboard on port **8080**.

```bash
docker build -t watchtower .
docker run -p 8080:8080 -e ANTHROPIC_API_KEY=sk-ant-... -v wt-data:/data watchtower
# any other provider works too, e.g.:  -e GEMINI_API_KEY=... -e LLM_MODEL=<model name>
# open http://localhost:8080 , or:
curl -XPOST localhost:8080/portals -H 'content-type: application/json' -d '{"url":"https://…"}'
```

## How it works

1. **Attach** (LLM, once per portal). Watchtower fetches the address it was given.
   - It shrinks the page into a compact DOM skeleton plus a link list, so it costs a few thousand tokens instead of the full HTML.
   - Claude decides whether this page is the tender list. If it is not (a notice summary, a home page, a search form), Claude says which link to follow, for at most 4 hops.
   - For JavaScript apps, a headless browser renders the page once and records the JSON API calls the app makes. The data source then becomes that API, read over plain HTTP.

   Claude writes a **recipe**: a declarative JSON spec, not code. It says:
   - where the list is and how it paginates (link, page, offset, or POST body);
   - which CSS selector, label or JSON path holds each field;
   - where the detail page is;
   - where the documents are, including multi-step "documents page → download page → file" chains;
   - the date formats and timezone, and the words used for "withdrawn".
2. **Test before trust.** Every model answer is executed straight away:
   - If the list matches 0 rows, has missing URLs or titles, or ids that don't match earlier ones, the model is asked to fix it.
   - A sample of extracted tenders is then shown to the model next to the page text, and it judges each field `ok`, `wrong` or `missing`. After one fix round, fields still judged wrong are removed from the recipe, because a missing value is better than a wrong one.
3. **Regular reads use no model at all.** The recipe is executed deterministically, every `READ_INTERVAL_MIN` and on `POST /refresh`:
   - Detail pages and documents are fetched only for new or changed tenders.
   - Files are deduplicated by sha256, and ZIPs are unpacked; files over 250 MB are skipped.
   - Dates are converted with the portal's IANA timezone (DST-correct), CPV codes are normalized, and procedure and contract types come from multilingual keyword maps.
4. **Notice.** Every read is validated before it may replace the stored data:
   - required fields are filled;
   - ids are unique;
   - a deadline is not before its publication date;
   - titles do not look like dates;
   - fill rates and tender counts have not drifted against the last good read.

   A failing read moves the portal to `healing`. The API keeps serving the last correct tenders.
5. **Repair ladder**, cheapest step first:
   1. retry;
   2. earlier recipe versions;
   3. model repair, which gets the old recipe, the diagnostics and the new page, and must keep tender ids identical;
   4. otherwise `failed`, with the reason in plain words.

   A portal that needs a login or captcha for its list or its documents is reported as `failed` and the reason says so. A captcha widget on a page whose documents still download is fine.
6. **Politeness.** Every request goes through a per-host limiter that allows at most 1 request per second: pages, APIs, downloads and browser sessions. There are no logins and no captcha solving.

The dashboard (`/`) is the operator's morning view — one self-contained page, no build step,
designed so the answer to "is everything fine?" takes a glance:
- **Header:** one verdict ("1 portal needs you" / "All 4 portals delivering") and a paste-to-add field.
- **State cards:** All / Failed / Healing / Integrating / Serving counts; click to filter.
- **Portal list (left):** grouped into *Needs attention*, *Attaching* and *Working*. Each row: a
  state dot, the portal, its tender count and when it last delivered; the reason is shown only when
  something is wrong. Refresh / Delete appear on hover (always on touch screens).
- **Portal detail (right):** a coloured callout saying what is happening and why, three facts
  (tenders, last correct read, last checked), a recent-runs strip, and two tabs — **What happened**
  (reads and repairs in plain words, click for the step log) and **Tenders** (status filter, buyer,
  deadline in the portal's local time, document downloads with sizes). No manual steering.
- **Settings** (header or footer): theme (System / Dark / Light, per browser), automatic checks on/off
  and their interval in minutes, the wait after a failed model repair, and the model in use (read-only;
  keys stay on the server). Schedule settings are stored in the database, apply on the next scheduler
  tick and are also available as `GET`/`PUT /settings`. Each portal shows when it is checked next.
- **Look:** Patterno branding (mark + wordmark in the header, favicon) and visual language (navy + indigo, Inter, EB Garamond wordmark, pills); dark by
  default with a light theme in the footer. Fonts fall back to system fonts offline.

Code map: `PROJECT_INSTRUCTIONS.md`, `ARCHITECTURE.md`, `DECISIONS.md`.

## Model provider

Watchtower needs one model to attach and repair portals (regular reads never call it). Any of these
works — set **one** key; the first one found is used, or force one with `LLM_PROVIDER`:

| Provider | Key variable | Model |
|---|---|---|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` | default `claude-opus-5-5`, override with `LLM_MODEL` |
| OpenAI | `OPENAI_API_KEY` | `LLM_MODEL` required |
| Google Gemini | `GEMINI_API_KEY` | `LLM_MODEL` required |
| Groq | `GROQ_API_KEY` | `LLM_MODEL` required |
| OpenRouter | `OPENROUTER_API_KEY` | `LLM_MODEL` required |
| DeepSeek | `DEEPSEEK_API_KEY` | `LLM_MODEL` required |
| Mistral | `MISTRAL_API_KEY` | `LLM_MODEL` required |
| Any OpenAI-compatible API (e.g. local Ollama `http://localhost:11434/v1`) | `LLM_BASE_URL` (+ `LLM_API_KEY` if needed) | `LLM_MODEL` required |

Everything except Anthropic goes through one OpenAI-compatible adapter (`/chat/completions`, JSON
mode when the provider supports it, automatic back-off on 429). Use the exact model name from your
provider's documentation. Pick a model with a context window of at least ~32k tokens, or lower
`PROMPT_PAGE_CHARS`. For local development the variables can live in a `.env` file in the project
root (git-ignored); real environment variables take precedence.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| *one provider key* (see above) | – (**required** to attach new portals) | Model access |
| `LLM_MODEL` (alias `WATCHTOWER_MODEL`) | `claude-opus-5-5` for Anthropic | Model used for learn, verify and repair |
| `LLM_PROVIDER` | auto | Force a provider when several keys are set |
| `LLM_BASE_URL` / `LLM_API_KEY` | – | Any OpenAI-compatible endpoint |
| `LLM_MAX_OUTPUT_TOKENS` | `8192` | Output cap for OpenAI-compatible models |
| `PROMPT_PAGE_CHARS` | `45000` | Compacted page size per prompt (lower for small-context models) |
| `MODEL_PRICE_INPUT_PER_MTOK` / `MODEL_PRICE_OUTPUT_PER_MTOK` | `4` / `20` for Anthropic, `0` otherwise | USD prices used for the cost display |
| `READ_INTERVAL_MIN` | `30` | Default automatic check interval (change it live in Settings) |
| `REPAIR_COOLDOWN_MIN` | `120` | Default wait after a failed model repair (change it live in Settings) |
| `MAX_TENDERS_PER_PORTAL` | `500` | Safety cap per read |
| `MAX_FILE_BYTES` | `262144000` | Files and ZIPs larger than this are skipped (250 MB) |
| `MIN_REQUEST_INTERVAL_SEC` | `1.05` | Per-host request spacing |
| `MAX_CONCURRENT_RUNS` | `8` | Portals processed in parallel |
| `USE_BROWSER` | `1` | Use headless Chromium while learning JS portals |
| `BLOCK_PRIVATE_NETWORKS` | `0` | `1` also refuses private and loopback targets (cloud metadata and link-local addresses are always refused) |
| `DATA_DIR` | `/data` | SQLite database and document store |

## Security

What Watchtower does to stay safe while reading untrusted portals:
- **Untrusted data stays data.** The model only writes a declarative recipe (schema-checked, no
  code execution); page text is never treated as instructions. Regexes in recipes are length-capped,
  checked for nested quantifiers and run on at most 20k characters.
- **Links:** only absolute `http(s)` URLs are delivered or rendered; `javascript:`/`data:` links
  from a portal become `null`. The dashboard escapes all portal text.
- **SSRF:** cloud metadata services and link-local/special addresses are always refused — for the
  first request, every redirect hop and the learning browser. `BLOCK_PRIVATE_NETWORKS=1` also
  refuses private networks (off by default so portals on internal test hosts still work).
- **Cross-site requests:** writes (`POST`/`PUT`/`PATCH`/`DELETE`) carrying a foreign `Origin`, or
  `Sec-Fetch-Site: cross-site`, are refused with 403. API clients that send no `Origin` are unaffected.
- **Documents** are served by opaque id as downloads (`Content-Disposition: attachment`, `nosniff`,
  `CSP: sandbox`); HTML/SVG/XML/JS are always sent as `application/octet-stream`. ZIPs are unpacked
  with size and ratio limits into content-addressed storage (no path traversal).
- **Keys** only come from the environment or a git-ignored `.env`; the API and dashboard never show them.
- **No third-party requests from the dashboard** (no CDN fonts — GDPR); fonts are self-hosted when
  present in `watchtower/static/fonts/`, otherwise system fonts are used.
- The container runs as a non-root user.

Known limits — deploy accordingly:
- **No authentication** (required by the challenge's API contract). Anyone who can reach port 8080
  can add, delete and refresh portals and change settings. In production put Watchtower behind a
  private network or an authenticating reverse proxy.
- **Disk:** files over 250 MB are skipped, but there is no total storage quota; size the `/data`
  volume and monitor it.
- **Headless Chromium** runs with `--no-sandbox` (needed in containers) while learning JavaScript
  portals; it is short-lived, non-root and SSRF-guarded. Set `USE_BROWSER=0` to disable it.
- **DNS rebinding:** host names are checked when resolved; a name that changes its address between
  the check and the connection is not fully covered. The base image is pinned by tag, not by digest.

## What one portal costs in model usage

These are estimates. The real figure for each portal and each run is measured from the API's token usage and shown on the dashboard, and in `GET /portals/{id}/runs` as `llmCalls`, `inputTokens`, `outputTokens` and `costUsd`.

| Phase | Model calls | Tokens (approx.) | Cost with Opus 5.5 ($4 / $20 per MTok) |
|---|---|---|---|
| Attach | 4–8 (discover, list fix, detail, documents page, verify) | 40–90k input, 8–20k output | **≈ $0.30–0.80, once** |
| Regular read | **0** | 0 | **$0** |
| Repair (only when a portal changes) | 4–8 | the same as attach | ≈ $0.30–0.80 per real layout change |

Pages are compacted before they are sent: repeated rows are collapsed, scripts are dropped, and texts are truncated. This keeps the input small. With a cheaper model (`LLM_MODEL=claude-sonnet-5-5`), costs are about half; other providers cost what their price list says (set `MODEL_PRICE_*` to see it on the dashboard).

## Development

```bash
pip install -r requirements.txt && python -m playwright install chromium
python -m uvicorn mockportals.server:app --port 8090   # local test portals (SSR, SPA, multi-step, captcha, login, layout switch)
python -m uvicorn watchtower.main:app --port 8080
python -m pytest -q                                    # no API key needed (scripted model)
```

The tests cover:
- the API contract;
- attaching, healing after a layout switch with ids preserved, and rollback;
- documents blocked by a login and captchas that do not block;
- multi-step downloads, ZIP unpacking, SPA JSON APIs, and local-time deadlines across DST;
- the learner's test/fix/verify loop, with a scripted model.
