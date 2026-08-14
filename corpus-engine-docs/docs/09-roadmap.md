# 09 — Roadmap

This roadmap phases the work so that each milestone produces something
usable by the primary user on real research material. No milestone is
purely foundational — every phase ends with demonstrable capability
against the McClellan use case.

## Guiding sequencing principle

> **Build by extraction, not by anticipation.**

Resist the temptation to design the pack interface, full domain pack,
or abstraction layers up-front. Phase 1 builds the core engine with
the history-specific behavior hardcoded. Phase 2 adds a second domain
(biblical studies) and extracts the right abstractions. Only in Phase 3
does the pack system become a first-class, externally-consumable
thing. This is the proven way to end up with abstractions that fit
rather than framework-first abstractions that don't.

## Phase 0 — Setup (1–2 weeks)

### Goals

- Development environment set up.
- Dependencies chosen and provisioned.
- Open questions resolved (or triaged to "decide later, not blocking").

### Deliverables

- Working Python project skeleton with linting, testing, formatting.
- Postgres + pgvector running locally; connection pooling configured.
- Embedding model chosen (see [10-open-questions.md](10-open-questions.md));
  local or API-based.
- Anthropic API integration working end-to-end on a hello-world prompt.
- MCP SDK integrated; minimal server exposing one dummy tool and
  successfully connected to Claude Code.
- CI configured to run tests on every push.
- Repo README and contribution guide drafted.

### Exit criterion

A contributor clones the repo, runs setup, and sees a passing test
suite + a working dummy MCP tool visible from Claude Code within 30
minutes.

## Phase 1 — History-first core (10–14 weeks)

The central milestone. At the end of Phase 1, the primary author
conducts real McClellan research using Corpus Engine and Claude Code.
Everything is hardcoded for this use case — no pack abstraction yet.

### Workstream 1.1 — Storage & ingestion core (weeks 1–4)

- Core schemas from [04-data-model.md](04-data-model.md) implemented
  and migrated.
- PDF ingestion module (text-extractable).
- Plain text, Markdown, EPUB ingestion modules.
- CSV/YAML sidecar metadata merge.
- Default chunker (prose window with sentence-boundary snapping).
- `corpus-engine ingest` CLI with progress reporting.
- Ingestion idempotency and resume.
- Unit + fixture tests for each module.

**Exit:** 847 McClellan letters (or the actual available corpus)
ingested into a local database with accurate metadata.

### Workstream 1.2 — Hybrid search (weeks 3–6, overlaps 1.1)

- Postgres FTS integration.
- pgvector HNSW index and query path.
- Filter builder (SQL generator for the filter schema).
- RRF fusion.
- Cross-encoder rerank integration (see open questions for provider).
- `find_passages` MCP tool wired end-to-end.
- Search evaluation harness with a seed query set.

**Exit:** The primary user can ask Claude Code a natural-language
question and get back hits with accurate citations, consistently.

### Workstream 1.3 — Extraction framework (weeks 5–9)

- Schema format parser and validator.
- Extraction executor: cache check → prompt → LLM → validate → store.
- Evidence-span validation (substring check).
- Entity-ref and fuzzy-date post-processing.
- `extract`, `list_extraction_schemas`, `query_extractions` MCP tools.
- Two initial schemas (hardcoded in Phase 1, move to pack in Phase 2):
  `epistolary_references` and `claims`.

**Exit:** Running `epistolary_references` extraction against the
McClellan corpus produces useful, auditable records with evidence
spans for every one.

### Workstream 1.4 — Entities, events, timelines (weeks 7–11)

- Entity / alias / mention tables and CRUD.
- Event model with fuzzy-time support.
- `resolve_entity`, `find_mentions`, `get_entity` MCP tools.
- `events`, `timeline_compare` MCP tools.
- A small bootstrap prosopography (McClellan, Barlow, Lincoln, etc.)
  loaded as test data.
- Post-ingestion hook that creates a `letter_sent` event per ingested
  letter (hardcoded for now).

