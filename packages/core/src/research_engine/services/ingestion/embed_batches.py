"""Embedding a batch that may not fit in accelerator memory.

Passage length varies enormously — a whole-chapter passage sits next to a
one-sentence one — so any fixed batch size is periodically wrong. Rather than
sizing every batch for the worst passage in the corpus, a batch that fails is
halved and retried.

Shared by `embedding_backfill` and `reindex`, because a batch that fails in one
fails in the other for the same reason. It was in only one of them, and the
re-chunk of two book-length documents died on CUDA OOM as a result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from research_engine.domain.errors import EmbeddingUnavailable, describe_exception

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

logger = structlog.get_logger()

#: 32 -> 16 -> 8 -> 4 -> 2 -> 1 is five halvings. Below one passage there is
#: nothing left to split, and the failure is real rather than memory pressure.
MAX_HALVING_DEPTH = 5


def free_accelerator_memory() -> None:
    """Release cached GPU memory between retries.

    Without this a halved batch retries into the memory the failed attempt still
    holds, and fails again for the wrong reason.
    """
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


@dataclass
class BatchOutcome:
    embedded: int = 0
    failed_batches: int = 0
    halvings: int = 0
    failed_passages: list[UUID] = field(default_factory=list)


class StoreFn(Protocol):
    def __call__(
        self, ids: list[UUID], vectors: list[list[float]]
    ) -> Awaitable[None]: ...


async def embed_and_store(
    embedding: Any,
    ids: list[UUID],
    texts: list[str],
    store: Callable[[list[UUID], list[list[float]]], Awaitable[None]],
    outcome: BatchOutcome | None = None,
    *,
    depth: int = 0,
) -> BatchOutcome:
    """Embed *texts* and hand the vectors to *store*, halving on failure.

    *store* is a callback rather than a repository so the caller decides the
    transaction: the backfill commits per batch, while a re-chunk needs the
    whole document in one transaction.
    """
    outcome = outcome or BatchOutcome()
    if not ids:
        return outcome

    try:
        vectors = await embedding.embed_batch(texts)
        await store(ids, vectors)
        outcome.embedded += len(ids)
        return outcome
    except EmbeddingUnavailable:
        # Halving answers memory pressure. It cannot answer a backend that is
        # not there: the same call fails identically at size 1, so retrying
        # costs a full timeout per attempt and the run makes no progress while
        # looking busy. Let it out and stop the run.
        logger.error(
            "embed_backend_unavailable",
            size=len(ids),
            depth=depth,
            action="aborting run; smaller batches cannot help",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - recover per batch, never abort the run
        outcome.failed_batches += 1
        if len(ids) == 1 or depth >= MAX_HALVING_DEPTH:
            outcome.failed_passages.extend(ids)
            logger.error(
                "embed_batch_failed",
                passages=[str(i) for i in ids],
                depth=depth,
                error=describe_exception(exc),
            )
            return outcome
        logger.warning(
            "embed_batch_halving",
            size=len(ids),
            depth=depth,
            error=describe_exception(exc)[:160],
        )

    free_accelerator_memory()
    outcome.halvings += 1
    mid = len(ids) // 2
    await embed_and_store(embedding, ids[:mid], texts[:mid], store, outcome, depth=depth + 1)
    await embed_and_store(embedding, ids[mid:], texts[mid:], store, outcome, depth=depth + 1)
    return outcome
