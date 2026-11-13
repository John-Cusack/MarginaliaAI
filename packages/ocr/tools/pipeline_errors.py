"""A seeded set built from the readers' own mistakes, not from a seeder's.

The planted set asks an auditor to find errors somebody invented. The errors
that actually escape the pipeline are a different population: the confirm rule
stores a span when all three readers *agree*, so an error survives precisely when
every reader made it — which means a reader can never audit for it (its own
mistake is its own blind spot), and a planted-error sensitivity flatters any
auditor built from a reader.

This module builds the honest set. Each reader's words are aligned against the
truth, and a word is an *agreed error* when at least ``minimum`` readers replaced
it with the same wrong text (or dropped it the same way). The candidate text is
then the truth with exactly those agreed errors in it, so an auditor is measured
against the errors that a three-reader confirm step would have let through.

Nothing here needs the readers' code: it takes their readings as text.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from ocr_synth.seeded_errors import PlantedError, SeededText, _Emitter, _Word, words_of

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class AgreedError:
    """One word the readers collectively got wrong, with who agreed on what."""

    word_index: int
    truth_text: str
    agreed_text: str
    readers: tuple[str, ...]
    kind: str


def _reader_answers(truth_words: list[_Word], reading: str) -> dict[int, str]:
    """Per truth word: what this reader said there, for words it got wrong.

    A missing entry means the reader produced the same word; ``""`` means it
    dropped the word; anything else is its replacement.
    """
    reading_words = [word.text for word in words_of(reading)]
    truth_tokens = [word.text for word in truth_words]
    matcher = SequenceMatcher(None, truth_tokens, reading_words, autojunk=False)
    wrong: dict[int, str] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            for index in range(i1, i2):
                wrong[index] = ""
            continue
        if tag == "insert":
            continue
        paired = min(i2 - i1, j2 - j1)
        for offset in range(paired):
            if truth_tokens[i1 + offset] != reading_words[j1 + offset]:
                wrong[i1 + offset] = reading_words[j1 + offset]
        for offset in range(paired, i2 - i1):
            wrong[i1 + offset] = ""
    return wrong


def agreed_errors(
    truth: str,
    readings: Mapping[str, str],
    *,
    minimum: int | None = None,
) -> tuple[AgreedError, ...]:
    """Words at least ``minimum`` readers got wrong in the same way.

    ``minimum`` defaults to every reader there is: the pipeline stores a span
    only when all three agree, so a disagreement is caught and marked, not
    stored.
    """
    truth_words = words_of(truth)
    if not readings:
        return ()
    if minimum is None:
        minimum = len(readings)
    answers = {name: _reader_answers(truth_words, reading) for name, reading in readings.items()}
    agreed: list[AgreedError] = []
    for index, word in enumerate(truth_words):
        said: dict[str, tuple[str, ...]] = {}
        for name, wrong in answers.items():
            if index in wrong:
                said[wrong[index]] = (*said.get(wrong[index], ()), name)
        winners = [(text, names) for text, names in said.items() if len(names) >= minimum]
        if not winners:
            continue
        text, names = max(winners, key=lambda item: len(item[1]))
        if text == "":
            kind = "agreed_omission"
        elif text.casefold() == word.text.casefold():
            # Every reader answered the same word in the other case. It is a real
            # error against the canonical text and its own class: the case policy
            # is an operator decision, so it is counted and kept out of s_L.
            kind = "agreed_case"
        else:
            kind = "agreed_substitution"
        agreed.append(
            AgreedError(
                word_index=index,
                truth_text=word.text,
                agreed_text=text,
                readers=tuple(sorted(names)),
                kind=kind,
            )
        )
    return tuple(agreed)


def build_pipeline_set(
    truth: str,
    readings: Mapping[str, str],
    *,
    minimum: int | None = None,
    skip: Callable[[AgreedError], bool] | None = None,
) -> tuple[SeededText, tuple[AgreedError, ...]]:
    """The truth with the readers' agreed mistakes in it, as a seeded set.

    Built with the same forward emitter the planted sets use, so undoing every
    recorded span reproduces the page — the property that makes a seeded set
    usable for measuring anything at all.

    ``skip`` decides which agreed errors are *planted*: an error the independent
    checks would have marked anyway is not part of the population an auditor is
    measured on, and a skipped error is left as the truth so the candidate text
    is what the pipeline would actually have stored — the marked spans repaired,
    the unmarked ones wrong.
    """
    errors = agreed_errors(truth, readings, minimum=minimum)
    if skip is not None:
        errors = tuple(error for error in errors if not skip(error))
    truth_words = words_of(truth)
    emitter = _Emitter(truth)
    recorded: list[PlantedError] = []
    by_index = {error.word_index: error for error in errors}
    for index, word in enumerate(truth_words):
        error = by_index.get(index)
        if error is None:
            emitter.emit_token(word, word.text)
            continue
        start, end = emitter.emit_token(word, error.agreed_text)
        recorded.append(
            PlantedError(
                error.kind,
                start,
                end,
                error.agreed_text,
                word.start,
                word.end,
                word.text,
                f"readers agreed on {error.agreed_text!r} for {word.text!r} ({','.join(error.readers)})",
            )
        )
    return SeededText(truth=truth, final=emitter.text(), errors=tuple(recorded)), errors
