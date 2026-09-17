"""Token estimation that knows what script it is looking at.

Every chunker used to estimate tokens as ``len(text) // 4``. That constant is
calibrated on English, and it is the only thing standing between a chunker and
a passage the embedding model silently truncates — so where the constant is
wrong, the guard is wrong by the same factor.

Measured against bge-m3's own tokenizer on this corpus, it is wrong by a lot:

===========  =============  ==================================================
script       chars/token    measured on
===========  =============  ==================================================
ASCII                 3.60  600 corpus passages
Greek                 1.83  Greek runs from 600 BDAG passages
Hebrew                1.50  Hebrew runs from 600 corpus passages
CJK                   1.53  synthetic — no CJK in the corpus yet
Cyrillic              4.21  synthetic — no Cyrillic in the corpus yet
Arabic                3.46  synthetic — no Arabic in the corpus yet
===========  =============  ==================================================

So a "2,000 token" cap really admitted ~4,100 tokens of Greek and ~6,400 of
CJK. Nothing in the corpus crossed 8,192 — the largest BDAG passage measured
2,831 real tokens — but the margin was a third of what it appeared to be, and
for denser CJK the cap would not have held at all.

The rates below are the 10th percentile of what was measured, not the median:
this estimate exists to stop a passage being too big, so erring toward
*over*-counting tokens is the safe direction.

ASCII deliberately stays at exactly 4.0 even though 3.60 was measured. The
estimate feeds chunk-boundary decisions, so changing it re-chunks every
Latin-script document in the corpus — and at 4.0 the error is ~10%, far inside
the tolerance the contract already allows. The bug being fixed here is the
non-Latin one; widening the blast radius to 88% of the corpus to shave 10% off
an estimate that is not failing is not a trade worth making.
"""

from research_engine_sdk.chunking import (
    DEFAULT_CHARS_PER_TOKEN,
    approx_tokens,
    chars_per_token,
    min_chars_per_token,
    token_budget_chars,
)

__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "approx_tokens",
    "chars_per_token",
    "min_chars_per_token",
    "token_budget_chars",
]
