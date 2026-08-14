"""Evidence-span validation with whitespace normalization."""

from __future__ import annotations

import re

from research_engine.domain.errors import EvidenceNotFound

_WS = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """Collapse whitespace for fuzzy substring matching."""
    return _WS.sub(" ", text).strip()


def validate_evidence_spans(records: list[dict], passage_text: str, passage_id: object) -> None:
    """Validate that evidence_span fields are actual substrings of the passage."""
    for record in records:
        fields = record.get("fields", record.get("data", record))
        if not isinstance(fields, dict):
            continue
        for field_name, value in fields.items():
            if not isinstance(value, str):
                continue
            # Check if this looks like an evidence field
            if "evidence" not in field_name.lower():
                continue
            if value in passage_text:
                continue
            # Try fuzzy match with whitespace normalization
            norm_passage = normalize_whitespace(passage_text)
            norm_span = normalize_whitespace(value)
            if norm_span in norm_passage:
                continue
            raise EvidenceNotFound(
                field=field_name,
                passage_id=passage_id,
                span_text=value,
            )


def compute_byte_offsets(span_text: str, passage_text: str) -> tuple[int, int] | None:
    """Find byte offsets of a span within passage text."""
    idx = passage_text.find(span_text)
    if idx >= 0:
        byte_start = len(passage_text[:idx].encode())
        byte_end = byte_start + len(span_text.encode())
        return byte_start, byte_end

    # Try normalized
    norm_passage = normalize_whitespace(passage_text)
    norm_span = normalize_whitespace(span_text)
    idx = norm_passage.find(norm_span)
    if idx >= 0:
        return idx, idx + len(norm_span)

    return None
