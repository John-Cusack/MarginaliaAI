"""Which works cite this source — a grep with a stable shape.

Phase 0 scans files under the works directory and filters front-matter
entries (or `claims:` refs) by one selector. When the Step 4 mirror exists
the same tool queries it instead and reports `"source": "mirror"`; that
switch lives in the tool, keyed off a container flag — here everything
reports `"files"`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from research_engine.services.works.files import WorkFileReader

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

logger = structlog.get_logger()


class WorkCitationFinder:
    """Entries (or claim refs) across every work file, filtered by one key."""

    def __init__(self, works_dir: Path) -> None:
        self._reader = WorkFileReader(works_dir)

    async def find(
        self,
        *,
        document_id: UUID | None = None,
        edition_key: str | None = None,
        claim_ref: str | None = None,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        for work_path in self._reader.list_works():
            try:
                work = self._reader.read(work_path)
            except Exception as exc:  # noqa: BLE001 - one bad file hides no citation
                logger.warning("work_unreadable", work_path=work_path, error=str(exc))
                continue
            front = work.front_matter
            if claim_ref is not None:
                if claim_ref in front.claims:
                    matches.append({
                        "work_path": work.work_path,
                        "work": front.work,
                        "title": front.title,
                        "status": front.status.value,
                        "citation_id": None,
                        "intent": None,
                        "document_id": None,
                        "char_start": None,
                        "char_end": None,
                        "quoted_text": None,
                        "claim_ref": claim_ref,
                    })
                continue
            for entry in front.citations:
                if document_id is not None and entry.document_id != document_id:
                    continue
                if edition_key is not None and entry.edition_key != edition_key:
                    continue
                matches.append({
                    "work_path": work.work_path,
                    "work": front.work,
                    "title": front.title,
                    "status": front.status.value,
                    "citation_id": entry.id,
                    "intent": entry.intent.value,
                    "document_id": str(entry.document_id),
                    "char_start": entry.char_start,
                    "char_end": entry.char_end,
                    "quoted_text": entry.quoted_text,
                })
        return {"matches": matches, "source": "files"}
