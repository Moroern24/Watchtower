# ARCHITECTURE.md — Watchtower

## 1. Components

| Component | File | Responsibility |
|---|---|---|
| API | `watchtower/main.py` | REST contract, dashboard, file serving |
| Store | `watchtower/db.py` | SQLite (WAL): portals, runs, recipes, tenders, files, doc cache |
| Fetcher | `watchtower/fetcher.py` | Async httpx with **per-host 1 req/s** lock, streaming, size caps, encoding |
| Blockers | `watchtower/blockers.py` | Detects captcha and login walls in HTML and HTTP responses |
| Compactor | `watchtower/compact.py` | Shrinks HTML into a DOM skeleton and a link list for prompts (cost control) |
| Recipe + Executor | `watchtower/recipe.py`, `extract.py` | Declarative reading spec; deterministic execution |
| Normalizer | `watchtower/normalize.py` | Dates→ISO with offset, CPV, enums, country codes, status |
| Validator | `watchtower/validate.py` | Fill rates, invariants, drift vs. last good read |
| Documents | `watchtower/documents.py` | Download, follow HTML hops, sha256, ZIP unpack, content-addressed store |
| LLM | `watchtower/llm.py` | Anthropic client, JSON-schema output, token and cost accounting |
| Learner | `watchtower/learner.py` | discover → learn list → learn detail → verify → (fix); repair |
| Browser | `watchtower/browser.py` | Optional Playwright: render a page and capture XHR JSON |
| Orchestrator | `watchtower/orchestrator.py` | Run queue, state machine, scheduler, cancellation on delete |

## 2. State machine

```
POST ─► integrating ──learn+read ok──► serving ◄──repair ok── healing
             │                            │ read fails validation  ▲
             │ blocked / learn failed     └────────────────────────┘
             ▼                                     │ repair failed
           failed ◄────────────────────────────────┘
           (scheduled cheap re-read; if the recipe passes again → serving;
            LLM repair at most once per REPAIR_COOLDOWN_MIN)
```

Run kinds:
- `read`: the attach (first read) and every regular read.
- `repair`: started automatically after a read fails validation on a portal that has a recipe.

## 3. Data model (SQLite)

- `portals(id, url, norm_url UNIQUE, state, reason, created_at, last_success_at, last_read_at, deleted, recipe_version, good_count, good_profile JSON, last_repair_at)`
- `recipes(portal_id, version, recipe JSON, created_at, source)`. `source` is one of `learn`, `repair` or `rollback`.
- `runs(id, portal_id, kind, started_at, finished_at, result, reason, steps JSON, llm_calls, input_tokens, output_tokens, cost_usd, tender_count)`
- `tenders(portal_id, tender_id, data JSON, hash, last_changed_at, last_seen_at, listed, detail_hash)`. PK is `(portal_id, tender_id)`.
- `files(id, sha256, size, mime)`. The bytes live at `DATA_DIR/files/<sha256>`.
- `doc_cache(url, fetched_at, entries JSON)`. Maps a source URL to the files it produced (ZIP → many).

A "snapshot" is the set of tender rows. A read writes them only after validation passes, which is how healing keeps serving the last good data.

## 4. Recipe DSL (v1)

```jsonc
{
  "kind": "html",                    // "html" | "json"  (list source format)
  "list": {
    "url": "https://portal/tenders", // absolute
    "method": "GET", "params": {}, "body": null, "headers": {},
    "items": "table.results tbody tr",           // CSS (html) or JSON path to the array (json)
    "pagination": {"type": "page_param", "param": "page", "start": 1, "step": 1, "maxPages": 30,
                   "nextSelector": null, "inBody": false},  // type: none|next_link|page_param|offset_param
    "fields": { "<field>": FieldSpec }
  },
  "detail": {                        // optional; one request per new/changed tender
    "kind": "html",
    "urlTemplate": null,             // null → use the tender url; else e.g. "https://p/api/t/{id}"
    "fields": { "<field>": FieldSpec }
  },
  "documents": {                     // where the files are (evaluated on the detail page, else on the list item)
    "pageLink": null,                // FieldSpec → link to a separate documents page (multi-step)
    "links": "a[href$='.pdf'], a.download",       // CSS for <a> elements (html)
    "items": null, "urlPath": null, "namePath": null, // JSON variant
    "followSelector": null           // if a link returns HTML, follow this CSS link (≤2 hops; heuristics otherwise)
  },
  "dateFormats": ["%d.%m.%Y %H:%M", "%d.%m.%Y"],
  "dayFirst": true,
  "timezone": "Europe/Berlin",
  "countryCode": "DE",
  "statusKeywords": {"withdrawn": ["aufgehoben"], "closed": ["beendet"]}
}
```

