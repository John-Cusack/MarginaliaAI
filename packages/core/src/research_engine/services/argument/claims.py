"""The claim ledger's single atomic write path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from research_engine.domain.claims import (
    AnchorDraft,
    AnchorRole,
    AnchorVerifyStatus,
    ClaimWriteResult,
)
from research_engine.domain.entities import EntityDraft
from research_engine.services.verification.quote import Tier

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from research_engine.domain.claims import AnchorInput, ClaimDraft, ClaimEdgeDraft
    from research_engine.ports.repositories import (
        ClaimRepo,
        EditionRepo,
        SourceSpanRepo,
        Transaction,
    )
    from research_engine.services.argument.rules import ClaimAuditService
    from research_engine.services.entities.service import EntityService
    from research_engine.services.verification.quote import QuoteVerification, QuoteVerifier


class ClaimWriteRefused(ValueError):
    """Expected refusal: bad evidence or an edge to no stable claim ref."""

    def __init__(
        self, code: str, message: str, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class _VerifiedAnchor:
    draft: AnchorInput
    verification: QuoteVerification
    edition_id: UUID | None


class ClaimService:
    def __init__(
        self,
        claims_repo: ClaimRepo,
        spans_repo: SourceSpanRepo,
        editions_repo: EditionRepo,
        entity_service: EntityService,
        verification: QuoteVerifier,
        audit: ClaimAuditService,
        transaction_factory: Callable[..., Any],
    ) -> None:
        self._claims = claims_repo
        self._spans = spans_repo
        self._editions = editions_repo
        self._entities = entity_service
        self._verification = verification
        self._audit = audit
        self._transaction_factory = transaction_factory

    async def upsert(
        self,
        draft: ClaimDraft,
        *,
        edges: Sequence[ClaimEdgeDraft] = (),
        anchors: Sequence[AnchorInput] = (),
    ) -> ClaimWriteResult:
        """Verify first, then write the claim, edges, and anchors atomically."""
        self._validate_edges(draft.ref, edges)
        verified = await self._verify_anchors(anchors)

        targets = {}
        for edge in edges:
            target = await self._claims.get_by_ref(edge.target_ref)
            if target is None:
                raise ClaimWriteRefused(
                    "not_found",
                    f"Target claim {edge.target_ref!r} does not exist.",
                    {"target_ref": edge.target_ref},
                )
            targets[edge.target_ref] = target

        written_edges = []
        written_anchors = []
        async with self._transaction_factory() as tx:
            claim = await self._claims.upsert_claim(tx, draft)
            for edge in edges:
                target = targets[edge.target_ref]
                written_edges.append(
                    await self._claims.add_edge(
                        tx,
                        claim.id,
                        target.id,
                        edge.relation,
                        edge.confidence,
                        edge.note,
                    )
                )
            person_ids: dict[str, UUID] = {}
            for item in verified:
                location = item.verification.location
                assert location is not None
                span = await self._spans.resolve(
                    tx,
                    document_id=item.draft.document_id,
                    char_start=location.char_start,
                    char_end=location.char_end,
                )
                person_id = await self._person_id(tx, item.draft, person_ids)
                written_anchors.append(
                    await self._claims.add_anchor(
                        tx,
                        claim.id,
                        AnchorDraft(
                            role=item.draft.role,
                            quoted_text=item.draft.quote,
                            source_span_id=span.id,
                            person_entity_id=person_id,
                            verify_status=AnchorVerifyStatus(item.verification.tier.value),
                            verified_at=datetime.now(UTC),
                            parser_version=span.parser_version,
                            edition_id=item.edition_id,
                            edition_key=item.draft.edition_key,
                            locator=item.draft.locator,
                        ),
                    )
                )
        audit = await self._audit.audit([claim.ref])
        return ClaimWriteResult(
            claim=claim,
            edges=written_edges,
            anchors=written_anchors,
            findings=audit.findings,
        )

    @staticmethod
    def _validate_edges(ref: str, edges: Sequence[ClaimEdgeDraft]) -> None:
        seen = set()
        for index, edge in enumerate(edges):
            key = (edge.target_ref, edge.relation)
            if key in seen:
                raise ClaimWriteRefused(
                    "invalid_input",
                    f"Edge {index} duplicates an earlier target and relation.",
                    {"edge_index": index},
                )
            seen.add(key)
            if edge.target_ref == ref:
                raise ClaimWriteRefused(
                    "invalid_input",
                    f"Edge {index} points claim {ref!r} at itself.",
                    {"edge_index": index},
                )

    async def _verify_anchors(
        self, anchors: Sequence[AnchorInput]
    ) -> list[_VerifiedAnchor]:
        verified = []
        edition_ids: dict[str, UUID] = {}
        for index, anchor in enumerate(anchors):
            edition_id = None
            if anchor.edition_key is not None:
                if anchor.edition_key not in edition_ids:
                    edition = await self._editions.get_by_key(anchor.edition_key)
                    if edition is None:
                        raise ClaimWriteRefused(
                            "not_found",
                            f"Edition key {anchor.edition_key!r} does not exist.",
                            {
                                "anchor_index": index,
                                "edition_key": anchor.edition_key,
                            },
                        )
                    edition_ids[anchor.edition_key] = edition.id
                edition_id = edition_ids[anchor.edition_key]
            result = await self._verification.verify(anchor.quote, anchor.document_id)
            if result.tier in (Tier.NOT_FOUND, Tier.NO_CANONICAL_TEXT):
                raise ClaimWriteRefused(
                    "validation_error",
                    f"Anchor {index} was refused: verification returned "
                    f"{result.tier.value}.",
                    {"anchor_index": index, "tier": result.tier.value},
                )
            if result.location is None:
                raise ClaimWriteRefused(
                    "validation_error",
                    f"Anchor {index} has no addressable source span.",
                    {"anchor_index": index, "tier": result.tier.value},
                )
            verified.append(_VerifiedAnchor(anchor, result, edition_id))
        return verified

    async def _person_id(
        self,
        tx: Transaction,
        anchor: AnchorInput,
        person_ids: dict[str, UUID],
    ) -> UUID | None:
        if anchor.role is not AnchorRole.ASSERTS:
            return None
        assert anchor.person is not None
        name = anchor.person.strip()
        key = name.casefold()
        if key in person_ids:
            return person_ids[key]
        candidates = await self._entities.resolve(name, entity_type="person", k=1)
        if candidates and candidates[0].match_score > 0.95:
            person_id = candidates[0].entity_id
        else:
            entity = await self._entities.upsert(
                tx,
                EntityDraft(entity_type="person", canonical_name=name),
            )
            person_id = entity.id
        person_ids[key] = person_id
        return person_id
