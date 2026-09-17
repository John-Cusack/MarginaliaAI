# Claim ledger: remaining implementation handoff

**Status:** Steps 2–4 are implemented. This document covers the remaining acquisition, content, audit, graph, re-verification, works, and migration work.

**Branch at handoff:** `John-Cusack/verbatim-lexicon-entry-retrieval`

**Handoff date:** 2026-09-16

## 1. Purpose and precedence

This is an execution guide for the next implementation agent. It is self-contained because the earlier claim-ledger design files are not tracked in this worktree.

Use this order of authority when details conflict:

1. The current schema and implementation in this checkout.
2. The invariants and gates in this document.
3. Existing repository conventions in adjacent works, citation, span, and MCP code.

Do not rebuild Steps 2–4. Do not add future repository methods as stubs. Add a method to the protocol and repository surface guard only in the step that supplies its complete implementation.

## 2. Verified handoff state

### 2.1 Implemented

The following surface exists and was verified before this handoff:

- Claim domain models in `packages/core/src/research_engine/domain/claims.py`.
- `PGClaimRepo` in `packages/core/src/research_engine/adapters/storage/postgres/repositories/claims.py` with:
  - `upsert_claim`
  - `add_edge`
  - `add_anchor`
  - `get_by_ref`
  - `anchors_for`
  - `anchor_by_id`
  - `edges_for`
- Transactional `ClaimService.upsert` in `packages/core/src/research_engine/services/argument/claims.py`.
- Read-only `AnchorContextService` in `packages/core/src/research_engine/services/argument/context.py`.
- MCP tools:
  - `claim_upsert`
  - `anchor_context`
- `work_citations(include_context=true)` context hydration.
- Composition, container, repository protocol, exports, dispatch, and test cleanup wiring.
- Integration coverage in `tests/integration/test_claim_ledger.py`.

The implementation was verified with targeted unit tests, claim-ledger integration tests, works/context integration tests, an actual dispatch smoke scenario, and `make lint`.

### 2.2 Current write behavior

`ClaimService.upsert` currently:

1. Validates the claim, edge, and anchor vocabularies through Pydantic domain models.
2. Rejects duplicate/self edges.
3. Verifies every anchor before opening the write transaction.
4. Rejects the entire call when any anchor is `not_found` or `no_canonical_text`.
5. Stores `exact`, `normalized`, and `near` honestly; it never upgrades a tier.
6. Resolves verified offsets through the shared source-span resolver.
7. Requires and resolves a person for every `role='asserts'` anchor.
8. Writes the claim, edges, and anchors in one transaction.
9. Preserves the caller's typed quotation on the anchor and the canonical slice on the shared source span.

`ClaimWriteResult` does not yet contain audit findings. That change belongs to Step 6.

### 2.3 Current read behavior

`anchor_context` accepts exactly one of `anchor_id`, `claim_ref`, `citation_id`, or `span_id`. It returns a clamped canonical-text window and the smallest containing document node. It does not persist context.

Character offsets are Python-style, zero-based, half-open `[start, end)`. Preserve that convention. Do not copy one-based SQL substring arithmetic into service code.

### 2.4 Runtime and content blockers at handoff

The local Postgres endpoint at `localhost:5435` was unavailable on 2026-09-16. `research-engine doctor` consequently failed while connecting to Postgres. Current ledger row counts are unknown; do not assume the ledger is empty.

The required audit source file is also absent from this checkout:

```text
logos_mcp/sider_government_redistribution_analysis.md
```

No file matching the Sider analysis was found in the sibling checkout either. Step 5 is blocked until the operator supplies or locates that document.

The repository contains no Kindle or YourCloudLibrary pack manifest. Those packs are external/operator-installed plugins, not core packages in this checkout. Do not add fake pack modules or move their dependencies into core merely to make the guide advance.

## 3. Non-negotiable invariants

1. A quotation that verifies `not_found` or `no_canonical_text` never enters `argument.anchors`.
2. An `asserts` anchor always has a person entity.
3. The anchor stores the typed quotation; the shared source span stores the canonical slice.
4. A claim, all requested edges, and all requested anchors commit or roll back together.
5. Context is read from current canonical text and is never copied into the ledger.
6. Offsets survive re-chunking. Parser-version drift remains explicit.
7. Rule output reports mechanical failures only. A green audit never claims that an argument is sound.
8. Public/export code must not turn licensed source material into passage dumps.
9. Real research rows, not fixtures, are the schema acceptance test.
10. No auto-fixing, implicit status propagation, or reasoner belongs in the remaining core steps.
11. Keep integration tests isolated. Claim cleanup must remove explicit edges before claims because edge targets use `ON DELETE RESTRICT`.
12. No compatibility aliases, deprecated paths, placeholder methods, or second claim/citation storage convention.

## 4. Execution order

Run the remaining work in this order:

