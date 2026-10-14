# Decision 003 — The Dilemma Finder follows the v2 brief, amended

**Status:** accepted, 2026-10-13
**Amends:** [Decision 002](decision-002-claim-layer-in-core.md)
**Applies to:** migrations `021_provenance` and `022_dilemma_profiles`, the
SDK `ProfileClient` and `research_engine_sdk.units`, core's `unit_window`
chunker, `research-engine dilemma …`, and the `dilemma` pack.

## The question

The first milestone of the Dilemma Finder had been built to the operator's
first spec when a second brief arrived. It makes the engine
tradition-agnostic: a tradition is a *profile* that gives each shared text a
role. It loads two small corpora before anything large, and it scores a blind
validation stage by stage. Two reviews of that brief agreed it was the better
plan and disagreed with it in places. This records what was adopted, what was
changed, and why.

## What changed in the build

1. **Standing belongs to a profile, in one table.** `bibliography.edition_authority`
   is gone. `dilemma.profiles`, `dilemma.source_roles` and `dilemma.runs`
   (022) replace it. A role is `binding`, `external`, `reference`, `defense`,
   `critical` or `fact`, with a tier, a date window and an optional scope (a
   locator filter such as a list of books).
   - **Several roles, never the same one twice.** One edition may hold
     several roles under one profile: an essay can both bind and defend. The
     exclusion constraint forbids the same role twice over overlapping dates
     and scope.
   - **The vocabulary is the engine's, so the database checks it.** Stage 0
     of the funnel decides what may be a horn from it. What a profile *means*
     stays data.
   - **Why not keep both tables.** Keeping `edition_authority` as history,
     with `source_roles` derived from it, would leave two stores of tier and
     validity that must never diverge. The second reader the shape needs is a
     legal profile: the same statute, binding in one jurisdiction and
     persuasive in another.
2. **A run records what it ran under, not what it spent.** Each run stores
   the profile hash, its config and that config's hash, the code and model
   versions, counts in and out, and the cost gate's estimate. Actual spend
   stays in `core.llm_calls`, the one ledger of model calls.
3. **Work identity is core's.** `bibliography.editions.work_key` (021) says
   what an edition is an edition of, so two editions of one work are never
   read as two witnesses that agree, and a claim read once can be carried
   across.
4. **The structured importer is shared; format readers stay in the pack.**
   The brief put an OSIS/USFM importer in core, and the pack rule puts format
   ingestion in packs. So the unit layout is shared, and the readers are not.
   - **The layout** is `research_engine_sdk.units`. It turns verbatim units
     into canonical text, nodes, windows and marks, the same way for every
     source.
   - **The chunker** is core's `unit_window`, which rebuilds those windows
     from stored text on reindex.
   - **The readers** for each format (verse JSON, USFM, Tanzil, saved HTML)
     stay in the pack.
5. **Profiles are pack data.** The pack holds:
   - a shared editions catalog (`editions/*.yaml`, one entry per edition,
     with no standing);
   - `profiles/<key>/`, holding the roles, a manifest of slices, and
     optionally entities, topics, typed hatches, vouch seeds and gold;
   - the generic hatch types (`hatch_types.yaml`), which every hatch instance
     names.

   A profile's hash covers every file it ships plus the hatch types. Loading
   against stale data is refused.
6. **Loads are honest.**
   - **Mismatched counts.** A load whose counts do not match the edition's
     known shape is refused, and the refusal is recorded. Loading it anyway
     takes an explicit, recorded acceptance.
   - **No reloads.** A loaded edition is not loaded again until a supersede
     path exists. Text already stored by an interrupted run counts as present.
7. **Sites that forbid robots are read from the operator's own saves.** The
   Church's terms of use, which also govern the Joseph Smith Papers, forbid
   any robot or automated access but allow personal downloads. So those
   editions are `manual`: the operator saves the page into the edition's
   inbox, and the pack reads it from there. Both hosts are off the fetch
   allowlist, so the pack cannot reach them.

## The blind protocol, as built

- **No blind-profile specifics in the repository.** The first build's
  Islam-specific seeds, required references and the test that pinned them are
  removed, and the unpushed history was rewritten without them. The blind
  profile ships roles, a manifest and the disclosed title-to-books mapping,
  nothing else.
- **The sealed file lives outside every repository**, under
  `~/.research-engine/plugin-data/dilemma/sealed/` or wherever
  `RE_DILEMMA_SEALED` points. The brief's git-ignored `eval/sealed/` was
  one stray `git add -f` from being committed, and it sat where any agent
  grepping the tree could read it.
- **The guard has two layers.**
  - **Structural checks run everywhere, CI included.** A blind profile may
    ship only roles, a manifest and entities, with no locators in them.
    Nothing scanned may hold a reference qualified as a Qur'an reference.
  - **Sealed-term checks run only where the sealed file is.** They report a
    match by file and line, never the term.
  - **Why not a CI check against the sealed file.** CI cannot read a file
    that is never committed, and generic hatch type names legitimately appear
    in neutral prompts. The brief's design would have failed on both counts.
