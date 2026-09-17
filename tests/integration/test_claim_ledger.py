"""Claim writes and fidelity windows against the real Postgres schema."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import pytest

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGClaimRepo,
    PGDocumentNodeRepo,
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGEditionRepo,
    PGEntityRepo,
    PGMentionRepo,
    PGPassageRepo,
    PGSourceSpanRepo,
)
from research_engine.adapters.storage.postgres.schema import editions, entities
from research_engine.domain.claims import (
    AnchorInput,
    AnchorRole,
    ClaimDraft,
    ClaimEdgeDraft,
    ClaimKind,
    ClaimRelation,
)
from research_engine.domain.nodes import DocumentNodeDraft
from research_engine.domain.passages import PassageDraft
from research_engine.mcp.dispatch import dispatch_tool
from research_engine.services.argument import (
    AnchorContextService,
    ClaimAuditService,
    ClaimService,
)
from research_engine.services.entities.service import EntityService
from research_engine.services.verification import QuoteVerifier

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.testing import Corpus

pytestmark = [pytest.mark.integration]

TEXT = (
    "Opening context. The author says “land must return” in this sentence. "
    "Closing context."
)
TYPED_QUOTE = '"land must return"'
SOURCE_QUOTE = "“land must return”"


async def _ingest(engine: AsyncEngine, corpus: Corpus) -> UUID:
    document_id = await corpus.add_document(title="The argument source")
    async with transaction(engine) as tx:
        await PGDocumentTextRepo(engine).put(
            tx, document_id, TEXT, "test", "claim-ledger-1"
        )
        await PGPassageRepo(engine).insert_many(
            tx,
            document_id,
            [
                PassageDraft(
                    position=0,
                    char_start=0,
                    char_end=len(TEXT),
                    text=TEXT,
                    chunker="test",
                    chunker_version="1",
                )
            ],
        )
        await PGDocumentNodeRepo(engine).insert_many(
            tx,
            document_id,
            [
                DocumentNodeDraft(
                    path="r",
                    parent_path=None,
                    depth=0,
                    position=0,
                    node_type="section",
                    title="The section",
                    char_start=0,
                    char_end=len(TEXT),
                )
            ],
        )
    return document_id


def _services(engine: AsyncEngine) -> tuple[ClaimService, AnchorContextService]:
    texts = PGDocumentTextRepo(engine)
    passages = PGPassageRepo(engine)
    documents = PGDocumentRepo(engine)
    nodes = PGDocumentNodeRepo(engine)
    spans = PGSourceSpanRepo(engine)
    claims = PGClaimRepo(engine)
    verifier = QuoteVerifier(texts, passages, documents, nodes)
    claim_service = ClaimService(
        claims_repo=claims,
        spans_repo=spans,
        editions_repo=PGEditionRepo(engine),
        entity_service=EntityService(PGEntityRepo(engine), PGMentionRepo(engine)),
        verification=verifier,
        audit=ClaimAuditService(claims),
        transaction_factory=partial(transaction, engine),
    )
    return claim_service, AnchorContextService(spans, texts, nodes)


def _tool_container(
    service: ClaimService, context: AnchorContextService, engine: AsyncEngine
) -> Any:
    class Container:
        claim_service = service
        claims = PGClaimRepo(engine)
        anchor_context_service = context

    return Container()


@pytest.mark.asyncio
async def test_claim_upsert_preserves_typed_quote_and_shares_the_span(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    document_id = await _ingest(engine, corpus)
    target = await corpus.add_claim("TARGET-001")
    service, context_service = _services(engine)

    result = await service.upsert(
        ClaimDraft(
            ref="SOURCE-001",
            statement="The land must return.",
            kind=ClaimKind.OPPOSITION,
        ),
        edges=[
            ClaimEdgeDraft(
                target_ref=target.ref,
                relation=ClaimRelation.DEPENDS_ON,
            )
        ],
        anchors=[
            AnchorInput(
                role=AnchorRole.ASSERTS,
                quote=TYPED_QUOTE,
                document_id=document_id,
                person="An Author",
            ),
            AnchorInput(
                role=AnchorRole.ASSERTS,
                quote=TYPED_QUOTE,
                document_id=document_id,
                person="An Author",
            ),
        ],
    )
    corpus.adopt_claim(result.claim.id)
    for anchor in result.anchors:
        corpus.adopt_span(anchor.source_span_id)
    person_id = result.anchors[0].person_entity_id
    assert person_id is not None
    corpus.track(entities, person_id)

    assert len(result.edges) == 1
    assert len(result.anchors) == 2
    assert {anchor.source_span_id for anchor in result.anchors} == {
        result.anchors[0].source_span_id
    }
    assert {anchor.verify_status for anchor in result.anchors} == {"normalized"}
    assert {anchor.person_entity_id for anchor in result.anchors} == {person_id}
    assert result.anchors[0].quoted_text == TYPED_QUOTE
    span = await PGSourceSpanRepo(engine).get(result.anchors[0].source_span_id)
    assert span is not None
    assert span.quoted_text == SOURCE_QUOTE
    assert span.parser_version == "claim-ledger-1"

    context = await context_service.for_span(span.id, window=12)
    start = context.quote_offset_in_window
    assert context.window[start : start + context.quote_length] == SOURCE_QUOTE
    assert context.containing_node is not None
    assert context.containing_node.title == "The section"

@pytest.mark.asyncio
async def test_anchor_edition_key_resolves_to_restricted_identity(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    document_id = await _ingest(engine, corpus)
    service, context = _services(engine)
    async with transaction(engine) as tx:
        edition = await PGEditionRepo(engine).upsert_key(tx, "CLAIM-EDITION")
    corpus.track(editions, edition.id)

    result = await service.upsert(
        ClaimDraft(
            ref="EDITION-001",
            statement="The source names a known edition.",
            kind=ClaimKind.PREMISE,
        ),
        anchors=[
            AnchorInput(
                role=AnchorRole.SUPPORTS,
                quote=SOURCE_QUOTE,
                document_id=document_id,
                edition_key=edition.edition_key,
            )
        ],
    )
    corpus.adopt_claim(result.claim.id)
    corpus.adopt_span(result.anchors[0].source_span_id)
    assert result.anchors[0].edition_key == edition.edition_key
    assert result.anchors[0].edition_id == edition.id

    spans_before = await PGSourceSpanRepo(engine).for_document(document_id)
    refused = await dispatch_tool(
        _tool_container(service, context, engine),
        "claim_upsert",
        {
            "ref": "EDITION-UNKNOWN",
            "statement": "An unknown edition refuses the whole write.",
            "kind": "premise",
            "anchors": [
                {
                    "role": "supports",
                    "quote": SOURCE_QUOTE,
                    "document_id": str(document_id),
                    "edition_key": "NO-SUCH-EDITION",
                }
            ],
        },
    )
    assert refused["error"] == {
        "code": "not_found",
        "message": "Edition key 'NO-SUCH-EDITION' does not exist.",
        "details": {
            "anchor_index": 0,
            "edition_key": "NO-SUCH-EDITION",
        },
    }
    assert await PGClaimRepo(engine).get_by_ref("EDITION-UNKNOWN") is None
    assert await PGSourceSpanRepo(engine).for_document(document_id) == spans_before


@pytest.mark.asyncio
async def test_unverifiable_anchor_rolls_back_the_whole_call(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    document_id = await _ingest(engine, corpus)
    service, _ = _services(engine)
    container = _tool_container(service, _services(engine)[1], engine)

    response = await dispatch_tool(
        container,
        "claim_upsert",
        {
            "ref": "ROLLBACK-001",
            "statement": "Nothing from the call survives.",
            "kind": "premise",
            "anchors": [
                {
                    "role": "supports",
                    "quote": SOURCE_QUOTE,
                    "document_id": str(document_id),
                },
                {
                    "role": "supports",
                    "quote": "words absent from this document",
                    "document_id": str(document_id),
                },
            ],
        },
    )

    assert response["error"]["code"] == "validation_error"
    assert response["error"]["details"] == {
        "anchor_index": 1,
        "tier": "not_found",
    }
    assert await PGClaimRepo(engine).get_by_ref("ROLLBACK-001") is None
    assert await PGSourceSpanRepo(engine).for_document(document_id) == []


@pytest.mark.asyncio
async def test_edge_upsert_is_unique_and_claim_id_is_stable(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    target = await corpus.add_claim("EDGE-TARGET")
    service, _ = _services(engine)
    first = await service.upsert(
        ClaimDraft(ref="EDGE-SOURCE", statement="First wording", kind="mine"),
        edges=[
            ClaimEdgeDraft(
                target_ref=target.ref,
                relation="depends_on",
                note="first",
            )
        ],
    )
    corpus.adopt_claim(first.claim.id)
    second = await service.upsert(
        ClaimDraft(ref="EDGE-SOURCE", statement="Better wording", kind="mine"),
        edges=[
            ClaimEdgeDraft(
                target_ref=target.ref,
                relation="depends_on",
                note="revised",
            )
        ],
    )

    assert second.claim.id == first.claim.id
    assert second.claim.statement == "Better wording"
    edges = await PGClaimRepo(engine).edges_for(first.claim.id)
    assert len(edges) == 1
    assert edges[0].note == "revised"


@pytest.mark.asyncio
async def test_context_clamps_both_document_ends(
    engine: AsyncEngine, corpus: Corpus
) -> None:
    document_id = await _ingest(engine, corpus)
    _, context_service = _services(engine)

    at_start = await context_service.for_coordinates(document_id, 0, 7, window=20)
    at_end = await context_service.for_coordinates(
        document_id, len(TEXT) - 8, len(TEXT), window=20
    )

    assert at_start.window_start == 0
    assert at_start.quote_offset_in_window == 0
    assert at_start.window[:7] == TEXT[:7]
    assert at_end.window_end == len(TEXT)
    offset = at_end.quote_offset_in_window
    assert at_end.window[offset : offset + at_end.quote_length] == TEXT[-8:]
