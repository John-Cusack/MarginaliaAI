"""The script-aware token estimate, checked against what it is meant to prevent.

The number these functions return decides where a chunker cuts, and a cut in
the wrong place produces a passage the embedding model truncates: stored,
counted, and unreachable by search. So the assertions here are mostly about
direction — never claim a dense passage is smaller than it is — rather than
about hitting a particular figure.
"""

from __future__ import annotations

import pytest

from research_engine.services.text.tokens import (
    DEFAULT_CHARS_PER_TOKEN,
    approx_tokens,
    chars_per_token,
    min_chars_per_token,
    token_budget_chars,
)

ENGLISH = "The archive holds letters. Each letter carries a date. " * 20
GREEK = "λόγος πρὸς τὸν θεόν καὶ θεὸς ἦν ὁ λόγος οὗτος ἦν ἐν ἀρχῇ " * 20
HEBREW = "בְּרֵאשִׁית בָּרָא אֱלֹהִים אֵת הַשָּׁמַיִם וְאֵת הָאָרֶץ " * 20
CJK = "第一句話寫在這裡第二句話也寫在這裡內容比較長一些" * 20


def test_ascii_is_unchanged_so_latin_documents_do_not_re_chunk() -> None:
    """The old constant, preserved exactly.

    Every Latin-script document in the corpus was chunked against 4.0. Moving it
    would restate every one of them as stale and trigger a corpus-wide re-chunk
    to fix a ~10% error that is not causing harm.
    """
    assert chars_per_token(ENGLISH) == DEFAULT_CHARS_PER_TOKEN
    assert approx_tokens("x" * 4000) == 1000


@pytest.mark.parametrize("text", [GREEK, HEBREW, CJK], ids=["greek", "hebrew", "cjk"])
def test_dense_scripts_are_counted_as_costing_more_tokens(text: str) -> None:
    """The bug itself: these scripts were counted as if they were English."""
    assert chars_per_token(text) < DEFAULT_CHARS_PER_TOKEN
    assert approx_tokens(text) > len(text) // 4


def test_greek_is_estimated_at_roughly_twice_the_old_count() -> None:
    """Measured at 1.83 chars/token on BDAG; 4.0 was the old assumption."""
    assert 1.5 <= approx_tokens(GREEK) / (len(GREEK) / 4) <= 3.0


def test_a_mixed_document_lands_between_its_scripts() -> None:
    """A lexicon is Greek headwords in an English apparatus, and reads as both."""
    mixed = ENGLISH + GREEK
    assert chars_per_token(GREEK) < chars_per_token(mixed) < DEFAULT_CHARS_PER_TOKEN


def test_the_densest_script_present_bounds_the_absolute_ceiling() -> None:
    """Why `min_chars_per_token` exists.

    An average lets a run of unbroken Greek inside a mostly-English document
    exceed a ceiling budgeted on that average. The ceiling admits no exemption,
    so it is budgeted on the worst script present instead.
    """
    mixed = ENGLISH * 10 + GREEK
    assert min_chars_per_token(mixed) < chars_per_token(mixed)
    assert min_chars_per_token(mixed) == pytest.approx(chars_per_token(GREEK), abs=0.6)


def test_a_token_budget_buys_fewer_characters_of_a_dense_script() -> None:
    assert token_budget_chars(500, chars_per_token(ENGLISH)) == 2000
    assert token_budget_chars(500, chars_per_token(CJK)) < 1000


def test_sampling_a_long_text_agrees_with_measuring_all_of_it() -> None:
    """Long documents are sampled by stride; the answer must not move much."""
    short, long = GREEK, GREEK * 400
    assert len(long) > 60_000
    assert chars_per_token(long) == pytest.approx(chars_per_token(short), rel=0.1)


def test_sampling_is_deterministic() -> None:
    """The chunker contract requires identical output across runs."""
    long = (ENGLISH + GREEK) * 400
    assert chars_per_token(long) == chars_per_token(long)


def test_an_unknown_script_is_assumed_dense_rather_than_sparse() -> None:
    """Guessing high costs a smaller passage; guessing low costs a truncated one."""
    # Devanagari, which has no entry of its own.
    assert chars_per_token("पुरालेख में पत्र हैं। " * 20) < DEFAULT_CHARS_PER_TOKEN


def test_empty_and_tiny_inputs_do_not_divide_by_zero() -> None:
    assert chars_per_token("") == DEFAULT_CHARS_PER_TOKEN
    assert min_chars_per_token("") == DEFAULT_CHARS_PER_TOKEN
    assert approx_tokens("") == 1
    assert approx_tokens("a") == 1