**Field names:**
- `id`, `url`, `title`, `buyerName`, `publicationDate`, `submissionDeadline`, `status`
- `referenceNumber`, `description`, `procedureType`, `contractNature`, `cpvCodes`
- `postalCode`, `city`, `countryCode`

**FieldSpec** has one locator plus optional modifiers.
- **Locators:**
  - `{"css": "td:nth-child(2) a", "attr": "text"|"href"|"<attr>"}`
  - `{"label": "Vergabestelle"}`: finds the label element (dt/th/td/label/span/strong/div). The value is the next sibling cell or element.
  - `{"path": "buyer.name"}`: JSON, relative to the item; `a.b[0].c`.
  - `{"const": "DE"}`
- **Modifiers:**
  - `regex`: the first capture group wins.
  - `all: true`: returns a list, used for `cpvCodes`.
- The value of `url` and of every link is resolved against the page URL.
- `id` must come from the portal: a reference number, a URL parameter or an API id. Executor fallback: the stable hash of `url`.

**Merge:** detail fields override list fields when they are non-null.

## 5. Read pipeline (no LLM)

1. Fetch the list pages, following pagination, up to `maxPages` and `MAX_TENDERS`. Stop when a page yields no new ids.
2. Extract the raw items and compute each item's list-hash.
3. For new tenders, or tenders whose list-hash changed:
   - fetch the detail page;
   - extract the detail fields;
   - find the documents and download them through the doc cache.
4. Normalize (§6) and validate (§7).
5. Commit:
   - Upsert the tenders. When a tender's content hash changes, set `lastChangedAt=now`.
   - Mark unlisted tenders `closed`.
   - Mark tenders whose deadline has passed `closed`.

## 6. Normalization rules (deterministic)

- **Dates:**
  - Try `dateFormats` first, then dateutil with `dayFirst`.
  - A naive value is localized with `timezone` (zoneinfo, DST-correct).
  - A date-only deadline becomes 23:59:59 local time; `publicationDate` stays date-only.
  - If a value cannot be parsed, it becomes null, never a guess.
- **CPV:** regex `\b(\d{8})(?:-\d)?\b`, keep the 8 digits, deduplicated.
- **procedureType / contractNature:** multilingual keyword maps (DE/FR/ES/NL/PL/IT/EN/…). No match → omitted.
- **countryCode:** ISO-2 from codes or country names; falls back to the recipe `countryCode` only for `placeOfPerformance` when a city or postal code is present.
- **Status:**
  - `withdrawn` if a status or title text matches the withdrawn keywords;
  - else `closed` if the deadline is in the past or the tender is unlisted;
  - else `open`.

## 7. Validation and notice

A read is **valid** when all of these hold:
- At least 1 tender.
- `id`, `title` and `url` are present for every tender.
- Ids are unique.
- Each required field's fill rate has not dropped by more than 35 points against the last good profile.
- Count ≥ 40% of the last good count. A count below 40% is suspicious, which triggers repair; the repair may re-confirm the recipe.
- Invariants:
  - deadline ≥ publication;
  - title and buyer are not date or number-only strings;
  - CPV codes are 8 digits.

**Hard blocks:**
- A captcha or login wall where tenders were expected leads to `failed`, with `reason`.
- Documents that lead to a login wall for every tender lead to `failed` ("documents require login").

## 8. Learning and repair (LLM)

**Learning:**
- **Discover:**
  - Fetch the URL and compact it.
  - Check for blockers.
  - The LLM classifies the page (`tender_list | notice_or_summary | portal_home | detail | js_app | blocked`) and may pick a better link. At most 3 hops.
  - A JS app is rendered with the browser to capture JSON responses.
- **Learn list:** the LLM gets the compact list page or JSON samples and returns a list recipe (schema-validated). The executor runs page 1. If it gets 0 items, the LLM is given that feedback, at most 2 times.
- **Learn detail:** fetch 1–2 sample detail pages; the LLM returns the detail fields and documents spec.
- **Verify:**
  - Run the full recipe on a small sample.
  - Give the LLM the extracted record and the page text; it marks each field `ok | wrong | missing`.
  - Wrong fields lead to one fix round. Fields still wrong after it are dropped from the recipe, because null is better than a wrong value.

**Repair order:**
1. Retry once.
2. Try the previous recipe versions.
3. LLM repair: send the old recipe, the diagnostics and the new compact page, and get a patched recipe back.
4. Fresh learn.
5. `failed`.

Expected cost: about 4–7 calls per attach, with roughly 10–20k input tokens per call.

## 9. Concurrency

- One asyncio orchestrator.
- At most one active run per portal.
- A global semaphore caps concurrent portal runs.
- Per-host locks keep the 1 s spacing.
- DELETE sets the `deleted` flag and cancels the portal's task. The fetcher checks for cancellation before every request.
