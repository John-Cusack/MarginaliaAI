# External sources — implementation guide

**Status:** Implementation guide, written 2026-09-12 against the engine at
`/home/john/repos/MarginaliaAI` @ `3b5251d` and the Logos plugin @ `2cfcef0`.
It implements `external-source-citations-architecture.md` (option A: ingest
the six books), which the master governs without amendment except §8.1.
**Audience:** an implementer, human or LLM, who has not read the design docs.
**Companions:** the architecture guide (why), `works-implementation-guide.md`
(house rules — its Part 0 invariants bind every step below).

## Part 0 — Read this first

### 0.0 Progress (2026-09-12 — read before planning)

E1 (ingest) and E2 (keys) are **done** — six documents stored, seven keys
set, results in §F. Start at E3. Do not re-ingest: completed walks return
`already_complete` and stored chunks are never re-stored, but re-running
wastes hours. E3's gates: arch §8.1/§8.2 researcher-approved (see §F);
`get_entry` gate below is "present and tested," not "merged";
`work_verify` (works Step 2.4) is still unbuilt, so E3 verifies by hand via
`verify-quote` CLI with tiers recorded in §F until the tool exists.

### 0.1 How to use this guide

One chapter per build-order step (E1–E5). Every step has a **gate**, a
**scope**, explicit **non-goals**, a **test table**, and a **done-when**. Do
not start a step whose gate is not met. This ticket's build writes no
corpus: E1–E4 are specified for the build ticket; E5 (researcher acceptance
and merge of the two guides) is this ticket's done. Where you must deviate,
record it in the change log (§F) before moving on.

### 0.2 Gates and conventions

- Environment, gates and lint are the works guide's Part 0: `make db`
  (pgvector:pg15, port 5435), everything through `uv run`, ruff only,
  `RE_DB_URL` from the nearest `.env`. Logos calls additionally need the
  plugin venv (`marginalia-plugin-logos`, `PYTHONPATH=<engine>/packages/core/src`)
  and a seeded SSO profile (`logos-login` if `auth_status` fails).
- Corpus writes in E1 run **only** in the build ticket, via
  `logos.ingest_book` with an ingestion client — never the walk-only path,
  never direct SQL. The six resource ids:
  `LLS:46.30.16` (BDB), `LLS:CNCSHAL` (CHALOT), `LLS:NIDOTTE`,
  `LLS:TLOT`, `LLS:46.50.3` (TWOT), `LLS:HRMNEIA30AM` (Hermeneia Amos —
  **not** `LLS:HERMAM`; that id HTTP-500s).
- Test isolation: E-step checks are `SELECT`s except through
  `research_engine.testing.Corpus`. Ingest changes dev-corpus counts, so E4
  re-runs the full suites to catch count-sensitive tests; failures there
  are findings, not license to edit tests.
- Precedence: master, then the architecture guide, then this guide.

---

## Step E1 — Ingest the six books

**Gate.** Logos `auth_status` healthy; embedding server reachable
(`RE_EMBEDDING_BASE_URL`); `get_entry` merged in its repo (arch §8.4) —
needed for E3, confirm now so a missing read path never strands an ingest.

**Scope.** One `logos.ingest_book` run per book, smallest first
(Hermeneia → CHALOT → TWOT → TLOT → BDB → NIDOTTE), each resumed to
completion through its own checkpoints. After each store, record in §F:
`document_id`, `source`, passage/node/article counts, `document_texts`
chars and `parser_version`, and wall time.

**Non-goals.** No pipeline code changes (rewind for the mid-chain BDB
root, `TITLE` roots elsewhere, numeric bisection where chains are numeric,
alpha-scan recovery otherwise, whole-book store — all already built). No
`max_articles` partials except as a smoke test (≤50 articles, then delete
the nothing-stored walk state — walk-only mode stores no corpus rows).
No Gesenius, no LSJ completion, no re-chunking of existing books.

### E1.1 Per-book go/no-go (at the walk, before the store)

The walk checkpoints without touching the corpus — that is the free look.
Go to store unless: chain coverage <95% of TOC article ids (where the TOC
speaks article ids), or >5% failed articles after recovery, or (NIDOTTE
only) the assembled text or passage estimate breaks the embedding budget.
No-go sends that book to the §7 excerpt fallback in the architecture
guide; the other five proceed. Record the decision per book in §F.

### E1.2 Tests

