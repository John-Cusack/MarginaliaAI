"""Content hashing for frozen revisions — E.8, implemented exactly.

sha256 over the ordered tuple of `(block_key, parent_key, position,
block_type, title, body_markdown)` plus sorted citation and link rows,
rendered as canonical JSON: keys sorted, no whitespace, UTF-8. Ids that turn
over on copy (row ids) and timestamps are excluded — the hash is about
authored content, so a reverify that touches only `verified_at` does not move
it, and `AUTH_REVISION_MUTATED` means what it says.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_content_hash(
    blocks: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    source_links: list[dict[str, Any]],
    entity_links: list[dict[str, Any]],
) -> bytes:
    """Hash authored content. Callers pass rows already in a stable order.

    Blocks arrive in tree order; citations and links are sorted here by
    everything that identifies them except volatile columns.
    """
    payload = {
        "blocks": [
            {
                "block_key": block["block_key"],
                "parent_key": block["parent_key"],
                "position": block["position"],
                "block_type": block["block_type"],
                "title": block["title"],
                "body_markdown": block["body_markdown"],
            }
            for block in blocks
        ],
        "citations": sorted(
            (
                {
                    "block_key": item["block_key"],
                    "citation_key": item["citation_key"],
                    "position": item["position"],
                    "intent": item["intent"],
                    "placement": item["placement"],
                    "edition_id": item["edition_id"],
                    "edition_key": item["edition_key"],
                    "source_span_id": item["source_span_id"],
                    "quoted_text": item["quoted_text"],
                    "verify_status": item["verify_status"],
                    "locator": item["locator"],
                    "prefix": item["prefix"],
                    "suffix": item["suffix"],
                    "suppress_author": item["suppress_author"],
                }
                for item in citations
            ),
            key=lambda item: (
                item["block_key"],
                item["citation_key"],
                item["position"],
            ),
        ),
        "source_links": sorted(
            (
                {
                    "block_key": link["block_key"],
                    "source_span_id": link["source_span_id"],
                    "relation": link["relation"],
                    "confidence": link["confidence"],
                    "note": link["note"],
                }
                for link in source_links
            ),
            key=lambda link: (
                link["block_key"],
                link["source_span_id"],
                link["relation"],
            ),
        ),
        "entity_links": sorted(
            (
                {
                    "block_key": link["block_key"],
                    "entity_id": link["entity_id"],
                    "relation": link["relation"],
                    "surface_form": link["surface_form"],
                }
                for link in entity_links
            ),
            key=lambda link: (
                link["block_key"],
                link["entity_id"],
                link["relation"],
            ),
        ),
    }
    return hashlib.sha256(canonical_json(payload)).digest()
