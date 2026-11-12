"""Plant known errors in a page's text, and keep the ground truth.

The audit's release rule (§9.4) rests on one measured number: how often the
audit reader reports an error that is really there. That number cannot be
measured on pages whose errors nobody knows, so errors are planted here —
real-word substitutions, digit changes, omissions, insertions, punctuation and
word order — into text that is otherwise the page's own, and each one is
recorded with the character span it occupies in the *seeded* text.

The seeded text is built by emitting the page forward, word by word, and
recording each planted error's span as it is emitted. That is the only reason
the ground truth can be trusted: spans are measured where the text was written,
not reconstructed afterwards, and :func:`revert` — which inverts the plantings
in reverse emission order — must reproduce the page exactly.

The seeds and the counts are predeclared constants. They are the sensitivity
set: tuning them after a run invalidates the bound, exactly as extending an
audit sample after reading it does.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Predeclared: seeds for the sensitivity set. Never adjusted against results.
SEEDED_SEEDS: tuple[int, ...] = (20261111, 20261112, 20261113, 20261114)

# Predeclared: how many of each planted class a seeded page carries.
SEEDED_COUNTS: dict[str, int] = {
    "real_word": 3,
    "digit": 2,
    "omission": 2,
    "insertion": 2,
    "punctuation": 2,
    "order": 1,
}

# Real-word pairs a reader (and a spell-checker) cannot separate. Both members of
# each pair are the same length, so a planted substitution keeps every other
# offset on the page intact.
REAL_WORD_PAIRS: tuple[tuple[str, str], ...] = (
    ("from", "form"),
    ("then", "than"),
    ("affect", "effect"),
    ("causal", "casual"),
    ("principal", "principle"),
    ("was", "saw"),
    ("not", "nor"),
    ("has", "had"),
    ("one", "our"),
    ("their", "there"),
    ("where", "were"),
    ("that", "than"),
)

INSERTABLE_WORDS: tuple[str, ...] = ("the", "of", "and", "that")
PUNCTUATION_SWAPS: tuple[tuple[str, str], ...] = ((",", ";"), (";", ","), (".", ","))


class NoMaterial(RuntimeError):
    """The page cannot carry the requested planted errors; the set is malformed."""


@dataclass(frozen=True)
class PlantedError:
    """One planted error, with the span it occupies in the seeded text."""

    kind: str
    final_start: int
    final_end: int
    final_text: str
    truth_start: int
    truth_end: int
    truth_text: str
    note: str = ""

    @property
    def final_span(self) -> tuple[int, int]:
        return (self.final_start, self.final_end)


@dataclass(frozen=True)
class SeededText:
    """A page whose text has planted errors, with the truth kept beside it."""

    truth: str
    final: str
    errors: tuple[PlantedError, ...]

    @property
    def planted(self) -> int:
        return len(self.errors)

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for error in self.errors:
            counts[error.kind] = counts.get(error.kind, 0) + 1
        return counts


@dataclass(frozen=True)
class _Word:
    text: str
    start: int
    end: int


def words_of(text: str) -> list[_Word]:
    """Words with their spans; separators are the single spaces between them."""
    out: list[_Word] = []
    index = 0
    for raw in text.split(" "):
        start = index
        end = start + len(raw)
        index = end + 1
        if raw:
            out.append(_Word(text=raw, start=start, end=end))
    return out


class _Emitter:
    """Builds the seeded text and reports where each chunk landed.

    The page's own characters are emitted verbatim: the separator between two
    tokens is copied from the truth rather than rebuilt as one space. That
    matters for a page with any run of two spaces in it — a Markdown table's
    empty cell, for instance — where rebuilding would quietly change the page
    and the reversion check would (rightly) refuse the set.
    """

    def __init__(self, truth: str) -> None:
        self.truth = truth
        self.parts: list[str] = []
        self.length = 0
        self.cursor = 0

    def text(self) -> str:
        return "".join(self.parts)

    def _add(self, chunk: str) -> int:
        start = self.length
        self.parts.append(chunk)
        self.length += len(chunk)
        return start

    def separator(self, token_start: int) -> str:
        """The truth's own characters between the cursor and this token."""
        return self.truth[self.cursor : token_start]

    def emit_token(self, token: _Word, chunk: str, *, keep_cursor: bool = False) -> tuple[int, int]:
        """Emit a token's separator then ``chunk``; return the chunk's span."""
        self._add(self.separator(token.start))
        start = self._add(chunk)
        if not keep_cursor:
            self.cursor = token.end
        else:
            self.cursor = token.start
        return start, start + len(chunk)


