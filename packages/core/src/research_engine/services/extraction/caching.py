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

#: Bump when a change to the executor alters what a run produces from unchanged
#: inputs — a different output contract, different validation, different
#: anchoring. Cached rows written by an earlier version stop matching, and the
#: passages they cover are extracted again.
EXTRACTOR_VERSION = "1.0"


def extractor_version(
    schema_version: int, prompt_template: str, llm_model: str
) -> str:
    """The cache dimension for one (schema, prompt, model) combination.

    Human-readable prefix so a row is identifiable at a glance, plus a digest of
    the inputs that a version number does not capture.
    """
    material = "\n".join(
        [EXTRACTOR_VERSION, str(schema_version), llm_model, prompt_template]
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return f"{EXTRACTOR_VERSION}+{digest}"
