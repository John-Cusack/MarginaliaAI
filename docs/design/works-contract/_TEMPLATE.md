---
work: W-001
title: "<title>"
type: translation            # translation | essay | dossier | script | outline
status: draft                # draft | review | published
created: 2026-09-04
claims: []                   # claim refs (JUB-004 style) — inert until migration 009, grep-able now
citations: []
  # - id: c1
  #   document_id: <uuid>          # core.documents.id — required
  #   char_start: 0                # offset into document_texts.text — required
  #   char_end: 0                  # > char_start — required
  #   quoted_text: "<exact quote>" # must verify via verify_quote
  #   intent: quotation            # required — quotation | translation | support | contrast
  #                                #   | background | definition | source | see_also
  #   role: supports               # optional — only when this also backs a claim ref:
  #                                #   asserts | supports | rebuts | context
  #   edition: "3rd"               # optional until P3 bibliographic records land
  #   zotero_key: <ZOTERO_KEY>     # fill from day one; the join once P3 lands
  #   locator: {page: 0}           # page / verse / location the source provides
---

<!-- Body. Reference a citation in prose as [^c1]. Footnotes render the citation;
     the machine-readable copy is the front-matter entry, and that one is
     authoritative — keep them in sync by writing prose first, then anchoring. -->

## Notes

<!-- Reading notes, open questions, things to verify. Not citations. -->