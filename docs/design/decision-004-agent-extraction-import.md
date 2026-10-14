# Decision 004 — Extraction answers are imported through the executor's own validation

**Status:** accepted, 2026-10-13 (operator decision during M2)
**Amends:** [Decision 003](decision-003-dilemma-finder-v2.md) (model and cost assumptions only)
**Applies to:** `services/extraction/*`, migration `023_machine_claims`,
`services/dilemma/{packets,materialize}.py`, `research-engine dilemma extract …`,
and the dilemma pack's `doctrinal_claims` schema.

## The question

Decision 003 assumed extraction calls an API model through the synchronous
`LLMPort`, metered in dollars. The operator instead runs extraction with the
agent runtime already in use for this codebase (Muse agents, Wonder model):
no API key, no per-token bill, and no adapter to build. How do answers an
outside process produced enter the corpus without bypassing the checks that
make an extracted claim worth storing?

## The decision

1. **Packets out, answers in, one validation path.** Core builds extraction
   *packets* (rendered prompt, target passage, neighbours, entity hints) under
   a recorded `dilemma.runs` row. An external agent answers them. The import
   feeds each answer through `ExtractionExecutor.import_records`, which runs
   the same validate → enrich → store path as a live model call: schema
   conformance, evidence located inside the target passage, entity
   post-processing, caching. Provenance is the `llm_model` label on the
   extraction row plus the run's `model_versions`; `llm_call_id` stays null
   because there was no `core.llm_calls` row. A local `LLMPort` caller and an
   imported answer are indistinguishable downstream — that is the point.
2. **The cache key carries the whole question.** `extractor_version` (now
   `1.1`) digests the schema definition, the prompt template, the model
   label, the context-window sizes and the entity hints. Two runs that asked
   differently never share an answer.
3. **Machine claims are a materialization, not a second extraction.**
   `dilemma.claims` (023) is built from extraction records by code that
   re-locates every evidence quote in the passage, anchors it through
   `source_spans` at document offsets, and checks the canonical slice matches.
   Multi-span evidence keeps every span, in order (`claim_evidence`). Vouch
   statements extracted in the same pass become a claim plus a
   `dilemma.vouch_edges` row. Nothing enters `argument.claims`; promotion
   stays a human act.
4. **Resolution never guesses.** Entity labels resolve against
   `core.entities` in exact mode only (case-insensitive canonical-name or
   alias equality; fuzzy trigram search still exists for interactive
   resolution, now alias-aware). A hint/seed reference travels as
   `subject_ref` / `speaker_ref` / `target_ref` verbatim for the profile
   layer; an unresolved label stays a label.
5. **The cost gate speaks in items, tokens and minutes.** Building packets
   for more than 1,000 items prints the estimate (prompt tokens at
   4 chars/token, 220 output tokens per item, wall-clock at a measured
   seconds-per-item) and refuses without an explicit acceptance — before any
   run row or file exists. There is no dollar figure because there is no
   metered API; printing one would be fiction.
6. **A blind profile refuses packets.** Building packets for a profile whose
   settings mark it blind is an error, so "none of its output is viewed
   before the freeze" is enforced by the tool, not by habit.

## What this does not license

- Importing answers that skip executor validation because a human or an agent
  "already checked" them.
- A model label that does not name the runtime that actually answered.
- Resolving a seeded reference by fuzzy match when exact resolution fails.
  Same-named entities merging silently is the failure `exact` mode exists to
  prevent.

## Consequences recorded at the pilot (M2)

- The pilot's non-atomic bundling and speaker omissions are prompt defects,
  measured in the M2 report; the prompt is pack data, so fixing them does not
  reopen this decision.
- Seeded hint entities are not yet rows in `core.entities`, so exact
  resolution rarely fires. Seeding the global ontology is an M3 pre-step; the
  refs needed to join later are already stored.
