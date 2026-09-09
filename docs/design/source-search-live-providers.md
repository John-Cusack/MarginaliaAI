# Design: Live source-search providers (Logos, YourCloudLibrary, …)

**Status:** Draft / RFC
**Author:** (review pending)
**Scope:** `packages/core` — the plugin host / SDK boundary only. No plugin code.

## 1. Background

The `search_sources` system (commit `67615ca`) already implements the
cross-source discovery surface we want: one structured `SourceQuery` fans out to
every registered `SourceSearchProvider` in parallel, results are deduplicated
(DOI → ISBN → title+author+year), corpus-presence-enriched, and returned with an
`ingest_action` descriptor so the tool stays read-only.

What it **cannot do today** is search a live website. The user-facing goal —
"search Logos or YourCloudLibrary live, for the libraries that have it enabled" —
is blocked by three gaps in the host, not by the Protocol shape.

### Current code, for reference

- `domain/source_search.py` — `SourceSearchProvider` Protocol, `SourceQuery`,
  `SourceMatch`, `IngestAction`, `Availability`.
- `mcp/tools/search_sources.py` — fan-out tool.
- `plugins/loader.py:182-191` — Phase 4e, provider registration.
- `plugins/loader.py:231-278` — `build_plugin_clients`, the gated client bundle
  that MCP tools already get but providers do **not**.

## 2. Problems

### P1 — Providers receive no dependencies (the blocker)

`loader.py:186`:

```python
provider = ss_cls() if isinstance(ss_cls, type) else ss_cls
```

The provider is constructed with **no arguments**. A provider that hits Logos or
YourCloudLibrary needs an authenticated HTTP client, a rate limiter, and
config (cookies / credentials / base URLs). None of it reaches the provider, and
the permission-gated `build_plugin_clients()` bundle is bypassed entirely. As
written, a live provider could only work by reaching into global module state or
its own cookie file — defeating the permission model.

### P2 — No permission enforcement for providers

A provider does network I/O, but `permissions.network` (`NetworkPerm{none|egress|full}`)
and `network_allowlist` are never consulted for source-search. The `http` client
is gated via `GatedHttpClient`; providers are not. A "search Logos" provider could
silently hit any host.

### P3 — "Enabled" is conflated with "installed", and failures are invisible

`_run_provider` (`search_sources.py:100`) swallows **every** error and timeout to
`[]`. The agent cannot distinguish:

- "Logos has no match for this title" (genuinely empty), from
- "Logos session cookie expired" (needs re-auth), from
- "provider installed but never configured with an account".

"Enabled" today just means *installed + contributes `provides.source_search`*.
The user's mental model — "the libraries that have it enabled" — implies
*configured and authenticated*, which the system has no way to represent.

## 3. Proposed design

Three additive changes. All backward-compatible: an existing zero-arg provider
keeps working (it just gets no clients and is assumed always-healthy).

### 3.1 Inject a context via `bind(ctx)` (fixes P1)

Add an **optional** lifecycle method to the provider contract. The loader
constructs the provider as today, then, if it exposes `bind`, calls it with a
context built from the existing gated-client machinery.

New types in `domain/source_search.py`:

```python
@dataclass
class SourceSearchContext:
    """Scoped dependencies handed to a provider at load time."""
    http: HttpClient          # GatedHttpClient — permission + allowlist enforced
    corpus: CorpusClient      # so a provider can pre-check the local corpus
    llm: LLMClient | None     # None unless permissions.llm
    config: dict[str, Any]    # plugin settings: credentials, base URLs, etc.
    logger: Any               # structlog-bound logger, tagged with plugin name

@runtime_checkable
class BindableSourceSearchProvider(SourceSearchProvider, Protocol):
    def bind(self, ctx: SourceSearchContext) -> None: ...
```

Loader Phase 4e (`loader.py:182-191`) becomes:

```python
for ss_contrib in provides.source_search:
    ss_cls = self._import_entry(plugin_dir, ss_contrib.entry)
    provider = ss_cls() if isinstance(ss_cls, type) else ss_cls
    if hasattr(provider, "bind"):
        ctx = self._build_source_search_context(manifest)   # reuses build_plugin_clients
        provider.bind(ctx)
    self._registry.register_source_search_provider(provider, manifest.name)
```

`_build_source_search_context` reuses `build_plugin_clients(manifest.name)` so
the `http` client is the same `GatedHttpClient` MCP tools get — no second
permission path to keep in sync.

**Why `bind(ctx)` over constructor injection** (the chosen option): providers stay
constructible as `ss_cls()`, so the existing `runtime_checkable` conformance and
all current tests/stubs keep working; the context is layered on after
construction and is purely additive.

### 3.2 Gate provider network access (fixes P2)

