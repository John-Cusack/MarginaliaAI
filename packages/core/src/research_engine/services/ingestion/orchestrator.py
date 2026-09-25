"""Ingestion orchestrator — full pipeline with per-doc transactions, dedup, progress."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING, Any

import structlog

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.domain.documents import DocumentDraft
from research_engine.domain.errors import IngestionError, IngestRefused
from research_engine.domain.nodes import (
    DocumentNodeDraft,
    attach_nodes,
    build_node_tree,
)
from research_engine.services.ingestion.pipeline import build_document_draft, run_chunking
from research_engine.services.search.langconfig import pg_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from research_engine.ports.embedding import EmbeddingPort
    from research_engine.ports.repositories import (
        DocumentRepo,
        DocumentTextRepo,
        EditionRepo,
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
        default_language: str | None = None,
        document_texts: DocumentTextRepo | None = None,
        document_nodes: object | None = None,
        editions: EditionRepo | None = None,
        forbidden_roots: Sequence[Path] = (),
    ) -> None:
        self._document_texts = document_texts
        #: Optional like ``document_texts``: without it ingested edition keys
        #: never reach `bibliography.editions` and row citations cannot join
        #: them. The migration backfill covered the keys already stored.
        self._editions = editions
        #: Optional like ``document_texts``: a corpus ingested before the node
        #: table existed is still a valid corpus, and structure is an addition
        #: to retrieval rather than a precondition for it.
        self._document_nodes = document_nodes
        self._docs = docs
        self._passages = passages
        self._embedding = embedding
        self._ingestion_runs = ingestion_runs
        self._dispatcher = dispatcher
        self._engine = engine
        self._concurrency = concurrency
        self._embedding_batch_size = embedding_batch_size
        #: ISO 639-1 code assumed when neither the parser nor the caller supplies
        #: one. Left unset the corpus indexes under ``simple`` (no stemming),
        self._default_language = default_language
        #: Vault roots ingestion must never touch. Stored resolved so a
        #: symlink pointing into the vault is refused like the vault itself.
        self._forbidden_roots = tuple(
            root.resolve() for root in forbidden_roots
        )

    def _resolve_language(self, supplied: str | None) -> str | None:
        """Prefer what the caller or parser knows; otherwise the configured default."""
        return supplied or self._default_language

    async def _record_edition(
        self,
        tx: Any,
        metadata: dict[str, Any] | None,
        *,
        lock: bool = False,
    ) -> Any | None:
        """Materialize a declared edition and optionally serialize its file ingest."""
        if self._editions is None:
            return None
        key = (metadata or {}).get("edition_key")
        if not isinstance(key, str) or not key:
            return None
        if lock:
            return await self._editions.upsert_key(tx, key, lock=True)
        return await self._editions.upsert_key(tx, key)

    def _forbidden_root(self, path: Path) -> Path | None:
        """The forbidden root containing *path*, resolving symlinks first."""
        resolved = path.resolve()
        for root in self._forbidden_roots:
            if resolved == root or root in resolved.parents:
                return root
        return None

    async def ingest_paths(
        self, paths: list[Path], plugin_hint: str | None = None
    ) -> dict:
        """Ingest files from the given paths. Returns stats dict."""
        for path in paths:
            root = self._forbidden_root(path)
            if root is not None:
                raise IngestRefused(
                    f"Refusing to ingest {path}: it is the vault or under it "
                    f"({root}). Authored material never enters the corpus."
                )
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
        full_text: str | None = None,
        node_drafts: list[DocumentNodeDraft] | None = None,
    ) -> dict:
        """Ingest pre-chunked PassageDrafts directly, skipping parse/chunk stages.

        Useful for plugins that fetch content from APIs and chunk it themselves.
        Returns dict with document_id, passage_count.

        *full_text* is the canonical text the drafts' ``char_start`` /
        ``char_end`` index into. Supply it: without it the offsets address text
        that is not stored, so the passages cannot be quote-verified or
        re-anchored, and `reindex chunks` will skip the document.

        *node_drafts* is the document's structure, when the caller knows it. A
        pack that walks a table of contents already has the tree; without a way
        to hand it over it was discarded at the door, and every plugin-ingested
        document ended up with a bare root node — which is why a lexicon's
        passages cited the volume rather than the entry they sit in. Supplying
        it also attaches each passage to its deepest containing node, so the
        structure is usable by `locate_passage` rather than merely present.
        """

        # Hash the *content*, not `source:title`. The old form meant a document
        # was identified by its metadata, so re-ingesting the same file under a
        # differently-cased title produced a second document that the
        # (content_hash, source) unique constraint could not catch — which is how
        # one library book ended up in the corpus twice.
        content_hash = hashlib.sha256(
            (full_text if full_text is not None else "\n".join(
                getattr(d, "text", "") for d in passage_drafts
            )).encode()
        ).digest()

        existing = await self._docs.find_by_hash(content_hash, source)
        if existing is not None:
            logger.info(
                "ingest_drafts_skipped_dedup",
                document_id=str(existing.id),
                source=source,
                title=title,
            )
            passages = await self._passages.get_by_document(existing.id)
            return {
                "document_id": str(existing.id),
                "passage_count": len(passages),
                "skipped": "duplicate",
            }

        language = self._resolve_language(language)
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
            edition = await self._record_edition(tx, metadata)
            if edition is not None:
                doc_draft = doc_draft.model_copy(update={"edition_id": edition.id})
            doc = await self._docs.insert(tx, doc_draft)
            if full_text is not None and self._document_texts is not None:
                await self._document_texts.put(
                    tx, doc.id, full_text, "plugin_direct", "1.0"
                )
            elif full_text is None:
                logger.warning(
                    "ingest_drafts_without_canonical_text",
                    title=title,
                    source=source,
                    detail=(
                        "Passage offsets will address text that is not stored. "
                        "Pass full_text= to make them verifiable and re-anchorable."
                    ),
                )
            # Before the passages: `attach_nodes` needs the nodes' ids, which
            # only exist once they are written.
            if node_drafts and self._document_nodes is not None:
                stored_nodes = await self._document_nodes.insert_many(
                    tx, doc.id, node_drafts
                )
                passage_drafts = attach_nodes(passage_drafts, stored_nodes)

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

            await self._passages.index_fts(tx, passage_ids, texts, pg_config(language))

        logger.info(
            "ingest_drafts_ok",
            document_id=str(doc.id),
            passages=len(saved_passages),
            title=title,
        )
        return {
            "document_id": str(doc.id),
            "passage_count": len(saved_passages),
            "node_count": len(node_drafts or []),
        }

    async def _store_file_document(
        self,
        *,
        draft: DocumentDraft,
        metadata: dict[str, Any],
        full_text: str,
        module: Any,
        passage_drafts: list[Any],
        title: str,
        language: str | None,
    ) -> tuple[Any, list[Any], bool]:
        """Write one parsed file, with edition identity locked through commit."""
        async with transaction(self._engine) as tx:
            edition = await self._record_edition(tx, metadata, lock=True)
            if edition is not None:
                existing = await self._docs.find_by_edition_id(tx, edition.id)
                if existing is not None:
                    return existing, [], True
                draft = draft.model_copy(update={"edition_id": edition.id})

            doc = await self._docs.insert(tx, draft)
            if self._document_texts is not None:
                await self._document_texts.put(
                    tx, doc.id, full_text, module.id, module.version
                )
            if self._document_nodes is not None:
                stored_nodes = await self._document_nodes.insert_many(
                    tx,
                    doc.id,
                    build_node_tree(
                        metadata.get("sections") or [],
                        text_length=len(full_text),
                        title=title,
                    ),
                )
                passage_drafts = attach_nodes(passage_drafts, stored_nodes)
            saved_passages = await self._passages.insert_many(
                tx, doc.id, passage_drafts
            )

            passage_ids = [passage.id for passage in saved_passages]
            texts = [passage.text for passage in saved_passages]
            for index in range(0, len(texts), self._embedding_batch_size):
                batch_texts = texts[index : index + self._embedding_batch_size]
                batch_ids = passage_ids[
                    index : index + self._embedding_batch_size
                ]
                embeddings = await self._embedding.embed_batch(batch_texts)
                await self._passages.store_embeddings(
                    tx,
                    batch_ids,
                    embeddings,
                    self._embedding.model_name,
                    self._embedding.model_version,
                    self._embedding.dim,
                )

            await self._passages.index_fts(
                tx, passage_ids, texts, pg_config(language)
            )
        return doc, saved_passages, False

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
            language = self._resolve_language(metadata.get("language"))
            draft = build_document_draft(
                source_path=source_path,
                title=title,
                document_type=module.default_document_type(),
                parser_id=module.id,
                parser_version=module.version,
                metadata=metadata,
                language=language,
            )

            # Chunk
            chunker_id = module.default_chunker()
            passage_drafts = await run_chunking(
                full_text, chunker_id, metadata, parser_id=module.id
            )

            doc, saved_passages, duplicate = await self._store_file_document(
                draft=draft,
                metadata=metadata,
                full_text=full_text,
                module=module,
                passage_drafts=passage_drafts,
                title=title,
                language=language,
            )
            if duplicate:
                logger.info(
                    "ingestion_skipped_edition_duplicate",
                    document_id=str(doc.id),
                    edition_key=metadata.get("edition_key"),
                    source=str(source_path),
                )
                stats["skipped"] += 1
                await self._ingestion_runs.update_item(
                    item.id,
                    status="skipped",
                    document_id=doc.id,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
                return

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
                root = self._forbidden_root(path)
                if root is not None:
                    raise IngestRefused(
                        f"Refusing to ingest {path}: it resolves under the "
                        f"vault ({root})."
                    )
                yield path
            elif path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file() and not child.name.startswith("."):
                        root = self._forbidden_root(child)
                        if root is not None:
                            raise IngestRefused(
                                f"Refusing to ingest {child}: it resolves "
                                f"under the vault ({root})."
                            )
                        yield child