**Exit:** Claude Code can produce correspondence density timelines
and multi-stream overlays (letters vs. battles) via tool calls.

### Workstream 1.5 — Gap detection tool (weeks 10–13)

- Hardcoded `find_missing_letters` tool in core (will move to history
  pack in Phase 2).
- Reference resolution logic: match extracted references to letters
  in corpus; unresolved = candidate missing.
- Cadence anomaly detection over correspondence pairs.
- `correspondence_cadence` tool.
- Output formatting usable as a real archival research brief.

**Exit:** The primary author runs gap analysis on McClellan-Barlow
correspondence and surfaces concrete, verifiable candidate missing
letters with archival leads.

### Workstream 1.6 — Polish & real use (weeks 12–14)

- Provenance tool (`provenance_of`).
- `corpus_stats` tool.
- LLM call logging and cost reporting.
- Backup/restore.
- Error handling polish.
- Documentation for the primary user's actual workflow.

### Phase 1 exit criteria

- The primary author uses the system daily for real McClellan research
  for at least two weeks before Phase 2 starts.
- At least one real research artifact (gap analysis, sentiment
  timeline) is produced with zero hallucinated citations.
- The search P95 latency target is met on the actual corpus.
- All core MCP tools from [05-mcp-spec.md](05-mcp-spec.md) work
  end-to-end.

## Phase 2 — Second domain & pack extraction (8–12 weeks)

The goal of Phase 2 is **extracting the right abstractions** by
building a second domain (biblical studies, leveraging prior Logos
work) and letting the shared shape of the two domains dictate what
belongs in core vs. in packs.

### Workstream 2.1 — Biblical studies capability (weeks 1–6)

- Ingestion modules for OSIS and USFM biblical text.
- Entity types: verse, pericope, lemma.
- Extraction schemas for interpretive claims and cross-references.
- Specialized tools: pericope view, lemma search.
- Vocabularies: Strong's, Louw-Nida.

Built initially in the main codebase, *alongside* the history
capabilities, but namespaced from day one (e.g. `core.biblical.*`).

### Workstream 2.2 — Pack system extraction (weeks 4–10, overlaps 2.1)

With two domains living in the codebase, factor out:

- The `pack_sdk` module (types and base classes).
- The manifest format (finalize the v1 schema).
- The type registry (document_type, entity_type, event_type, etc.).
- The permission model.
- The pack loader, installer, uninstaller, reloader.

Then **move the history and biblical capabilities out** into separate
repositories:

- `corpus-engine-history`
- `corpus-engine-biblical`

The core repository is now domain-clean.

### Workstream 2.3 — Pack developer experience (weeks 8–12)

- `pack init` scaffolding.
- `pack test` harness with fixtures.
- Pack documentation site (cookbook style) with 5+ worked examples.
- A third small pack written *against the public SDK* (not from
  inside the core team), to validate the API. Candidate: a personal
  journal / commonplace-book pack.

### Phase 2 exit criteria

- Core repo contains no domain-specific code.
- Two official packs install cleanly from GitHub URLs and are
  productive on real work.
- A third, small pack is written against the public SDK by someone
  following the cookbook (may be a team member, but working only
  from the public interface).
- The primary author continues using the system in daily research
  throughout, with no regression.

## Phase 3 — External usability (6–10 weeks)

At this point the system works for its primary user. Phase 3 makes
it usable by other people.

### Workstream 3.1 — Setup experience

- One-command install (`pip install corpus-engine` or equivalent).
- Optional Docker Compose for Postgres.
- `corpus-engine init` that produces a working local instance with
  sensible defaults.
- Interactive setup wizard for LLM provider configuration.
- Clear onboarding doc: "first hour with Corpus Engine."

### Workstream 3.2 — Documentation

- User guide (concept overview, common workflows).
- Pack author guide (cookbook, API reference, testing).
- Operator guide (backup/restore, scaling, troubleshooting).
- Example research walkthroughs (McClellan gap analysis as a
  published case study; a biblical cross-reference study as another).

### Workstream 3.3 — First external users

