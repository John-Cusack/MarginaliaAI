"""Hold a model's output to the schema that asked for it, and anchor its quotes.

Two jobs, and the second is the point of the whole extraction layer: an
extracted claim is only worth storing if you can go back to the sentence it came
from. That means every quotation the model returns has to be *located* — turned
into character offsets into the passage — not merely recognised as present.

Offsets returned here always index the original passage text, so
``passage.text[start:end]`` is the quotation. An earlier version fell back to
searching a whitespace-collapsed copy and returned offsets into *that*, which
are silently wrong by however much whitespace preceded the match: a citation
that verifies against nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from research_engine.domain.errors import EvidenceNotFound, ValidationError
from research_engine.services.extraction.schemas import (
    evidence_field_names,
    required_field_names,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_WS = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """Collapse whitespace for fuzzy substring matching."""
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class ValidatedRecord:
    """One record the model returned, checked and anchored to its passage."""

    record_type: str
    fields: dict[str, Any]

    #: Character offsets of this record's first evidence field within the
    #: passage text. Document-relative offsets are these plus the passage's own
    #: ``char_start``, which is why they are stored passage-relative: a passage
    #: with no span of its own still yields a usable anchor.
    evidence_start: int
    evidence_end: int


def locate_span(span_text: str, passage_text: str) -> tuple[int, int] | None:
    """Character offsets of *span_text* in *passage_text*, or None.

    Tolerates whitespace differences, because a model re-typing a quotation out
    of a hard-wrapped passage will not reproduce its line breaks. The tolerance
    is in the *pattern*, never in the text being searched, so the offsets that
    come back still address the passage exactly as stored.
    """
    if not span_text or not span_text.strip():
        return None

    index = passage_text.find(span_text)
    if index >= 0:
        return index, index + len(span_text)

    words = span_text.split()
    if not words:
        return None
    pattern = re.compile(r"\s+".join(re.escape(word) for word in words))
    if match := pattern.search(passage_text):
        return match.start(), match.end()
    return None


def validate_records(
    records: list[dict],
    passage_text: str,
    passage_id: object,
    record_types: Mapping[str, dict],
) -> list[ValidatedRecord]:
    """Check each record against its declared type and anchor its evidence.

    Raises on the first violation rather than dropping the record: a schema that
    half-works produces a table that is quietly incomplete, and the caller's one
    retry exists precisely to give the model the error text and another go.
    """
    validated: list[ValidatedRecord] = []
    for position, record in enumerate(records):
        record_type = record.get("record_type")
        if record_type not in record_types:
            raise ValidationError(
                f"Record {position} from passage {passage_id} has record_type "
                f"{record_type!r}; the schema declares "
                f"{', '.join(sorted(record_types)) or 'none'}."
            )

        fields = record.get("fields")
        if not isinstance(fields, dict):
            raise ValidationError(
                f"Record {position} from passage {passage_id} has no 'fields' "
                f"object."
            )

        definition = record_types[record_type]
        missing = [
            name for name in required_field_names(definition) if not fields.get(name)
        ]
        if missing:
            raise ValidationError(
                f"Record {position} of type '{record_type}' from passage "
                f"{passage_id} is missing required field(s): "
                f"{', '.join(missing)}."
            )

        anchor = _anchor(fields, definition, passage_text, passage_id, record_type)
        validated.append(
            ValidatedRecord(
                record_type=record_type,
                fields=fields,
                evidence_start=anchor[0],
                evidence_end=anchor[1],
            )
        )
    return validated


def _anchor(
    fields: dict[str, Any],
    definition: dict,
    passage_text: str,
    passage_id: object,
    record_type: str,
) -> tuple[int, int]:
    """Locate every evidence field, and return the first one's offsets.

    Every declared evidence field is checked, not just the one that becomes the
    anchor, because an unlocatable quotation in any field means the model
    invented text — and that record's other fields are no more trustworthy for
    it having quoted correctly somewhere else.
    """
    declared = evidence_field_names(definition)
    if not declared:
        raise ValidationError(
            f"Record type '{record_type}' declares no {'evidence_span'!r} "
            f"field, so nothing it produces can be checked against the corpus."
        )

    located: list[tuple[int, int]] = []
    for name in declared:
        value = fields.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValidationError(
                f"Evidence field '{name}' of record type '{record_type}' came "
                f"back as {type(value).__name__}, not a quotation."
            )
        span = locate_span(value, passage_text)
        if span is None:
            raise EvidenceNotFound(
                field=name, passage_id=passage_id, span_text=value
            )
        located.append(span)

    if not located:
        raise ValidationError(
            f"Record of type '{record_type}' from passage {passage_id} quoted "
            f"nothing; one of {', '.join(declared)} is required."
        )
    return located[0]
