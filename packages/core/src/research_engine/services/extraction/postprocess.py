"""Turn what the model wrote into something the corpus can compare.

Two of the field types a schema may declare are not really strings, and were
stored as strings anyway:

``fuzzy_date``
    The history pack's ``epistolary_references`` declares
    ``referenced_date: fuzzy_date`` for exactly the phrase "yours of the 15th
    ult." Left as text, ``find_missing_letters`` put it in ``expected_date`` and
    compared it against a timeline of real datetimes, where it matched nothing —
    so the tool reported no missing letters, which reads identically to there
    being none.

``entity_ref``
    Declared for ``referenced_party_entity_id``, whose description asks the
    model for a "resolved entity". A model cannot know this corpus's UUIDs, so
    the field was null or invented, and ``find_missing_letters`` filters
    correspondence on equality against it.

Resolution is written *beside* the model's answer, never over it. ``<field>``
stays exactly as the model wrote it and ``<field>_resolved`` carries the
structured form, because the whole point of storing extractions is that you can
go back and see what was actually said.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from research_engine.services.extraction.schemas import EVIDENCE_TYPE
from research_engine.services.extraction.validation import ValidatedRecord
from research_engine.services.text.dates import parse_fuzzy_date

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from research_engine.domain.passages import Passage
    from research_engine.ports.repositories import DocumentRepo, EntityRepo

logger = structlog.get_logger()

#: Suffix for the structured form of a resolved field.
RESOLVED = "_resolved"

#: Below this, a name match is a coincidence rather than a resolution. Entity
#: resolution is tiered exact -> alias -> trigram, and trigram similarity on
#: short nineteenth-century surnames ("Burnside" against "Burns") gets close
#: enough to be dangerous.
MIN_ENTITY_SCORE = 0.8


class RecordEnricher:
    """Resolves the declared field types that are not really strings."""

    def __init__(
        self,
        documents: DocumentRepo,
        entities: EntityRepo,
        document_nodes: Any = None,
    ) -> None:
        self._documents = documents
        self._entities = entities
        #: Structure nodes carry a date when their section states one — one per
        #: letter in a bound volume. Without them the only anchor available is
        #: the document's own date, which for a collected edition is one date
        #: for hundreds of letters, or none at all.
        self._nodes = document_nodes
        self._document_anchors: dict[UUID, datetime | None] = {}
        self._node_anchors: dict[UUID, datetime | None] = {}

    async def enrich(
        self,
        records: list[ValidatedRecord],
        passage: Passage,
        record_types: Mapping[str, dict],
    ) -> list[ValidatedRecord]:
        if not records:
            return records
        anchor = await self._anchor_for(passage)
        return [
            await self._enrich_one(record, record_types, anchor) for record in records
        ]

    async def _enrich_one(
        self,
        record: ValidatedRecord,
        record_types: Mapping[str, dict],
        anchor: datetime | None,
    ) -> ValidatedRecord:
        definition = record_types.get(record.record_type, {})
        fields = dict(record.fields)
        for name, spec in definition.get("fields", {}).items():
            declared = spec.get("type")
            if declared == EVIDENCE_TYPE:
                continue
            value = fields.get(name)
            if not isinstance(value, str) or not value.strip():
                continue
            if declared == "fuzzy_date":
                fields[name + RESOLVED] = _date_to_json(
                    parse_fuzzy_date(value, relative_to=anchor)
                )
            elif declared == "entity_ref":
                fields[name + RESOLVED] = await self._resolve_entity(
                    value, spec.get("entity_type")
                )
        if fields == record.fields:
            return record
        return ValidatedRecord(
            record_type=record.record_type,
            fields=fields,
            evidence_start=record.evidence_start,
            evidence_end=record.evidence_end,
        )

    async def _anchor_for(self, passage: Passage) -> datetime | None:
        """The date "the 15th" is relative to: this letter's, then this book's.

        The containing section first, because that is where the date actually
        lives in a collected edition — one document, hundreds of letters, each
        with its own dateline. Falling back to the document covers a single
        letter ingested on its own, which is what the `letter` document type
        describes.

        Memoized both ways: a run over one letterbook asks the same questions
        thousands of times and neither answer can change mid-run.
        """
        if passage.node_id is not None:
            node_anchor = await self._node_anchor(passage.node_id)
            if node_anchor is not None:
                return node_anchor
        return await self._document_anchor(passage.document_id)

    async def _node_anchor(self, node_id: UUID) -> datetime | None:
        if self._nodes is None:
            return None
        if node_id in self._node_anchors:
            return self._node_anchors[node_id]
        anchor = await self._read_node_date(node_id)
        self._node_anchors[node_id] = anchor
        return anchor

    async def _read_node_date(self, node_id: UUID) -> datetime | None:
        """This node's date, or the nearest dated ancestor's.

        A passage inside a subsection of a letter is still inside that letter,
        and the letter is where the dateline is.
        """
        try:
            node = await self._nodes.get(node_id)
            if (dated := _node_date(node)) is not None:
                return dated
            ancestors = await self._nodes.get_ancestors(node_id)
        except Exception as exc:  # noqa: BLE001 - anchoring is best-effort
            logger.warning("node_anchor_failed", node_id=str(node_id), error=str(exc))
            return None
        for ancestor in sorted(ancestors, key=lambda n: n.depth, reverse=True):
            if (dated := _node_date(ancestor)) is not None:
                return dated
        return None

    async def _document_anchor(self, document_id: UUID) -> datetime | None:
        if document_id in self._document_anchors:
            return self._document_anchors[document_id]
        document = await self._documents.get(document_id)
        anchor = document.created_date_start if document else None
        self._document_anchors[document_id] = anchor
        return anchor

    async def _resolve_entity(
        self, surface: str, entity_type: str | None
    ) -> dict[str, Any] | None:
        """Match a name as written against the entity store.

        Returns None rather than a best guess when nothing clears the bar. A
        wrongly resolved correspondent silently reassigns a letter to the wrong
        person, and nothing downstream can tell that from a real attribution.
        """
        try:
            candidates = await self._entities.search_by_name(surface, entity_type, 2)
        except Exception as exc:  # noqa: BLE001 - resolution is best-effort
            logger.warning("entity_resolution_failed", surface=surface, error=str(exc))
            return None
        if not candidates:
            return None
        best = candidates[0]
        if best.match_score < MIN_ENTITY_SCORE:
            return None
        # Two names equally close is not a resolution, it is a question.
        if len(candidates) > 1 and candidates[1].match_score >= best.match_score:
            return None
        return {
            "entity_id": str(best.entity_id),
            "canonical_name": best.canonical_name,
            "entity_type": best.entity_type,
            "match_score": best.match_score,
        }


def _node_date(node: Any) -> datetime | None:
    """The date a structure node's section stated, if it stated one."""
    if node is None:
        return None
    raw = (node.metadata or {}).get("date_start")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _date_to_json(date: Any) -> dict[str, Any] | None:
    if date is None:
        return None
    return {
        "start": date.start.isoformat(),
        "end": date.end.isoformat(),
        "precision": str(date.precision),
    }
