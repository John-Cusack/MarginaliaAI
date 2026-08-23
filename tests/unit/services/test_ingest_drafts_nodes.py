"""`ingest_drafts` must be able to accept a document's structure.

A pack that walks a table of contents already knows the tree. There was no way
to hand it over, so it was discarded at the door and every plugin-ingested
document got a bare root node from a later `reindex`. That is why 2,525 Logos
documents had structure for none of them, and why a lexicon passage cited the
volume instead of the entry it sits in.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from research_engine.domain.nodes import DocumentNodeDraft
from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator

pytestmark = pytest.mark.unit

# Two entries, the shape of a lexicon: each article its own node.
ENTRY_A = "logos. Word, speech, account. In Johannine usage the term carries."
ENTRY_B = "pistis. Faith, trust, faithfulness. Paul uses it of both."
TEXT = f"{ENTRY_A}\n\n{ENTRY_B}"
B_START = len(ENTRY_A) + 2


def passage(text: str, char_start: int) -> PassageDraft:
    return PassageDraft(
        position=0,
        char_start=char_start,
        char_end=char_start + len(text),
        text=text,
        chunker="test",
        chunker_version="5.0",
        token_count=8,
    )


def node(path: str, parent: str | None, depth: int, pos: int,
         title: str, start: int, end: int) -> DocumentNodeDraft:
    return DocumentNodeDraft(
        path=path, parent_path=parent, depth=depth, position=pos,
        node_type="section" if parent else "document",
        title=title, char_start=start, char_end=end,
    )


TREE = [
    node("r", None, 0, 0, "A Greek-English Lexicon", 0, len(TEXT)),
    node("r.n0", "r", 1, 0, "logos", 0, len(ENTRY_A)),
    node("r.n1", "r", 1, 1, "pistis", B_START, len(TEXT)),
]


class FakeDoc:
    def __init__(self) -> None:
        self.id = uuid4()


class FakeDocRepo:
    async def find_by_hash(self, *_: Any) -> None:
        return None

    async def insert(self, tx: Any, draft_doc: Any) -> FakeDoc:
        return FakeDoc()


class FakePassageRepo:
    def __init__(self) -> None:
        self.received: list[Any] = []

    async def insert_many(self, tx: Any, document_id: UUID, drafts: list[Any]) -> list[Any]:
        self.received = drafts
        return [type("P", (), {"id": uuid4(), "text": d.text})() for d in drafts]

    async def store_embeddings(self, *a: Any, **k: Any) -> None: ...
    async def index_fts(self, *a: Any, **k: Any) -> None: ...


class FakeNodeRepo:
    """Assigns ids the way the real repo does, parents before children."""

    def __init__(self) -> None:
        self.written: list[Any] = []

    async def insert_many(self, tx: Any, document_id: UUID, drafts: list[Any]) -> list[Any]:
        by_path: dict[str, Any] = {}
        stored = []
        for d in drafts:
            n = type("N", (), {
                "id": uuid4(), "document_id": document_id, "path": d.path,
                "parent_id": by_path[d.parent_path].id if d.parent_path else None,
                "depth": d.depth, "title": d.title,
                "char_start": d.char_start, "char_end": d.char_end,
                "node_type": d.node_type, "metadata": {},
            })()
            by_path[d.path] = n
            stored.append(n)
        self.written = stored
        return stored


class FakeEmbedding:
    model_name, model_version, dim = "fake", "1.0", 4

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


class FakeTx:
    conn = None


@pytest.fixture
def orchestrator(monkeypatch: pytest.MonkeyPatch):
    from contextlib import asynccontextmanager

    from research_engine.services.ingestion import orchestrator as module

    @asynccontextmanager
    async def fake_transaction(_engine):
        yield FakeTx()

    monkeypatch.setattr(module, "transaction", fake_transaction)

    passages, nodes = FakePassageRepo(), FakeNodeRepo()
    service = IngestionOrchestrator(
        docs=FakeDocRepo(), passages=passages, embedding=FakeEmbedding(),
        ingestion_runs=object(), dispatcher=object(), engine=object(),
        document_nodes=nodes,
    )
    return service, passages, nodes


async def test_supplied_structure_is_written(orchestrator) -> None:
    service, _, nodes = orchestrator

    result = await service.ingest_drafts(
        "A Greek-English Lexicon", "logos_book",
        [passage(ENTRY_A, 0)], full_text=TEXT, node_drafts=TREE,
    )

    assert result["node_count"] == 3
    assert [n.title for n in nodes.written] == [
        "A Greek-English Lexicon", "logos", "pistis",
    ]


async def test_passages_attach_to_their_entry_not_the_root(orchestrator) -> None:
    """The whole point: a citation must name the entry, not the volume."""
    service, passages, nodes = orchestrator

    await service.ingest_drafts(
        "A Greek-English Lexicon", "logos_book",
        [passage(ENTRY_A, 0), passage(ENTRY_B, B_START)],
        full_text=TEXT, node_drafts=TREE,
    )

    root = next(n for n in nodes.written if n.depth == 0)
    entries = {n.title: n.id for n in nodes.written if n.depth == 1}
    landed = [d.node_id for d in passages.received]

    assert landed == [entries["logos"], entries["pistis"]]
    assert root.id not in landed, "a passage on the root cites the volume, not the entry"


async def test_structure_stays_optional(orchestrator) -> None:
    """A corpus ingested without a tree is still a valid corpus."""
    service, passages, nodes = orchestrator

    result = await service.ingest_drafts(
        "Some Book", "logos_book", [passage(ENTRY_A, 0)], full_text=TEXT,
    )

    assert result["node_count"] == 0
    assert nodes.written == []
    assert all(getattr(d, "node_id", None) is None for d in passages.received)
