"""Ingestion orchestrator — full pipeline with per-doc transactions, dedup, progress."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING, Any

import structlog

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.domain.documents import DocumentDraft
from research_engine.domain.errors import IngestionError
from research_engine.services.ingestion.pipeline import build_document_draft, run_chunking

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from research_engine.ports.embedding import EmbeddingPort
    from research_engine.ports.repositories import (
        DocumentRepo,
        IngestionRunRepo,
        PassageRepo,
    )
    from research_engine.services.ingestion.dispatch import ModuleDispatcher

logger = structlog.get_logger()


class IngestionOrchestrator:
    def __init__(
        self,
        docs: DocumentRepo,
        passages: PassageRepo,
        embedding: EmbeddingPort,
        ingestion_runs: IngestionRunRepo,
        dispatcher: ModuleDispatcher,
        engine: object,  # AsyncEngine
        concurrency: int = 4,
        embedding_batch_size: int = 32,
    ) -> None:
        self._docs = docs
        self._passages = passages
        self._embedding = embedding
        self._ingestion_runs = ingestion_runs
        self._dispatcher = dispatcher
        self._engine = engine
        self._concurrency = concurrency
        self._embedding_batch_size = embedding_batch_size

    async def ingest_paths(
        self, paths: list[Path], plugin_hint: str | None = None
    ) -> dict:
        """Ingest files from the given paths. Returns stats dict."""
        run = await self._ingestion_runs.start_run(
            {"paths": [str(p) for p in paths], "hint": plugin_hint}
        )

        stats = {"total": 0, "ok": 0, "skipped": 0, "failed": 0}
        semaphore = asyncio.Semaphore(self._concurrency)

        tasks = []
        async for source_path in self._discover(paths):
            stats["total"] += 1

            async def ingest_one(sp: Path = source_path) -> None:
                async with semaphore:
                    await self._ingest_one(run.id, sp, plugin_hint, stats)

            tasks.append(asyncio.create_task(ingest_one()))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        status = "ok" if stats["failed"] == 0 else "partial" if stats["ok"] > 0 else "failed"
        await self._ingestion_runs.complete_run(run.id, status, stats)
        logger.info("ingestion_complete", run_id=str(run.id), **stats)
        return stats

    async def find_existing(
        self, *, source: str | None = None, source_pattern: str | None = None
    ) -> list[dict[str, Any]]:
        """Look up already-ingested documents by source path or substring.

        Either ``source`` (exact match) or ``source_pattern`` (substring, case-insensitive)
        must be provided. Returns matching documents with their passage counts; empty list
        if nothing matches.
        """
        from research_engine.domain.documents import DocumentFilter

        if not source and not source_pattern:
            raise ValueError("find_existing requires source or source_pattern")

        filt = DocumentFilter(source_pattern=source_pattern or source)
        results: list[dict[str, Any]] = []
        async for doc in self._docs.iter_by_filter(filt):
            if source is not None and doc.source != source:
                continue
            passages = await self._passages.get_by_document(doc.id)
            results.append({
                "document_id": str(doc.id),
                "title": doc.title,
                "document_type": doc.document_type,
                "source": doc.source,
                "ingested_at": doc.ingested_at.isoformat() if doc.ingested_at else None,
                "passage_count": len(passages),
                "metadata": doc.metadata,
            })
        return results

    async def ingest_drafts(
        self,
        title: str,
        document_type: str,
        passage_drafts: list,
        *,
        source: str = "",
        metadata: dict[str, Any] | None = None,
        language: str | None = None,
    ) -> dict:
        """Ingest pre-chunked PassageDrafts directly, skipping parse/chunk stages.

        Useful for plugins that fetch content from APIs and chunk it themselves.
        Returns dict with document_id, passage_count.
        """
        from research_engine.domain.passages import PassageDraft

        content_hash = hashlib.sha256(
            f"{source}:{title}".encode()
        ).digest()

        doc_draft = DocumentDraft(
            title=title,
            document_type=document_type,
            language=language,
            source=source,
            content_hash=content_hash,
            parser="plugin_direct",
            parser_version="1.0",
            metadata=metadata or {},
        )

        async with transaction(self._engine) as tx:
            doc = await self._docs.insert(tx, doc_draft)
            saved_passages = await self._passages.insert_many(
                tx, doc.id, passage_drafts
            )

            passage_ids = [p.id for p in saved_passages]
            texts = [p.text for p in saved_passages]

            for i in range(0, len(texts), self._embedding_batch_size):
                batch_texts = texts[i : i + self._embedding_batch_size]
                batch_ids = passage_ids[i : i + self._embedding_batch_size]
                embeddings = await self._embedding.embed_batch(batch_texts)
                await self._passages.store_embeddings(
                    tx, batch_ids, embeddings,
                    self._embedding.model_name, self._embedding.model_version,
                    self._embedding.dim,
                )
                del embeddings

            # Free GPU memory after embedding to avoid OOM on successive calls
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass

            await self._passages.index_fts(tx, passage_ids, texts, "english")

        logger.info(
            "ingest_drafts_ok",
            document_id=str(doc.id),
            passages=len(saved_passages),
            title=title,
        )
        return {
            "document_id": str(doc.id),
            "passage_count": len(saved_passages),
        }

    async def _ingest_one(
        self, run_id: object, source_path: Path, hint: str | None, stats: dict
    ) -> None:
        item = await self._ingestion_runs.add_item(
            run_id, str(source_path), "pending"
        )
        start = time.monotonic()
        try:
            # Dedup check
            content_hash = hashlib.sha256(source_path.read_bytes()).digest()
            existing = await self._docs.find_by_hash(content_hash, str(source_path.resolve()))
            if existing:
                logger.info("ingestion_skipped_dedup", source=str(source_path))
                stats["skipped"] += 1
                await self._ingestion_runs.update_item(
                    item.id, status="skipped", duration_ms=int((time.monotonic() - start) * 1000)
                )
                return

            # Dispatch
            module = await self._dispatcher.dispatch(source_path, hint)

            # Parse
            full_text, title, metadata = await module.parse(source_path)

            # Build document draft
            draft = build_document_draft(
                source_path=source_path,
                title=title,
                document_type=module.default_document_type(),
                parser_id=module.id,
                parser_version=module.version,
                metadata=metadata,
            )

            # Chunk
            chunker_id = module.default_chunker()
            passage_drafts = await run_chunking(full_text, chunker_id, metadata)

            # Transaction: insert doc + passages + embeddings + FTS
            async with transaction(self._engine) as tx:
                doc = await self._docs.insert(tx, draft)
                saved_passages = await self._passages.insert_many(tx, doc.id, passage_drafts)

                # Embed in batches
                passage_ids = [p.id for p in saved_passages]
                texts = [p.text for p in saved_passages]

                for i in range(0, len(texts), self._embedding_batch_size):
                    batch_texts = texts[i : i + self._embedding_batch_size]
                    batch_ids = passage_ids[i : i + self._embedding_batch_size]
                    embeddings = await self._embedding.embed_batch(batch_texts)
                    await self._passages.store_embeddings(
                        tx, batch_ids, embeddings,
                        self._embedding.model_name, self._embedding.model_version,
                        self._embedding.dim,
                    )

                # FTS index
                await self._passages.index_fts(tx, passage_ids, texts, "english")

            duration_ms = int((time.monotonic() - start) * 1000)
            await self._ingestion_runs.update_item(
                item.id, status="ok", document_id=doc.id, duration_ms=duration_ms
            )
            stats["ok"] += 1
            logger.info(
                "ingestion_ok",
                source=str(source_path),
                document_id=str(doc.id),
                passages=len(saved_passages),
                duration_ms=duration_ms,
            )

        except IngestionError as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            await self._ingestion_runs.update_item(
                item.id, status="failed", error=str(e), duration_ms=duration_ms
            )
            stats["failed"] += 1
            logger.warning("ingestion_failed", source=str(source_path), error=str(e))
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            await self._ingestion_runs.update_item(
                item.id, status="failed", error=str(e), duration_ms=duration_ms
            )
            stats["failed"] += 1
            logger.error("ingestion_error", source=str(source_path), error=str(e))

    async def _discover(self, paths: list[Path]) -> AsyncIterator[Path]:
        """Yield individual files from paths (files directly, dirs recursively)."""
        for path in paths:
            if path.is_file():
                yield path
            elif path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file() and not child.name.startswith("."):
                        yield child
