"""Work packets for an extraction run done outside this process.

A packet is one sampled passage with the exact prompt the executor would
render for it and the output schema the answer must satisfy, so an external
runner (a person driving another model, a batch job) does the same work the
executor would and `import_records` can hold its answers to the same
validation. Sampling is seeded and stratified by edition: the same seed picks
the same passages, and a stratum that cannot fill its quota refuses the whole
build rather than quietly skewing the sample toward bigger editions.
"""

from __future__ import annotations

import json
import math
from typing import Any

import sqlalchemy as sa

from research_engine.services.extraction.schemas import (
    build_output_schema,
    render_prompt,
)

#: Rough prompt size in tokens: four characters each. Good enough to gate a
#: run's cost before it starts, which is all the estimate is for.
PROMPT_CHARS_PER_TOKEN = 4

#: Assumed answer size per packet, in output tokens.
OUTPUT_TOKENS_PER_ITEM = 220


def estimate(packets: list[dict[str, Any]], *, seconds_per_item: float) -> dict[str, Any]:
    """What running these packets would cost, roughly: tokens and wall time."""
    items = len(packets)
    return {
        "items": items,
        "input_tokens_est": sum(
            math.ceil(len(packet.get("prompt", "")) / PROMPT_CHARS_PER_TOKEN)
            for packet in packets
        ),
        "output_tokens_est": items * OUTPUT_TOKENS_PER_ITEM,
        "seconds_est": items * seconds_per_item,
    }


async def build_packets(
    engine: Any,
    passages: Any,
    schemas_repo: Any,
    *,
    profile_key: str,
    schema_ref: str,
    strata: dict[str, int],
    seed: int,
    context_before: int,
    context_after: int,
    entity_hints: str,
) -> list[dict[str, Any]]:
    """Sample passages per edition and render one packet for each.

    Refuses (``ValueError``) rather than building a misleading sample when the
    profile is unknown, the profile is blind (a blind run must not be pointed
    at sampled passages chosen for it from outside), an edition holds no role
    under the profile, or an edition yields fewer anchored passages than its
    stratum asks for.
    """
    if not strata:
        raise ValueError("no strata given: name at least one edition and its size")
    for edition_key, count in strata.items():
        if count <= 0:
            raise ValueError(f"stratum for {edition_key!r} must be positive, got {count}")

    async with engine.connect() as conn:
        profile = (
            await conn.execute(
                sa.text(
                    "SELECT id, settings FROM dilemma.profiles WHERE key = :key"
                ),
                {"key": profile_key},
            )
        ).first()
        if profile is None:
            raise ValueError(f"unknown profile: {profile_key!r}")
        settings = profile.settings
        if isinstance(settings, str):
            settings = json.loads(settings)
        if settings and settings.get("blind") is True:
            raise ValueError(
                f"profile {profile_key!r} is blind: extraction packets are "
                "not built for a blind profile"
            )

        for edition_key in strata:
            has_role = (
                await conn.execute(
                    sa.text(
                        "SELECT 1 FROM dilemma.source_roles r "
                        "JOIN bibliography.editions e ON e.id = r.edition_id "
                        "WHERE r.profile_id = :profile_id "
                        "AND e.edition_key = :edition_key LIMIT 1"
                    ),
                    {"profile_id": profile.id, "edition_key": edition_key},
                )
            ).first()
            if has_role is None:
                raise ValueError(
                    f"edition {edition_key!r} has no role under profile "
                    f"{profile_key!r}"
                )

        sampled: list[Any] = []
        for edition_key, count in strata.items():
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT p.id AS passage_id, p.document_id, p.locator, "
                        "p.text, e.edition_key "
                        "FROM core.passages p "
                        "JOIN core.documents d ON d.id = p.document_id "
                        "JOIN bibliography.editions e ON e.id = d.edition_id "
                        "WHERE e.edition_key = :edition_key "
                        "AND p.char_start IS NOT NULL "
                        "ORDER BY md5(:seed || p.id::text) LIMIT :n"
                    ),
                    {"edition_key": edition_key, "seed": str(seed), "n": count},
                )
            ).all()
            if len(rows) < count:
                raise ValueError(
                    f"edition {edition_key!r} yields {len(rows)} anchored "
                    f"passages, fewer than the {count} requested"
                )
            sampled.extend(rows)

    schema = await _schema(schemas_repo, schema_ref)
    output_schema = build_output_schema(schema)

    packets: list[dict[str, Any]] = []
    for row in sampled:
        context_before_text, passage_text, context_after_text = "", row.text, ""
        if context_before or context_after:
            before, target, after = await passages.get_context(
                row.passage_id, before=context_before, after=context_after
            )
            passage_text = target.text
            context_before_text = "\n\n".join(p.text for p in before)
            context_after_text = "\n\n".join(p.text for p in after)
        packets.append(
            {
                "passage_id": str(row.passage_id),
                "document_id": str(row.document_id),
                "edition_key": row.edition_key,
                "locator": row.locator,
                "prompt": render_prompt(
                    schema.prompt_template,
                    passage_text,
                    entity_hints=entity_hints,
                    context_before=context_before_text,
                    context_after=context_after_text,
                ),
                "output_schema": output_schema,
                "passage_text": passage_text,
            }
        )
    return packets


async def _schema(schemas_repo: Any, schema_ref: str) -> Any:
    parts = schema_ref.split(":")
    version = int(parts[1]) if len(parts) > 1 and parts[1] else 1
    schema = await schemas_repo.get_by_name_version(parts[0], version)
    if schema is None:
        raise ValueError(f"Schema not found: {schema_ref}")
    return schema
