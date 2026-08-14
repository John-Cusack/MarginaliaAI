"""Extraction cache key computation."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


def compute_cache_key(
    passage_id: UUID,
    schema_id: UUID,
    schema_version: int,
    extractor_version: str,
    llm_model: str,
) -> str:
    """Compute a deterministic cache key for an extraction."""
    raw = f"{passage_id}:{schema_id}:{schema_version}:{extractor_version}:{llm_model}"
    return hashlib.sha256(raw.encode()).hexdigest()
