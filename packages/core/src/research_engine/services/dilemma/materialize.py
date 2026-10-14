"""Turn extraction records into machine claims anchored to their passages.

An extraction record is what a model said about a passage. A machine claim is
that record made checkable: every verbatim evidence field is located again in
the passage, resolved to a source span through the span resolver, and the
span's stored slice — read from the document's canonical text, never from the
model's answer — must equal the passage slice at those offsets. A record that
fails any of that stores nothing: a claim that cannot be traced back to its
sentence is an opinion, and this table is for evidence.

Materializing is idempotent. Each record writes in its own transaction, and
``(extraction_record_id, statement)`` is unique, so re-running over the same
records counts the claims it already made and creates none of them again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from research_engine.domain.machine_claims import (
    ClaimEvidenceDraft,
    ClaimPolarity,
    MachineClaimDraft,
    Modality,
    TargetForm,
    VouchEdgeDraft,
    VouchKind,
)
from research_engine.services.extraction.validation import locate_span

if TYPE_CHECKING:
    from research_engine.adapters.storage.postgres.repositories import (
        PGMachineClaimRepo,
    )
    from research_engine.domain.extractions import ExtractionRecord, ExtractionSchema


class _EvidenceFailure(Exception):
    """The resolved span does not quote the passage slice it should."""


class ClaimMaterializer:
    """Materializes `claim` and `vouch` extraction records under one run."""

    def __init__(
        self,
        extractions: Any,
        extraction_schemas: Any,
        passages: Any,
        spans: Any,
        claims: PGMachineClaimRepo,
        entities: Any,
        transaction_factory: Any,
        *,
        documents: Any = None,
    ) -> None:
        self._extractions = extractions
        self._schemas = extraction_schemas
        self._passages = passages
        self._spans = spans
        self._claims = claims
        self._entities = entities
        self._transaction = transaction_factory
        #: For a claim's edition, which is its document's. Without a documents
        #: repo the edition stays NULL — unknown, never guessed.
        self._documents = documents
        self._edition_by_document: dict[UUID, UUID | None] = {}

    async def materialize(
        self,
        *,
        run_id: UUID,
        schema_ref: str,
        extractor_version: str | None = None,
    ) -> dict[str, int]:
        """Materialize every stored record of one schema under `run_id`.

        Returns counts: records read, claims and vouches created, records
        skipped because they had already materialized, records skipped
        because their passage carries no document offsets, records whose
        evidence could not be verified against the canonical text, and
        created claims whose statement still looks bundled (Decision 005 —
        a trend counter for extraction quality, never a gate).
        """
        schema = await self._schema(schema_ref)
        records = await self._extractions.records_for_schema(
            schema.id, extractor_version
        )
        stats = {
            "records": len(records),
            "claims_created": 0,
            "vouches_created": 0,
            "skipped_existing": 0,
            "skipped_no_offsets": 0,
            "evidence_failures": 0,
            "bundled_flagged": 0,
        }
        for record in records:
            if record.record_type not in ("claim", "vouch"):
                continue
            await self._materialize_one(run_id, schema, record, stats)
        return stats

    async def _materialize_one(
        self,
        run_id: UUID,
        schema: ExtractionSchema,
        record: ExtractionRecord,
        stats: dict[str, int],
    ) -> None:
        data = record.data or {}
        passage = await self._passages.get(record.passage_id)
        if passage is None:
            stats["evidence_failures"] += 1
            return
        if passage.char_start is None:
            # No document offsets, no span: a passage-relative anchor is not
            # an address another stage can cite.
            stats["skipped_no_offsets"] += 1
            return

        located = self._locate_evidence(schema, record.record_type, data, passage.text)
        if located is None:
            stats["evidence_failures"] += 1
            return

        try:
            draft, vouch = await self._drafts(run_id, record, passage, data)
        except (ValueError, ValidationError):
            # A statement, modality or kind the vocabularies do not hold. The
            # record was validated against its schema on the way in, so this
            # is a schema/engine vocabulary drift — counted, never stored
            # half-translated.
            stats["evidence_failures"] += 1
            return

        created = False
        try:
            async with self._transaction() as tx:
                claim = await self._claims.insert_claim(tx, draft)
                if claim is None:
                    stats["skipped_existing"] += 1
                    return
                evidence = []
                for ordinal, (field, start, end) in enumerate(located):
                    span = await self._spans.resolve(
                        tx,
                        document_id=passage.document_id,
                        char_start=passage.char_start + start,
                        char_end=passage.char_start + end,
                    )
                    # Re-verify against the canonical slice the resolver
                    # stored, not against what the model typed.
                    if span.quoted_text != passage.text[start:end]:
                        raise _EvidenceFailure(field)
                    evidence.append(
                        ClaimEvidenceDraft(
                            claim_id=claim.id,
                            source_span_id=span.id,
                            field=field,
                            ordinal=ordinal,
                        )
                    )
                await self._claims.insert_evidence(tx, evidence)
                if vouch is not None:
                    await self._claims.insert_vouch(
                        tx, vouch.model_copy(update={"claim_id": claim.id})
                    )
                created = True
        except _EvidenceFailure:
            stats["evidence_failures"] += 1
            return
        except IntegrityError:
            stats["skipped_existing"] += 1
            return
        if created:
            stats["claims_created"] += 1
            if vouch is not None:
                stats["vouches_created"] += 1
            elif bundled_looking(draft.statement, _text(data, "evidence") or ""):
                stats["bundled_flagged"] += 1

    async def _drafts(
        self, run_id: UUID, record: ExtractionRecord, passage: Any, data: dict[str, Any]
    ) -> tuple[MachineClaimDraft, VouchEdgeDraft | None]:
        statement = _text(data, "statement")
        if not statement:
            raise ValueError("record has no statement")
        common: dict[str, Any] = {
            "run_id": run_id,
            "extraction_record_id": record.id,
            "document_id": passage.document_id,
            "edition_id": await self._edition_id(passage.document_id),
            "statement": statement,
            "speaker_label": _text(data, "speaker"),
            "speaker_ref": _text(data, "speaker_ref"),
            "speaker_entity_id": await self._resolve_entity(data, "speaker"),
            "confidence": _confidence(data.get("confidence")),
        }
        if record.record_type == "vouch":
            draft = MachineClaimDraft(
                **common,
                modality=Modality(_text(data, "modality") or "asserts"),
            )
            vouch = VouchEdgeDraft(
                # Replaced with the stored claim's id inside its transaction.
                claim_id=record.id,
                run_id=run_id,
                kind=VouchKind(_text(data, "kind") or ""),
                target_label=_text(data, "target"),
                target_ref=_text(data, "target_ref"),
                target_entity_id=await self._resolve_entity(data, "target"),
                target_form=TargetForm(
                    _text(data, "target_form") or "unspecified"
                ),
            )
            return draft, vouch
        draft = MachineClaimDraft(
            **common,
            subject_label=_text(data, "subject"),
            subject_ref=_text(data, "subject_ref"),
            subject_entity_id=await self._resolve_entity(data, "subject"),
            predicate=_text(data, "predicate"),
            object_label=_text(data, "object"),
            polarity=ClaimPolarity(_text(data, "polarity") or "affirms"),
            modality=Modality(_text(data, "modality") or ""),
            scope_quantifier=_text(data, "scope_quantifier"),
            temporal_scope=_text(data, "temporal_scope"),
            topic_keys=_topic_keys(data.get("topic_keys")),
            attributes=_group_attributes(data),
        )
        return draft, None

    def _locate_evidence(
        self,
        schema: ExtractionSchema,
        record_type: str,
        data: dict[str, Any],
        passage_text: str,
    ) -> list[tuple[str, int, int]] | None:
        """Offsets of every declared evidence field the record filled in.

        In the schema's declaration order, which becomes the evidence rows'
        ordinal order. None when a filled field cannot be located in the
        passage, or none was filled at all.
        """
        located: list[tuple[str, int, int]] = []
        for field in _evidence_fields(schema.schema_def, record_type):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            offsets = locate_span(value, passage_text)
            if offsets is None:
                return None
            located.append((field, offsets[0], offsets[1]))
        return located or None

    async def _resolve_entity(
        self, data: dict[str, Any], field: str
    ) -> UUID | None:
        """The entity a label names, exactly — or None, never a guess.

        The enricher's ``<field>_resolved`` wins when present; otherwise an
        exact-mode name search must name exactly one entity. No entity is
        ever created here: an unresolved label stays a label.
        """
        resolved = data.get(f"{field}_resolved")
        if isinstance(resolved, dict) and resolved.get("entity_id"):
            try:
                return UUID(str(resolved["entity_id"]))
            except ValueError:
                return None
        label = _text(data, field)
        if not label or self._entities is None:
            return None
        candidates = await self._entities.search_by_name(
            label, None, 2, exact=True
        )
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        entity_id = (
            candidate.get("entity_id")
            if isinstance(candidate, dict)
            else getattr(candidate, "entity_id", None)
        )
        return UUID(str(entity_id)) if entity_id else None

    async def _edition_id(self, document_id: UUID) -> UUID | None:
        if document_id not in self._edition_by_document:
            edition_id = None
            if self._documents is not None:
                document = await self._documents.get(document_id)
                edition_id = document.edition_id if document else None
            self._edition_by_document[document_id] = edition_id
        return self._edition_by_document[document_id]

    async def _schema(self, schema_ref: str) -> ExtractionSchema:
        parts = schema_ref.split(":")
        version = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        schema = await self._schemas.get_by_name_version(parts[0], version)
        if schema is None:
            raise ValueError(f"Schema not found: {schema_ref}")
        return schema


def _evidence_fields(schema_def: dict[str, Any], record_type: str) -> list[str]:
    """Evidence-span field names a record type declares, in order."""
    for definition in schema_def.get("record_types", []):
        if definition.get("id") == record_type:
            return [
                name
                for name, spec in definition.get("fields", {}).items()
                if isinstance(spec, dict) and spec.get("type") == "evidence_span"
            ]
    return []


def _text(data: dict[str, Any], field: str) -> str | None:
    value = data.get(field)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _topic_keys(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"confidence {result} is outside 0..1")
    return result


_GROUP_RELATIONS = frozenset(
    {"composite_act", "independent_list", "condition", "purpose"}
)


def _group_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Sentence-group metadata a record declared, kept when well-formed.

    Grouping is advisory: it lets a later stage reassemble claims split from
    one sentence (Decision 005). A malformed group value says nothing about
    the claim itself, so it is dropped here rather than costing the record —
    the claim is stored, the hint is not.
    """
    attributes: dict[str, Any] = {}
    group = data.get("group")
    if isinstance(group, bool):
        pass
    elif isinstance(group, int):
        attributes["group"] = group
    elif isinstance(group, float) and group.is_integer():
        attributes["group"] = int(group)
    relation = _text(data, "group_relation")
    if relation in _GROUP_RELATIONS:
        attributes["group_relation"] = relation
    return attributes


def bundled_looking(statement: str, evidence_text: str) -> bool:
    """Whether a statement still reads like several claims in one.

    A cheap tripwire, not a judge: it counts, run over run, how often
    extraction leaves coordinated assertions fused into one statement, so a
    prompt change can be measured instead of hoped about (Decision 005).
    Kept-whole conditionals will sometimes trip it. That is acceptable for a
    trend counter and fatal for a gate, so nothing is ever refused on it.
    """
    if ";" in statement:
        return True
    if "; " in evidence_text and " and " in statement.lower():
        return True
    return statement.count(", and ") >= 2
