"""Stable chunking DTOs, token estimates, and span-preserving helpers."""

from __future__ import annotations

from research_engine_sdk.interfaces import Chunker
from research_engine_sdk.types import PassageDraft

DEFAULT_CHARS_PER_TOKEN = 4.0
ABSOLUTE_MAX_TOKENS = 2_000

_SCRIPT_RANGES: tuple[tuple[int, int, float], ...] = (
    (0x0370, 0x03FF, 1.68),
    (0x1F00, 0x1FFF, 1.68),
    (0x0590, 0x05FF, 1.40),
    (0xFB1D, 0xFB4F, 1.40),
    (0x0400, 0x04FF, DEFAULT_CHARS_PER_TOKEN),
    (0x0600, 0x06FF, 3.40),
    (0x0750, 0x077F, 3.40),
    (0x3040, 0x30FF, 1.50),
    (0x3400, 0x4DBF, 1.50),
    (0x4E00, 0x9FFF, 1.50),
    (0xAC00, 0xD7AF, 1.50),
    (0xF900, 0xFAFF, 1.50),
    (0x0100, 0x024F, 3.00),
    (0x1E00, 0x1EFF, 3.00),
    (0x0300, 0x036F, 2.00),
)
_UNKNOWN_CHARS_PER_TOKEN = 2.0
_SAMPLE_CEILING = 60_000
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


def _sample(text: str) -> str:
    if len(text) <= _SAMPLE_CEILING:
        return text
    return text[:: (len(text) // _SAMPLE_CEILING) + 1]


def chars_per_token(text: str) -> float:
    """Conservative average characters-per-token estimate for mixed scripts."""

    if not text or text.isascii():
        return DEFAULT_CHARS_PER_TOKEN
    sample = _sample(text)
    tokens = sum(_tokens_per_char(ord(char)) for char in sample)
    return len(sample) / tokens if tokens > 0 else DEFAULT_CHARS_PER_TOKEN


def min_chars_per_token(text: str) -> float:
    """Characters per token for the densest script present in ``text``."""

    if not text or text.isascii():
        return DEFAULT_CHARS_PER_TOKEN
    return 1.0 / max(_tokens_per_char(ord(char)) for char in _sample(text))


def approx_tokens(text: str, rate: float | None = None) -> int:
    if not text:
        return 1
    return max(1, int(len(text) / (rate if rate is not None else chars_per_token(text))))


def token_budget_chars(max_tokens: int, rate: float) -> int:
    return max(1, int(max_tokens * rate))


def trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Trim surrounding whitespace by moving the span, never by mutating text."""

    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def split_at_boundary(
    text: str, start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    """Split ``[start, end)`` at newlines/spaces while preserving offsets."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if start < 0 or end < start or end > len(text):
        raise ValueError("span is outside the supplied text")
    if end - start <= max_chars:
        return [(start, end)] if text[start:end].strip() else []

    pieces: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > max_chars:
        window_end = cursor + max_chars
        cut = text.rfind("\n", cursor + 1, window_end)
        if cut <= cursor:
            cut = text.rfind(" ", cursor + 1, window_end)
        if cut <= cursor:
            cut = window_end
        pieces.append((cursor, cut))
        cursor = cut
    if cursor < end:
        pieces.append((cursor, end))
    return [(piece_start, piece_end) for piece_start, piece_end in pieces if text[piece_start:piece_end].strip()]


def cap_spans(
    text: str, spans: list[tuple[int, int]], max_tokens: int
) -> list[tuple[int, int]]:
    """Re-split only spans whose own script density exceeds ``max_tokens``."""

    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    capped: list[tuple[int, int]] = []
    for start, end in spans:
        piece = text[start:end]
        if approx_tokens(piece) <= max_tokens:
            capped.append((start, end))
            continue
        budget = max(1, int(max_tokens * min_chars_per_token(piece)))
        capped.extend(split_at_boundary(text, start, end, budget))
    return capped


__all__ = [
    "ABSOLUTE_MAX_TOKENS",
    "DEFAULT_CHARS_PER_TOKEN",
    "Chunker",
    "PassageDraft",
    "approx_tokens",
    "cap_spans",
    "chars_per_token",
    "min_chars_per_token",
    "split_at_boundary",
    "token_budget_chars",
    "trim_span",
]