- **A freeze manifest comes later.** At the tuning freeze, the hashes of
  every prompt, config and profile file are recorded. Nothing from the blind
  profile is viewed before that freeze, and planted-pair results on it are
  reported only in aggregate until then.
- **Stage 0 for a blind profile filters only on role, date and modality.**
  Blocking on topic or entity would need seeds the blind profile may not
  have; "the blind run blocks on nothing" (Decision 002) stands.
- **The primary control is edge ablation.** Remove every extracted vouch edge
  and keep the text. The brief's control removes only the known vouch verses,
  but the corpus holds many more vouching passages than any short list. The
  dilemma could then still assemble from the remaining verses, and that would
  be the system working, not memory. Deleting text is a secondary control.

## Sequence

Re-sequenced from the brief on the second review's advice. A never-tuned
profile now validates the frozen settings before the result that matters
most:

| Milestone | Scope |
|---|---|
| M1 | Two small corpora: the LDS standard works, KJV and gold documents; the Qur'an in Arabic and two translations. Coverage gates, work identity, the guard. |
| M2 | Extraction **pilot**: about 300 stratified units, with a human audit of claim precision, speaker and modality errors, and claims per unit. From it, projected pair volume, NLI throughput and Stage 3 intake. Fixes the executor's context window and cache key, and builds entity alias search with a no-fuzzy mode. |
| M3 | Full extraction behind the cost gate. Funnel Stages 0–2, tuned on LDS only. Planted positives *and* planted near-miss negatives, at a preset N, with Wilson intervals. |
| M4 | `reformed_westminster`, loaded as data alone and never tuned on: validation. Then freeze and hash. |
| M5 | The blind run, Stages A–D, with the edge-ablation control. |
| M6 | Adjudication, hatches, reports and review. Then blind Stages E–F. |
| M7 | Edition diffs in depth; hadith and tafsir. |

## Commitments for later milestones

Recorded now so they are built, not rediscovered:

- **Extraction**
  - **Once per work, against one global entity ontology.** A profile's entity
    and topic mapping is a layer over claims, so a shared text is extracted
    once.
  - **Multi-span evidence.** A claim's evidence may span verses. It is a list
    of spans, each re-verified against `document_texts` when the claim is
    materialized.
- **Dates**
  - **Every run has an explicit `as_of`.** A role's `valid_from` means
    "binding from this date"; it is not the date of utterance or of
    publication.
  - **The fact behind a gold item is checked, not assumed.** Gold lds-01 is
    an example: D&C 130 entered the 1876 edition, canonized in 1880, and the
    *Lectures* left in 1921, so the pair was co-canonical for 41 years.
- **Pairs and findings**
  - **Horn eligibility comes through a vouch path, per source, profile and
    date.** Binding–external pairs are stored but not processed until the
    external text is promoted. The one exception is the blind Stage C
    measurement, which says so.
  - **A finding is 1..n claims, plus optional diff, alignment or fact
    references.** A candidate pair is one kind of finding. Failed prophecies,
    edition changes, parallel anomalies and three-way inconsistencies fit
    beside it.
- **Adjudication**
  - **Every minimal premise is anchored to a binding span or marked
    analytic.**
  - **A vouch counts only where it covers the horn's own text.** It records
    whether it vouches for the work as given or for the edition now extant;
    that gap is the transmission hatch.
  - **Code checks the adjudication.** Closers must come from binding
    documents and best-response quotes from defense documents. An entailment
    check confirms the summary rests on its quotes.
- **Ranking**
  - **Ranking is lexicographic, with every component displayed:** tier pair,
    then vouch, then closure, then adjudicator confidence. Only τ and k are
    tuned; there are too few gold items to fit a product of weights.
  - **Closure separates "no hatch applies" from "defense retrieval found
    nothing".**
- **Operations**
  - **Adjudications are cached by content hash** (pair, prompt, model) and
    carry their `llm_call_id`, so a re-run does not re-pay Stage 3.
  - **No Anthropic Batch port in this plan.** It does not fit the
    synchronous `LLMPort`, its corrective retry or `BudgetGuard`.
  - **Review is blinded.** Reviewed items are shuffled with known negatives,
    and scores are hidden.
- **Facts still to check**
  - **WCF 31.3 [1647] / 31.4 [American] numbering.** Check which number holds
    "synods may err" in each text when the Westminster texts load.

## Unchanged from Decision 002

- Anchoring on `evidence.source_spans`, with no durable `passage_id`.
- No tradition in core code.
- No pack DDL.
- No licence by default.
- Docs split by kind: decisions here, plans in the plan file and PR
  descriptions.
- Generated reports and Works that quote copyrighted text live under
  `~/.research-engine/plugin-data/dilemma/`, outside every repository. That
  excludes the brief's `works/dilemmas/`, and the configured `RE_WORKS_DIR`
  as well, because the latter points inside another checkout.