1. Restore the runtime and inspect actual data.
2. Complete narrow source acquisition.
3. Perform the twenty-row content audit.
4. Implement Step 6 only after those rows exist.
5. Implement Steps 7–9 only when their explicit usage gates fire.
6. Implement the Step 10 file-path resolution immediately after Step 5; implement the row-path resolution only when a canonical row carrier for claim refs exists.
7. Add migration 018 and the licensing rule in Step 10.

Steps 7–9 are deliberately conditional. Building them before their gate fires repeats the project's existing tool-building failure mode.

## 5. Phase A: restore runtime and establish facts

### 5.1 Start and migrate the database

From this worktree:

```bash
make db
make migrate
uv run research-engine doctor
```

Confirm the database is at revision `017_vector_index_restore` before writing migration 018.

Then record actual row counts:

```sql
SELECT count(*) AS claims FROM argument.claims;
SELECT count(*) AS edges FROM argument.claim_edges;
SELECT count(*) AS anchors FROM argument.anchors;

SELECT verify_status, count(*)
FROM argument.anchors
GROUP BY verify_status
ORDER BY verify_status;
```

Also confirm schemas `evidence`, `argument`, `authored`, and `bibliography` exist.

Do not delete real rows to make tests pass. Integration tests must use `Corpus` cleanup or the isolated scratch database used by the migration round-trip test.

### 5.2 Locate external prerequisites

Before Step 5, locate all of:

- `logos_mcp/sider_government_redistribution_analysis.md` or its authoritative replacement.
- The installed `kindle` plugin and its own manifest/environment.
- The installed `yourcloudlibrary` plugin and its own manifest/environment.
- Licensed copies or authorized provider access for the requested books.

If the Sider analysis document remains unavailable, stop the content phase and report that exact missing prerequisite. Do not synthesize twenty rows from this guide.

### 5.3 Verify the implemented write/read path against the live runtime

With the database available, run one throwaway claim through actual MCP dispatch:

1. `claim_upsert` with one exact or normalized anchor.
2. `anchor_context` by returned `anchor_id`.
3. Confirm the context contains the canonical anchored slice.
4. Delete the throwaway rows through a tracked test helper or explicit transaction.

The stored typed quote and returned canonical text may differ for `normalized`; that is expected.

## 6. Phase B: narrow source acquisition

**Gate:** runtime healthy. This phase must finish before the twenty-row audit.

### 6.1 Repair external plugin health, not core by accident

The earlier environment reported `plugin_missing_pip_deps` for:

- `kindle`: `playwright`
- `yourcloudlibrary`: `pytesseract`

Inspect the installed plugin manifests and install dependencies into the environments that actually load those plugins. Confirm both with `research-engine doctor` in each relevant checkout/environment.

Do not add these dependencies to core unless the current plugin architecture explicitly requires core to own them. The packs are not present in this repository.

### 6.2 Ingest only the sources needed for live claims

Priority sources:

1. Ronald Sider, *Rich Christians in an Age of Hunger*, **6th edition, 2015**.
2. Nicholas Wolterstorff, *Justice: Rights and Wrongs*.
3. Stephen Mott.
4. John Howard Yoder, *The Politics of Jesus*, especially the Jubilee chapter.
5. Christopher J. H. Wright, *Old Testament Ethics for the People of God*.
6. Catholic social teaching as structured HTML:
   - *Rerum Novarum*
   - *Quadragesimo Anno*
   - *Laborem Exercens*
   - *Centesimus Annus*
   - the *Compendium*
7. Allied prior art, at minimum David Chilton, *Productive Christians in an Age of Guilt-Manipulators*; Beisner and Grudem remain optional until a claim needs them.

Every ingested document must have:

- canonical `document_texts` content;
- `metadata.edition_key`;
- a matching `bibliography.editions` row;
- a hand-picked sentence that verifies `exact` or `normalized`;
- zero failed articles and zero missed TOC entries at provider walk/store boundaries.

Capture `corpus_stats` before and after. The document delta must equal the intended narrow ingest, not the whole reading list.

**Acquisition completion gate:** *Rich Christians* 6th edition plus at least three of the other five principal works are present with canonical text and edition identity.

## 7. Phase C: Step 5 — twenty-row content audit

**Gate:** the authoritative analysis document exists, *Just Politics* and *Rich Christians* are queryable, and Steps 2–4 pass against the live runtime.

This is editorial research, not a bulk-import task. Enter exactly twenty initial claims one at a time through `claim_upsert`.

For every claim about Sider:

1. Use `kind='opposition'`.
2. Write one sentence in Sider's terms, not the researcher's rebuttal vocabulary.
3. Fill `steelman` before looking for rebuttal material.
4. Search *Just Politics* and *Rich Christians* with `find_passages`.
5. Verify the proposed quotation with `verify_quote`.
6. Add a `role='asserts'` anchor naming Sider only when the text supports the statement.
7. Read the returned anchor with `anchor_context` before accepting the row.
8. If no source text supports the attribution, store the claim with no anchor and keep `status='open'`.
9. Only after the opposition claim is faithful may a `kind='mine'` claim and a `rebuts` edge be added.

