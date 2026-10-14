"""The doctrinal_claims schema the pack ships can actually run.

These checks hold the YAML to the shape core's registration demands — every
record type quotes the passage, field names shared by two record types agree
on their type, the prompt interpolates the four variables the executor
supplies — and to the declaration plugin.yaml makes for it. Toy text only:
this file, like the schema, carries no reference-shaped locators.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
import yaml
from dilemma import guard
from jinja2 import meta

from research_engine.services.extraction.registration import validate_schema_definition
from research_engine.services.extraction.schemas import build_output_schema

pytestmark = pytest.mark.unit

PACK = guard.pack_root()
SCHEMA_PATH = PACK / "schemas" / "extraction_schemas" / "doctrinal_claims.yaml"
PLUGIN_PATH = PACK / "plugin.yaml"

MODALITIES = [
    "asserts",
    "commands",
    "prophesies",
    "reports_other_view",
    "quotes_scripture",
    "hypothetical",
    "question",
]

CLAIM_FIELDS = {
    "statement": ("string", True),
    "subject": ("string", False),
    "subject_ref": ("string", False),
    "predicate": ("string", False),
    "object": ("string", False),
    "polarity": ("enum", True),
    "modality": ("enum", True),
    "speaker": ("string", True),
    "speaker_ref": ("string", False),
    "scope_quantifier": ("string", False),
    "temporal_scope": ("string", False),
    "group": ("integer", False),
    "group_relation": ("enum", False),
    "topic_keys": ("array", False),
    "evidence": ("evidence_span", True),
    "evidence_2": ("evidence_span", False),
    "evidence_3": ("evidence_span", False),
    "confidence": ("number", True),
}

VOUCH_FIELDS = {
    "statement": ("string", True),
    "kind": ("enum", True),
    "target": ("string", False),
    "target_ref": ("string", False),
    "target_form": ("enum", False),
    "speaker": ("string", True),
    "speaker_ref": ("string", False),
    "modality": ("enum", True),
    "evidence": ("evidence_span", True),
    "evidence_2": ("evidence_span", False),
    "confidence": ("number", True),
}


def _schema() -> dict:
    return yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))


def _record_types(schema: dict) -> dict[str, dict]:
    return {record_type["id"]: record_type for record_type in schema["record_types"]}


def _field_shapes(record_type: dict) -> dict[str, tuple[str, bool]]:
    return {
        name: (spec["type"], bool(spec.get("required", False)))
        for name, spec in record_type["fields"].items()
    }


def test_the_schema_file_parses_with_its_declared_identity() -> None:
    schema = _schema()

    assert schema["id"] == "doctrinal_claims"
    assert schema["version"] == 3
    assert schema["owner"] == "dilemma"
    assert set(_record_types(schema)) == {"claim", "vouch"}


def test_every_record_type_quotes_the_passage_and_every_field_is_described() -> None:
    for record_type in _record_types(_schema()).values():
        evidence = [
            name
            for name, spec in record_type["fields"].items()
            if spec["type"] == "evidence_span"
        ]
        assert evidence, f"{record_type['id']} declares no evidence_span field"
        for name, spec in record_type["fields"].items():
            description = spec.get("description", "")
            assert description.strip(), f"{record_type['id']}.{name} has no description"
            assert "\n" not in description, f"{record_type['id']}.{name} description wraps"


def test_claim_fields_match_the_contract() -> None:
    claim = _record_types(_schema())["claim"]
    fields = claim["fields"]

    assert _field_shapes(claim) == CLAIM_FIELDS
    assert fields["polarity"]["values"] == ["affirms", "denies"]
    assert fields["modality"]["values"] == MODALITIES
    assert fields["group_relation"]["values"] == [
        "composite_act",
        "independent_list",
        "condition",
        "purpose",
    ]
    assert fields["confidence"]["range"] == [0, 1]


def test_vouch_fields_match_the_contract() -> None:
    vouch = _record_types(_schema())["vouch"]
    fields = vouch["fields"]

    assert _field_shapes(vouch) == VOUCH_FIELDS
    assert fields["kind"]["values"] == [
        "affirms_reliability",
        "affirms_authority",
        "claims_prophesied_by",
        "self_reliability",
        "affirms_fallibility",
    ]
    assert fields["target_form"]["values"] == [
        "work_as_given",
        "extant_edition",
        "unspecified",
    ]
    assert fields["modality"]["values"] == MODALITIES
    assert fields["confidence"]["range"] == [0, 1]


def test_shared_field_names_agree_on_type_and_core_accepts_the_schema() -> None:
    schema = _schema()
    record_types = _record_types(schema)
    claim_fields = record_types["claim"]["fields"]
    vouch_fields = record_types["vouch"]["fields"]

    shared = set(claim_fields) & set(vouch_fields)
    assert shared == {
        "statement",
        "speaker",
        "speaker_ref",
        "modality",
        "evidence",
        "evidence_2",
        "confidence",
    }
    for name in shared:
        assert claim_fields[name]["type"] == vouch_fields[name]["type"], name
    assert claim_fields["modality"]["values"] == vouch_fields["modality"]["values"]
    assert claim_fields["confidence"]["range"] == vouch_fields["confidence"]["range"]

    # The merger core runs at registration rejects disagreeing base types.
    output = build_output_schema(schema)
    items = output["properties"]["records"]["items"]["properties"]
    assert items["record_type"]["enum"] == ["claim", "vouch"]
    assert set(items["fields"]["properties"]) == set(claim_fields) | set(vouch_fields)
    validate_schema_definition(schema, schema["prompt"])


def test_the_prompt_interpolates_all_four_variables() -> None:
    prompt = _schema()["prompt"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)

    assert meta.find_undeclared_variables(env.parse(prompt)) == {
        "entity_hints",
        "context_before",
        "passage_text",
        "context_after",
    }

    rendered = env.from_string(prompt).render(
        entity_hints="river-1 | The River | thing | | the wide one",
        context_before="Before the bend, the path was dry.",
        passage_text="The river is wide.",
        context_after="After the bend, the path ends.",
    )

    assert "river-1 | The River | thing" in rendered
    assert "Before the bend, the path was dry." in rendered
    assert "The river is wide." in rendered
    assert "After the bend, the path ends." in rendered
    assert "{{" not in rendered


def test_the_prompt_teaches_atomicity_and_always_names_a_speaker() -> None:
    prompt = _schema()["prompt"]

    assert "ATOMICITY" in prompt
    assert "composite_act" in prompt
    assert "ALWAYS state one" in prompt
    assert "A vouch missing any of these is rejected" in prompt


def test_plugin_yaml_declares_the_schema_it_ships() -> None:
    schema = _schema()
    manifest = yaml.safe_load(PLUGIN_PATH.read_text(encoding="utf-8"))

    assert manifest["provides"]["extraction_schemas"] == [
        {
            "id": "doctrinal_claims",
            "version": 3,
            "file": "schemas/extraction_schemas/doctrinal_claims.yaml",
        }
    ]
    declared = manifest["provides"]["extraction_schemas"][0]
    assert declared["id"] == schema["id"]
    assert declared["version"] == schema["version"]
    assert (PACK / declared["file"]).is_file()


def test_the_schema_and_this_test_are_guard_clean() -> None:
    mine = [SCHEMA_PATH, Path(__file__).resolve()]
    for path in mine:
        text = path.read_text(encoding="utf-8")
        assert not guard.LOCATOR.search(text), f"reference-shaped locator in {path.name}"
        assert not guard.QURAN_REF.search(text), f"qualified reference in {path.name}"

    found = [
        violation
        for violation in guard.structural_violations(mine)
        if Path(violation.path).name in {path.name for path in mine}
    ]
    assert [str(violation) for violation in found] == []
