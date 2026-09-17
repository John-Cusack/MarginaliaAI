# Setting up the Bible corpus

`make db && make migrate` gives you an empty schema at head (`017`). Everything
below fills it. None of it runs automatically, none of it is idempotent by
accident, and the order is not arbitrary — later steps validate against rows
earlier steps write, and refuse rather than guess when those rows are absent.

This exists because the sequence was previously recorded only inside each
script's own docstring, which you can only read once you already know the script
is there.

## Before you start

The Westminster Leningrad Codex is ingested from a morphhb checkout, and the
edition metadata records whichever commit is present:

```bash
git clone https://github.com/openscriptures/morphhb /tmp/morphhb
git -C /tmp/morphhb checkout 3d15126f
```

**`3d15126f` is the provenance recorded in every WLC document in this corpus.**
Ingesting a different revision is not wrong, but it is a different edition, and
nothing downstream will tell you that it changed — so pin it deliberately.

The Lexham Hebrew Bible and the ESV do not come from this repository. They are
ingested through the Logos pack, which needs its own authentication; step 5
below only attaches structure to chapters that are already present.

## The sequence

Every script takes `--dry-run`. Run it first — each one prints what it would
write and touches nothing, and that is the cheapest way to find a missing
prerequisite.

| # | Command | Writes | This corpus |
|---|---------|--------|-------------|
| 1 | `uv run python scripts/ingest_wlc.py` | WLC chapters, texts, passages, embeddings, the `bibliography.editions` row | 929 documents, 4,772 passages |
| 2 | `uv run python scripts/backfill_locators.py` | Verse locators on the WLC passages | 4,772 of 4,772 |
| 3 | `uv run python scripts/backfill_nodes.py` | A chapter node and its verse nodes for WLC | 23,213 verse nodes |
| 4 | `uv run python scripts/backfill_words.py` | `core.words` — Strong's numbers and morphology | 305,517 words |
| 5 | `uv run python scripts/backfill_editions.py LHB`<br>`uv run python scripts/backfill_editions.py ESV` | Locators and verse nodes for the other two editions | 23,213 LHB, 31,083 ESV |
| 6 | `uv run python scripts/load_versification.py` | `core.editions_versification`, `core.edition_books`, `core.verse_map` | 3 editions, 144 books, 1,978 mappings |

Step 1 must precede 2, 3 and 4: all three address spans against the canonical
text the ingest stored. Steps 2, 3 and 4 are independent of each other.

**Step 6 must come last, and this is the ordering that is easy to get wrong.**
`load_versification.py` validates its map verse-by-verse against the verse nodes
in all three editions, so it needs step 3 *and* step 5 to have run. Without them
it reports every mapping as missing and writes nothing — which is the correct
behaviour, but reads like a broken map rather than a missing prerequisite.

## What each step refuses to do

These are guards, not error handling. A failure here means the script declined
to write, and the corpus is exactly as it was.

- **Step 1** hashes each chapter's full text and skips a `(content_hash, source)`
  it already holds, before embedding. Re-running costs one query per chapter.
- **Step 4** writes a chapter only if the text it renders is byte-identical
  (md5) to the text the corpus stores, every word's span quotes that word
  exactly, and no stretch of text left unclaimed by any word contains a Hebrew
  letter. The third is the one that answers *do I have all the words* — a count
  can agree with a mistake, a gap cannot.
- **Step 5** takes verse spans from `bible_layout`, which reads them back out of
  the stored text under a coverage guard. A chapter whose parse leaves text
  unaccounted for is skipped and named, never written from a guess.
- **Step 6** runs every check on a real load, not only under `--validate-only`,
  and one failure refuses the whole load rather than leaving a half-populated
  map.

## Checking it worked

```sql
SELECT count(*) FROM core.words;                    -- 305,517
SELECT count(*) FROM core.verse_map;                -- 1,978
SELECT count(*) FROM core.edition_books;            -- 144
SELECT count(*) FROM core.document_nodes WHERE node_type = 'verse';
```

Then the end-to-end check, which exercises the whole chain at once: call
`find_lemma(strong="4941")` from an MCP client. It should return **422**
occurrences across **31** books, each carrying a verse reference and an
`english` block. If that block reports `"mapping": "unmapped"` with a null
`ref`, step 6 has not run: the tool is telling you it has no map rather than
assuming the two traditions agree. The Hebrew references are unaffected and
remain citable.

`tests/integration/test_versification.py` and `test_find_lemma.py` assert these
counts against a loaded corpus, so a green integration run is itself a check
that the sequence completed.
