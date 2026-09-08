# Decision 001 — `core.words` stays in core, as a token index

**Status:** accepted, 2026-09-08
**Applies to:** `core.words` (migration `014_words`), and any future table that
indexes a document below the passage.

## The question

`core.words` landed on 2026-09-08 holding 305,517 rows of Strong's numbers and
morphology for the Westminster Leningrad Codex. Every row is `language = 'he'`.
Three of its columns — `from_qere`, `homograph`, `prefixes` — are named after
Masoretic and OSHB conventions, and the only thing that writes it,
`scripts/backfill_words.py`, hardcodes `LANGUAGE = "he"`.

The pack rule in `corpus-engine-docs/docs/07-pack-system.md` asks whether a
librarian building a digital library of family recipes would want the thing;
that document's own table routes *"OSIS biblical text ingestion"* and *"entity
type verse"* to a biblical pack, and closes with *"when in doubt, put it in a
pack."* `01-vision.md` principle 5 says core *"knows nothing about letters,
verses, genes, or case citations."* On the face of it the table is misplaced.

The question was raised while `core.words` still had **zero consumers** — grep
returned only `014_words.py`, `schema.py` and `backfill_words.py` — which is the
cheapest moment relocation will ever have. That is why it was settled before
`find_lemma` was written rather than after.

## The decision

The table stays in `core`, and the boundary is drawn one level lower than the
question assumed: **core owns tokens, the pack owns what the tokens mean.**

| Core owns | A domain pack owns |
|---|---|
| `document_id`, `position`, `char_start`, `char_end` | the OSHB parser that fills the rows |
| `surface`, `lemma`, `strong`, `morph`, `language`, `ref` | what `HNcmsa` parses to |
| the FK to `core.documents` and its `ON DELETE CASCADE` | what `k/` and `b/` mean as idiom |
| the span invariant, and the guard that proves it | which Strong's numbers are worth surveying |

Read that way the structure is not biblical at all. *"One row per word of a
document's canonical text, addressed by character span, carrying a lemma, a
morphological code and a language"* is what any lemmatised search wants —
Latin, Koine, Old English, or an English lemmatiser over a letter collection.
It is the same address shape `core.passages` and `core.document_nodes` already
use, one level finer. The domain-specific part of `core.words` is its *values*
and three nullable columns, not its shape.

## Why not the pack

Three things make "move it to a pack" more expensive here than the general rule
suggests, and none of them is churn-aversion.

1. **No in-repo pack owns a table.** `packages/plugins/history` — the only pack
   in this repository — owns none. The precedents cited for pack-owned schema,
   `logos` (7 tables) and `acad` (6), are both *out-of-repo* packs that open
   their own asyncpg pool on `RE_DB_URL` and manage their own DDL outside
   Alembic. Neither has a foreign key into a core table.

2. **The FK is the point.** `core.words.document_id` references
   `core.documents(id) ON DELETE CASCADE`, and the rows address
   `core.document_texts` by character offset. A pack table cannot hold that
   relationship under either existing precedent: outside Alembic, core's
   migrations cannot see it, and `test_schema_truthfulness` — which compares
   declared tables against actual ones — would stop covering 305,517 offsets
   that only a span guard protects.

3. **It would require a core change first.** `requires.plugins` is parsed in
   `plugins/manifest.py` and read by nothing, so a pack that depends on another
   pack's contribution fails at use time with an unhelpful error rather than at
   install time. Changing core to enable an optional relocation is the tail
   wagging the dog. (This was already the decisive argument against splitting a
   `scripture` pack out of `logos` on 2026-09-07; the same argument applies
   here, and the same fix — see below — retires it in both places.)

## What this does *not* license

Core does not gain biblical knowledge. Specifically:

- **No versification logic in the token layer.** Verse mapping is data, in
  `core.editions_versification` and `core.verse_map` (migration `016`), loaded
  from a source file — not rules in code.
- **`ref` is an opaque string to core.** It is written by whatever ingest
  produced the document and read back verbatim. Core does not parse it, does not
  know that `Ps.36.7` has three parts, and does not know which scheme it is in
  without consulting `editions_versification`.
- **The next non-Hebrew edition settles the column question.** `from_qere`,
  `homograph` and `prefixes` are nullable and currently only Hebrew uses them.
  If a second language arrives and needs different ones, they become a `jsonb`
  `analysis` column rather than accumulating a column per tradition. Not worth
  doing on one consumer.

## Fixed alongside, because both were wrong either way

- **`strong` had no language discriminator.** `words_strong_idx` was on `strong`
  alone, and the column stores bare digits. Loading any Greek would collide
  G4941 with H4941, recoverable only by remembering a filter nothing enforced.
  Migration `015` adds `words_language_strong_idx` on `(language, strong)`, and
  `find_lemma` requires a language (defaulting to `he`) rather than trusting the
  caller to filter.
- **`requires.plugins` was parsed and unread.** `PluginLoader` now checks it
  against the set of enabled plugins and refuses to load a pack whose declared
  pack dependencies are missing, the same way it already refuses on `core_api`
  and `pip`. This is the enforcement a future pack split needs, landed
  independently of whether that split ever happens.

## When to revisit

Revisit if a second language is loaded into `core.words` and cannot be described
by the existing columns, or if a pack ever needs to write the table without core
having a migration for it. Neither is true today.