| Test | Kind | Asserts |
|---|---|---|
| one document per book | psql | exactly one `core.documents` row with `source = 'logos:{rid}'`, `document_type = 'logos_book'` |
| text landed | psql | `document_texts` chars same order of magnitude as the book-root `resourceLength` (indexed chars ≠ markdown chars — record both; investigate only past 2× divergence); `parser_version` recorded |
| offsets true | psql | for 5 sampled passages, `text = document_texts.text[char_start:char_end]` byte-for-byte |
| entries addressable | psql | node `SHIN`-style letter sections exist; ≥1 entry node carries `metadata.article_id` (spot: BDB `LBDB.2184.4`) |
| probe quotes verify | CLI | 3 quotes per book via `verify-quote --json` return `exact` at the stored offsets |
| no batch fragments | psql | no `source LIKE 'logos:{rid}:batch:%'` rows for the six (legacy path must not fire) |

**Done when** six (or five-plus-fallback) documents are stored, the §F
table is filled, and every row of the test table passes.

---

## Step E2 — Edition keys on the new documents (and HALOT)

**Gate.** E1 done. Works guide Step 2.2 (`work set-key`) built; if it
slipped, use `PGDocumentRepo.update_metadata` directly and say so in §F —
the keys are metadata either way. (2026-09-12: done via
`update_metadata`; `set-key` still unbuilt.)

**Scope.** Mint `BDB`, `CHALOT`, `NIDOTTE`, `TLOT`, `TWOT`, `HERMENEIA`;
set each on its document; backfill `HALOT` on `01a039d7-…` (the survey
already cites that key in c54–c56). File the one-line ingest-convention
note with the plugin: `doc_metadata` gains `edition_key` for future walks
(same transaction as today; picked up by guide Step 6.6's editions upsert
at 012 — no change to that step).

**Non-goals.** No `bibliography.editions` rows (012 owns that). No
backfilling BDAG/TDNT/Louw-Nida keys — optional ride-along, default omit.
No renaming: `edition_key` per live contract, pending master §5 amendment
(arch §8.1).

### E2.1 Tests

| Test | Kind | Asserts |
|---|---|---|
| keys present | psql | `metadata->>'edition_key'` equals the minted key on all seven docs |
| key agreement | CLI | `work verify` reports no `AUTH_EDITION_KEY_UNKNOWN` / `AUTH_EDITION_KEY_MISMATCH` for the seven docs (and no longer for survey c54–c56) |

**Done when** seven documents carry keys and the convention note is filed.

---

## Step E3 — Re-anchor the survey's uncited entries

**Gate.** E1–E2 done; `get_entry` present and tested (deployed copy at
`~/.research-engine/plugins/logos@0.1.0`, repo copy on the plugin's
`verbatim-lexicon-entry-retrieval` namesake branch — merge tracked
separately, not a precondition); works Step 2.4 (`work_verify`) built —
its gates are this step's done criterion. Fallback while Step 2 is
unbuilt (current state): hand-verify every new entry with `verify-quote
--json` and record tiers in §F; the step is done when the hand-verification
table is complete, and `work verify --gate review` re-confirms it once the
tool exists.

**Scope.** For each uncited span (survey §§2.2–2.3, 3.1–3.3, Hermeneia note,
Ps-72 Koch/NIDOTTE lines, tables): pull the verbatim entry with
`logos.get_entry` (`article_id` once known — never re-walk a picked
article), locate the cited words with the window-hint verify path, and
write a front-matter entry per the existing contract (arch §6 table).
Intents: `definition` for sense inventories, `support` for theological
synthesis, `background` for commentary context. Locators:
`{entry, article}` + page refs. Anything that cannot be anchored verbatim
is cut or rewritten — prose summaries do not ship (contract §3).

**Non-goals.** No edits beyond anchoring (no re-interpretation, no status
change — the file stays `draft`). No touching the 56 existing citations.
No new rule ids: every failure already has one (`AUTH_QUOTE_UNVERIFIED`,
`AUTH_SPAN_NOT_NARROWED`, `AUTH_SPAN_REGION`, `AUTH_EDITION_KEY_MISMATCH`).

### E3.1 Tests

| Test | Kind | Asserts |
|---|---|---|
| review gate | CLI | `work verify --gate review` on the survey: zero errors; remaining warnings each explained in §F |
| tier honesty | CLI | every new entry's stored tier equals a fresh `verify-quote` run; `normalized` never stored as `exact` |
| narrowing | CLI | no `quotation`/`translation` entry spans a whole passage (`AUTH_SPAN_NOT_NARROWED` absent); `definition` regions carry at most `AUTH_SPAN_REGION` |
| markers | CLI | every new entry id has ≥1 `[^cN]` marker; no dangling markers; multi-marked ids (one span cited by two lines, e.g. assessment restatements) listed in §F |
| grep parity | grep | every sentence making a claim about one of the six works carries a marker; exempt: section headings (labels), data tables (summaries of cited sections), the §1 source table, inventory enumerations, footnotes |