Expected corrections while reading:

- Four phrases attributed to Sider were not found in *Just Politics*.
- A real sentence states: “The libertarian view that the state has no responsibility to care for and empower the poor flies in the face of clear biblical teaching.”
- Sider explicitly rejects applying the specific Jubilee and sabbatical-release mechanisms directly to modern global markets; he treats the paradigm as normative.
- He describes the return of families to ancestral land, affirms the importance of private property, and denies that Jubilee is the communist model.
- The narrower surviving state-action claim rests on: “presumably the rulers were supposed to lead in its implementation.”

Do not convert these notes directly into rows. They are checkpoints against the authoritative source text, not substitutes for it.

### 7.1 Acceptance queries

```sql
-- At least twenty manually accepted rows.
SELECT count(*) FROM argument.claims;

-- No fifth verification grade entered.
SELECT count(*)
FROM argument.anchors
WHERE verify_status IS NULL;

-- Every attributed assertion names a person.
SELECT count(*)
FROM argument.anchors
WHERE role = 'asserts' AND person_entity_id IS NULL;

-- Unanchored claims remain visible and open.
SELECT c.ref, c.status
FROM argument.claims c
WHERE NOT EXISTS (
    SELECT 1 FROM argument.anchors a WHERE a.claim_id = c.id
)
ORDER BY c.ref;
```

Required outcomes:

- total claims `>= 20`;
- null verification count `= 0`;
- personless `asserts` count `= 0`;
- every unanchored claim has `status='open'`.

### 7.2 Mandatory schema verdict

Record every field wanted during the twenty-row exercise but absent from the model. Record an explicit empty list if no field was missing. Do not add fields during row entry; evaluate all twenty first.

Use this table:

| Date | Missing field or pressure | Claim refs affected | Decision |
|---|---|---|---|
| | None / describe exact pressure | | Keep schema / amend in a named migration |

Step 5 is not complete until the table is filled and the twenty rows have been read back.

## 8. Phase D: Step 6 — claim audit rules 1, 2, and 6

**Gate:** Step 5 is complete. The rules need real rows to validate their usefulness.

### 8.1 Files and surface

Add:

- `packages/core/src/research_engine/services/argument/rules.py`
- `packages/core/src/research_engine/mcp/tools/claim_audit.py`

Update only the necessary domain models, repository protocol/implementation, service exports, composition/container wiring, MCP registry/dispatch, tool catalog tests, and repository surface guard.

Recommended domain output:

```python
class ClaimFinding(BaseModel):
    rule_id: str
    severity: Literal["error", "warning", "info"]
    claim_ref: str
    message: str
    detail: dict[str, Any] | None = None

class ClaimAuditReport(BaseModel):
    findings: list[ClaimFinding]
    checked_refs: list[str] | None = None
    assurance: str
```

The exact model names may follow repository convention, but tool JSON must be stable and must identify the subject claim.

Implement `PGClaimRepo.audit(refs=None)` only now, with a complete body. `refs` scopes findings by their subject `claim_ref`. For a rebuttal rule, the subject is the rebutting/source claim and the target ref belongs in `detail`.

### 8.2 Rules

| Rule | Severity | Fires when |
|---|---|---|
| `CLM_OPPOSITION_UNANCHORED` | error | `kind='opposition'` has no `role='asserts'` anchor |
| `CLM_REBUTS_UNANCHORED` | error | a `rebuts` edge targets a claim with no `asserts` anchor carrying a non-null person |
| `CLM_PUBLIC_UNEARNED` | error | `public_ready=true` on a claim for which either rule above is open |

Structural straw-man query:

```sql
SELECT src.ref AS rebutter, tgt.ref AS target, tgt.statement
FROM argument.claim_edges e
JOIN argument.claims src ON src.id = e.source_id
JOIN argument.claims tgt ON tgt.id = e.target_id
WHERE e.relation = 'rebuts'
  AND NOT EXISTS (
        SELECT 1
        FROM argument.anchors a
        WHERE a.claim_id = e.target_id
          AND a.role = 'asserts'
          AND a.person_entity_id IS NOT NULL
  );
```

Do not auto-create anchors, change statuses, or clear `public_ready`.

### 8.3 Tool contract

`claim_audit(refs?: list[str])` returns the report. Its description and every clean report must state:

> Green means the implemented mechanical checks found no failure. It does not mean the argument is sound or the source has been interpreted faithfully.

The text may be tightened, but that meaning is mandatory.

After the write transaction commits, `claim_upsert` must audit the written claim and return findings alongside the successful write. Extend `ClaimWriteResult` rather than changing the existing claim/edge/anchor fields. Audit happens after commit; never place a read-only audit inside the write transaction merely to make the response shape convenient.

