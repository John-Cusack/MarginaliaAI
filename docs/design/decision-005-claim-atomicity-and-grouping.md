# Decision 005 — Machine claims are atomic, grouped by their sentence, and reassembled before judgment

**Status:** accepted, 2026-10-13 (operator decision during M2 review)
**Supplements:** [Decision 003](decision-003-dilemma-finder-v2.md) (claim commitments),
[Decision 004](decision-004-agent-extraction-import.md) (extraction import)
**Applies to:** the dilemma pack's `doctrinal_claims` schema (prompt and record
fields), the claim materializer (`services/dilemma/materialize.py`), the funnel
stages still to be built (Stages 0–3), and every audit rubric that scores
extraction quality.

## The question

The texts this engine reads routinely state several propositions in one
sentence, joined by "and", commas, or semicolons. The M2 pilot measured the
consequence of storing those sentences as single machine claims: of 60 audited
claims, roughly a third bundled two or more separable propositions into one
record. At what granularity are machine claims stored, and what happens to the
sentence-level whole that the parts came from?

## The decision

1. **The stored unit is the proposition, not the sentence.** An atomic claim
   asserts one thing that could be affirmed or denied on its own. Where a
   sentence coordinates separable assertions, extraction emits one claim per
   assertion, each with its own exact evidence quote. Prose in this project
   refers to the pilot tradition as Mormon; its texts supplied the adjudicated
   examples below, but the rule names no tradition and the prompt that teaches
   it stays tradition-neutral.

2. **The split test is meaning, not grammar.** Split unless splitting would
   change what the text claims:
   - **Keep whole:** joint conditions of one assertion ("whosoever will
     hearken unto my words and repenteth and is baptized, the same shall be
     saved" — the three verbs are joint conditions of the promise; splitting
     would fabricate "whoever repents shall be saved"), conditionals,
     purpose clauses, comparisons, and single-verb constructions with a
     compound object.
   - **Split:** coordinated assertions each of which another text could deny
     while affirming the rest (a five-item list of a people's traits is five
     claims).
   - **Split and group:** coordinated elements that form one composite act or
     invitation in discourse ("repent, and come unto me") are separable —
     the corpus itself commands the elements separately elsewhere — so they
     are split, and the link is preserved by the group mechanism below.

3. **Grouping is mandatory, and cheap.** Claims split from one sentence carry
   a group tag and the relation the extractor saw
   (`composite_act | independent_list | condition | purpose`). For M3 these
   travel in the claim's `attributes`; they are promoted to columns only if
   funnel queries demonstrably need them. The group, the exact evidence, and
   the containing passage together preserve everything the sentence meant.

4. **Regrouping happens before any judgment.** Stages 0–2 (blocking,
   embedding shortlist, NLI) run on atoms. Before adjudication, a candidate
   pair's packet is expanded with both claims' group siblings and both full
   passages; no verdict is ever rendered on a naked atom. Findings are 1..n
   claims (Decision 003), so a horn may be recorded as the recomposed
   composite. Human sign-off still gates every "confirmed" result.

5. **Bundling is counted, not silently corrected.** Import flags statements
   that still look bundled (coordinated finite verbs; semicolon-joined
   evidence) and reports the count in run stats. An automatic split-and-retry
   loop is built only if the measured rate stays high after the prompt fix —
   machinery follows measurement.

## Why this side of the tradeoff

The two errors are not symmetric:

- **Over-splitting** yields *apparent* contradictions: pairs that arrive, are
  regrouped, are judged `apparent` or `not_contradiction`, and are counted
  against precision on the gold negatives. The error is visible and
  measurable at every stage, and its cost is adjudication volume — a dial
  tuned on the pilot profile's tune split (for example, requiring a
  `composite_act` group's siblings to also find counterparts before the pair
  advances).
- **Under-splitting** yields *missed* contradictions: a fused claim embeds
  between its components, matches none of them well, blocks under one subject
  or none, and no later stage knows to look inside it. The error is invisible
  and unmeasurable, in a system whose credibility rests on having searched
  the whole binding corpus.

Sentence-level storage was rejected for that reason, and a dual-level
hierarchy (sentence claims *and* atoms as separate stored layers) was
rejected as doubling storage, embeddings and pair volume to preserve what
the group tag and the passage already preserve.

## What this does not license

- Splitting a conditional, purpose clause, or comparison into free-standing
  claims. That invents assertions the text never made; the pilot's worst
  single error was dropping a condition in exactly this way.
- Treating a group tag as permission to skip the passage at adjudication.
  The group reconstructs the *sentence*; only the passage supplies speaker,
  antecedents and rhetorical force.
- Deciding borderline linkage by extraction-time certainty. When in doubt,
  split and group; the question of whether the linkage is material belongs
  to adjudication and human review, where the whole text is in view.

## Measurement

Atomicity is a scored dimension of every extraction audit, alongside
faithfulness, speaker and modality errors. Prompt v2 carries the three
adjudicated examples (keep-whole, split-list, split-and-group) as worked
cases. Before any full extraction run, a re-run of the M2 pilot passages
must show the bundling rate down from the pilot's ~20/60 audited claims to
low single digits, and the operator's human audit must confirm the
material-error rate the machine pre-audit estimated.
