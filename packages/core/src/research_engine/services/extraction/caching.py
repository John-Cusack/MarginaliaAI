"""What counts as "the same extraction", for caching and for the unique key.

``extractions`` is unique on ``(passage_id, schema_id, extractor_version)``, so
``extractor_version`` has to carry everything that changes the answer. Editing a
prompt without changing the schema's version number is routine while a schema is
being developed, and keying on the version number alone would serve the old
records back forever.

The model belongs in the key too. Its identity is also kept in its own
``llm_model`` column for querying, but two models are two different opinions
about the same passage and both are worth having on the record.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Bump when a change to the executor alters what a run produces from unchanged
#: inputs — a different output contract, different validation, different
#: anchoring. Cached rows written by an earlier version stop matching, and the
#: passages they cover are extracted again.
EXTRACTOR_VERSION = "1.1"


def extractor_version(
    schema_version: int,
    prompt_template: str,
    llm_model: str,
    *,
    schema_def: dict[str, Any] | None = None,
    context_before: int = 0,
    context_after: int = 0,
    entity_hints: str = "",
) -> str:
    """The cache dimension for one (schema, prompt, model, options) combination.

    Human-readable prefix so a row is identifiable at a glance, plus a digest
    of everything that can change the answer: the schema definition itself
    (field edits under an unchanged version number are routine while a schema
    is being developed), the size of the context window shown around the
    passage, and the entity hints interpolated into the prompt.
    """
    canonical_schema_def = (
        json.dumps(schema_def, sort_keys=True, separators=(",", ":"), default=str)
        if schema_def is not None
        else ""
    )
    material = "\n".join(
        [
            EXTRACTOR_VERSION,
            str(schema_version),
            llm_model,
            prompt_template,
            canonical_schema_def,
            str(context_before),
            str(context_after),
            entity_hints,
        ]
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return f"{EXTRACTOR_VERSION}+{digest}"
