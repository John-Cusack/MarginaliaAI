"""Splitting a collected volume of letters into letters.

A volume of correspondence — Bence Jones's *Life and Letters of Faraday*, the
McClellan papers — is ingested as one document, but the unit of evidence is the
letter: each has its own date, place, writer and recipient. This package holds
the parts of `history.structure_letters` that need no I/O, so each rule can be
tested against real datelines:

- `openings` places each `letter_opening` record on the volume's text and
  merges the duplicates that overlapping passages produce;
- `units` cuts the volume into letters and describes each one;
- `dating` decides each letter's date, or why it is being held for review.

The governing rule is the engine's: a wrong date on a timeline is worse than a
gap, because a gap is visible. Anything doubtful is held, never guessed.
"""