### 8.4 Required tests

1. An opposition claim with only a `supports` anchor fires rule 1; an `asserts` anchor clears it.
2. A `rebuts` edge onto an unanchored target fires rule 2; a person-attributed `asserts` anchor clears it.
3. An `asserts` anchor without a person does not clear rule 2. Use repository-level fixture setup if the service correctly refuses such an anchor.
4. `public_ready=true` with rule 1 open fires rule 6; `false` does not.
5. `refs=[...]` excludes findings whose subject is outside the requested refs.
6. A clean report still includes the assurance text.
7. `claim_upsert` returns findings after a successful commit.
8. Audit never mutates claims, edges, anchors, statuses, or readiness flags.

Run a full audit over the twenty real rows and read every finding before declaring Step 6 complete.

## 9. Phase E: Step 7 — `claim_get` and `argument_graph`

**Gate:** a researcher has tried to inspect a claim subtree by hand and could not. If that has not happened, defer this step.

### 9.1 `claim_get`

Add `claim_get(ref)` returning:

- the claim;
- every anchor with:
  - document title;
  - edition key and, after migration 018, edition identity;
  - locator;
  - typed quote and verification metadata;
  - canonical context from `AnchorContextService`;
- immediate incoming and outgoing edges, each with both endpoint refs.

Reuse `AnchorContextService`; do not duplicate its windowing or node-selection logic.

### 9.2 `argument_graph`

Add `argument_graph(ref, depth=3, relations=None)`.

Contract:

- validates `depth >= 0` and applies a conservative maximum;
- walks incoming and outgoing edges;
- returns each node once at its minimum depth;
- labels nodes as root, upstream, downstream, or both;
- returns natural edge direction (`source_ref`, `target_ref`, `relation`);
- applies the relation filter during traversal, not only after traversal;
- terminates on cycles.

For `depends_on`, a target is upstream of its source; a source is downstream of its target. Keep the raw edge direction for all relation types so clients need not infer semantics from the traversal label.

Implement `PGClaimRepo.graph_from` with cycle-safe SQL or an equally bounded repository algorithm. Do not use an unbounded recursive `UNION ALL`.

### 9.3 Required tests

- depth 1 returns immediate neighbors only;
- incoming and outgoing nodes are both returned and labelled;
- a two-claim cycle returns each node once;
- `relations=['depends_on']` excludes support/rebuttal edges from both traversal and output;
- hydrated anchors include document title and edition key;
- unknown refs use the standard not-found envelope.

## 10. Phase F: Step 8 — `claim_leverage`

**Gate:** the graph has approximately sixty claims and can no longer be prioritized by inspection. Otherwise defer it.

Add `claim_leverage(ref=None, max_depth=12)`.

For one claim, walk downstream dependents through `relation='depends_on'`:

```sql
WITH RECURSIVE downstream AS (
    SELECT e.source_id AS claim_id, 1 AS depth
    FROM argument.claim_edges e
    WHERE e.relation = 'depends_on'
      AND e.target_id = :claim_id
  UNION
    SELECT e.source_id, d.depth + 1
    FROM argument.claim_edges e
    JOIN downstream d ON e.target_id = d.claim_id
    WHERE e.relation = 'depends_on'
      AND d.depth < :max_depth
)
SELECT c.ref, c.statement, min(d.depth) AS depth
FROM downstream d
JOIN argument.claims c ON c.id = d.claim_id
GROUP BY c.ref, c.statement
ORDER BY depth, c.ref;
```

Use `UNION`, not `UNION ALL`. Return minimum depth when a claim has multiple paths.

With no ref, rank all `status='open'` claims by the count of distinct transitive downstream dependents. Stable ordering is dependent count descending, then ref ascending.

Required tests:

- a four-node chain reports depth 3 at the far end;
- a cycle terminates with unique claims;
- paths of length 2 and 4 report depth 2;
- non-`depends_on` edges are ignored;
- no-argument mode ranks all open claims by distinct dependent count.

Completion requires using the result to choose actual work, not merely making the query green.

## 11. Phase G: Step 9 — stale-anchor re-verification and audit rules 3–5

**Gate:** either a parser version changed or two argument bricks now exist and can conflict. Otherwise defer it.

### 11.1 Stale-anchor selection

Implement `PGClaimRepo.stale_anchors(limit)` with this predicate:

```sql
SELECT c.ref, a.id, a.quoted_text, s.document_id,
       a.parser_version AS anchored_at, dt.parser_version AS current
FROM argument.anchors a
JOIN argument.claims c       ON c.id = a.claim_id
JOIN evidence.source_spans s ON s.id = a.source_span_id
JOIN core.document_texts dt  ON dt.document_id = s.document_id
WHERE a.verify_status IS NULL
   OR a.parser_version IS DISTINCT FROM dt.parser_version
ORDER BY c.ref, a.id
LIMIT :limit;
```

