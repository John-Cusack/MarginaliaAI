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

from __future__ import annotations

#: The English-calibrated rate, kept exactly as it was so that Latin-script
#: documents chunk identically to before this module existed.
DEFAULT_CHARS_PER_TOKEN = 4.0

#: (first, last, chars_per_token), inclusive codepoint ranges.
_SCRIPT_RANGES: tuple[tuple[int, int, float], ...] = (
    # Greek and Coptic, then Greek Extended (polytonic — the diacritics cost
    # tokens, which is most of why the rate is this low).
    (0x0370, 0x03FF, 1.68),
    (0x1F00, 0x1FFF, 1.68),
    # Hebrew, including pointing, and the presentation forms HALOT uses.
    (0x0590, 0x05FF, 1.40),
    (0xFB1D, 0xFB4F, 1.40),
    # Cyrillic tokenizes at least as well as Latin; held at the default rather
    # than its measured 4.21 so the estimate never runs optimistic.
    (0x0400, 0x04FF, DEFAULT_CHARS_PER_TOKEN),
    (0x0600, 0x06FF, 3.40),
    (0x0750, 0x077F, 3.40),
    # CJK: Han, kana, Hangul, and compatibility ideographs.
    (0x3040, 0x30FF, 1.50),
    (0x3400, 0x4DBF, 1.50),
    (0x4E00, 0x9FFF, 1.50),
    (0xAC00, 0xD7AF, 1.50),
    (0xF900, 0xFAFF, 1.50),
    # Latin Extended: accented Latin, transliteration marks.
    (0x0100, 0x024F, 3.00),
    (0x1E00, 0x1EFF, 3.00),
    # Combining diacritics, attached to whatever precedes them.
    (0x0300, 0x036F, 2.00),
)

#: Any other non-ASCII character. Deliberately pessimistic: an unrecognised
#: script is more likely to tokenize densely than sparsely, and the cost of
#: guessing high is a slightly smaller passage.
_UNKNOWN_CHARS_PER_TOKEN = 2.0

#: Scanning every character of a book to estimate a ratio is wasted work. A
#: fixed stride is deterministic, which the chunker contract requires.
_SAMPLE_CEILING = 60_000

#: Codepoint -> tokens-per-character. Small in practice: a document draws on a
#: few hundred distinct codepoints, so this converges after the first lines.
_rate_cache: dict[int, float] = {}


def _tokens_per_char(codepoint: int) -> float:
    if (cached := _rate_cache.get(codepoint)) is not None:
        return cached
    rate = _UNKNOWN_CHARS_PER_TOKEN
    if codepoint < 0x80:
        rate = DEFAULT_CHARS_PER_TOKEN
    else:
        for first, last, script_rate in _SCRIPT_RANGES:
            if first <= codepoint <= last:
                rate = script_rate
                break
    value = 1.0 / rate
    _rate_cache[codepoint] = value
    return value


def chars_per_token(text: str) -> float:
    """Average characters per token for *text*'s mix of scripts.

    Compute this once per document and hand it to :func:`approx_tokens` for each
    span. Doing it per span is correct but turns an O(1) estimate into an O(n)
    one inside the chunkers' innermost loop.
    """
    if not text:
        return DEFAULT_CHARS_PER_TOKEN
    # The overwhelmingly common case, and the one that must stay exact.
    if text.isascii():
        return DEFAULT_CHARS_PER_TOKEN

    sample = text
    if len(text) > _SAMPLE_CEILING:
        sample = text[:: (len(text) // _SAMPLE_CEILING) + 1]

    tokens = 0.0
    for char in sample:
        tokens += _tokens_per_char(ord(char))
    if tokens <= 0:
        return DEFAULT_CHARS_PER_TOKEN
    return len(sample) / tokens


def min_chars_per_token(text: str) -> float:
    """The rate of the *densest* script present, not the average.

    :func:`chars_per_token` describes a document as a whole, which is what a
    window budget wants. An absolute ceiling wants something stronger: a
    guarantee for every passage, including one that happens to be denser than
    the document it came from. A Greek lexicon is largely ASCII by character
    count — abbreviations, references, punctuation — so its average rate is far
    higher than a run of unbroken Greek inside it, and budgeting the ceiling
    against the average lets exactly that run cross the line.
    """
    if not text:
        return DEFAULT_CHARS_PER_TOKEN
    if text.isascii():
        return DEFAULT_CHARS_PER_TOKEN

    sample = text
    if len(text) > _SAMPLE_CEILING:
        sample = text[:: (len(text) // _SAMPLE_CEILING) + 1]

    # 1 / tokens-per-char is chars-per-token; the largest tokens-per-char is the
    # densest script, so the smallest chars-per-token.
    return 1.0 / max(_tokens_per_char(ord(char)) for char in sample)


def approx_tokens(text: str, rate: float | None = None) -> int:
    """Estimated token count for *text*.

    *rate* is a ``chars_per_token`` result for the enclosing document. Passing
    it keeps this O(1); omitting it measures *text* itself.
    """
    if not text:
        return 1
    if rate is None:
        rate = chars_per_token(text)
    return max(1, int(len(text) / rate))


def token_budget_chars(max_tokens: int, rate: float) -> int:
    """How many characters *max_tokens* buys at this script mix.

    Chunkers cut on character offsets but budget in tokens, and that conversion
    is exactly where the old constant was wrong: 500 tokens of Greek is about
    840 characters, not 2,000.
    """
    return max(1, int(max_tokens * rate))
