"""Link stored documents whose own first page declares a stable identity.

Dry-run is the default. DOI and JSTOR identities can be applied explicitly;
ISBN candidates are report-only because a review's first page commonly prints
the ISBN of the book under review rather than the review's own identity.

Usage from the repository root:
    uv run python scripts/backfill_document_editions.py
    uv run python scripts/backfill_document_editions.py --apply-safe
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from research_engine.config import load_settings
from research_engine.services.ingestion.identifiers import (
    first_page_text,
    with_first_page_identity,
)

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class Candidate:
    document_id: UUID
    title: str
    source: str
    edition_key: str
    metadata_patch: dict[str, str]

    @property
    def kind(self) -> str:
        return self.edition_key.split(":", 1)[0]


async def candidates(engine: AsyncEngine) -> tuple[list[Candidate], int]:
    """Return recoverable identities and the number with no safe candidate."""
    query = sa.text(
        "SELECT d.id, d.title, d.source, d.metadata, t.text "
        "FROM core.documents AS d "
        "LEFT JOIN core.document_texts AS t ON t.document_id = d.id "
        "WHERE d.edition_id IS NULL "
        "ORDER BY d.ingested_at, d.id"
    )
    async with engine.connect() as connection:
        rows = (await connection.execute(query)).mappings().all()

    found: list[Candidate] = []
    for row in rows:
        metadata = dict(row["metadata"] or {})
        text = row["text"] or ""
        enriched = with_first_page_identity(
            metadata, first_page_text(text, metadata.get("pages"))
        )
        edition_key = enriched.get("edition_key")
        if not isinstance(edition_key, str) or not edition_key:
            continue
        patch = {
            key: enriched[key]
            for key in ("edition_key", "doi", "isbn", "jstor_stable_url")
            if isinstance(enriched.get(key), str) and enriched.get(key)
        }
        found.append(
            Candidate(
                document_id=row["id"],
                title=row["title"] or "(untitled)",
                source=row["source"],
                edition_key=edition_key,
                metadata_patch=patch,
            )
        )
    return found, len(rows) - len(found)


async def linked_keys(engine: AsyncEngine) -> set[str]:
    query = sa.text(
        "SELECT DISTINCT e.edition_key "
        "FROM bibliography.editions AS e "
        "JOIN core.documents AS d ON d.edition_id = e.id"
    )
    async with engine.connect() as connection:
        return set((await connection.execute(query)).scalars())


def safe_candidates(
    found: list[Candidate], already_linked: set[str]
) -> tuple[list[Candidate], list[Candidate]]:
    """Separate unambiguous DOI/JSTOR rows from collisions and ISBN review."""
    counts = Counter(candidate.edition_key for candidate in found)
    safe: list[Candidate] = []
    held: list[Candidate] = []
    for candidate in found:
        collision = (
            candidate.edition_key in already_linked
            or counts[candidate.edition_key] > 1
        )
        if candidate.kind in {"doi", "jstor"} and not collision:
            safe.append(candidate)
        else:
            held.append(candidate)
    return safe, held


async def apply_safe(engine: AsyncEngine, approved: list[Candidate]) -> int:
    upsert_edition = sa.text(
        "INSERT INTO bibliography.editions (id, edition_key) "
        "VALUES (gen_random_uuid(), :edition_key) "
        "ON CONFLICT (edition_key) DO UPDATE "
        "SET edition_key = EXCLUDED.edition_key "
        "RETURNING id"
    )
    update_document = sa.text(
        "UPDATE core.documents "
        "SET edition_id = :edition_id, "
        "metadata = (metadata::jsonb || CAST(:patch AS jsonb))::json "
        "WHERE id = :document_id AND edition_id IS NULL"
    )
    written = 0
    async with engine.begin() as connection:
        for candidate in approved:
            edition_id = (
                await connection.execute(
                    upsert_edition, {"edition_key": candidate.edition_key}
                )
            ).scalar_one()
            result = await connection.execute(
                update_document,
                {
                    "document_id": candidate.document_id,
                    "edition_id": edition_id,
                    "patch": json.dumps(candidate.metadata_patch),
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"document changed during backfill: {candidate.document_id}"
                )
            written += 1
    return written


def report(
    found: list[Candidate], safe: list[Candidate], held: list[Candidate], none: int
) -> None:
    by_kind = Counter(candidate.kind for candidate in found)
    print(
        f"unlinked={len(found) + none} candidates={len(found)} "
        f"doi={by_kind['doi']} jstor={by_kind['jstor']} "
        f"isbn={by_kind['isbn']} none={none}"
    )
    print(f"safe={len(safe)} held={len(held)}")
    if held:
        print("\nHELD FOR REVIEW:")
        for candidate in held:
            print(
                f"  {candidate.document_id} | {candidate.edition_key} | "
                f"{candidate.title} | {candidate.source}"
            )


async def run(apply: bool) -> int:
    settings = load_settings()
    engine = create_async_engine(settings.db_url, pool_pre_ping=True)
    try:
        found, none = await candidates(engine)
        safe, held = safe_candidates(found, await linked_keys(engine))
        report(found, safe, held, none)
        if not apply:
            print("\nDry run only; pass --apply-safe to write DOI/JSTOR identities.")
            return 0
        written = await apply_safe(engine, safe)
        print(f"\nApplied {written} DOI/JSTOR identities.")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply-safe",
        action="store_true",
        help="Write non-colliding DOI/JSTOR identities; ISBN remains report-only.",
    )
    return asyncio.run(run(parser.parse_args().apply_safe))


if __name__ == "__main__":
    raise SystemExit(main())
