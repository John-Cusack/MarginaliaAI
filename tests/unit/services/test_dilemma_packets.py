"""Packet estimates and sampling refusals, without a database.

`build_packets` reads the profile, its roles, and the sample through three
SQL statements, so a scripted fake connection answers them. What is under
test is the refusal semantics — a blind profile, an edition the profile gives
no role, a stratum the edition cannot fill — and the packet shape, not the
SQL, which the integration suite exercises against a real database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_engine.domain.extractions import ExtractionSchema
from research_engine.services.dilemma.packets import build_packets, estimate

pytestmark = pytest.mark.asyncio

PROFILE_ID = uuid4()

SCHEMA = ExtractionSchema(
    id=uuid4(),
    name="packet_claims",
    version=1,
    owner="test",
    schema={
        "record_types": [
            {
                "id": "claim",
                "fields": {
                    "statement": {"type": "string", "required": True},
                    "evidence": {"type": "evidence_span", "required": True},
                },
            }
        ]
    },
    prompt_template=(
        "Before: {{ context_before }}\nPassage: {{ passage_text }}\n"
        "After: {{ context_after }}\nHints: {{ entity_hints }}"
    ),
    created_at=datetime.now(UTC),
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Conn:
    """Answers build_packets' three statements from scripted state."""

    def __init__(self, engine):
        self._engine = engine

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "FROM dilemma.profiles" in sql:
            return _Result(
                [self._engine.profile] if self._engine.profile else []
            )
        if "FROM dilemma.source_roles" in sql:
            has = params["edition_key"] in self._engine.roles
            return _Result([SimpleNamespace()] if has else [])
        if "FROM core.passages" in sql:
            rows = self._engine.passages.get(params["edition_key"], [])
            return _Result(rows[: params["n"]])
        raise AssertionError(f"unexpected SQL: {sql}")


class _Engine:
    def __init__(self, *, settings=None, roles=(), passages=None, known=True):
        self.profile = (
            SimpleNamespace(id=PROFILE_ID, settings=settings or {})
            if known
            else None
        )
        self.roles = set(roles)
        self.passages = passages or {}

    @asynccontextmanager
    async def connect(self):
        yield _Conn(self)


class _Schemas:
    async def get_by_name_version(self, name, version):
        return SCHEMA if name == SCHEMA.name else None


class _Passages:
    async def get_context(self, passage_id, before=0, after=0):
        return (
            [SimpleNamespace(text="before text")] if before else [],
            SimpleNamespace(text="the passage"),
            [SimpleNamespace(text="after text")] if after else [],
        )


def _row(edition_key: str = "ED-1"):
    return SimpleNamespace(
        passage_id=uuid4(),
        document_id=uuid4(),
        locator={"page": 3},
        text="the passage",
        edition_key=edition_key,
    )


async def _build(engine, **overrides):
    given = {
        "profile_key": "reader",
        "schema_ref": "packet_claims:1",
        "strata": {"ED-1": 1},
        "seed": 1,
        "context_before": 1,
        "context_after": 1,
        "entity_hints": "some hints",
    }
    given.update(overrides)
    return await build_packets(engine, _Passages(), _Schemas(), **given)


def test_estimate_counts_tokens_and_time() -> None:
    packets = [{"prompt": "x" * 8}, {"prompt": "y" * 9}]

    result = estimate(packets, seconds_per_item=20)

    assert result == {
        "items": 2,
        "input_tokens_est": 5,  # ceil(8/4) + ceil(9/4)
        "output_tokens_est": 440,
        "seconds_est": 40,
    }


def test_estimate_of_nothing_is_nothing() -> None:
    assert estimate([], seconds_per_item=20) == {
        "items": 0,
        "input_tokens_est": 0,
        "output_tokens_est": 0,
        "seconds_est": 0,
    }


async def test_build_packets_refuses_an_unknown_profile() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        await _build(_Engine(known=False))


async def test_build_packets_refuses_a_blind_profile() -> None:
    with pytest.raises(ValueError, match="blind"):
        await _build(_Engine(settings={"blind": True}, roles={"ED-1"}))


async def test_build_packets_refuses_an_edition_with_no_role() -> None:
    with pytest.raises(ValueError, match="no role under profile"):
        await _build(_Engine(roles=set(), passages={"ED-1": [_row()]}))


async def test_build_packets_refuses_a_stratum_the_edition_cannot_fill() -> None:
    with pytest.raises(ValueError, match="fewer than the 2 requested"):
        await _build(
            _Engine(roles={"ED-1"}, passages={"ED-1": [_row()]}),
            strata={"ED-1": 2},
        )


async def test_build_packets_refuses_an_unknown_schema() -> None:
    with pytest.raises(ValueError, match="Schema not found"):
        await _build(
            _Engine(roles={"ED-1"}, passages={"ED-1": [_row()]}),
            schema_ref="nope:1",
        )


async def test_a_packet_carries_the_rendered_prompt_and_output_schema() -> None:
    row = _row()

    [packet] = await _build(
        _Engine(roles={"ED-1"}, passages={"ED-1": [row]})
    )

    assert packet["passage_id"] == str(row.passage_id)
    assert packet["document_id"] == str(row.document_id)
    assert packet["edition_key"] == "ED-1"
    assert packet["locator"] == {"page": 3}
    assert packet["passage_text"] == "the passage"
    assert "Before: before text" in packet["prompt"]
    assert "Passage: the passage" in packet["prompt"]
    assert "After: after text" in packet["prompt"]
    assert "Hints: some hints" in packet["prompt"]
    assert packet["output_schema"]
