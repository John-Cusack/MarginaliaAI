"""Get an extraction schema into the database, having first proved it can run.

Nothing could register a schema. ``PGExtractionSchemaRepo`` had an insert, no
caller reached it, and the executor resolves schemas by name from the database —
so the extraction layer was complete except for the step that lets you use it.
Packs were worse off still: the loader parses a pack's declared schemas into an
in-memory registry that nothing read back, so a pack could ship a schema that
could never execute.

Registration is also the right place to be strict. The checks below cost one
round trip here and would otherwise surface as a failed run partway through a
corpus: a prompt that never interpolates the passage, a field type that is not a
JSON Schema type, a record type that quotes nothing and so produces claims no
one can check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from research_engine.domain.errors import ValidationError
from research_engine.domain.extractions import ExtractionSchemaDraft
from research_engine.services.extraction.schemas import (
    EVIDENCE_TYPE,
    build_output_schema,
    evidence_field_names,
    parse_schema_yaml,
    record_type_definitions,
    render_prompt,
)

if TYPE_CHECKING:
    from research_engine.domain.extractions import ExtractionSchema
    from research_engine.plugins.registry import PluginRegistry
    from research_engine.ports.repositories import ExtractionSchemaRepo

logger = structlog.get_logger()

#: Fed through the prompt template at registration so an undefined variable or a
#: template that forgets the passage entirely fails here rather than on the
#: first of ten thousand passages.
_PROBE_TEXT = "PROBE PASSAGE TEXT"


class ExtractionSchemaService:
    """Validate and store extraction schemas, from files or from packs."""

    def __init__(
        self, schemas: ExtractionSchemaRepo, transaction_factory: Any
    ) -> None:
        self._schemas = schemas
        self._transaction = transaction_factory

    async def register(self, draft: ExtractionSchemaDraft) -> ExtractionSchema:
        validate_schema_definition(draft.schema_def, draft.prompt_template)
        async with self._transaction() as tx:
            stored = await self._schemas.save(tx, draft)
        logger.info(
            "extraction_schema_registered",
            name=stored.name,
            version=stored.version,
            owner=stored.owner,
        )
        return stored

    async def register_yaml(
        self, content: str, owner: str | None = None
    ) -> ExtractionSchema:
        return await self.register(draft_from_yaml(content, owner))

    async def sync_packs(self, registry: PluginRegistry) -> list[ExtractionSchema]:
        """Register every schema the installed packs declare.

        A pack's schema is owned by the pack, so re-syncing updates it in place
        and a schema the pack has edited takes effect without a version bump.
        """
        registered: list[ExtractionSchema] = []
        for schema_id, version, definition, plugin in registry.get_extraction_schemas():
            draft = ExtractionSchemaDraft(
                name=schema_id,
                version=version,
                owner=plugin or "unknown",
                schema_def=definition,
                prompt_template=definition.get("prompt", ""),
            )
            registered.append(await self.register(draft))
        return registered


def draft_from_yaml(content: str, owner: str | None = None) -> ExtractionSchemaDraft:
    """Build a draft from schema YAML.

    The authored ``id`` becomes the schema's ``name``: it is what ``extract``
    takes as ``name:version``, and calling the same thing by two names in the
    file and at the command line helps nobody.
    """
    definition = parse_schema_yaml(content)
    if not isinstance(definition, dict):
        raise ValidationError("A schema file must contain a YAML mapping.")

    name = definition.get("id") or definition.get("name")
    if not name:
        raise ValidationError("A schema file needs an 'id'.")
    prompt = definition.get("prompt")
    if not prompt:
        raise ValidationError(
            f"Schema '{name}' has no 'prompt'; there is nothing to ask the model."
        )
    return ExtractionSchemaDraft(
        name=str(name),
        version=int(definition.get("version", 1)),
        owner=owner or str(definition.get("owner", "local")),
        schema_def=definition,
        prompt_template=str(prompt),
    )


def validate_schema_definition(definition: dict[str, Any], prompt: str) -> None:
    """Reject a schema that cannot produce checkable records.

    Raises ``ValidationError`` naming the first problem found.
    """
    record_types = record_type_definitions(definition)
    if not record_types:
        raise ValidationError("A schema needs at least one record_type.")

    for rt_id, record_type in record_types.items():
        if not record_type.get("fields"):
            raise ValidationError(f"Record type '{rt_id}' declares no fields.")
        if not evidence_field_names(record_type):
            raise ValidationError(
                f"Record type '{rt_id}' declares no '{EVIDENCE_TYPE}' field. "
                f"Every record has to quote the passage it came from, or there "
                f"is no way to check it against the corpus later."
            )

    # Raises on a field name two record types disagree about the type of.
    build_output_schema(definition)

    try:
        rendered = render_prompt(prompt, _PROBE_TEXT)
    except Exception as exc:
        raise ValidationError(f"The prompt template does not render: {exc}") from exc
    if _PROBE_TEXT not in rendered:
        raise ValidationError(
            "The prompt template never interpolates {{ passage_text }}, so the "
            "model would be asked to extract from a passage it cannot see."
        )