### 11.2 `claim_reverify`

Add `claim_reverify(refs=None, limit=100)`.

For each selected anchor:

1. Re-run whole-document quote verification on the stored typed quote.
2. On `exact`, `normalized`, or `near`, resolve the returned current coordinates and update:
   - `source_span_id` when coordinates changed;
   - `verify_status`;
   - `verified_at`;
   - the anchor's `parser_version` to the current document-text parser version.
3. On `not_found` or `no_canonical_text`, report the failure and leave the anchor unchanged.
4. Never delete an anchor or silently point a failed verification at a different passage.
5. Isolate writes per anchor or use savepoints so one failed anchor does not erase successful re-verifications.

Important subtlety: the source-span resolver may return an existing span created under an older parser version when the coordinates did not move. Do not copy that stale span version back onto the anchor. The successful re-verification's current document parser version is the authoritative value for the anchor.

### 11.3 Additional audit rules

| Rule | Severity | Fires when |
|---|---|---|
| `CLM_ANCHOR_STALE` | error | anchor verification is null or anchor parser version differs from current document text |
| `CLM_SPAN_DOUBLE_USE` | warning | one `source_span_id` supports one claim and rebuts another |
| `CLM_SELF_DEFEAT` | warning | a `rebuts` or `contradicts` edge has `kind='mine'` at both ends |

Queries:

```sql
-- CLM_SPAN_DOUBLE_USE
SELECT a.source_span_id,
       array_agg(DISTINCT c.ref || ':' || a.role ORDER BY c.ref || ':' || a.role) AS uses
FROM argument.anchors a
JOIN argument.claims c ON c.id = a.claim_id
GROUP BY a.source_span_id
HAVING bool_or(a.role = 'supports')
   AND bool_or(a.role = 'rebuts');

-- CLM_SELF_DEFEAT
SELECT src.ref, e.relation, tgt.ref
FROM argument.claim_edges e
JOIN argument.claims src ON src.id = e.source_id AND src.kind = 'mine'
JOIN argument.claims tgt ON tgt.id = e.target_id AND tgt.kind = 'mine'
WHERE e.relation IN ('rebuts', 'contradicts');
```

The audit output must also say that self-defeat can only be reported after the edge is recorded. The engine does not discover that an argument proves too much.

### 11.4 Required tests

- bumping one fixture document's parser version makes only its anchors stale;
- successful re-verification changes `verified_at` and uses the current parser version;
- failed re-verification reports the anchor, without deletion or re-pointing;
- one span supporting A and rebutting B fires rule 4; two supports do not;
- a mine-to-mine rebuttal/contradiction fires rule 5; mine-to-opposition does not;
- the full audit still carries the mechanical-checks disclaimer.

## 12. Phase H: Step 10 — works integration, migration 018, and quote quota

**Gate:** at least one real work file carries a `claims:` ref from Step 5.

### 12.1 File work verification

`packages/core/src/research_engine/services/works/verify.py` currently emits `AUTH_CLAIM_UNRESOLVED` at `info` for every front-matter ref and says the ledger is absent. Replace that placeholder behavior:

- inject the claim repository into `WorkVerifier`;
- resolve all refs in one repository query, not one query per ref;
- known ref: no finding;
- unknown ref: `AUTH_CLAIM_UNRESOLVED`, severity `error`;
- remove the stale “ledger does not exist” wording.

Add a complete `existing_refs(refs)` repository method only when implementing this path.

Update composition where `WorkVerifier` is created and update file-verifier tests for both known and unknown refs.

### 12.2 Row work validation: do not invent a carrier

`WorkValidationService` currently has no claim repository and deliberately emits no claim-ref rule. More importantly, assembled revision rows do not have a first-class claim-ref field.

The module docstring says front-matter refs are carried in `revision.metadata.port`, but current code only reads `metadata.port.file` for drift and no import/flip path in this checkout writes front matter into row metadata. `work_create` also does not expose revision metadata.

Therefore:

1. Inspect any import/flip work that has landed by the time this step starts.
2. If it persists original front matter in `revision.metadata.port.front_matter`, read `claims` from that canonical object and resolve it in `WorkValidationService`.
3. If no canonical carrier exists, do not add an undocumented ad hoc key merely to satisfy the test. Either implement the already-designed file-to-row port in its own scoped change or leave row validation explicitly gated.
4. Once a carrier exists, inject the claim repository, emit `AUTH_CLAIM_UNRESOLVED` as an error for missing refs, add it to the default severity map, and prove it blocks freeze.

A resolved claim should not create a finding. Claim status is not part of this rule; `AUTH_OPEN_DEPENDENCY` remains separate and deferred.

### 12.3 Migration 018: anchor edition identity

Create the next Alembic revision after `017_vector_index_restore`, conventionally named `018_anchor_editions.py`.

