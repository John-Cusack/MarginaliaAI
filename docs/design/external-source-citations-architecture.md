# Citeable non-ingested sources — architecture

**Status:** Design guide. Written 2026-09-12 against engine `main` @ `3b5251d`
(migrations 001–008; 009/012/P3 not built), Logos plugin @ `2cfcef0`
(branch `John-Cusack/scripture-filter-executes`), workspace branch
`John-Cusack/readme-support-link`.
**Controlling doc:** `works-architecture-master.md`. This guide proposes no
master amendment except the one flagged in §8.1.
**Companion:** `external-source-citations-implementation.md` (build steps).
**Ticket:** `briefs/2026-09-12-external-source-citations.md`. No code, no
migrations, no corpus writes in this ticket.

## 0. Recommendation in one paragraph

**Ingest the six books as ordinary corpus documents through the existing
`logos.ingest_book` pipeline (option A), then cite them with the unchanged
front-matter shape.** It is the only option that satisfies all twelve
works-contract invariants with zero amendments: every other option breaks
invariant 1 (anchor = `document_id` + offsets into `document_texts`) and
builds the second parallel citation universe the ticket declares failure.
And the expensive machinery already exists and is proven on harder cases —
TDNT's 7,982-article hierarchy, LSJ's 188,724-article walk, and this survey's
own HALOT citations verifying `exact` (§6). The strongest objection is
proportionality: ~35.7M new characters (≈5× HALOT) plus embeddings and
walk-hours to ground dozens of citations. Answered in §4: the standing
decision already accepts personal-use ingestion; the "dozens" miscounts —
every future work re-pays excerpt round-trips while whole books pay once and
become searchable (the bridge doc's motivating query is literally "which
works cite TDNT s.v. *deror*?"); and any single unwalkable book falls back to
per-book excerpts without changing the model (§7).

## 1. The gap

Created works cite `(document_id, char_start, char_end)` into
`core.document_texts`, checked by `verify_quote`. Six reference works the
survey `works/mishpat-tsedaqah-survey.md` rests on are not corpus documents,
so §§2.2–2.3, 3.1–3.3, the Hermeneia note (§5, line 1087), the Koch/NIDOTTE
Ps-72 discussion (lines 1212–1224) and the synthesis tables (lines 1271–1275)
are uncited prose — which the file contract refuses to recognize as
citations. Goal: a BDB or TLOT entry as citable and checkable as an LHB
verse, with no second citation universe.

## 2. Verified substrate (re-verified live 2026-09-12; map, not memory)

Engine `main` is still `3b5251d`; works Phase 0 (guide Steps 1–2) is
**unbuilt** — no `work_*` tools, no `set-key` in the engine (only
`PGDocumentRepo.update_metadata`, which `set-key` will call). Corpus state:

| Claim | Live evidence |
|---|---|
| HALOT `LLS:46.30.12` = doc `01a039d7-…`, 13,247 passages, 7,034,729 chars, parser `plugin_direct` 1.0 | `SELECT` on `core.documents` / `passages` / `document_texts` |
| Survey c54 verifies `exact` at 6326805–6326907, page locator 1615 | `research-engine verify-quote --json` (offsets match the file byte-for-byte) |
| Survey c1 (LHB Gen 18:19) verifies `exact` at 2287–2319 | same CLI |
| Entry node `section "I שַׁעַר"` (`SHIN.568`) spans 6326805–6340364 | `SELECT` on `core.document_nodes` |
| Present: BDAG `LLS:46.30.18`, TDNT `LLS:46.10.16`, Louw-Nida `LLS:46.30.4` | `SELECT` on `core.documents` |
| Absent: BDB, CHALOT, NIDOTTE, TLOT, TWOT, Hermeneia | same query returns no rows |
| Edition keys exist only for ESV/LHB/WLC; 31 docs (incl. all lexica) are keyless; HALOT metadata is `{resource_id, abbreviated_title: HALOT, authors: []}` | `GROUP BY metadata->>'edition_key'` |

Logos read path (all read-only, this session):

| Claim | Live evidence |
|---|---|
| BDB `LLS:46.30.16` owned: 6,194,845 chars; CHALOT 1,472,491; NIDOTTE 17,454,593; TLOT 4,871,853; TWOT 4,596,504 | `GET /api/app/books/{id}` × 5 |
| Hermeneia Amos owned as **`LLS:HRMNEIA30AM`** (1,086,906 chars, 10 TOC nodes), **not** `LLS:HERMAM` (HTTP 500) — the survey's §1 table is wrong; correct before any build | `GET /api/app/books/…` + `/api/app/library?query=Hermeneia+Amos` |
| word-study `משפט` → lemma `lemma.lbs.he.מִשְׁפָּט`, links only HALOT/BDB/Gesenius (`LLS:46.30.24`); nothing for NIDOTTE/TLOT/TWOT/CHALOT | `GET /api/app/guides/word?reference=משפט`, `definition`-kind sections |
| `logos.get_entry` returns BDB `LBDB.2184.4` verbatim (מִשְׁפָּט, indexed off 5710842 len 5265, 85,771 HTML chars) | live `handler(resource_id, headword)` call; unit tests 17/17 pass |
| Ingest store = one document per book (`source logos:{rid}`, full text, rebased offsets, node tree, page locators); installed plugin's `ingest_book.py` is byte-identical to its repo | `_store_resource` (`ingest_book.py:1238`), `diff` installed vs repo |
| Lexicon chunking = `VerseChunker` 5.0, paragraph-split with token cap; `draft.text == text[start:end]` invariant | `chunker.py:31` |
| BDB root sits mid-chain (`LBDB.468.0`, has `previousArticleId`) → needs the rewind walk; the other five roots are chain tops | book-root responses |

Three orientation corrections (§6 of the ticket requires prominence): (1)
the Hermeneia id above; (2) `logos.get_entry` — the verbatim-entry read
path options B/C would reuse — is **uncommitted** in its repo (`??`
on `John-Cusack/scripture-filter-executes`), though already deployed to
`~/.research-engine/plugins/logos@0.1.0`; any design touching it depends on
that branch landing; (3) the live file contract uses `edition_key`
(commit `2942d3a` renamed it); `docs/design/works-contract/` still shows
`zotero_key` and is stale, and master §5's DDL still says `zotero_key` —
flagged in §8.1.

## 3. Decision matrix

Criteria: **Fidelity** (satisfies the 12 invariants unamended), **Check**
(`verify_quote` tiers apply unchanged), **Search** (entries retrievable, not
just quotable), **Build** (new code/surface), **Run** (per-citation cost and
flakiness), **Revert** (cost of undoing). Scores are relative, 1 (worst) to
5 (best); weights reflect the ticket ("no second citation universe" is a
hard fail, not a preference).

| Criterion (weight) | A: ingest six books | B: external model + live verify | C: excerpt-only ingest | D: marked prose |
|---|---|---|---|---|
| Fidelity ×3 | 5 — zero amendments (§5) | 1 — breaks 1, 4, 6; second universe | 3 — model holds, corpus goes two-tier | 1 — contract refuses prose as citation |
| Check ×3 | 5 — tiers unchanged (§4) | 2 — tiers reimplemented over network; vendor edits rot silently | 4 — tiers hold inside excerpts; context clipped at edges | 0 — nothing checks |
| Search ×1 | 5 — entries + nodes + FTS | 1 — live lookup per question | 2 — only cited entries exist | 0 |
| Build ×1 | 5 — no pipeline code; run the tool | 1 — new shape, new verifier, new rules | 3 — small excerpt storer + key discipline | 5 — nothing |
| Run ×1 | 3 — one walk + embed per book, then free | 1 — auth + latency + 500s per verify, forever | 4 — tiny ingest per entry, repeated forever | 5 — nothing |
| Revert ×1 | 4 — delete six docs (RESTRICT guards cited ones) | 5 — nothing stored | 3 — fragment docs accumulate | 5 — nothing stored |
| **Weighted total** | **56** | **20** | **37** | **18** |

Why B fails on Fidelity, precisely: no `document_texts` row means
invariant 1 has no address, invariant 4 has no span row, invariant 6
(`ON DELETE RESTRICT`) has nothing to protect — Logos can edit text under a
stored offset with no `parser_version` tripwire, so re-verify discovers
nothing and previously-`exact` citations rot without a record. Live
verification also inherits the failure modes observed this session
(auth renewal, HTTP 500s) on every `work_verify` run, forever.

Why C is second, not first: it keeps the model but splits the corpus into
whole books and fragment docs. Fragments clip passage context at the entry
edge (no straddling spans, truncated near-miss divergence), miss the book's
node tree, need per-entry documents that pollute "which works cite BDB"
joins, and each uncited-today entry costs a future ingest round-trip. It is
the correct **per-book fallback** (§7), not the primary.

Why D fails: the survey's thesis (judicial/forensic, not redistributional)
rests on these entries. Marking them non-verifying demotes the work's own
argument to decoration — and `not_found`-class content "does not ship"
(contract §3).

## 4. Why A answers the proportionality objection

Total indexed text ≈ 35.7M chars ≈ 5× HALOT ≈ ~60–70k passages — six
documents (+0.2% on 3,078) carrying the char volume of five HALOTs, the
largest single doc NIDOTTE at 2.5× HALOT, inside the verify window
machinery's proven range (the service comments cite a 23.2M-char document). Walk rate observed: ~110
articles/min; the walks are resumable (`logos_ingest_progress`) and the
store is one document per book. Against that one-time cost: (a) the
standing decision already accepts personal-use ingestion; (b) the survey
alone needs 8 headwords × 5 lexica plus TLOT/NIDOTTE topical articles plus
Hermeneia pericopes, and the next work pays excerpt round-trips again —
whole books amortize; (c) only whole books make entries *searchable*,
which is the bridge doc's whole point. Per-book go/no-go stays: if NIDOTTE
strains embedding/FTS, that book alone drops to excerpt fallback with no
model change.

## 5. Invariant walk (all twelve; satisfied or amendment-flagged)

1. **Anchor `(document_id, char_start, char_end)` into
   `document_texts.text`.** Satisfied: each book is one document; entries
   cite spans exactly like HALOT c54–c56 today.
2. **Never `normalized` as `exact`.** Satisfied: tiers come from the
   unchanged verifier (§6).
3. **Search never writes.** Satisfied: ingestion goes through
   `ingest_drafts`, not search; `get_entry` is read-only by construction
   (read path copied, writers never imported — `get_entry.py:150`).
4. **One span row per coordinates, resolver-owned.** Satisfied: 009/012
   machinery applies to the new documents with no change.
5. **Span owns address + slice; row owns quote + tier + locator.**
   Satisfied: unchanged.
6. **`ON DELETE RESTRICT` on cited sources.** Satisfied — and this is what
   makes ingest strictly stronger than live-verify: a cited book cannot
   silently vanish.
7. **Rendered strings never authoritative.** Satisfied: footnotes from
   `work_render`; entry titles from node headings are display only.
8. **Works are not `core.documents`.** Satisfied: the six books are
   *sources*, not authored text; nothing authored enters `passages`.
9. **No tables outside Alembic.** Satisfied: zero new tables. (The plugin's
   staging tables live in the plugin DB under its own migrations, as today.)
10. **Agents propose; a human act commits.** Satisfied: entry anchoring is a
    file edit the researcher accepts; no auto-ingest on search.
11. **`stdout` is the MCP transport.** Satisfied: no new tools in this path
    (`logos.ingest_book` already exists).
12. **Tests remove exactly what they create.** Satisfied: verification is
    `SELECT`s plus `Corpus`-helper fixtures (companion §E-steps).

## 6. Citation shape and tiers under the winner

Front-matter entries are byte-for-byte the existing contract — no new
fields, no new intents. Per field, today vs after 012/P3-1:

| Field | Today (Phase 0 file) | After 012 (rows) | After P3-1 |
|---|---|---|---|
| `id` | `[^cN]` handle | occurrence key | unchanged |
| `document_id` | new book doc uuid (recorded at ingest) | span's document | unchanged |
| `char_start`/`char_end` | offsets from `verify-quote` output | `source_spans` coordinates via resolver | unchanged |
| `quoted_text` | typed quote (must verify) | item's typed quote; span holds canonical slice | unchanged |
| `intent` | `definition` for lexicon senses (survey §§2–3 inventory word meaning), `support` for theological synthesis (Koch/NIDOTTE), `background` for commentary context (Hermeneia) | occurrence intent; granularity rules apply (`quotation`/`translation` must narrow; `MAX_QUOTE_CHARS` = 1000 forces sub-entry spans — BDB indexed entries run 5k+ chars) | unchanged |
| `edition` | free text (e.g. `"2nd"`) | item column | unchanged |
| `edition_key` | `BDB` / `CHALOT` / `NIDOTTE` / `TLOT` / `TWOT` / `HERMENEIA` (minted §E2); HALOT backfilled | `edition_id` backfilled from `bibliography.editions` (guide Step 6.6 upsert) | FK tightened |
| `locator` | `{entry: "מִשְׁפָּט", article: "LBDB.2184.4"}` + page refs where the book carries them | item locator | answers master §11.3 for lexica |

`verify_quote` tiers apply with zero change: `exact`/`normalized` ship
(`verified` = those two, `quote.py:115`); `near` → re-anchor
(`AUTH_QUOTE_UNVERIFIED`); `not_found` never enters rows; a book with no
stored text answers `no_canonical_text` (`AUTH_SOURCE_UNCHECKABLE`) — a
different failure from a bad quote, which is exactly the honesty rule that
kills option B. The Step 2.7b window hint works from search hits on the new
books; the region rule (§2.7c) is what forces entry *narrowing* rather than
whole-entry cites.

Before/after (survey; `document_id`s are recorded at ingest — shown as
handles, never fabricated):

- **BDB.** Before: §2.2 prose — "Judgment, act of deciding… Legal right,
  privilege, due…", frequency 422×, no citation. After: `c57:
  {document: <bdb-doc>, span: <verify-quote offsets>, quoted_text:
  "<verbatim BDB מִשְׁפָּט span, pulled in E3 via get_entry LBDB.2184.4 and
  verified>", intent: definition, edition_key: BDB, locator: {entry:
  "מִשְׁפָּט", article: "LBDB.2184.4"}}`. Exemplar of done: HALOT c54
  (`exact` 6326805–6326907, p. 1615).
- **TLOT.** Before: §3.3 prose — Koch's judicial-vs-governmental "basic
  meaning" dispute, uncited. After: `c58: {document: <tlot-doc>, …,
  intent: support, edition_key: TLOT, locator: {entry: "שפט"}}` over the
  verbatim Koch span. The Ps-72 Koch/NIDOTTE quotations (lines 1212–1224)
  anchor the same way or are cut — prose summaries of them do not ship.

## 7. Fallback without a second universe

If one book proves unwalkable (chain pathology beyond the three recovery
strategies, or NIDOTTE scale): ingest **that book's cited entries only** as
small `logos_book` documents (`source: "logos:{rid}:excerpt:{article}"`,
text = `html_to_markdown_with_refs` output chunked by the same
`VerseChunker`, metadata carries `resource_id` + `article_id` +
`edition_key`). Same front-matter shape, same tiers, same rules — smaller
context, no node tree, one document per entry. The decision is per book,
taken at the E1 go/no-go, and never changes the model.

## 8. Open decisions (researcher-owned)

1. **`edition_key` vs master §5 `zotero_key` — APPROVED and EXECUTED
   2026-09-12.** Renamed in master (14 hits) + bridge (9 hits) + two prose
   stragglers; remaining `Zotero` mentions are legitimate (CSL import
   source, eventual record authority). Lineage: `zotero_key` entered in
   satellite commit `80f5909`; `2942d3a` renamed to `edition_key` (most
   recent, on the satellite branches only — engine `main` @ `3b5251d`
   carries no works contract at all). E2 keys already written as metadata
   values, unaffected. Out of scope, flagged: `research-program-on-
   marginalia.md` (4 hits, older program doc) and the already-stale
   `works-contract/` snapshot were left untouched.
2. **Locator vocabulary for entries** (master §11.3) — **CONFIRMED
   2026-09-12.** `{entry, article}` + page refs where the book carries
   them; E3 writes this shape.
3. **NIDOTTE go/no-go** at 17.5M chars (largest doc in corpus if ingested):
   full ingest default; excerpt fallback per §7 on embedding/FTS strain.
4. **`get_entry` landing**: uncommitted on the plugin's
   `verbatim-lexicon-entry-retrieval` namesake branch — required for E3
   anchoring and any §7 fallback. Confirm merge/commit before E3.
5. **Phase-0 gate**: E3 needs `work_verify` (works Step 2.4); fallback is
   hand-verification via `verify-quote` CLI, but gates stay red until the
   tool exists. Out of scope: Gesenius `LLS:46.30.24` (linked but uncited —
   do not ingest for this ticket).

## 9. Copyright / personal use

Standing decision governs: private-corpus ingestion for personal research
is acceptable. No new handling: master decision 3 keeps rights out of
scope; the corpus DB is local; only author-quoted spans (fair-use scale)
enter work files while full text stays private; `get_entry` returns are
transient reads, never stored. No export gate, no classification column.

## 10. Guide self-check (ticket §5)

Invariants §5 ✓. Master §§4/5/9/11: no silent contradiction — §4/§9
untouched (no new migration, no renumbering); §5 naming conflict listed in
§8.1; §11 extended, not contradicted, by §8. Survey: BDB + TLOT
before/after in §6. Investigator's choice: §0 (two reasons — zero-amendment
fidelity; proven machinery — plus the proportionality objection answered).