**Done when** the gate passes, the grep is clean, and the survey's every
lexicon/theological claim descends to characters.

### E3.2 Entry queue (resolve each via `get_entry`, then verify, then write)

Lexica (headwords; language filter where noted): מִשְׁפָּט (BDB
`LBDB.2184.4` known), צְדָקָה, צֶדֶק, צַדִּיק, שָׁפַט, דִּין (verb + noun),
רִיב (verb + noun) × BDB / CHALOT (disambiguate שער-class collisions via
the tool's candidate lists — never take the first hit silently); TWOT /
NIDOTTE / TLOT topical articles שפט / צדק. Theological synthesis: TLOT Koch
"basic meaning" span, NIDOTTE Reimer (צדק) + Schultz (Justice) spans, Ps-72
lines. Commentary: Hermeneia Amos pericopes covering 5:21–24 (plus 5:7,
6:12 cross-refs where the survey leans on them). Tables (§Phase 7):
anchor one span per row-group claim or cut the cell to background prose.

---

## Step E4 — Regression and corpus footprint

**Gate.** E3 done.

**Scope.** `make lint`, `make test`, `make test-integration` (needs
`make db`). Triage every failure as count-sensitive (corpus grew by six
documents — expected; fix the test's assumption, never its assertion) vs
real regression (stop, fix, re-run). Confirm the E1 `SELECT`s once more
against the final state.

**Non-goals.** No new tests for ingestion itself (plugin repo owns its
suite). No performance work beyond noting walk/store wall times in §F.

| Test | Kind | Asserts |
|---|---|---|
| gates | suites | lint clean; unit green; integration green on the grown corpus |
| footprint | suite | autouse corpus guard passes (tests still remove exactly what they create) |

**Done when** all three commands are green in one session and pasted in
the report.

---

## Step E5 — Researcher acceptance, guides merged

**Gate.** E1–E4 done (build ticket) — or, for this ticket, the two guides
written and self-checked.

**Scope.** The researcher reviews both guides against ticket §5 (invariants,
master §§4/5/9/11, survey before/after, §0 choice paragraph), signs or
amends the open decisions (arch §8 — especially §8.1 naming and §8.3
NIDOTTE), and merges the guides. Merging is the last act this design needs;
the build ticket starts at E1.

**Done when** both files are merged unopposed or with recorded amendments.

---

## Migration sketch (prose — no migration files in this ticket or the build)

Numbered, additive, independently revertible. The headline is that the
winner needs **no new migration**: every table the design touches exists
(001–008) or is already specified (009 spans, 012 `authored` +
`bibliography.editions` stub, P3-1 full bibliography).

- **M1 — Ingest (no DDL).** E1 writes six `documents` rows (+ texts,
  passages, nodes) through the existing `ingest_drafts` path. Additive by
  construction; revert per book with `DELETE FROM core.documents WHERE
  source = 'logos:{rid}'` (cascades to texts/passages; nodes cascade).
  Revert is refused while a work cites the book — that refusal *is*
  invariant 6 working, and the operator resolves the citations first,
  loudly.
- **M2 — Keys (no DDL).** E2 writes `metadata.edition_key` values only.
  Revert by clearing the key; `work_verify` reports `AUTH_EDITION_KEY_UNKNOWN`
  again, nothing else changes.
- **M3 — 012 pickup (already specified, unchanged).** The editions upsert
  (works guide §6.6) reads the six keys into `bibliography.editions`;
  `citation_items.edition_id` backfills by key comparison. No renumbering
  of master §9; no new table; downgrade story unchanged.
- **M4 — Excerpt fallback, conditional (no DDL).** If E1 no-goes a book,
  its entry-documents are ordinary `logos_book` rows with
  `source LIKE 'logos:{rid}:excerpt:%'` — same revert as M1, no mapping
  table, no flag column.
- **M5 — Ordering.** M1 → M2 → E3 anchoring → M3 at 012 → P3-1
  tightening. Each step reverts without touching the others: dropping an
  excerpt doc never affects whole-book docs; clearing keys never affects
  spans; 012's downgrade never affects corpus rows.

## §F — Change log (fill during build)

### E1 ingest table (built 2026-09-12, all walks 0 failed articles, 0 missed TOC)

| Book | document_id | passages | nodes | text chars | articles walked |
|---|---|---|---|---|---|
| Hermeneia Amos | `01a095b3-92e3-7b91-aa0f-9ebe92be8a98` | 1,100 | 460 | 1,127,221 | 449 |
| CHALOT | `01a095b4-c9ba-71e0-9848-6833ab34cb47` | 10,364 | 10,133 | 1,547,880 | 10,104 |
| TLOT | `01a095ba-2b31-7cf0-90c0-ab674e864c8b` | 3,637 | 462 | 5,037,470 | 429 |
| TWOT | `01a095c1-2a94-77c3-86fc-488b536c4193` | 8,838 | 7,081 | 4,670,671 | 7,075 |
| BDB | `01a095e6-6f83-7310-a4b0-23fcb1126146` | 14,405 | 11,458 | 6,029,727 | 11,431 |
| NIDOTTE | `01a0961f-66d0-7390-99b7-20ad677797f5` | 23,521 | 15,729 | 16,290,225 | 15,695 |

One document per book (`source logos:{rid}`), no batch fragments,
`parser_version` 1.0 throughout. Embedding server was down ~08:00–08:55;
walks checkpointed meanwhile and stores ran when it returned — no fallback
used, no deviation.

### E2 key table (built 2026-09-12, via `update_metadata` — `set-key` unbuilt)

`HERMENEIA`, `CHALOT`, `TLOT`, `TWOT`, `BDB`, `NIDOTTE` on the six docs
above; `HALOT` backfilled on `01a039d7-…`. Plugin `doc_metadata`
convention note: not yet filed.

### E4 gates (2026-09-12, on the grown corpus — all green)

`ruff check packages/ tests/`: clean. `pytest tests/unit/`: 1261 passed,
1 skipped. `pytest tests/integration/`: 101 passed (incl. the corpus
autouse guard — tests still remove exactly what they create).

### E3 anchoring table (built 2026-09-12, hand-verified via `verify-quote`)

126 entries (c57–c182), every one `exact` at write time; SQL re-check same
day: all substrings present at exactly the stored offsets, no drift. 182
entries total, YAML parses, no dup ids, no dangling markers. Multi-marked
ids (one span, two lines — assessment restatements): c62, c76, c132, c140,
c142, c144, c145, c162, c164, c166, c167, c168, c170, c175. Coverage: BDB
mishpat/tsedaqah, CHALOT all nine blocks, TWOT both, NIDOTTE mishpat/tsedeq
+ Ps-72 shaphat co-occurrence, TLOT both + seven Koch spans, Hermeneia
5:10/5:21-27/5:24, HALOT mishpat development line. §1 source table, §2.3
heading, and provenance note updated; `LLS:HERMAM` corrected to
`LLS:HRMNEIA30AM`. `work verify --gate review` re-confirmation still owed
once works Step 2.4 exists.

### Sign-offs (2026-09-12)

Arch §8.1 (`edition_key` amendment) and §8.2 (`{entry, article}` locators):
researcher-approved. Master §5 + bridge §4 mirror edits executed 2026-09-12
(23 code hits + 2 prose stragglers; program doc + stale `works-contract/`
snapshot deliberately untouched).

## §G — Operator appendix (exact recipes, verified 2026-09-12)

Engine venv runs everything (`/home/john/repos/MarginaliaAI`, `uv run`).
The plugin venv lacks engine deps — do not use it for container work.

- Walk (no corpus writes):
  `PYTHONPATH=/home/john/repos/marginalia-plugin-logos RE_ENV_FILE=/home/john/repos/MarginaliaAI/.env uv run python /tmp/logos_ingest_run.py '<RID>' walk`
  (runner: builds the container, calls `logos.tools.ingest_book.handler`
  without an ingestion client; re-run to consume the script before it
  rots — it lives in `/tmp`, not in any repo).
- Store: same with `full` instead of `walk` (passes
  `ingestion=container.ingestion`). Requires the embedding server
  (`curl -m 8 http://100.110.103.86:9882/` answering); bulk never falls
  back to local — if down, walks checkpoint and stores wait.
- Verbatim entry (read-only):
  `logos.tools.get_entry.handler(resource_id, headword)` — same env/venv
  as above; on multiple candidates pick `article_id` and re-call.
- Verify: `uv run research-engine verify-quote "<text>" --document-id
  <uuid> --json` (strip log lines before parsing: `awk '/^\{/,0'`).
- Keys: `PGDocumentRepo.update_metadata(UUID, {"edition_key": key})`
  (merge semantics) until `work set-key` exists.
- E4: `make lint`, `make test`, `make test-integration` from the engine
  root (last needs `make db`).
- Progress source of truth: `logos_ingest_progress` (`walk_complete`,
  `total_articles`); corpus truth: `core.documents` by `source =
  'logos:{rid}'`.