Upgrade:

1. Add nullable `argument.anchors.edition_id uuid`.
2. Add an FK to `bibliography.editions(id)` with `ON DELETE RESTRICT`.
3. Add an index on `edition_id`.
4. Backfill `edition_id` by joining `anchors.edition_key = bibliography.editions.edition_key`.
5. Fail with a clear migration error if a non-null anchor `edition_key` has no bibliography row; do not silently manufacture CSL metadata.
6. Drop the legacy free-text `argument.anchors.edition` column.
7. Keep denormalized `edition_key` beside the FK, matching `authored.citation_items`.

Downgrade reverses the schema cleanly: restore nullable `edition`, drop the edition-id index and FK, then drop `edition_id`.

Update:

- `packages/core/src/research_engine/adapters/storage/postgres/schema.py`;
- `Anchor` and internal anchor draft/write models;
- claim repository row mapping and insert;
- `ClaimService` composition to use `PGEditionRepo`;
- hydrated claim output.

External MCP input remains `edition_key`, not a caller-supplied database UUID. Resolve a non-null key through `PGEditionRepo.get_by_key`; refuse an unknown key. Leave `edition_id` null when no edition key was supplied.

Required tests:

- upgrade/downgrade/upgrade/downgrade on the isolated scratch database leaves no orphan column, index, or constraint;
- a pre-existing anchor with a valid edition key receives the matching ID;
- deleting an edition referenced by an anchor fails;
- a newly written keyed anchor stores both key and ID;
- an unknown key is refused without a partial claim write.

Extend `tests/integration/test_spans.py::test_migrations_revert_cleanly`; that test provisions a scratch database and is the correct place for destructive migration round trips.

### 12.4 Licensing rule

Use rule ID `AUTH_LICENSE_EXPORT`.

Policy:

- cap total stored quoted characters from one source document in one work;
- use the existing `MAX_QUOTE_CHARS = 1000` as the initial cap rather than introducing a second unexplained number;
- count each citation item's non-null `quoted_text` length once, grouped by the cited source span's document;
- all intents count because the licensing risk is copied source text, not semantic intent;
- report per-document totals and the cap in `detail`;
- warning at no gate/freeze, error at publish, so drafting and freezing remain possible but publication cannot become a passage dump;
- allow the existing work-type severity policy to tighten the rule, not silently disable it.

Do not count canonical context windows: they are read-only tool responses and are not stored in the work. Do not export those windows.

Required test: a work whose citation items exceed the cap for one document fails the publish gate; the same total split across two documents is evaluated separately.

### 12.5 Step 10 completion

Step 10 is complete when:

- known and unknown file refs resolve correctly;
- row refs resolve and block freeze only after a canonical row carrier exists;
- migration 018 round-trips and backfills safely;
- edition deletion is restricted;
- the quote quota blocks publish;
- one real work freezes with a resolved claim ref, if the file-to-row port exists.

If the port still does not exist, report that acceptance item as externally gated rather than faking revision metadata through a test-only path.

## 13. Deferred work and exact triggers

| Deferred work | Trigger |
|---|---|
| `argument.derivations` and a reasoner | graph exceeds about 300 claims and manual status propagation becomes unreliable |
| `claim_phrasings` plus duplicate detection | the same premise is entered twice under different wording, twice |
| redistribution-claim extraction schema | hand curation cannot keep up with ingest and the relevant extraction run is actually scheduled |
| `ClaimStatusFilter` | repeated need to search only open claims |
| verification-attempt history | first concrete need for attempt history |
| database CHECK constraints for claim kind/relation | at least 30 real rows have stabilized the vocabulary |
| Zotero integration | an outside collaborator requires CSL interchange |
| `AUTH_OPEN_DEPENDENCY` | row works carry first-class claim dependencies and publication policy needs status gating |

Do not implement deferred items “while here.”

## 14. Verification matrix

Run the smallest relevant checks after each step, then the complete focused set before handoff.

```bash
# Existing claim path
uv run pytest tests/integration/test_claim_ledger.py -q

# MCP registry/dispatch and repository surface
uv run pytest \
  tests/unit/test_registry.py \
  tests/unit/test_dispatch.py \
  tests/unit/adapters/test_repository_surface.py \
  tests/unit/test_surface_invariants.py -q

# File and row works behavior
uv run pytest \
  tests/unit/works/test_work_verify.py \
  tests/integration/test_works.py \
  tests/integration/test_work_spine.py -q

# Migration round trip
uv run pytest tests/integration/test_spans.py::test_migrations_revert_cleanly -q

# Final static validation
make lint
```

Add focused test modules for audit, graph, leverage, and re-verification when those features land. Do not force all conditional steps into one large integration test.

For every MCP tool added, also verify:

- registry list;
- schema and required fields;
- actual `dispatch_tool` success response;
- invalid input envelope;
- unknown ref/not-found envelope;
- tool-catalog count/description expectations.