def _fits(kind: str, words: list[_Word], index: int) -> bool:
    word = words[index]
    if kind == "order":
        return index + 1 < len(words)
    if kind == "real_word":
        return any(word.text == left for left, _ in REAL_WORD_PAIRS)
    if kind == "digit":
        return any(char.isdigit() for char in word.text)
    if kind == "punctuation":
        return any(char in ",;." for char in word.text if not char.isdigit())
    if kind in {"omission", "insertion"}:
        return len(word.text) >= 4 and word.text.isalpha()
    raise NoMaterial(f"unknown planted class {kind!r}")


def _pick(kind: str, words: list[_Word], rng: random.Random, used: set[int]) -> int:
    candidates = [
        index
        for index in range(len(words))
        if index not in used and (kind != "order" or index + 1 not in used) and _fits(kind, words, index)
    ]
    if not candidates:
        raise NoMaterial(f"no word left to plant a {kind!r} error")
    return rng.choice(candidates)


def _plan(kind: str, words: list[_Word], index: int, rng: random.Random) -> tuple:
    """The operation for one planted error, before any text is emitted."""
    word = words[index]
    if kind == "order":
        return ("order",)
    if kind == "real_word":
        return ("real_word", next(right for left, right in REAL_WORD_PAIRS if left == word.text))
    if kind == "digit":
        position = next(offset for offset, char in enumerate(word.text) if char.isdigit())
        return ("digit", position, "1" if word.text[position] != "1" else "2")
    if kind == "punctuation":
        for offset, char in enumerate(word.text):
            for left, right in PUNCTUATION_SWAPS:
                if char == left:
                    return ("punctuation", offset, right)
        raise NoMaterial("no punctuation to swap")
    if kind == "omission":
        return ("omission",)
    if kind == "insertion":
        return ("insertion", rng.choice(INSERTABLE_WORDS))
    raise NoMaterial(f"unknown planted class {kind!r}")