- Invite 3–5 external beta users: serious independent researchers in
  fields where the existing packs apply.
- Feedback cycle focused on setup friction, ingestion robustness,
  pack installation, tool ergonomics.
- Fix the top issues surfaced.

### Workstream 3.4 — Optional: lightweight registry

- A public git repo listing known packs.
- `pack search` command against it.
- PR-based pack submission with a manifest validator.

### Phase 3 exit criteria

- An external beta user, starting from nothing, installs Corpus Engine
  and one or two packs, ingests their own corpus, and conducts useful
  research within one working day of elapsed setup time.
- No critical bugs outstanding.
- Documentation is sufficient for self-service pack authoring.

## Phase 4+ — Growth and maturation (ongoing)

Not planned in detail; themes:

- **More packs.** Third- and fourth-party packs for legal, genomics,
  intelligence/OSINT, literary studies, etc.
- **Pack sandboxing.** Move from permission-declaration + user-trust
  to genuine process-level or WASM-level isolation.
- **Scale.** If institutions adopt, optimize for multi-million-passage
  corpora and optional dedicated vector stores.
- **Collaborative features.** Multi-user libraries with
  permissioning. Likely a v2 architecture discussion.
- **Agent-side ergonomics.** Curated prompt patterns and system prompts
  that make Claude Code especially fluent with Corpus Engine's tool
  surface.
- **Real-time ingestion / feeds.** RSS, arXiv, mailing list archives.

## Milestones summary

| Milestone | Rough timing | Exit demo |
|-----------|--------------|-----------|
| M0 — Setup complete | Week 2 | Hello-world MCP tool visible in Claude Code. |
| M1 — Ingestion works | Week 4 | McClellan corpus indexed; searchable by keyword. |
| M2 — Hybrid search works | Week 6 | Semantic + keyword queries return good hits with citations. |
| M3 — Extraction works | Week 9 | `epistolary_references` produces auditable records. |
| M4 — Timelines work | Week 11 | Correspondence density + overlay queries render correctly. |
| M5 — Gap detection works | Week 13 | Real archival leads produced for McClellan-Barlow. |
| M6 — Phase 1 complete | Week 14 | Daily research use by primary author. |
| M7 — Second domain | Week 20 | Biblical studies capability working. |
| M8 — Pack split | Week 24 | Core is domain-clean; two official packs installable. |
| M9 — External usable | Week 30 | Beta user productive within 1 day. |

## Risk register

| Risk | Mitigation |
|------|------------|
| Embedding model choice is suboptimal; re-indexing mid-project | Schema supports multiple embedding generations side-by-side; re-index is not destructive. |
| Hybrid search quality disappoints | Evaluation harness from day one; rerank is available as a fallback quality lever. |
| LLM cost balloons during extraction | Aggressive caching, visible cost reporting, configurable concurrency caps. |
| Pack abstractions extracted prematurely | Phase sequencing enforces two-domain-driven extraction. |
| Category C pack ecosystem creates legal exposure | Strict policy in [06-ingestion-modules.md](06-ingestion-modules.md); main repo and docs never advertise or link. |
| Single-author dependency | Document extensively; invite external Phase 2/3 contributors. |
| Performance fails to scale beyond McClellan-sized corpora | Performance targets are stated per corpus size; if scale fails, architecture decisions (dedicated vector store, partitioning) are available. |

## What would cause us to replan

- Search quality, after implementation, is meaningfully worse than
  available alternatives (e.g. a dedicated vector DB with its own
  hybrid search). Trigger: re-evaluate pgvector choice.
- Extraction quality is unacceptable (high hallucination rate despite
  substring verification). Trigger: invest in agentic extraction or
  constrained-generation approaches.
- External-user feedback in Phase 3 shows setup friction is the
  primary blocker. Trigger: larger investment in installer and
  wizard UX, potentially shipping a bundled Postgres.
- Pack ecosystem fails to materialize (no third-party packs after 6
  months of Phase 3). Trigger: more aggressive outreach, more
  reference packs, possibly a hosted registry.