- At registration, if a manifest declares `provides.source_search` but
  `permissions.network == none`, **fail the load** with a clear error ("a
  source-search provider must request `permissions.network`"). This makes intent
  explicit and prevents a provider that can't legally make requests.
- Because `ctx.http` is the existing `GatedHttpClient`, `network_allowlist` is
  enforced for free — a Logos provider restricted to `logos.com` / `faithlife.com`
  cannot exfiltrate the query elsewhere.

### 3.3 Health + status surfacing (fixes P3)

Add an **optional** `healthcheck` to the contract and a typed auth error:

```python
class ProviderStatus(enum.StrEnum):
    ok = "ok"                          # reachable + authenticated
    unauthenticated = "unauthenticated"  # installed but no/expired session
    unconfigured = "unconfigured"      # no credentials supplied
    error = "error"
    timeout = "timeout"

class ProviderHealth(BaseModel):
    status: ProviderStatus
    detail: str | None = None

class ProviderAuthError(Exception):
    """Raise from search() to signal unauthenticated instead of returning []."""

@runtime_checkable
class HealthCheckable(Protocol):
    async def healthcheck(self) -> ProviderHealth: ...
```

Changes in `search_sources.py`:

- `_run_provider` distinguishes outcomes instead of collapsing to `[]`:
  `ProviderAuthError → unauthenticated`, `TimeoutError → timeout`, other
  exceptions → `error`, success → `ok`. It returns `(matches, status)`.
- The tool result gains a `provider_status` map so the agent (and UI) can say
  "Logos: session expired — re-run the Logos login flow" instead of silently
  showing zero results.
- Optionally, `handler` runs `healthcheck()` first (short-circuit timeout) for
  providers that implement it, so an unauthenticated source is reported even when
  the query itself would have returned nothing.

This is what gives the user a real answer to "which libraries have it enabled":
`provider_status` = the registered providers and, for each, whether it's actually
usable right now.

## 4. Data-model cleanups (low-risk, do alongside)

- **Hoist `doi` / `isbn` onto `SourceMatch`.** `SourceQuery` has `doi/isbn/asin`
  but `SourceMatch` only carries them in `metadata`, and `_dedup_key`
  (`search_sources.py:81`) reads `match.metadata.get("doi")`. A typo in a
  provider's metadata key silently breaks dedup. Make them first-class fields and
  fall back to metadata for compatibility.
- **Batch `_enrich_with_corpus`** (`search_sources.py:113`). It currently awaits
  `find_existing` once per match in a loop (N round-trips). Gather them.

## 5. Open questions — resolved against the Logos plugin

The Logos plugin (`/home/john/repos/marginalia-plugin-logos`) already does live
web search and self-manages auth. Reading it answers most of §5 and **revises the
design** — see §8 for the detail; the short version:

1. **Where does config/credentials come from?** *Resolved: the plugin owns it,
   not the host.* Logos resolves credentials from `LOGOS_USERNAME`/`LOGOS_PASSWORD`
   env vars or the main repo `.env` (`auth/credentials.py`) and persists the
   session at `~/.logos-mcp/cookies.json` (`auth/cookie_store.py`), refreshed by a
   background keeper (`auth/manager.py`). The host should **not** try to own
   credentials. `ctx.config` becomes optional/secondary, not the mechanism.
2. **Should `healthcheck` be cached?** Yes — and the template already exists:
   `auth/verify.py:check_session` is a tri-state probe of `/api/app/me`
   (`True` / `False` / `None`) that maps 1:1 onto `ProviderStatus`. Cache with a
   short TTL (~60s) in the registry to avoid an auth probe per call.
3. **User-level enable/disable** beyond install + the call-time `sources` filter —
   still open, still out of scope unless you want it folded in.

## 6. Phased implementation plan

1. **Types** — add `SourceSearchContext`, `BindableSourceSearchProvider`,
   `ProviderStatus`, `ProviderHealth`, `ProviderAuthError`, `HealthCheckable` to
   `domain/source_search.py`; re-export from `plugins/sdk/__init__.py`.
2. **Loader** — `_build_source_search_context`, call `bind` in Phase 4e, enforce
   the network-permission rule. (`loader.py`)
3. **Tool** — status-aware `_run_provider`, `provider_status` in the result,
   optional `healthcheck` pass. (`search_sources.py`)
4. **Model cleanups** — §4.
5. **Tests** — extend `tests/unit/test_source_search.py`:
   - a bindable stub receives its `ctx`;
   - a stub raising `ProviderAuthError` surfaces `unauthenticated`, not `[]`;
   - a non-bindable legacy stub still registers and runs (back-comp);
   - manifest with `source_search` + `network: none` fails to load;
   - `healthcheck` reflected in `provider_status`.

## 7. What this unblocks

A plugin (e.g. `marginalia-plugin-logos`) can ship a `LogosSourceProvider` whose
`search()` calls the live Logos catalog and returns `SourceMatch` records with
`availability=ingestable` and an `ingest_action` of `logos_ingest_book`. The same
pattern applies to a YourCloudLibrary provider (`availability=borrowable`). That
reference provider is deliberately out of scope here — this doc only makes the
host capable of hosting it. **Critically (see §8), the Logos provider needs almost
nothing from the host to do the live search itself** — the value the host adds is
the fan-out, the dedup, the corpus cross-reference, and the *status surfacing*.

## 8. Findings from the Logos plugin — what's real vs. what I assumed

Reading `marginalia-plugin-logos` changed the priorities. The plugin already
performs live web search and owns its full auth stack, so two pillars of §3 need
correcting.

### 8.1 Live search already exists — the provider is a thin adapter

- `logos/tools/search.py` (`logos.search`) → `POST /api/app/search/v2/books`.
- `logos/tools/library.py` (`logos.library`) → `GET /api/app/library`.

Both go through `logos/http/client.py`'s module-level singleton `logos_client`.
A `LogosSourceProvider.search()` is therefore ~30 lines: call `logos_client`,
map the JSON to `SourceMatch`. It needs **no injection** to do the network call.

### 8.2 The plugin owns auth and HTTP — `bind(ctx.http)` does NOT fit (revises P1/P2)

`logos/http/client.py` is a long-lived `httpx.AsyncClient` with **cookie
injection, 401→re-auth→retry, streaming, and connection pooling**, backed by
`auth/manager.py` (background session keeper, silent password-free renewal,
mtime-aware cookie cache) and `auth/cookie_store.py` (`~/.logos-mcp/cookies.json`).
`pack.yaml` declares `permissions.network: full`.

Implications:

- **The SDK `HttpClient` Protocol is too thin for this.** It exposes only
  `get(url) -> bytes` / `post(url, json) -> bytes` — no cookies, no streaming, no
  status-code-aware retry. A real provider like Logos cannot route through
  `ctx.http` / `GatedHttpClient` without losing its auth machinery.
- **So §3.2's "gate provider network via `GatedHttpClient` + `network_allowlist`"
  does not match reality** and would break the existing plugin. The honest status:
  self-managing plugins do their own egress under a coarse `network: full` grant;
  the host does **not** currently sandbox that. Tightening it would require a
  transport-level proxy/allowlist that intercepts plugin-owned `httpx` — a
  separate, larger piece of work. Flag it as a known gap, don't pretend the thin
  SDK client closes it.
- **`bind(ctx)` is therefore demoted from "the unblock" to "a convenience."** It's
  still worth adding so a provider can reach `ctx.corpus` (pre-check the local
  corpus) and `ctx.llm` — but `ctx.http` is not how Logos will make its calls.

### 8.3 Health/status is the real win — and the template already exists (validates P3)

`auth/verify.py:check_session(jar)` is a **tri-state** probe of `/api/app/me`:

| `check_session` result | meaning | → `ProviderStatus` |
|---|---|---|
| `True` | authenticated | `ok` |
| `False` | 200 `isAuthenticated:false`, or 401/403 | `unauthenticated` |
| `None` | network blip / 5xx / bad body | `error` (transient) |

`manager.verify_auth()` returns the richer `{authenticated, email, alias}` that the
`logos.auth_status` MCP tool already surfaces. So a Logos `healthcheck()` is a
3-line wrapper, and the "no stored session" path in `http/client.py:80-84`
(`profile_seeded()` false → "Run 'logos-login'") is exactly the `unconfigured`
vs `unauthenticated` distinction §3.3 wanted.

**This is the answer to the user's literal ask** — "search the libraries that have
it enabled." Enabled = registered *and* `healthcheck()` returns `ok`. The fan-out
should report, per provider, `ok` / `unauthenticated` / `unconfigured` / `error`,
so the agent can say "Logos: run `logos-login`" instead of showing an empty list.

### 8.4 Revised priority order

1. **P3 / §3.3 — status-aware fan-out + `healthcheck`.** Highest value, smallest
   surface, directly answers the user's goal. Logos already has the probe.
2. **Reference `LogosSourceProvider`** in the plugin — a thin adapter over
   `logos_client` + a `healthcheck()` over `check_session`. Proves the contract.
3. **`bind(ctx)` / §3.1** — keep, but for `corpus`/`llm` access, not http. Lower
   urgency than originally stated.
4. **§3.2 network gating** — re-scope as a separate sandboxing initiative; the
   thin SDK http client does not deliver it for self-managing plugins.
5. **§4 data-model cleanups** — unchanged, low-risk.

## 9. Findings from the YourCloudLibrary plugin — two provider archetypes

`marginalia-plugin-yourcloudlibrary` (`ycl`) is shaped nothing like Logos, and
that difference is the most important architectural input so far: **not every
"library" can answer an arbitrary query.** The global-search design must support
two provider archetypes, not one.

### 9.1 YCL has no catalog search

`ycl/api/client.py` exposes only `get_book(book_id)`, `get_manifest(isbn)`, and
`fetch_chapter_text` — every call is keyed on a book the user has **already
borrowed**. There is no title→results endpoint anywhere in the plugin (grep for
`search`/`catalog`/`discover` finds none). The only discovery surface is
`ycl.list_books`, which reads the **local `BorrowStore`**, not the YCL website.
YCL's purpose (per `pack.yaml`) is to *capture borrowed ebooks before the loan
expires*, not to find new ones.

### 9.2 The two archetypes

| | **Discovery provider** (Logos, academic-journal) | **Holdings provider** (YCL, likely Kindle) |
|---|---|---|
| `search(query)` hits | a live external catalog | the user's own borrowed/owned set (local store) |
| Can match a title the user doesn't have? | yes | **no** — only titles already borrowed/owned |
| Typical `availability` | `ingestable`, `in_corpus` | `borrowable` (active loan), `in_corpus`, else absent |
| Role in fan-out | finds *new* sources to ingest | confirms *presence/borrow-status* of a known work |

This is not a flaw to fix — it's the correct division. A YCL
`SourceSearchProvider.search(query)` should match `query`/`isbn` against the
`BorrowStore` and return `borrowable` matches (with an `ingest_action` of
`ycl.ingest_book`). It is a **presence/holdings** signal, complementary to
Logos's discovery signal. The existing `Availability` ladder already encodes
exactly this (`borrowable` sits between `ingestable` and `purchasable`), and the
`_merge` dedup will correctly fuse "Logos has it to ingest" with "and you also
have an active YCL loan" under `also_available_via`. The design holds — it just
needs to be stated that providers self-declare which archetype they are (or
simply return `[]` for queries outside their holdings).

### 9.3 Network gating is declared but unenforced — YCL is the concrete target

YCL contradicts the tentative conclusion from Logos in §8.2. Its `pack.yaml`
declares the **fine-grained** form the host already models:

```yaml
permissions:
  network: egress
  network_allowlist:
    - "epub.yourcloudlibrary.com"
    - "yourcloudlibrary.com"
    - "*.yourcloudlibrary.com"
  subprocess: true
```

So the allowlist concept is real and already authored by a plugin — but the host
**does not enforce it** against the plugin's own `httpx` clients (`ycl/api/client.py`
talks to `ebook.`/`epubservice.`/`epub.yourcloudlibrary.com` directly). The honest
status from §8.2 stands, but sharper: the host has the *declaration* and no
*enforcement*. YCL — which already opted into `egress` + an allowlist — is the
natural first enforcement target if/when §3.2 becomes real work. (Logos opted out
with `network: full`, so it can't be the test case.)

### 9.4 YCL's healthcheck is weaker than Logos's — presence, not liveness

Logos's `check_session` does a live tri-state probe of `/api/app/me`. YCL's
`ycl.auth_status` only checks **cookies-on-disk + a decodable config cookie**
(`tools/auth_status.py`) — no live call — so it can't cheaply distinguish
`ok` from `unauthenticated` (expired-but-present cookies). A live probe is
available but costs a request (`check_book` with `live_status`). And YCL has **no
background keeper / silent renewal** — cookies expire and the user must re-run
`python -m ycl.cli.login` manually.

Consequences for `ProviderStatus` (§3.3):

- The probe must be **provider-defined**, not host-imposed — each plugin knows how
  expensive/accurate its own check is. Logos returns a true `ok`/`unauthenticated`;
  YCL returns at best `unconfigured` (no cookies) vs. an *optimistic* `ok` (cookies
  present), upgrading to a real liveness check only if it chooses to spend a request.
- Because YCL won't auto-renew, `unauthenticated` is a **frequent, expected** state
  — which makes the status-surfacing work (P3) even more valuable than the
  Logos-only analysis suggested. The fan-out telling the user "YCL: loan session
  expired — run `python -m ycl.cli.login`" is the difference between the feature
  feeling reliable and feeling broken.

### 9.5 Net effect on the plan

- §8.4 priority order is unchanged, but **P3 is now even more clearly #1**: two
  independent plugins both need it, and YCL's frequent-expiry, presence-only auth
  makes silent `[]` actively misleading.
- Add to the contract: `SourceSearchProvider.healthcheck()` is **optional and
  provider-owned**; providers without it are treated as `unknown` (not `ok`), and
  the tool reports that honestly.
- The reference-provider work should ship **both** a Logos provider (discovery)
  and a YCL provider (holdings) — they exercise the two archetypes and prove the
  `_merge`/`also_available_via` fusion across them. ~30 lines each.
- Remaining unchecked plugins: `marginalia-plugin-kindle`,
  `marginalia-plugin-books`, `marginalia-plugin-academic-journal`. Kindle/books
  are likely holdings-style; academic-journal is discovery-style. Worth a pass
  before finalizing, but unlikely to introduce a third archetype.

## 10. Plan: turn YCL into a discovery provider (overall-library catalog search)

**Goal (user's vision):** "ask the MCP for content → it finds books across enabled
libraries → potentially ingests them, without me listing each one." Today YCL only
knows borrowed books (§9.1). This plan adds real catalog search and exposes it via
both a direct MCP tool and the `search_sources` fan-out. **Kindle is out of scope**
for now — its content-ingestion model needs separate changes.

### 10.1 What the browser probe found (reverse-engineered, session-cookie auth)

Drove the saved YCL session (`~/.marginalia/plugins/yourcloudlibrary/cookies.json`,
library "Palm Beach County Library System") in headless Playwright and captured the
network traffic. Catalog search is **not Coveo** — it's two Remix `_data` loader
routes on `ebook.yourcloudlibrary.com`, both authenticated by the existing session
cookie (no bearer token):

**A. Suggestion / autocomplete — `GET /library/{name}/suggestion`**
- params: `suggest={text}&_data=routes/library.$name.suggestion`
- **Stateless: works via plain `httpx` with the session cookie** (verified).
- Returns `{suggestions: [{bibliographicIdentifier, isbn, title, subtitle,
  contributors[], label, matchedText, ...}]}`.
- `bibliographicIdentifier` IS the `book_id` used by `ycl.scrape_book`/`ycl.ingest_book`.
- **Limitation:** title/author **prefix** matcher. `"quantum mechanics"` → 29 hits,
  but `"augustine confessions"` → 0 (multi-word non-prefix). Good for
  title/author lookups, weak for topical/relevance queries.

**B. Full search — `GET /library/{name}/search`**
- params: `query={q}&format=&available=any&language=&sort=&orderBy=relevence&owned=yes&_data=routes/library.$name.search`
- Relevance-ranked (`orderBy=relevence` [sic]); handles multi-word queries.
- Returns `results.search = {totalItems, totalSegments, query, items: [...]}`,
  20 items/segment. Each item is rich:
  `id`/`bibliographicIdentifier` (book_id), `documentId`, `isbn`, `title`,
  `subtitle`, `authors[]`, `contributors[]`, `yearPublished`, `format`,
  `summary`, `subjects[]` (BISAC), `seriesTitle`, `language`, `publisherName`,
  `matchingScore` (→ confidence), `rating`, `imageLinkThumbnail`, and **live
  availability**: `totalCopies`, `currentlyAvailable`, `currentlyLoaned`,
  `currentlyReserved`, `isPayPerUse`.
- **CATCH — it requires a warmed browser context; pure httpx cannot reproduce it.**
  Exhaustively tested:
  - Cold `httpx` GET (any header combo) → `results.search={query}`, empty.
  - `httpx` with the browser's *exact* header set (`accept: */*`,
    `sec-fetch-site: same-origin`, **no `X-Requested-With`**, brotli decoded),
    cookies shared, `/featured` fetched first → still empty.
  - Suggestion-priming (GET `/suggestion` then `/search` on one client) → empty.
  - Playwright `ctx.request.get(searchURL)` **cold** → HTTP 204, empty.
  - Playwright `ctx.request.get(searchURL)` **after `page.goto(/featured)`** →
    **200, `totalItems=29, items=20`.** ✅
  So loading a page in the browser context establishes session state (most likely
  an in-memory/httpOnly cookie not present in `cookies.json`, since `/featured`
  emits no `Set-Cookie` visible to httpx) that the `/search` loader requires. The
  edge also appears to gate real-vs-empty responses on browser-only request
  characteristics. **Conclusion: full search must run through a warmed headless
  Playwright context** (load `/featured` once, reuse the context, then issue
  `ctx.request.get` per query — no page render/typing needed per search). The
  plugin already bundles Playwright + Chromium, so this is in-band, just heavier
  (~1 warm context + cheap API-style calls).

### 10.2 Availability mapping (full-search item → `Availability`)

| YCL item condition | `Availability` | `ingest_action` |
|---|---|---|
| `currentlyAvailable >= 1` | `borrowable` | `ycl.ingest_book{book_id}` (auto-borrow path TBD) |
| `currentlyAvailable == 0 && totalCopies >= 1` | `borrowable` (+`metadata.hold_required=true`) | — (must place a hold first) |
| `isPayPerUse == true` | `purchasable` | — |
| already in `BorrowStore` for this library | `in_corpus` if ingested, else `borrowable` | `ycl.ingest_book{book_id}` |

`matchingScore` → `SourceMatch.confidence` (normalized). `bibliographicIdentifier`
→ `source_id`. The provider can short-circuit `in_corpus` via the local
`BorrowStore`/`find_existing` enrichment already in `search_sources`.

### 10.2b IMPLEMENTATION STATUS (built in marginalia-plugin-yourcloudlibrary, v0.2.0)

Shipped and live-verified:

- `ycl/api/catalog.py` — `CatalogSearcher` (persistent warmed Playwright context,
  loads `/featured` once then `ctx.request` per query) + `CatalogItem`. Live test:
  "augustine confessions" → relevance results suggestion can't return.
- `ycl/api/client.py` — `YclClient.borrow()` / `return_book()` via the detail
  loader. Live test: `CAN_LOAN → LOAN → CAN_LOAN`, account net-zero.
- `ycl/source_provider.py` — `YclSourceProvider` (discovery). Conforms to the
  core `SourceSearchProvider` protocol (`isinstance` check passes); maps
  `CatalogItem → SourceMatch` with availability + normalized confidence +
  `ingest_action`; optional `healthcheck()`. Imports the types from
  `research_engine.domain.source_search` (the SDK aggregate is unavailable in the
  skeleton worktree; domain module is the canonical, pydantic-only source).
- `ycl/tools/search_catalog.py` — read-only discovery MCP tool.
- `ycl/tools/acquire_and_ingest.py` — borrow→(delegate to `ycl.ingest_book`)→
  optional return. Respects existing loans (never auto-returns a book that was
  already on loan). `return_after=True` default = recycling.
- `pack.yaml` v0.2.0 — registers both tools + `provides.source_search`. Parses
  against the branch `PluginManifest`. 46 existing unit tests pass + 4 new
  (`tests/unit/test_catalog.py`).

**Host-env integration test — built and running** (`tests/integration/` in the
plugin, `integration` extra pulls `research_engine` editable from
`../MarginaliaAI/packages/core`):

- Fixtures (`conftest.py`): a **real `IngestionOrchestrator`** built against the
  dev Postgres (`build_engine` + `PGDocumentRepo`/`PGPassageRepo`, `RE_DB_URL` or
  the default :5435) with a deterministic `FakeEmbedder` (only the embedder is
  faked; doc/passage/embedding/FTS writes are real). Skips if the DB or a YCL
  session is absent. `ycl_session` guard skips without cookies.
- Results (live, real Postgres): `test_catalog_search_live` ✅,
  `test_provider_live` ✅, `test_acquire_borrow_and_return_safety` ✅ (borrows a
  live available book and verifies the loan is always returned — no leak — even
  when ingest fails), `test_acquire_respects_existing_loan` ✅ (never returns a
  pre-existing loan), `test_acquire_full_ingest` **XFAIL** (see §10.7).
- Hardening the test forced: `acquire_and_ingest` now returns the loan on ingest
  **failure** too (not just success) when it was the borrower — no leaked slots.

### 10.7 BLOCKER (diagnosed): the scrape session is **expired**, not a different auth

The full borrow→**scrape**→ingest path fails at scrape: `get_manifest(isbn)` →
`epubservice.yourcloudlibrary.com/manifest/{isbn}` → **401**. The reader-login
investigation pinned the real cause — and corrected an earlier wrong guess:

- The saved cookies (`~/.marginalia/plugins/yourcloudlibrary/cookies.json`) were
  captured **2026-05-07 12:04** and **successfully scraped** `onc5689` (492 KB of
  real text on disk) at **12:22 the same day** — using the *same httpx →
  epubservice path the plugin still uses*. So the scrape path is correct and the
  reader is **not** a separate AEM auth system (the browser-reader redirect to
  `www…/userinfo.json?authorizableId=anonymous` was a red herring — the scraper
  never uses the browser reader).
- The cookies are simply **expired**: `__session_PROD` (the reading cookie)
  expired **2026-05-08** — a **~1-day lifetime** — and `__config_PROD` on
  2026-06-07. Today (2026-06-30) both are dead.
- Why it was masked: **`epubservice` strictly enforces `__session_PROD` expiry
  (→ 401), while the catalog (`ebook.`) is lenient** and still serves
  search/borrow on the stale session. So "I can borrow" ≠ "I can read", and the
  old `auth_status`/`healthcheck` (presence-only checks) reported a healthy
  session — the trap that produced the wrong AEM theory.

**Fix: re-run `ycl.cli.login`.** A fresh `__session_PROD` restores scrape (for
its ~1-day window). This must be done by the user (interactive library
card+PIN/SSO); it can't be automated here.

**Hardening shipped so this is never silently misdiagnosed again:**
`api/cookies.py:reading_session_status()` checks `__session_PROD` presence +
expiry. Wired into: the provider `healthcheck()` (returns `status="expired"` with
a re-login hint), the `ycl.auth_status` tool (now returns `can_read` +
`reading_hint`, not just `authenticated`), `ycl.acquire_and_ingest` (preflights
and **fails fast without borrowing** on a dead session — no wasted loan), and
`ycl.cli.login` (prints the reading-session expiry so the short window is
visible). 4 unit tests in `tests/unit/test_reading_session.py`.

**RESOLVED + verified end-to-end (2026-06-30).** After a fresh `ycl.cli.login`
(via a catalog-direct flow — opening `ebook…/library/{name}/featured` so the
sign-in sets `__config_PROD`+`__session_PROD`; the old www-marketing start point
didn't reliably yield them), the full path ran live: borrowed "The Shortest
History of Scandinavia", manifest resolved (36 chapters, **no 401**), ingested
into real Postgres (200 passages), loan returned. **All 5 integration tests pass**
(the `xfail` is gone — `test_acquire_full_ingest` now PASSES, guarded by a
`ycl_can_read` fixture that *skips* on a stale session rather than failing).

Two robustness fixes the live run forced:
- `core.documents` has `UNIQUE(content_hash, source)`, so `force_reingest` of
  *identical* content throws `IntegrityError` — the tests use fresh (un-ingested)
  titles instead of forcing duplicates.
- `acquire_and_ingest` now wraps the ingest delegation in try/except so an ingest
  **exception** (not just an error result) still returns the borrowed loan — no
  leak under any failure mode.

The find→borrow→ingest→return vision is therefore fully working; the only
operational caveat is the ~1-day reading-session lifetime (re-login when
`auth_status.can_read` goes false).

### 10.2c Code-review fix pass (high-effort review — all findings addressed)

- **Browser lifecycle:** `_ensure_warm` tears down partials on any `BaseException`
  (no orphaned Chromium); the provider uses a **non-blocking background warm** —
  returns `[]` on a cold first fan-out, warms out-of-band, hits a warm context the
  next round — so it fits the host's 8s `wait_for`. Provider + `search_catalog`
  share one process-wide `CatalogSearcher` (`get_shared_searcher`); a search lock
  serializes concurrent calls.
- **Loan safety:** `borrowed_by_us` is set the moment `borrow()` returns without
  raising (closes a leak path); auto-return **retries** and surfaces
  `return_failed`/`warning` instead of swallowing; `BorrowStore` is reconciled
  after a return so a returned book no longer reports as an active loan.
- **Correctness:** catalog query is `urlencode`d (titles with `&`/`#`/`=` work);
  pay-per-use maps to `purchasable` regardless of copy count; defensive raise when
  YCL returns `{book, error}` on a borrow/return refusal.
- **Cleanup:** shared `_book_from_raw` Book mapping; `_err` imported from `ingest_book`.
- **Verified:** 64 unit (new URL-encode, `_book_from_raw`, `_availability` cases) +
  5 live integration green; non-blocking-warm, encoding, and no-leak confirmed live.

### 10.2d Second review pass (the shared-singleton fix above was over-engineered)

A follow-up high-effort review caught that making the searcher a process-shared
singleton with `aclose()` tearing it down created its own bug cluster. Corrected:

- **Ownership reverted:** the provider owns its own `CatalogSearcher` and the
  `ycl.search_catalog` tool keeps its own module-level one — single owner each, no
  cross-teardown. Removed `get_shared_searcher`/`close_shared_searcher`.
- **Warm-task teardown:** `CatalogSearcher.close()` now cancels an in-flight
  background `_warm_task` first, so it can't relaunch a Chromium after teardown.
- **Loan leak on cancellation (the real one):** `acquire_and_ingest` now returns a
  loan it opened even when ingest is **cancelled** (host timeout/shutdown) — the
  return runs in a `finally` via a shielded `_return_loan` helper, then the
  `CancelledError` re-propagates. Unit-tested (`test_acquire_loan_safety.py`).
- **Auth signal:** `catalog.search()` raises `NotAuthenticatedError` on 401/403
  instead of returning `[]` (so the user is told to re-login, not "no results").
- **Return confirmation:** `_return_loan` confirms a return via a follow-up
  `get_book` when the response shape is ambiguous, avoiding a false `return_failed`.
- **Cleanup:** `acquire` imports `_LOANED_STATUSES` from `client` (dedup).
- Intentional / kept: cold first fan-out returns `[]` (the chosen background-warm
  trade-off); `borrowed_by_us` set on any non-raising borrow (no-leak-safe).
- **Verified:** 66 unit (2 new cancellation tests) + 5 live integration green;
  own-instance independence, warm-task cancellation, and no-leak confirmed live.

### 10.3 Build order

1. **`ycl.search_catalog` MCP tool (suggestion-backed, stateless).** New tool in
   the YCL plugin: `GET /suggestion` via the existing httpx client, map to a clean
   result list (`book_id, isbn, title, authors`). Ships value immediately, no
   browser, no new auth. This alone satisfies "find books by title/author without
   listing them."
2. **`YclSourceProvider` (SourceSearchProvider).** ~30-line adapter that calls the
   same suggestion path and emits `SourceMatch` (archetype: discovery), plus a
   `healthcheck()` over `ycl.auth_status` (cookies-present → optimistic `ok`;
   none → `unconfigured`; §9.4). Registers via `provides.source_search` (Phase 4e).
   This wires YCL into the cross-library fan-out.
3. **Full relevance search (`/search`) — phase 2.** Finish reverse-engineering the
   stateful init, then either (a) replicate it with httpx (preferred — cheap), or
   (b) fall back to a headless-browser fetch (the plugin already bundles Playwright
   and does browser login, so a ~2–5s per-search headless call is feasible, just
   heavier). Upgrade the tool + provider to use it for relevance + live
   availability + the richer item fields in §10.2.
4. **Ingest-without-listing loop.** With the provider returning `borrowable`
   matches carrying `ingest_action`, the agent can fan out a topical query, pick
   the best matches, and execute `ycl.ingest_book` per match — the user's vision,
   delivered through the existing read-only-search + ingest-action split.

### 10.5 Borrow / return — CONFIRMED via live capture (user-authorized)

Borrowed an available throwaway title ("History", `documentId=w9ymbg9`) and
returned it — account left net-zero. Both endpoints are cookie-authenticated
loader GETs on the **detail route** (work over plain `httpx`, unlike full search):

- **Borrow:** `GET /library/{name}/detail/{documentId}?action=borrow&itemId={documentId}&_data=routes/library.$name.detail.$id`
- **Return:** `GET /library/{name}/detail/{documentId}?action=return&itemId={documentId}&_data=routes/library.$name.detail.$id`

Observed **status state machine** (the `book.status` field):

```
CAN_LOAN  --(action=borrow)-->  LOAN (canRead=true)  --(action=return)-->  CAN_LOAN
CAN_HOLD  = not available; must place a hold (not borrowable right now)
```

The plugin's `YclClient.get_book(id)` already drives this exact loader, so borrow/
return are a thin add: same URL + `action`/`itemId` params, parse `book.status`.

**Loan limit — investigated, not cleanly fetchable headlessly.** Real route ids
came from the Remix manifest (`window.__remixManifest`): `mybooks`,
`mybooks.current`, `mybooks.history`, `mybooks.holds`, `mybooks.saved`. But their
loaders return **HTTP 400 "Unexpected Server Error"** even with the `segment`
param the chunk reads — they appear to need SPA-generated params (`udid`/`token`
seen in `mybooks.current` JS) that aren't reproducible headlessly. The
`__config_PROD` cookie has no limit field either. **Practical substitute (now
used in code):** the per-book `book.canBorrow` flag gates whether a borrow is
allowed, and an over-limit borrow returns `book.error` (raised as `YclApiError`).
The recycling loop returns each book immediately, so it never approaches the cap —
the numeric limit isn't needed. Pinning it exactly is a deferred follow-up.

### 10.5a CRITICAL id mapping: search id ≠ borrow id

Verified on ISBN 9780310522744 ("Four Views on the Church's Mission"):

| field (full-search item) | value | usable as detail/borrow id? |
|---|---|---|
| `id` / `bibliographicIdentifier` | `ispil6oqn` | ❌ 500 |
| `catalogItemId` | `dig-bibg-clispil6oqn` | ❌ 500 |
| **`documentId`** | **`onc5689`** | ✅ **this is the book_id** |

So the **discovery→acquisition join is `documentId`** (and ISBN as a fallback
cross-check). Consequences:

- The **suggestion endpoint is insufficient for acquisition** — it returns only
  `bibliographicIdentifier`/`isbn`, *not* `documentId`, so its results can't be
  borrowed/ingested directly. Another reason the **full search is the backbone**:
  it's the only endpoint that yields the borrowable `documentId`.
- `SourceMatch.source_id` for YCL should be the `documentId`; `ingest_action`
  becomes `ycl.acquire_and_ingest{book_id: documentId}` (or the existing
  `ycl.ingest_book` once it accepts a freshly-borrowed documentId).

### 10.6 Dual-mode by tool intent (per user)

Two distinct caller intents, two behaviors — don't conflate:

- **"What's available?"** → read-only discovery. `ycl.search_catalog` /
  `search_sources` return matches with availability; **no borrow**. This is the
  default and is always safe.
- **"Get me this / ingest it"** → acquisition. A separate, clearly-named action
  (e.g. `ycl.acquire_and_ingest`) borrows (if needed), scrapes, ingests, and —
  per the recycling idea — optionally returns to free the slot. This path mutates
  the library account and should require the limit/borrow/return work above.

The `SourceMatch.ingest_action` descriptor already models this split: discovery
stays read-only; the action is executed only on explicit intent.

### 10.4 Open decisions for the user

- **Auto-borrow:** ingesting an *available* YCL book may require placing a borrow
  first (`currentlyAvailable` consumes a loan slot / hold). Should the provider's
  `ingest_action` auto-borrow, or only surface `borrowable` and let the user
  confirm the loan? (Loans are a finite library resource.)
- **Suggestion-only v1 vs. wait for full search:** ship the stateless
  suggestion-backed tool now (prefix-limited), or hold for the stateful relevance
  search? Recommendation: ship v1 now, upgrade in phase 2 — the abstraction
  doesn't change, only the endpoint behind it.
