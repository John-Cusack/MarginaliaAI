"""Registering a schema, and refusing to register one that cannot be checked.

Registration is the only door into the extraction layer — `extract` resolves a
schema by name from the database — so it is also the last place a schema can be
rejected cheaply. Everything asserted here would otherwise surface partway
through a corpus pass.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from research_engine.domain.errors import ValidationError
from research_engine.domain.extractions import ExtractionSchema
from research_engine.services.extraction.registration import (
    ExtractionSchemaService,
    draft_from_yaml,
    validate_schema_definition,
)
from research_engine.testing.corpus import new_id

GOOD_YAML = """
id: interpretive_claims
version: 2
owner: core
record_types:
  - id: claim
    fields:
      assertion:
        type: string
        required: true
      quote:
        type: evidence_span
        required: true
prompt: |
  Extract interpretive claims from this passage.

  {{ passage_text }}
"""


class FakeSchemaRepo:
    def __init__(self):
        self.saved = []

    async def save(self, tx, draft):
        self.saved.append(draft)
        return ExtractionSchema(
            id=new_id(),
            name=draft.name,
            version=draft.version,
            owner=draft.owner,
            schema=draft.schema_def,
            prompt_template=draft.prompt_template,
            created_at=datetime.now(UTC),
        )


@asynccontextmanager
async def fake_transaction():
    yield SimpleNamespace(conn=None)


class TestDraftFromYAML:
    def test_the_authored_id_becomes_the_name(self):
        """It is what `extract` takes as `name:version`."""
        draft = draft_from_yaml(GOOD_YAML)
        assert (draft.name, draft.version, draft.owner) == (
            "interpretive_claims",
            2,
            "core",
        )
        assert "{{ passage_text }}" in draft.prompt_template

    def test_owner_can_be_overridden(self):
        assert draft_from_yaml(GOOD_YAML, owner="john").owner == "john"

    def test_a_file_without_an_id_is_rejected(self):
        with pytest.raises(ValidationError, match="'id'"):
            draft_from_yaml("version: 1\nprompt: x {{ passage_text }}")

    def test_a_file_without_a_prompt_is_rejected(self):
        with pytest.raises(ValidationError, match="prompt"):
            draft_from_yaml("id: x\nversion: 1")


class TestValidateSchemaDefinition:
    def test_a_good_schema_passes(self):
        draft = draft_from_yaml(GOOD_YAML)
        validate_schema_definition(draft.schema_def, draft.prompt_template)

    def test_a_record_type_that_quotes_nothing_is_rejected(self):
        """The rule that makes the corpus checkable rather than merely stocked.

        A record type with no evidence_span field produces claims with no route
        back to the text they came from — inspectable in the sense that you can
        read them, and useless in the sense that you cannot verify one.
        """
        definition = {
            "record_types": [{"id": "claim", "fields": {"assertion": {"type": "string"}}}],
        }
        with pytest.raises(ValidationError, match="evidence_span"):
            validate_schema_definition(definition, "x {{ passage_text }}")

    def test_a_prompt_that_never_shows_the_passage_is_rejected(self):
        definition = {
            "record_types": [
                {"id": "claim", "fields": {"quote": {"type": "evidence_span"}}}
            ],
        }
        with pytest.raises(ValidationError, match="passage_text"):
            validate_schema_definition(definition, "Extract every claim you can.")

    def test_a_prompt_referring_to_an_undefined_variable_is_rejected(self):
        definition = {
            "record_types": [
                {"id": "claim", "fields": {"quote": {"type": "evidence_span"}}}
            ],
        }
        with pytest.raises(ValidationError, match="does not render"):
            validate_schema_definition(
                definition, "{{ passage_text }} in {{ book_title }}"
            )

    def test_a_record_type_with_no_fields_is_rejected(self):
        with pytest.raises(ValidationError, match="no fields"):
            validate_schema_definition(
                {"record_types": [{"id": "claim"}]}, "{{ passage_text }}"
            )


class TestRegister:
    @pytest.mark.asyncio
    async def test_a_valid_schema_is_stored(self):
        repo = FakeSchemaRepo()
        service = ExtractionSchemaService(repo, fake_transaction)

        schema = await service.register_yaml(GOOD_YAML)

        assert schema.name == "interpretive_claims"
        assert len(repo.saved) == 1

    @pytest.mark.asyncio
    async def test_an_invalid_schema_is_not_stored(self):
        repo = FakeSchemaRepo()
        service = ExtractionSchemaService(repo, fake_transaction)

        with pytest.raises(ValidationError):
            await service.register_yaml("id: x\nversion: 1\nprompt: no passage here")

        assert repo.saved == []


class TestSyncPacks:
    @pytest.mark.asyncio
    async def test_pack_schemas_reach_the_database(self):
        """The loader registers these into memory; nothing read them back.

        The executor resolves schemas from the database, so a pack could declare
        an extraction schema that could never be run — which is the state the
        Logos pack's declared schemas have been in since they were written.
        """
        definition = {
            "id": "scripture_claims",
            "version": 1,
            "record_types": [
                {"id": "claim", "fields": {"quote": {"type": "evidence_span"}}}
            ],
            "prompt": "Extract from {{ passage_text }}",
        }
        registry = SimpleNamespace(
            get_extraction_schemas=lambda: [
                ("scripture_claims", 1, definition, "logos")
            ]
        )
        repo = FakeSchemaRepo()
        service = ExtractionSchemaService(repo, fake_transaction)

        [registered] = await service.sync_packs(registry)

        assert registered.name == "scripture_claims"
        assert registered.owner == "logos"
