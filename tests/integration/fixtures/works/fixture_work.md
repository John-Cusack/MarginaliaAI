---
# Template for the Step 2 integration tests, not a work itself: every
# `__DOCUMENT_ID__` is replaced with the ingested fixture document's id before
# the file is written into a tmp works dir. Spans mirror
# lexicon_fixture.json; if that text changes, recompute them there first.
work: W-001
title: "A dabaris fragment"
type: essay
status: draft
created: 2026-09-04
claims: [TEST-001]
citations:
  - id: c1
    document_id: __DOCUMENT_ID__
    char_start: 34
    char_end: 62
    quoted_text: "The prophets pair two words."
    intent: quotation
    edition_key: DABAR_2026
    locator: {page: 1}
  - id: c2
    document_id: __DOCUMENT_ID__
    char_start: 63
    char_end: 163
    quoted_text: 'He requires "justice and righteousness" of every ruler - a phrase the translations render unevenly'
    intent: quotation
    edition_key: DABAR_2026
    locator: {page: 1}
  - id: c3
    document_id: __DOCUMENT_ID__
    char_start: 0
    char_end: 130
    quoted_text: |
      Dabar is “word” — matter, thing.

      The prophets pair two words. He requires “justice and righteousness” of every ruler — a phrase t
    intent: background
    edition_key: DABAR_2026
  - id: c4
    document_id: __DOCUMENT_ID__
    char_start: 0
    char_end: 130
    quoted_text: |
      Dabar is “word” — matter, thing.

      The prophets pair two words. He requires “justice and righteousness” of every ruler — a phrase t
    intent: quotation
    edition_key: DABAR_2026
  - id: c5
    document_id: __DOCUMENT_ID__
    char_start: 63
    char_end: 117
    quoted_text: 'He requires "justice and righteousness" of every king, and the moon besides'
    intent: quotation
    edition_key: DABAR_2026
  - id: c6
    document_id: __DOCUMENT_ID__
    char_start: 34
    char_end: 62
    quoted_text: "A marginal gloss on the ninth hour never entered here."
    intent: quotation
    edition_key: DABAR_2026
  - id: c7
    document_id: __DOCUMENT_ID__
    char_start: 103
    char_end: 152
    quoted_text: "of every ruler — a phrase the translations render"
    intent: quotation
    edition_key: DABAR_2026
    locator: {page: 1}
---

## Notes

Reading [^c1] closely, then [^c2] for context. The region [^c3] frames it,
though [^c4] overclaims it. Against [^c5] and [^c6], which fail loudly, the
straddler [^c7] holds.