For significant behavior, run one actual scenario through the tool dispatch layer. Unit and integration tests are necessary regression coverage, not proof that the MCP surface is usable.

## 15. Known implementation traps

1. **Skipping Step 5 because it is not code.** This leaves the schema unvalidated and makes later audit rules theater.
2. **Treating `near` as exact.** Store the actual tier. Works publication policy may still refuse it.
3. **Using canonical quote as anchor input.** Preserve the researcher's typed quote separately from the shared span slice.
4. **N+1 ref resolution.** Work verification and audit scopes should resolve sets in one query.
5. **Recursive cycles.** Use cycle-safe traversal and minimum depth.
6. **Stale parser version from an existing span.** Re-verification uses the current document-text version.
7. **Forgetting edge RESTRICT during cleanup.** Delete explicit edges before target claims.
8. **Assuming `metadata.port` contains claims.** Prove a writer creates the key before making it authoritative.
9. **Running destructive migration checks against the dev corpus.** Use the scratch-database fixture.
10. **Calling a green audit proof.** Every report must preserve the mechanical-only disclaimer.
11. **Ingesting the whole bibliography.** Acquire only what a live claim requires.
12. **Adding all future repo methods up front.** Complete methods only; no stubs.

## 16. Build log

Append one dated subsection per completed phase with:

- files changed;
- live gate evidence;
- deviations from this guide and why;
- exact commands/scenarios run;
- test results;
- remaining external blockers.

Step 5 must include the schema-verdict table, even when the verdict is “no missing fields.”

### 2026-09-16 — Phase A: runtime and live-path restoration

- Files changed: none; this phase changed the local runtime only.
- Live gate evidence:
  - `make db && make migrate && uv run research-engine doctor` restored Postgres and passed the corpus checks over 156,723 passages.
  - The starting Alembic head was `017_vector_index_restore`.
  - Schemas `argument`, `authored`, `bibliography`, and `evidence` existed.
  - Initial ledger counts were 0 claims, 0 edges, and 0 anchors.
  - Actual `claim_upsert` then `anchor_context` dispatch stored an exact throwaway anchor, returned its canonical slice, and cleaned up the claim, anchor, and orphan span.
- Deviations: none.
- Remaining blockers after this phase: acquisition and the twenty-row audit.

### 2026-09-16 — Phase B: narrow acquisition

- Repository files changed: none. Provider data and dependency changes remain outside core.
- External prerequisites located:
  - authoritative analysis: `/home/john/Downloads/limitation of government-20260411T192431Z-3-001/limitation of government/sider_government_redistribution_analysis.md`;
  - installed packs: `~/.research-engine/plugins/kindle@0.3.1` and `~/.research-engine/plugins/yourcloudlibrary@0.1.0`;
  - authenticated Logos, Kindle, and YourCloudLibrary state under the operator's existing profiles.
- Plugin health:
  - installed the manifests' missing `playwright` and `pytesseract` dependencies into the environment that loads the packs;
  - aligned Playwright to the Kindle pack lock (`1.59.0`) after the latest browser download timed out;
  - `research-engine doctor` then loaded both `kindle` and `yourcloudlibrary`.
- `corpus_stats` moved from 3,084 documents / 156,723 passages to 3,088 documents / 158,500 passages: exactly the four intended new documents.
- Acquired documents:
  - *Rich Christians in an Age of Hunger*, sixth edition, Logos `LLS:9780718037192`: 481 articles, 776 passages, 0 failed articles, 0 missed TOC articles;
  - *Old Testament Ethics for the People of God*, Logos `LLS:LDTSTMNTTHPPLGD`: 298 articles, 880 passages, 0 failed articles, 0 missed TOC articles;
  - Mott and Sider, “Economic justice: a biblical paradigm”: 71 passages, 0 failed files;
  - *Rerum Novarum* from the official Vatican HTML: 50 passages, 0 failed files.
- *Just Politics* was already queryable. All five live sources received `metadata.edition_key`, matching `bibliography.editions` rows with CSL data, and an exact or normalized hand-picked verification.
- Commands/scenarios: `logos_ingest_book` for both licensed Logos books; `research-engine ingest` for the Mott/Sider PDF and Vatican HTML; `verify_quote` for each source; before/after `corpus_stats`.
- Remaining acquisition scope: Wolterstorff and Yoder were not required after the completion gate was met and were not substituted with different works.

### 2026-09-16 — Phase C: Step 5 twenty-row audit