def plant(
    truth: str,
    rng: random.Random,
    counts: dict[str, int] | None = None,
) -> SeededText:
    """Plant ``counts`` errors of each class, deterministically for a given rng."""
    wanted = dict(SEEDED_COUNTS if counts is None else counts)
    words = words_of(truth)
    if not words:
        raise NoMaterial("no words to plant errors in")

    plan: dict[int, tuple] = {}
    used: set[int] = set()
    for kind, count in wanted.items():
        for _ in range(count):
            index = _pick(kind, words, rng, used)
            plan[index] = _plan(kind, words, index, rng)
            used.add(index)
            if kind == "order":
                used.add(index + 1)

    emitter = _Emitter(truth)
    errors: list[PlantedError] = []
    consumed: set[int] = set()
    for index, word in enumerate(words):
        if index in consumed:
            continue
        operation = plan.get(index)
        kind = operation[0] if operation else ""

        if kind == "omission":
            separator = emitter.separator(word.start)
            start = emitter.length
            emitter.cursor = word.end
            errors.append(
                PlantedError(
                    "omission",
                    start,
                    start,
                    "",
                    word.start - len(separator),
                    word.end,
                    separator + word.text,
                    f"dropped {word.text!r}",
                )
            )
            continue

        if kind == "order":
            other = words[index + 1]
            consumed.add(index + 1)
            between = truth[word.end : other.start]
            swapped = f"{other.text}{between}{word.text}"
            start, end = emitter.emit_token(word, swapped)
            emitter.cursor = other.end
            errors.append(
                PlantedError(
                    "order",
                    start,
                    end,
                    swapped,
                    word.start,
                    other.end,
                    truth[word.start : other.end],
                    f"swapped {word.text!r}/{other.text!r}",
                )
            )
            continue

        if kind == "insertion":
            invented = operation[1]
            start, end = emitter.emit_token(word, invented, keep_cursor=True)
            errors.append(
                PlantedError(
                    "insertion",
                    start,
                    end,
                    invented,
                    word.start,
                    word.start,
                    "",
                    f"invented {invented!r}",
                )
            )
            emitter.emit_token(word, word.text)
            continue

        if kind == "real_word":
            replacement = operation[1]
            start, end = emitter.emit_token(word, replacement)
            errors.append(
                PlantedError(
                    "real_word",
                    start,
                    end,
                    replacement,
                    word.start,
                    word.end,
                    word.text,
                    f"{replacement} <- {word.text}",
                )
            )
            continue

        if kind in {"digit", "punctuation"}:
            position, replacement = operation[1], operation[2]
            patched = word.text[:position] + replacement + word.text[position + 1 :]
            start, _ = emitter.emit_token(word, patched)
            errors.append(
                PlantedError(
                    kind,
                    start + position,
                    start + position + 1,
                    replacement,
                    word.start + position,
                    word.start + position + 1,
                    word.text[position],
                    f"{word.text!r} -> {patched!r}",
                )
            )
            continue

        emitter.emit_token(word, word.text)

    return SeededText(truth=truth, final=emitter.text(), errors=tuple(errors))


def revert(seeded: SeededText) -> str:
    """Undo every planted error; must reproduce the truth exactly.

    This is what makes the ground truth trustworthy: a planted set whose
    reversal does not reconstruct the page is not a measurement instrument, it
    is a second approximation of one. Errors are inverted in reverse emission
    order, so a deletion and an insertion at the same place cannot cancel
    wrongly.
    """
    text = seeded.final
    for error in reversed(seeded.errors):
        text = text[: error.final_start] + error.truth_text + text[error.final_end :]
    return text


def can_carry(truth: str, counts: dict[str, int] | None = None) -> bool:
    """Can this page carry the predeclared set at all?

    A sparse page has no dictionary words to swap and no numbers to change, so
    it cannot be part of the real-word or digit measurement. The check is a dry
    run of the planting itself, so it never answers a different question than
    :func:`plant` does.
    """
    try:
        plant(truth, random.Random("can-carry"), counts)
    except NoMaterial:
        return False
    return True


def build_seeded(screens: Iterable, counts: dict[str, int] | None = None) -> tuple[SeededText, ...]:
    """One seeded variant per screen, from the predeclared seeds.

    ``counts`` exists for tests; the sensitivity set uses the predeclared
    :data:`SEEDED_COUNTS`. The page's own id and text select the seed, so a set
    generated twice is identical.
    """
    out: list[SeededText] = []
    for position, screen in enumerate(screens):
        seed = SEEDED_SEEDS[position % len(SEEDED_SEEDS)]
        rng = random.Random(f"{seed}:{screen.screen_id}")
        out.append(plant(screen.truth_text, rng, counts))
    return tuple(out)


def with_counts(counts: dict[str, int]) -> dict[str, int]:
    """Helper for tests: the predeclared counts with some classes overridden."""
    merged = dict(SEEDED_COUNTS)
    merged.update(counts)
    return merged


__all__ = [
    "NoMaterial",
    "PlantedError",
    "REAL_WORD_PAIRS",
    "SEEDED_COUNTS",
    "SEEDED_SEEDS",
    "SeededText",
    "build_seeded",
    "can_carry",
    "plant",
    "revert",
    "with_counts",
    "words_of",
]
