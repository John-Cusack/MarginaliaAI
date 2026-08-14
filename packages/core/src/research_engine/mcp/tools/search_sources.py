"""search_sources tool — fan out a structured query across plugin source providers.

Returns a unified, deduplicated list of ``SourceMatch`` records the agent can
walk to assemble an ingestion plan. Read-only: each match carries an
``ingest_action`` descriptor instead of triggering ingestion directly.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog

from research_engine.domain.source_search import (
    Availability,
    SourceMatch,
    SourceQuery,
    SourceSearchProvider,
    availability_rank,
)

logger = structlog.get_logger()

TOOL_NAME = "search_sources"
TOOL_DESCRIPTION = (
    "Search across all installed source plugins (Logos library, YourCloudLibrary, "
    "academic journals, Kindle, etc.) for a title/author/citation. Returns a "
    "deduplicated list of matches with availability info ('in_corpus', "
    "'ingestable', 'borrowable', 'purchasable', 'external_only') and a structured "
    "ingest_action descriptor each match can be executed against. Use this to "
    "turn a reading list into an ingestion plan in one round trip."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Free-text search: title, author, or full citation.",
        },
        "title": {"type": "string", "description": "Optional parsed title hint."},
        "author": {"type": "string", "description": "Optional parsed author hint."},
        "year": {"type": "integer", "description": "Optional publication year hint."},
        "doi": {"type": "string", "description": "Optional DOI for exact lookup."},
        "isbn": {"type": "string", "description": "Optional ISBN for exact lookup."},
        "asin": {"type": "string", "description": "Optional Amazon ASIN."},
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Restrict to specific provider names (e.g. ['logos','acad']). "
                "Omit to fan out to all registered providers."
            ),
        },
        "limit": {
            "type": "integer",
            "default": 10,
            "description": "Max matches per provider.",
        },
        "timeout_s": {
            "type": "number",
            "default": 8.0,
            "description": "Per-provider timeout in seconds.",
        },
    },
    "required": ["query"],
}


_PUNCT_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for dedup keys."""
    out = _PUNCT_RE.sub(" ", title.lower())
    return _WS_RE.sub(" ", out).strip()


def _dedup_key(match: SourceMatch) -> str:
    """Best-available identity key. DOI > ISBN > title+first-author+year.

    Prefers the first-class ``doi``/``isbn`` fields, falling back to the
    ``metadata`` dict so providers that stash identity there still dedupe.
    """
    doi = match.doi or match.metadata.get("doi") or ""
    if doi:
        return f"doi:{doi.lower()}"
    isbn = match.isbn or match.metadata.get("isbn") or ""
    if isbn:
        return f"isbn:{isbn.replace('-', '').replace(' ', '')}"
    first_author = match.authors[0] if match.authors else ""
    return f"t:{_normalize_title(match.title)}|a:{_normalize_title(first_author)}|y:{match.year or ''}"


async def _run_provider(
    provider: SourceSearchProvider,
    query: SourceQuery,
    *,
    limit: int,
    timeout_s: float,
) -> list[SourceMatch]:
    """Call one provider with a timeout. Errors and timeouts return []."""
    try:
        return await asyncio.wait_for(
            provider.search(query, limit=limit), timeout=timeout_s
        )
    except TimeoutError:
        logger.warning("source_search_timeout", provider=provider.plugin_name, timeout_s=timeout_s)
        return []
    except Exception as e:
        logger.warning("source_search_failed", provider=provider.plugin_name, error=str(e))
        return []


async def _enrich_with_corpus(
    container: Any, matches: list[SourceMatch]
) -> list[SourceMatch]:
    """Mark matches whose work is already in the corpus.

    Uses ``IngestionOrchestrator.find_existing`` with an **exact** ``source``
    match against a per-match source hint providers stash in
    ``metadata['corpus_source']`` (or the legacy ``corpus_source_pattern`` key).
    Exact matching is deliberate: a substring match (e.g. '10.1/1' inside
    '10.1/100') would wrongly flag an unrelated document as already-ingested and
    silently skip ingesting the real source. Best-effort — providers without a
    stable source hint simply skip enrichment.
    """
    ingestion = getattr(container, "ingestion", None)
    if ingestion is None or not hasattr(ingestion, "find_existing"):
        return matches

    for m in matches:
        source = m.metadata.get("corpus_source") or m.metadata.get("corpus_source_pattern")
        if not source:
            continue
        try:
            existing = await ingestion.find_existing(source=source)
        except Exception as e:
            logger.warning("find_existing_failed", source=source, error=str(e))
            continue
        if existing:
            m.availability = Availability.in_corpus
            m.document_id = existing[0].get("document_id")
    return matches


def _merge(matches: list[SourceMatch]) -> list[SourceMatch]:
    """Group by dedup key, keep the highest-availability winner, attach the rest
    under metadata['also_available_via']."""
    by_key: dict[str, list[SourceMatch]] = {}
    for m in matches:
        by_key.setdefault(_dedup_key(m), []).append(m)

    merged: list[SourceMatch] = []
    for group in by_key.values():
        group.sort(
            key=lambda m: (availability_rank(m.availability), m.confidence),
            reverse=True,
        )
        winner = group[0]
        if len(group) > 1:
            also = winner.metadata.setdefault("also_available_via", [])
            for other in group[1:]:
                also.append({
                    "plugin": other.plugin,
                    "source_id": other.source_id,
                    "availability": other.availability.value,
                    "ingest_action": other.ingest_action.model_dump() if other.ingest_action else None,
                })
        merged.append(winner)

    # Stable ranking for the caller.
    merged.sort(
        key=lambda m: (availability_rank(m.availability), m.confidence),
        reverse=True,
    )
    return merged


async def handler(
    container: Any,
    *,
    query: str,
    title: str | None = None,
    author: str | None = None,
    year: int | None = None,
    doi: str | None = None,
    isbn: str | None = None,
    asin: str | None = None,
    sources: list[str] | None = None,
    limit: int = 10,
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    try:
        registry = container.registry
        providers = registry.get_source_search_providers()
        available = sorted(providers.keys())
        if sources:
            providers = {k: v for k, v in providers.items() if k in sources}

        if not providers:
            if available:
                note = (
                    f"None of the requested sources {sources} matched a registered "
                    f"provider. Available providers: {available}."
                )
            else:
                note = (
                    "No source search providers are registered. "
                    "Install/enable a plugin that contributes provides.source_search."
                )
            return {
                "matches": [],
                "providers_queried": [],
                "note": note,
            }

        sq = SourceQuery(
            query=query,
            title=title,
            author=author,
            year=year,
            doi=doi,
            isbn=isbn,
            asin=asin,
        )

        results = await asyncio.gather(
            *(
                _run_provider(p, sq, limit=limit, timeout_s=timeout_s)
                for p in providers.values()
            ),
            return_exceptions=False,  # _run_provider already swallows
        )

        flat: list[SourceMatch] = [m for batch in results for m in batch]
        flat = await _enrich_with_corpus(container, flat)
        merged = _merge(flat)

        return {
            "matches": [m.model_dump(mode="json") for m in merged],
            "providers_queried": list(providers.keys()),
            "raw_count": len(flat),
            "deduped_count": len(merged),
        }
    except Exception as e:
        logger.error("search_sources_error", error=str(e))
        return {"error": {"code": "search_sources_failed", "message": str(e), "details": None}}
