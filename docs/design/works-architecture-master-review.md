# Review of `works-architecture-master.md` — findings, shortcomings, proposed amendments

**Status:** Review / audit. Not a spec; it proposes amendments for the master to
ratify or reject. Written 2026-09-04.
**Reviews:** `works-architecture-master.md` (the controlling doc), against its own
stated baseline — the engine checkout at `/home/john/repos/MarginaliaAI` @ `3b5251d`
— plus its companions (`authored-works-architecture.md`,
`work-citations-architecture.md`, `research-program-on-marginalia.md`), the file
contract (`works/README.md`, `works/_TEMPLATE.md`), and the engine code itself.
**Method:** every built/not-built claim in the master was verified against the
engine repo (migration files, tool registrations, schema, tests); every cross-doc
claim was traced to its source section. Line references are to the files as they
stand in this worktree at review time.

---

## 0. Verdict

The master doc is unusually good at what controlling docs are for: its factual
baseline is accurate (all built/not-built claims match the engine at `3b5251d`),
its conflict resolutions are each argued rather than asserted, and its sequencing
resists the project's own named top risk (tool-building as procrastination).

The weaknesses are concentrated in two places:

1. **The headline benefit of its central decision is unenforced.** §4 sells
   identity joins ("a FK match, not the span-overlap geometry the bridge doc had
   to settle for") but specifies no span identity rule — no uniqueness, no
   resolver, no merge semantics. As spec'd, `claim_upsert` and `work_cite` each
   mint fresh span rows and the identity join silently degrades back to the
   geometry it claims to replace.
2. **The flip is specified as a moment, not a mechanism.** Per-work flip vs.
   global tooling, post-flip file edits, `work_index`'s lifetime, and the
   markdown→blocks port heuristics are all unstated or ambiguous — and the first
   three become operational failures on the day the first work freezes.

Everything below is fixable with doc edits now; three findings become migrations
or data events if they wait past migration 010. The ranked list is §5; proposed
amendment text and DDL are §4.

---

## 1. Ground truth, verified

### 1.1 Where everything actually lives

The review had to establish this first, because the doc set obscures it:

- The **engine** is `/home/john/repos/MarginaliaAI` @ `3b5251d` — the commit the
  master doc names. Migrations: `packages/core/src/research_engine/adapters/
  storage/postgres/migrations/versions/` (`001_initial` … `008_passage_node`).
  MCP tools: 26 registered in `mcp/dispatch.py`, including `verify_quote`
  (`mcp/tools/verify_quote.py`, plus CLI `research-engine verify-quote`).
- The **current working directory** is a linked worktree
  (`John-Cusack/global-search`): 3 commits, a source-search slice whose
  `dispatch.py` imports 18 tool modules that do not exist in this checkout. The
  entire doc set (`docs/`), `works/`, and `briefs/` are **untracked** here.
- The dev database the briefs name (`postgresql+asyncpg://re_dev@localhost:5435/
  research_engine`) is served by `tools/dev-postgres/docker-compose.yml` in the
  engine repo: image `pgvector/pgvector:pg15`, ports `5435:5432`, user `re_dev`.

### 1.2 Built / not-built: every claim matches

| Master doc claim | Verified state |
|---|---|
| Migrations 001–008 built | **True** — eight Alembic revisions at head; no 009+ |
| `verify_quote` built (exact/normalized/near/not_found) | **True** — MCP tool + CLI, registered |
| `document_texts`, real offsets, span index, nodes, entities, typed edges built | **True** (mig. 003/004/007/008) |
| Migration 009 (`argument.*`) not built | **True** — no such schema anywhere |
| P3 (`bibliographic_records` etc.) not built | **True** — identity still lives in `documents.metadata` JSON |
| `works/` directory + front-matter contract built | **True as files** — `README.md` + `_TEMPLATE.md` only; **zero real works exist** |
| Phase-0 tools (`work_verify`, `work_index`, `work_citations`, `work_render`) | **Do not exist** — design only, in these docs |
| `evidence.source_spans`, `authored.*`, `core.works_index`, `core.work_citations`, `core.annotations` | **None exist** in any migration or `schema.py` |

This accuracy is worth stating because it is rare: most design docs drift from
their baseline within a section. This one does not.

### 1.3 Facts the doc leaves open that are already answerable

1. **The Postgres 15 env note (§3) can be closed now.** The dev server is
   `pgvector/pgvector:pg15` (the compose file the brief's connection string
   points at), and `UNIQUE NULLS NOT DISTINCT` is a Postgres 15 feature —
   supported. One `SHOW server_version` against the live server confirms it;
   the "partial unique index" fallback is almost certainly moot. The doc flagged
   an env question whose answer was sitting in the engine repo's own compose
   file.
2. **No rights classification exists anywhere in the engine.** Grepping the
   storage layer for `rights` / `license_class` returns zero hits. This matters
   for §7 (see finding 3.5).
3. **`verify_quote` has zero automated tests.** No file in the engine's `tests/`
   mentions it. The bridge doc's "verified end-to-end (guide II P3-5)" was a
   manual spike. Every Phase-0 gate — `work_verify`, the publish gate, the
   eventual freeze rule — inherits this function's correctness, and nothing in
   the repo would catch a regression in tier assignment. The bridge appendix's
   test #1 ("tier honesty") is the right first test and it does not exist for
   the substrate itself.

---

## 2. What the approach gets right

Each of these is a decision the master made correctly, with its reasoning
restated so the amendments in §4 do not disturb them.

1. **Staged authority is the correct resolution, not a dodge.** Pure-files (the
   bridge position) loses FK integrity and the leverage queries; pure-DB (the
   target position) pays for revision machinery during exactly the phase where
   prose churns and git already provides diffing. Files-canonical-until-first-
   freeze matches cost to lifecycle stage, and §1's framing — "both models are
   honest; only the flip is a decision" — is the right way to say it.
2. **Conflict 3 (one span table) is decided at the cheapest possible moment.**
   Migration 009 exists only on paper; centralizing anchoring before three
   consumers each build inline copies is a doc edit now versus a migration plus
   re-anchor after 010 lands. The reasoning is exactly right, and the
   consequences claimed (reverify visits one table; drift detected once) are
   real — *provided* the identity rule that finding 3.1 says is missing.
3. **The P3-dependency removal is a genuine fix.** The target's
   `edition_id NOT NULL` made every citation unrepresentable before P3 exists;
   the amended `CHECK (edition_id IS NOT NULL OR zotero_key IS NOT NULL)` (§5)
   makes the bridge schema buildable now and tighten-able later. The
   `bibliography.work`/`bibliography.edition` naming also cleanly resolves the
   `works` collision with guide II P3-1's Option B.
4. **Nothing is thrown away.** The file contract was designed as a strict subset
   of the target rows: front-matter fields map 1:1 onto occurrence + item rows,
   the mirror is derivable, the flip is a port rather than a rewrite. This is a
   real upgrade path, not an aspiration — and it is the doc's strongest
   structural property.
5. **The right instincts, carried consistently:** works never become
   `core.documents` (circular evidence); typed FK tables over polymorphic
   `core.edges`; rendered strings never authoritative; `normalized` never
   collapsed into `exact`; `ON DELETE RESTRICT` on cited sources; waivers as
   rows with actor + reason + timestamp; free text at exactly one boundary
   (machine-generated output). Each appears with its reason, in the house style.
6. **Sequencing discipline resists scope creep.** Phase A trimmed from nine
   tools to five; the mirror gated on >3 works / >20 citations; everything else
   on named triggers. The program doc's §9 named "tool-building as
   procrastination" as the top risk; the master's §8/§9 are a direct answer to
   it.
7. **Governance is explicit.** The §1 conflict table (conflict → bridge position
   → target position → call → trigger) and the §10 superseded-material index are
   the right artifacts for a three-doc pile with known contradictions. This
   review's finding 3.7 holds the master to the standard it set for itself —
   which is the compliment, not the criticism.
8. **The definition of done is testable.** §12's six criteria are observable on a
   named real work (the Lev 25 / *deror* candidate) rather than generic. Two of
   them (5, 6) are currently untestable as spec'd — see 3.5 and 3.6 — but the
   form is right.

---

## 3. Shortcomings, detailed

Severity is about consequence-if-unfixed, not doc quality. Every item includes
the evidence, the failure mode, and a pointer to the proposed amendment in §4.

### 3.1 The identity-join benefit is unenforced: spans have no identity rule — **High**

**The claim** (master §4, lines 158–162):

> claim ↔ work joins become identity joins. A claim anchor and a work citation
> over the same span share `source_span_id` — "this essay renders the evidence of
> that claim" is a FK match, not the span-overlap geometry the bridge doc had to
> settle for.

**The gap.** For that to be true, two writers citing the same characters —
`claim_upsert` (migration 010, per the program doc §4) and `work_cite` (Phase 1)
— must receive the *same* `source_span_id`. Nothing in the master §4 DDL, the
target doc §7, or the program doc §2 specifies:

- any uniqueness beyond the UUID PK (no `UNIQUE (document_id, char_start,
  char_end)`);
- a lookup-before-insert resolver for `work_cite` / `claim_upsert`;
- merge semantics when two rows for identical coordinates already exist.

The target doc gestures at it — `SourceSpanRepo` is specified as "create,
resolve, and find spans" (§11.1) — but a resolver is a protocol, not a table, and
no doc states it. As written, each tool call mints a fresh UUID row and the
headline benefit silently degrades to exactly the span-overlap geometry §4 says
it is not.

**The compounding problem: `locator` has three homes.** After the master's
amendments, `locator jsonb` lives on:

1. `evidence.source_spans` (§4 DDL, line 143),
2. `argument.anchors` (kept per §4's revision; program doc §2 lines 163–166),
3. `authored.citation_items` (§5 DDL, line 203).

No precedence is stated. This is not academic: identical coordinates cited twice
with different locators (page 214 vs. Lev 25:10) is the normal case for a Bible
translation citing a lexicon. If spans dedupe, the span's locator is
first-writer-wins — arbitrary; if spans are per-citation rows, the identity join
collapses. The schema as written forces one failure or the other.

**The deeper question the doc silently answers twice.** Is `verify_status` a
property of the span or of the citing row?

- The bridge treats it as **per-citation**: the mirror's `verify_status NOT NULL`
  including `not_found`, with the stated honesty rule "a broken citation is data
  about the work" (bridge §4).
- The master/target treat it as **span-level**: tiers live on
  `evidence.source_spans`, and §4's anchor revision *removes* `quoted_text` and
  the verify columns from `argument.anchors`.

If tiers are span-level and spans dedupe on coordinates, then a citer whose typed
quote verified `normalized` (OCR noise) may point at a span another citer's
cleaner typing marked `exact` — which erodes §2's own invariant ("`normalized`
is never collapsed into `exact`") at the row the reader actually consults. And
the anchor no longer stores what the claim actually quoted, only the shared
snapshot — a quiet weakening of the program doc's honesty rule for anchors.

**Proposed fix** — §4.1. Two coherent shapes exist; the master must pick one and
write it down:

- **Option A (span owns typed quote + tier; master's current shape):** dedupe on
  `(document_id, char_start, char_end)`, resolver keeps the strongest tier
  (exact > normalized > near), accept that per-citation tier nuance and the
  second citer's typed text are not stored. `locator` must move off the span.
- **Option B (span owns coordinates + canonical slice + parser provenance;
  citing rows own typed quote + tier) — recommended.** Dedupe on coordinates is
  then safe by construction (same offsets = same slice), the master's §4 anchor
  revision gets *smaller* (anchors keep `quoted_text`/`verify_status`/
  `verified_at`/`parser_version`, which program doc §2 already has, and swap only
  the three coordinate columns for `source_span_id`), the bridge's per-citation
  honesty survives the flip, and §2's tier invariant holds per citing row.

Under B, "reverify visits one table" becomes slightly weaker but still true
where it matters: stale spans are *discovered* in one table (parser_version
mismatch), then each citing row's tier is recomputed. Option B also makes the
mystery of the silently-dropped `normalized_quote_hash` (see 3.7 item 2) resolve
itself — the hash existed to support span-level tier recomputation, which B
moves to citing rows.

**Cost if deferred:** after 010 lands with inline-anchor semantics or
unprincipled span rows, fixing this is a migration plus a re-anchor of every
claim anchor and work citation — the exact cost the master's §1 used to justify
deciding conflict 3 now.

### 3.2 The flip is a trigger, not a protocol — **High**

**The gap.** §3 defines the flip per-work ("A work flips to DB-canonical when
its first revision is frozen") but every operational consequence is global and
unstated:

1. **Post-flip file edits.** "Files become exports" (§3) — so when the author
   edits a frozen work's file out of git muscle memory, what happens? The edit is
   silently lost (the DB is canonical, the file is a projection)? Flagged as
   drift? Overwritten by the next `work_export` run? The doc does not say, and
   this is *the* classic dual-authority failure. "The flip to DB-canonical makes
   the question smaller, not larger" (§11.1) is hand-waving: smaller is not
   *none*, and the first occurrence will feel like data loss.
2. **`work_index`'s lifetime.** §8's table says `work_index` is "dropped at
   flip" — meaningless per-work. The mirror (011) and the tool are one unit;
   they must survive while *any* pre-012 work remains unflipped, and drop
   together when the last one flips. As written, an honest reader cannot tell
   whether they may run `work_index` on a mixed fleet.
3. **New works after 012.** §8's Phase 1 includes `work_create`, implying
   DB-born works; §3's "files canonical until a work's first freeze" reads as a
   lifecycle available to any work at any time. Both readings are defensible;
   the doc must pick. (Recommended: post-012 works are DB-born; the file-first
   path remains only as *import*, which the bundle format already covers —
   target §15.2.)
4. **The port is untested by construction.** §3's port procedure is three
   heuristics: block boundaries "from the markdown structure" (flat markdown has
   no block structure — heading/paragraph detection is a guess about the
   author's logical blocks), `[^cN]` handles rewritten to `{{cite:<uuid>}}`
   markers (a *prose rewrite* — the export file will differ textually from the
   authority file, which §3 never says aloud), and "one `work_cite` per
   front-matter entry" (assumes every entry passes the target CHECK — see 3.7
   item 3). The first time all three run is the day a real work's authority
   changes. That is a data event being used as a test.

**Proposed fix** — §4.2: a six-clause flip protocol, plus a cheap **flip
rehearsal**: port one work with the Phase-1 spine as a drill *before* first
freeze, export, diff, discard. The master's own conflict-3 logic ("decide now
because it's free; decide later because it's a migration") applies verbatim: the
port format's defects are doc-edit-cheap now and data-event-expensive at the
first real freeze.

**Cost if deferred:** silent file/DB divergence on the first frozen work, and a
port failure in the middle of the one work that justified the whole subsystem.

### 3.3 Phase 0 is paper: the contract is ratified with zero instances — **Medium**

**Evidence.** None of the four Phase-0 tools exists in the engine (verified:
zero `work_*` modules). `works/` contains `README.md` and `_TEMPLATE.md` and no
work. So §3's "answers 'which works cite this source' from day one" means: day
one *after building tools that do not exist*, against a directory containing
nothing to index.

**Two specific consequences:**

1. **`status: published` is currently a self-declared string.** The gate lives
   in `work_verify` (bridge §6), which does not exist. Until it does, the
   README's contract is aspirational — the bridge doc says so itself ("the tool
   that makes the README's contract enforceable instead of aspirational"), but
   the master's §8 table presents Phase 0 as though it were the present tense.
2. **The substrate is untested.** `verify_quote` — which every gate, every
   `work_verify` run, and the eventual freeze rule call — has zero automated
   tests in the engine (verified: no test file mentions it). The bridge
   appendix's test #1 (tier honesty: normalized never reported exact) is the
   load-bearing case and does not exist for the function itself.

**Proposed fix** — §4.3: apply the program doc's own §8 discipline to Phase 0 —
"three real rows before the migration is written" becomes "one real work, hand-
verified, before the contract is ratified." The Lev 25 / *deror* candidate named
in the master's DoD is the obvious instance: write it, run its citations through
the existing `verify_quote` CLI by hand, let the fields the contract forgot
surface now, then commit the contract — and add tier-honesty tests for
`verify_quote` before `work_verify` inherits its behavior.

### 3.4 Interim-forever risk — **Medium**

**The mechanism.** The bridge answers ~90% of the felt need — which works cite
this source, machine-checked quotes on demand. The flip's stated justification
("every block rendering λόγος, across works and revisions," §6; "the third waits
for Phase 1 and is named as the flip's justification," §12.2) sits behind two
migrations *and* a freeze event *and* Phase B. Open decision #2 puts the escape
hatch on the researcher's felt need — which is precisely the muscle the program
doc §9 says fails ("tool-building as procrastination" cuts both ways:
**not-flipping is also procrastination**). If the flip never fires, the system
calcifies on mirror tables designed as "explicitly temporary" (§9: "droppable at
flip"), with no eviction date and no forcing function.

**Proposed fix.** Two cheap additions: (a) the flip rehearsal from 3.2, which
keeps the port path warm and surfaces its defects; (b) an explicit review
trigger — e.g., "at >10 works or the first cross-work lemma question, the flip
decision is *re-taken*, not assumed" — so deferral is a decision that recurs
rather than a state that persists by default. The mirror's own trigger (>3
works / >20 citations) shows the doc knows how to write these; the flip just
needs one.

### 3.5 Rights are asserted but not schema'd — in the doc or the engine — **High**

**The claim** (master §7, lines 258–261):

> Rights, adopted unchanged from target doc §16 (the bridge doc's omission, now
> fixed at master level): `quoted_text` is a verification snapshot, not a
> republication license; a document's rights classification travels on the span
> row; …

**Three problems, in increasing order of severity:**

1. **The master's own §4 DDL has no rights column** on `evidence.source_spans`
   — nor does the target doc §7 it was "adopted unchanged" from. The sentence
   and the schema in the same doc disagree.
2. **The engine has no rights classification anywhere** (verified: zero hits for
   `rights`/`license_class` in the storage layer). There is nothing for a span
   to carry. The §9 migration table contains no migration that would create it.
3. **DoD #5 is therefore untestable as spec'd**: "rights policy is enforced at
   that boundary — not remembered in anyone's head." `AUTH_LICENSE_EXPORT`
   cannot fire without a classification to consult, and the material at stake —
   TDNT, Logos, Kindle, YCL — is exactly why the master calls this
   "load-bearing, not decorative."

**Proposed fix** — §4.4: put a rights-classification column on
`core.documents` (engine migration, named in §9's table with its trigger), and
reword §7 so the span *joins* to it rather than denormalizing it — a copied
column goes stale on reclassification; the FK join cannot. Rights on the citing
artifact's export gate = span → document → rights, evaluated at export, logged.
That is the whole mechanism; the doc currently asserts its conclusion without
its premise.

### 3.6 Trigger and tooling mismatches in the Phase-1 spine — **Medium**

1. **`work_freeze` is deferred to the wrong trigger.** §8 defers
   `work_freeze`/`work_export` to "first publication." But the flip trigger is
   *first freeze* (§3). A work frozen for review — the exact case that flips
   authority — has no freeze tool. The trigger for `work_freeze` is trivially
   "first freeze"; it belongs in the spine, not the deferred list.
2. **`work_get` was dropped without comment.** The target's nine tools included
   `work_get` ("return ordered blocks plus structured links," §12.1); the
   master's five-tool spine cuts it. `work_trace` is provenance-shaped, not
   content-shaped — reading a work's blocks back is a day-one need for any
   authoring loop. Either restore it or state that `work_trace` doubles as the
   read path, with the response shape that implies.

### 3.7 Internal inconsistencies — each **Low**, collectively the standard

A controlling doc is fairly held to the standard it sets in §10 ("so no
companion doc is silently contradicted"). Six items miss it:

1. **§2 vs. §7 on `near`.** §2: "`near` and `not_found` block publication."
   §7 adopts the target §13 waiver model wholesale — and target §8.3 says a
   `near` quotation freezes with "an explicit override recorded in the decision
   log." Precise statement: `not_found` can never *enter* (the §4 CHECK excludes
   it; `claim_upsert` refuses to store it); `near` blocks freeze **unless
   waived**, with actor + reason + timestamp.
2. **`normalized_quote_hash bytea` silently dropped.** Target §7's span DDL
   includes it; the master's §4 DDL does not, presents itself as "adopted from
   target doc §7," and does not index the deviation in §10. (Under §4.1's
   Option B the drop becomes *correct* — the hash's purpose moves to citing
   rows — but the doc must say so.)
3. **Phase-0-representable but unportable citations.** The mirror has no
   identity CHECK (bridge §4: `zotero_key` nullable, no constraint); the target
   requires `edition_id OR zotero_key` (§5). A citation with neither is
   representable today (the README only *asks* for `zotero_key`: "fill it from
   day one") and fails at port time. `work_verify` should flag zero-identity
   citations as findings **now**, so port day holds no surprises.
4. **Failed-verification records vanish post-flip.** §4: "Failed *attempts* are
   recorded by the gate/mirror layer" — but the mirror is dropped at flip, and
   Phase 1's atomic `work_cite` aborts the entire citation on failure. Post-flip
   there is no durable record that a quote was tried and failed. Either a small
   additive `verify_attempts` log (fits the house honesty rule: "no canonical
   text" is a different answer from "not found" — but only if someone can read
   the answer later) or an explicit acceptance of the gap.
5. **§9's 009 is oversold against the doc's own discipline.** "now-worthy,
   additive — one copy of anchoring; **no consumers yet**." By the §8/§9 trigger
   discipline, a consumer-less table is the definition of a pull that has not
   fired; it is justified only because 010 lands in the same week (program doc
   week 2). Say that instead.
6. **Migration-numbering residue.** The master renumbers 009–012 (§9, correctly
   replacing both companions' numbering), but the bridge doc still calls the
   ledger "009" and its own migration "010" in §2/§4/§8. §10 indexes bridge
   §4's schema but not its numbering. One line in §10 ("bridge doc references
   to 009/010 are renumbered per §9") closes it.

### 3.8 The doc set's own provenance — **Low**

A doc whose function is "supersedes the conflicting sections of its
companions" currently exists as **untracked files in a satellite worktree** —
not in the main checkout its own header cites, not committed anywhere. Three
concrete consequences:

1. The supersession has no provenance: there is no commit to diff against when
   the master is later amended.
2. `work_path` (the mirror's PK, bridge §4) is a filesystem-relative string
   while `works/` lives in this worktree and the indexing engine lives in
   another checkout. §11.1 debates *which repo* works belong in but never
   notices the *path resolution* question the mirror schema bakes in. One config
   decision, worth a sentence in §11.1.
3. The de facto answer to open decision #1 is already "untracked in this
   worktree, unpushed" — which is the master's own stated default. Fine as a
   default; but the *controlling doc* deserves better than the default it
   grants the works.

---

## 4. Proposed amendments for the master

Drop-in text and DDL, per finding. Option B is recommended in 4.1; everything
else is single-shaped.

### 4.1 Span identity rule (fixes 3.1)

Add to §4, after the DDL:

```sql
-- Span identity: one row per (document, coordinates). Same offsets = same
-- slice of the same document; there is nothing else to distinguish.
ALTER TABLE evidence.source_spans
    ADD CONSTRAINT source_spans_coordinates_uk
    UNIQUE (document_id, char_start, char_end);
```

Resolver protocol (normative, applies to `work_cite` and `claim_upsert`):

1. Before insert, look up `(document_id, char_start, char_end)`; if present,
   reuse the id. Insert only on miss, in the same transaction (ON CONFLICT DO
   NOTHING + re-select for the race).
2. `quoted_text` on the span is the **canonical slice**
   `document_texts.text[char_start:char_end]`, not the author's typed quote.
   The typed quote and its tier are properties of the citing row.
3. `locator` is removed from `source_spans`. Locators are citing-row data
   (anchors and citation items keep theirs; two citers of the same span may
   cite different pages/verses).
4. Consequently the §4 anchor revision shrinks: `argument.anchors` **keeps**
   `quoted_text`, `verify_status`, `verified_at`, `parser_version` (all already
   in program doc §2) and swaps only `document_id`/`char_start`/`char_end` for
   `source_span_id NOT NULL`. `citation_items` gains `verify_status` /
   `verified_at` with the same CHECK as anchors (never `not_found` — a failed
   citation is not stored; see 4.6 item 4).
5. Re-verify: stale spans are discovered in one table (`parser_version`
   mismatch vs. `document_texts`); each citing row's tier is then recomputed
   from its own `quoted_text`.

§4's bullets survive with one edit: "reverify visits one table" becomes
"reverify *discovers* in one table."

### 4.2 Flip protocol (fixes 3.2)

Add to §3, as "The flip, as a protocol":

- **F1 — Per-work flip, global tooling.** A work flips at its first freeze.
  Migration 012 and the Phase-1 tools land once, globally. Both facts are
  normal; the clauses below are what stops them fighting.
- **F2 — Post-flip, the file is a read-only export.** `work_export` rewrites it
  and git diff is the audit trail. A manual edit to a flipped work's file is
  drift: the next validate/verify run reports it; the database never follows the
  file. There is no state in which both are editable.
- **F3 — `work_index` and the mirror are one unit.** They live while any
  pre-012 work is unflipped; both drop together when the last one flips. §8's
  "dropped at flip" reads per-fleet, not per-work.
- **F4 — New works after 012 are DB-born** (`work_create`). The file-first path
  survives only as *import* (the bundle format, target §15.2), which is
  explicit and transactional, not a second authority.
- **F5 — The port is one transaction per work**: `work_create` + block upserts
  + `work_cite` + `work_link`, then `work_export` regenerates the file from
  rows. Acceptable diff: citation markers only (`[^cN]` → `{{cite:<uuid>}}`).
  Any other diff is a port bug, and the port logs the marker mapping.
- **F6 — Flip rehearsal before first freeze.** Port one work with the Phase-1
  spine as a drill, export, diff, discard. The port's three heuristics (block
  boundaries from markdown structure; marker rewrite; CHECK-passing citations)
  are tested while fixing them is still a doc edit.

### 4.3 Phase-0 reality checklist (fixes 3.3)

Before the contract in `works/README.md` is treated as ratified:

1. Write the first real work (the Lev 25 / *deror* candidate from §12).
2. Verify every citation by hand through the existing `verify_quote` CLI; note
   any field the contract forgot (program doc §8: "the second and third are
   where the fields you forgot show up").
3. Add tier-honesty tests for `verify_quote` itself (bridge appendix #1 is the
   spec: normalized never reported as exact; near returns a diff; straddling
   quotes still match). Every Phase-0 gate inherits this function's tiers.
4. Commit the doc set (see 3.8) — a ratified contract needs a revision to be
   ratified *in*.

### 4.4 Rights, made real (fixes 3.5)

- Add a §9 row: `core.documents.rights_class text` — additive engine migration,
  trigger: the first export-gate enforcement (`AUTH_LICENSE_EXPORT`), riding
  whichever migration is nearest when that trigger fires. Populate from pack
  ingest conventions (Logos/Kindle/YCL plugins know their material).
- Reword §7: "a document's rights classification is consulted at export via the
  span's `document_id`; the span needs no rights column of its own" — a
  denormalized copy stales on reclassification; the join cannot.

### 4.5 Spine and trigger fixes (fixes 3.6)

- Move `work_freeze` into the Phase-1 spine; its trigger is "first freeze,"
  which is also the flip trigger. `work_export` stays deferred to first
  publication (a frozen-but-unpublished work does not need it — F2's export-
  on-freeze is the `work_render` successor's job).
- Restore `work_get` to the spine, or specify that `work_trace` returns full
  block content and rename the claim accordingly.

### 4.6 Wording fixes (fixes 3.7)

1. §2: "`near` blocks freeze unless waived (actor + reason + timestamp);
   `not_found` never enters the tables at all."
2. §4: note the `normalized_quote_hash` drop and its reason (it moves to
   citing rows under 4.1).
3. §7/`work_verify` spec: zero-identity citations (no `zotero_key`, no
   `edition`) are findings from day one — the §5 CHECK will refuse them at port
   time.
4. §4: failed attempts post-flip are either a `verify_attempts` log table
   (additive; recommended) or an accepted gap, stated.
5. §9/009: "lands with 010 in program week 2" rather than "now-worthy."
6. §10: one line noting the bridge doc's 009/010 references are renumbered per
   §9.

---

## 5. Ranked improvements, with costs

Ranked by (consequence × urgency). "Doc edit" = free now, by the master's own
conflict-3 argument; the deferred column is what the same fix costs after the
named event.

| # | Improvement | Fixes | Cost now | Cost if deferred |
|---|---|---|---|---|
| 1 | Span identity rule + locator placement (§4.1) | 3.1 | Doc edit + one constraint in 009 | Migration + re-anchor of every anchor and citation after 010 |
| 2 | Flip protocol F1–F6 + rehearsal (§4.2) | 3.2, 3.4 | Doc edit + one drill | Silent file/DB divergence at first freeze; port failure mid-work |
| 3 | Rights column + §9 row + §7 rewording (§4.4) | 3.5 | Doc edit + one additive migration | DoD #5 untestable; licensed egress ungated — the load-bearing case |
| 4 | One real work + `verify_quote` tier tests (§4.3) | 3.3 | ~A day, no migration | Contract ratified blind; gates inherit untested tiers |
| 5 | `work_freeze` to spine; `work_get` restored (§4.5) | 3.6 | Doc edit | Flip occurs without its tool; no content read path |
| 6 | Wording/CHECK fixes incl. zero-identity findings (§4.6) | 3.7 | Doc edits | Port-day surprises; controlling doc loses its own standard |
| 7 | Commit the doc set; name the `work_path` config (3.8) | 3.8 | `git add` + one sentence | Supersession with no provenance |

Items 1–3 are the ones with deadlines: each is free until the event named in its
deferred column, and each named event (010, first freeze, first export gate) is
already on the doc's own schedule.

---

## 6. Test consequences

The bridge appendix's six tests remain the right first tests for the tools; two
change shape under this review, and the substrate needs its own:

1. **Tier honesty** (bridge #1) — promote to an engine test for `verify_quote`
   itself: normalized never reported exact; near returns a diff;
   straddle-matching survives; "no canonical text" ≠ "not found." This test
   gates everything else in the list and does not currently exist.
2. **Identity join** (replaces bridge #6's overlap join, once §4.1 lands):
   `claim_upsert` and `work_cite` over the same coordinates share one
   `source_span_id`; a second `work_cite` of the same coordinates creates a
   citation and no new span (idempotency).
3. **Port drill** (new, for F6): port a fixture work, export, assert the diff
   is markers-only; assert a zero-identity front-matter citation is a `work_verify`
   finding before the port, not a failure during it.
4. **Flip drift** (new, for F2): edit a flipped work's file; assert the next
   validate reports drift and the DB is unchanged.
5. Bridge #2–#5 (mirror idempotency, RESTRICT, drift detection, publish gate)
   — unchanged.

---

## 7. Questions this review adds to §11

The master's three open decisions stay researcher-owned. This review adds two it
cannot answer for the doc:

4. **Span tier ownership** (§4.1 Option A vs. B) — a schema decision, not a
   preference; the review recommends B, but the master must ratify it because
   it amends the target doc §7 and program doc §2 in one stroke.
5. **Failed-attempt recording post-flip** — `verify_attempts` log table or an
   accepted gap; either is fine, silence is not.

And it sharpens one existing one: §11.1 (where files live) should also name the
`work_path` resolution config, since the mirror's PK is a filesystem-relative
string and the works and the engine currently live in different checkouts.

---

## 8. Findings summary

| Finding | Severity | One line | Fixed by |
|---|---|---|---|
| 3.1 Span identity unenforced | **High** | The §4 headline (identity joins) has no uniqueness, resolver, or merge rule; `locator` has three homes | §4.1 |
| 3.2 Flip is a trigger, not a protocol | **High** | Post-flip file edits, `work_index` lifetime, new-work lifecycle, and the port's three heuristics are all unspecified | §4.2 |
| 3.5 Rights asserted, unschema'd | **High** | No rights column in §4's DDL, the target §7, or the engine; DoD #5 untestable; §9 has no rights migration | §4.4 |
| 3.3 Phase 0 is paper | Medium | Zero tools, zero works, `published` self-declared, and `verify_quote` (every gate's substrate) has zero tests | §4.3 |
| 3.4 Interim-forever risk | Medium | The bridge satisfies ~90% of the need; the flip has no forcing function and the mirror has no eviction date | §4.2 F6 + review trigger |
| 3.6 Spine/trigger mismatches | Medium | `work_freeze` deferred past its own trigger; `work_get` cut without comment | §4.5 |
| 3.7 Internal inconsistencies (6) | Low | `near` vs. waivers; dropped hash unindexed; unportable citations; failed attempts post-flip; oversold 009; numbering residue | §4.6 |
| 3.8 Doc-set provenance | Low | Controlling doc is untracked in a satellite worktree; `work_path` config unnamed | §5 item 7 |

**Standing verdict.** Ratify the architecture; do not build 009/010 until items
1–3 of §5 are doc edits applied. The master's own standard — decide what is free
to decide *before* the migration makes it expensive — is the whole of this
review's argument applied back to the master itself.