- Live data changed: `SIDER-001` through `SIDER-020` were entered sequentially through `claim_upsert`.
- All twenty rows are `kind='opposition'`, `status='open'`, and have a completed steelman.
- Each row was researched through `find_passages`, verified through `verify_quote`, written with a person-attributed `asserts` anchor naming Ronald J. Sider, then read through `anchor_context`.
- Verification remained honest: `SIDER-001`–`SIDER-009` are normalized, `SIDER-010`–`SIDER-020` are exact, and no `near` result was promoted.
- The rows incorporate the source corrections in this handoff: the surviving state-responsibility sentence, the limited ruler-implementation claim, family land/private-property/anti-communist qualifications, and Sider's rejection of directly transplanting the ancient mechanisms into modern global markets.
- Acceptance SQL:
  - claims: 20;
  - null verification grades: 0;
  - personless `asserts` anchors: 0;
  - unanchored claims: 0.
- Schema verdict:

| Date | Missing field or pressure | Claim refs affected | Decision |
|---|---|---|---|
| 2026-09-16 | None | None | Keep schema |

- Remaining blockers after this phase: none for Step 6 or the file-based Step 10 path.

### 2026-09-16 — Phase D: Step 6 mechanical audit

- Files changed:
  - `packages/core/src/research_engine/domain/claims.py`;
  - `packages/core/src/research_engine/ports/repositories.py`;
  - `packages/core/src/research_engine/adapters/storage/postgres/repositories/claims.py`;
  - `packages/core/src/research_engine/services/argument/{claims,rules,__init__}.py`;
  - `packages/core/src/research_engine/mcp/tools/claim_audit.py`;
  - `packages/core/src/research_engine/mcp/dispatch.py`;
  - `packages/core/src/research_engine/composition.py`;
  - focused claim-ledger and repository-surface tests.
- Implemented `CLM_OPPOSITION_UNANCHORED`, `CLM_REBUTS_UNANCHORED`, and `CLM_PUBLIC_UNEARNED`; audits are read-only and scoped by subject ref.
- `claim_audit` always carries the mechanical-checks disclaimer. A full live audit over all twenty rows returned no findings.
- `claim_upsert` now runs the audit after commit and returns findings. A dispatch smoke wrote an unanchored opposition claim, returned `CLM_OPPOSITION_UNANCHORED`, proved the row had committed, and removed the throwaway row.
- Conditional gates:
  - Step 7 did not fire: the ledger has no edges or subtree to inspect;
  - Step 8 did not fire: the graph has 20 claims, below the approximate 60-claim trigger;
  - Step 9 did not fire: stale-anchor count is 0, edge count is 0, and no conflicting argument bricks exist.
- Tests: 11 focused claim integration tests and 168 registry/dispatch/repository/surface tests passed.

### 2026-09-16 — Phase H: Step 10 works, edition identity, and licensing

- Files changed:
  - `works/mishpat-tsedaqah-survey.md`;
  - `packages/core/src/research_engine/services/works/{verify,validate}.py`;
  - `packages/core/src/research_engine/adapters/storage/postgres/repositories/{claims,work_blocks}.py`;
  - `packages/core/src/research_engine/adapters/storage/postgres/schema.py`;
  - `packages/core/src/research_engine/adapters/storage/postgres/migrations/versions/018_anchor_editions.py`;
  - claim domain/service/composition wiring and focused works/migration tests.
- File refs now resolve in one `existing_refs` query. The real survey carries `SIDER-006`; live `work verify` read all 182 citations with 0 errors, 0 warnings, and no unresolved claim finding.
- Migration `018_anchor_editions` is the live head. It backfilled all keyed anchors, removed the legacy `edition` column, created the restricted edition FK and index, and left 0 keyed anchors without an ID and 0 key/ID mismatches.
- The scratch migration test exercises upgrade/downgrade/upgrade/downgrade, valid-key backfill, index/constraint cleanup, and edition-delete restriction.
- New keyed anchors resolve the external key through `PGEditionRepo`, store both key and ID, and reject unknown keys before opening the claim write transaction.
- `AUTH_LICENSE_EXPORT` counts each stored citation-item quote once per source document. It warns at no gate/freeze, becomes an error at publish, cannot be silently disabled by work-type policy, and evaluates split-source totals independently.
- Actual dispatch smoke: a 1,002-character quote produced `AUTH_LICENSE_EXPORT` with cap 1,000 and blocked the publish gate; the throwaway work and span were removed.
- The dispatch smoke exposed and fixed timestamp-equivalence on JSON (`Z`) optimistic block updates; the regression test now covers the real serialized tool value.
- Row claim-ref validation remains gated. No import, flip, or create path persists `revision.metadata.port.front_matter.claims`; no ad hoc carrier was invented.

### 2026-09-16 — focused verification

- `pytest tests/integration/test_claim_ledger.py tests/integration/test_claim_audit.py -q`: 11 passed.
- Registry, dispatch, repository-surface, and surface-invariant set: 168 passed.
- File/row works set: 54 passed.
- `pytest tests/integration/test_spans.py::test_migrations_revert_cleanly -q`: 1 passed.
- `make lint`: passed.
- Remaining external gate: canonical row-level claim refs await a real file-to-row front-matter carrier.
