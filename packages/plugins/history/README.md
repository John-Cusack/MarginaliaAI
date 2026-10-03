# Research Engine History Plugin

The reference external-plugin implementation for Research Engine. It contributes
historical correspondence types, epistolary, claim and letter-opening extraction schemas, and
three MCP tools: `history.structure_letters`, `history.find_missing_letters` and
`history.correspondence_cadence`.

## Splitting a volume of letters

A collected edition is ingested as one document, but each letter in it has its own date,
place, writer and recipient. `history.structure_letters` splits it:

1. Mark the volume a `letter_collection` and give it its settings —
   `mode: configure` with `default_sender`, `alias_map` (kinship names that mean one person
   in this book only), `excluded_ranges` (catalogue, index) and `chronology_tolerance_days`.
2. Run the `letter_openings:1` schema over the volume's passages, leaving out the excluded
   ranges (`research-engine extraction run letter_openings:1 --document-id … --exclude-range …
   --model claude-sonnet-5`).
3. Run `history.structure_letters` as a dry run and read what it would hold, then again with
   `dry_run: false`. Each letter becomes a `letter` document with its own date and a
   `letter_sent` event whose actors carry direction. The volume stays for reading order and is
   left out of default search.
4. Work through `mode: review_queue` and date held letters with `accept`.

A letter is held, never guessed, when its readers disagree, its year is not printed, a
yearless date is not bracketed by dated neighbours, or its date breaks the order its
neighbours set. The pack needs the `ingest` and `write` permissions for this.

## Install and approve

```bash
python -m pip install marginalia-ai-plugin-history
research-engine plugin audit history
research-engine plugin enable history
```

Installation only makes the static manifest discoverable. Core does not import the package
until the operator approves the exact distribution version, manifest hash, contributions, and
permissions. The plugin runs in-process after approval and must be trusted.

This distribution depends only on `marginalia-ai-sdk>=0.6,<0.7`. Integration environments
install the matching `marginalia-ai` artifact separately.

See the [repository](https://github.com/John-Cusack/MarginaliaAI),
[changelog](https://github.com/John-Cusack/MarginaliaAI/blob/main/CHANGELOG.md), and
[issues](https://github.com/John-Cusack/MarginaliaAI/issues). Licensed under Apache-2.0.
