"""Adapter that conforms the core extraction services to the SDK
``ExtractionClient`` Protocol.

The composition root injects ``ExtractionExecutor`` directly today, but its
surface (``execute(passage_ids, schema_ref, options) -> ExtractionBatch``) does
not match the ``ExtractionClient`` Protocol (``extract(...) -> dict`` +
``query_records(...)``). This adapter bridges the two and adds a
document-scoped convenience: callers may pass ``options={"document_id": ...}``
with an empty ``passage_ids`` to extract over every passage of a document
(used by citation extraction, which only knows the document).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from research_engine.domain.extractions import ExtractionOptions

if TYPE_CHECKING:
    from research_engine.ports.repositories import ExtractionRepo, PassageRepo
    from research_engine.services.extraction.executor import ExtractionExecutor

_OPTION_FIELDS = set(ExtractionOptions.model_fields)


class ExtractionServiceAdapter:
    """Concrete implementation of the SDK ``ExtractionClient`` Protocol."""

    def __init__(
        self,
        executor: ExtractionExecutor,
        passages: PassageRepo,
        extractions: ExtractionRepo,
    ) -> None:
        self._executor = executor
        self._passages = passages
        self._extractions = extractions

    async def extract(
        self, passage_ids: list[UUID], schema: str, options: dict | None = None
    ) -> dict:
        opts = dict(options or {})
        document_id = opts.pop("document_id", None)

        if not passage_ids and document_id is not None:
            doc_passages = await self._passages.get_by_document(UUID(str(document_id)))
            resolved_ids = [p.id for p in doc_passages]
        else:
            resolved_ids = [p if isinstance(p, UUID) else UUID(str(p)) for p in passage_ids]

        if not resolved_ids:
            return {"records": [], "results": []}

        ext_options = ExtractionOptions(
            **{k: v for k, v in opts.items() if k in _OPTION_FIELDS}
        )
        batch = await self._executor.execute(resolved_ids, schema, ext_options)

        records: list[dict] = []
        results: list[dict] = []
        for res in batch.results:
            for rec in res.records:
                records.append({**rec, "passage_id": str(res.passage_id)})
            results.append(
                {
                    "passage_id": str(res.passage_id),
                    "status": res.status,
                    "records": res.records,
                    "from_cache": res.from_cache,
                    "error": res.error,
                }
            )
        return {
            "records": records,
            "results": results,
            "schema_name": batch.schema_name,
            "schema_version": batch.schema_version,
        }

    async def query_records(
        self, record_type: str, filters: dict | None = None, k: int = 100
    ) -> list:
        rows = await self._extractions.query_records(record_type, filters, k)
        return [
            {
                "id": str(r.id),
                "extraction_id": str(r.extraction_id),
                "passage_id": str(r.passage_id),
                "record_type": r.record_type,
                "data": r.data,
                "evidence_start": r.evidence_start,
                "evidence_end": r.evidence_end,
            }
            for r in rows
        ]
