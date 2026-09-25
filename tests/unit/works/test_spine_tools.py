"""Phase-1 tool envelopes: unconfigured, bad input, and refusals answer, not crash."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from research_engine.domain.errors import (
    FrozenRevisionError,
    NotFoundError,
    StaleWriteError,
)
from research_engine.mcp.tools import (
    work_block_upsert,
    work_cite,
    work_create,
    work_export,
    work_freeze,
    work_get,
    work_import,
    work_link,
    work_promote,
    work_trace,
    work_validate,
)
from research_engine.services.works.attach import AttachedItem, AttachRefused, CitationAttached
from research_engine.services.works.drafting import ImportDiff, ImportRefused
from research_engine.services.works.publication import FreezeBlocked, RevisionSealed
from research_engine.services.works.trace import TraceNode
from research_engine.services.works.validate import GateResult, ValidationReport
from research_engine.services.works.work_service import (
    BlockWritten,
    LinkWritten,
    WorkCreated,
)

pytestmark = pytest.mark.unit

KEY = "11111111-1111-1111-1111-111111111111"
WORK = "33333333-3333-3333-3333-333333333333"


class _FakeWorkService:
    def __init__(self, **behaviour: Any) -> None:
        self._behaviour = behaviour

    async def create(self, **kwargs: Any) -> Any:
        return self._result("create", **kwargs)

    async def get(self, **kwargs: Any) -> Any:
        return self._result("get", **kwargs)

    async def upsert_block(self, **kwargs: Any) -> Any:
        return self._result("upsert_block", **kwargs)

    async def link(self, **kwargs: Any) -> Any:
        return self._result("link", **kwargs)

    def _result(self, name: str, **kwargs: Any) -> Any:
        outcome = self._behaviour[name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestWorkCreateTool:
    @pytest.mark.asyncio
    async def test_taken_slug(self):
        container = SimpleNamespace(
            work_service=_FakeWorkService(create=ValueError("slug 's' is taken"))
        )

        result = await work_create.handler(container, slug="s", title="t", work_type="essay")

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_ok(self):
        created = WorkCreated(
            work_id=UUID(WORK), slug="s", revision_id=UUID(KEY),
            revision_number=1, state="draft",
        )
        container = SimpleNamespace(work_service=_FakeWorkService(create=created))

        result = await work_create.handler(container, slug="s", title="t", work_type="essay")

        assert result["slug"] == "s"
        assert result["revision_number"] == 1


class TestWorkGetTool:
    @pytest.mark.asyncio
    async def test_needs_a_selector(self):
        container = SimpleNamespace(work_service=_FakeWorkService(get={}))

        result = await work_get.handler(container)

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_bad_uuid(self):
        container = SimpleNamespace(work_service=_FakeWorkService(get={}))

        result = await work_get.handler(container, work_id="not-a-uuid")

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_missing_work(self):
        container = SimpleNamespace(
            work_service=_FakeWorkService(get=NotFoundError("work", "s"))
        )

        result = await work_get.handler(container, slug="s")

        assert result["error"]["code"] == "not_found"


class TestWorkBlockUpsertTool:
    def _container(self, outcome: Any) -> SimpleNamespace:
        return SimpleNamespace(work_service=_FakeWorkService(upsert_block=outcome))

    @pytest.mark.asyncio
    async def test_bad_key(self):
        result = await work_block_upsert.handler(
            self._container(None), slug="s", position=0,
            block_type="paragraph", body_markdown="x", block_key="nope",
        )

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_stale_write_is_a_conflict(self):
        container = self._container(StaleWriteError("changed under you"))

        result = await work_block_upsert.handler(
            container, slug="s", position=0, block_type="paragraph",
            body_markdown="x", block_key=KEY, expected_updated_at="then",
        )

        assert result["error"]["code"] == "conflict"

    @pytest.mark.asyncio
    async def test_frozen_revision_is_a_conflict(self):
        container = self._container(FrozenRevisionError("not draft"))

        result = await work_block_upsert.handler(
            container, slug="s", position=0, block_type="paragraph", body_markdown="x"
        )

        assert result["error"]["code"] == "conflict"

    @pytest.mark.asyncio
    async def test_ok(self):
        written = BlockWritten(block_id=UUID(WORK), block_key=UUID(KEY), updated_at=None)

        result = await work_block_upsert.handler(
            self._container(written), slug="s", position=0,
            block_type="paragraph", body_markdown="x",
        )

        assert result["block_key"] == KEY


class TestWorkCiteTool:
    def _attached(self) -> CitationAttached:
        return CitationAttached(
            occurrence_id=UUID(WORK), citation_key=UUID(KEY),
            marker="{{cite:" + KEY + "}}",
            item=AttachedItem(verify_status="exact", edition_key="DABAR_2026"),
        )

    @pytest.mark.asyncio
    async def test_bad_key(self):
        container = SimpleNamespace(citation_service=object())

        result = await work_cite.handler(
            container, slug="s", block_key="nope", intent="support"
        )

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_bad_window(self):
        container = SimpleNamespace(citation_service=object())

        result = await work_cite.handler(
            container, slug="s", block_key=KEY, intent="support",
            window={"char_start": 10, "char_end": 5},
        )

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_refusal_names_its_rule(self):
        async def attach(**kwargs: Any) -> Any:
            raise AttachRefused("AUTH_SPAN_NOT_NARROWED", "narrow it")
        container = SimpleNamespace(citation_service=SimpleNamespace(attach=attach))

        result = await work_cite.handler(
            container, slug="s", block_key=KEY, intent="quotation", quote="x"
        )

        assert result["error"]["code"] == "validation_error"
        assert result["error"]["details"]["rule_id"] == "AUTH_SPAN_NOT_NARROWED"

    @pytest.mark.asyncio
    async def test_ok(self):
        async def attach(**kwargs: Any) -> Any:
            return self._attached()
        container = SimpleNamespace(citation_service=SimpleNamespace(attach=attach))

        result = await work_cite.handler(
            container, slug="s", block_key=KEY, intent="support", edition_key="DABAR_2026"
        )

        assert result["marker"] == "{{cite:" + KEY + "}}"
        assert result["warnings"] == []


class TestWorkLinkTool:
    @pytest.mark.asyncio
    async def test_needs_one_target(self):
        async def link(**kwargs: Any) -> Any:
            raise ValueError("exactly one target")
        container = SimpleNamespace(work_service=SimpleNamespace(link=link))

        result = await work_link.handler(container, slug="s", block_key=KEY, relation="discusses")

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_ok(self):
        written = LinkWritten(kind="entity", target_id=UUID(WORK), relation="renders")
        async def link(**kwargs: Any) -> Any:
            return written
        container = SimpleNamespace(work_service=SimpleNamespace(link=link))

        result = await work_link.handler(
            container, slug="s", block_key=KEY, relation="renders", entity_id=WORK
        )

        assert result["kind"] == "entity"


class TestWorkValidateTool:
    def _report(self) -> ValidationReport:
        return ValidationReport(
            work="s", revision_number=1, state="draft",
            gate=GateResult(name="freeze", passed=True, blockers=[]),
        )

    @pytest.mark.asyncio
    async def test_bad_gate(self):
        async def validate(**kwargs: Any) -> Any:
            raise ValueError("Unknown gate 'someday'")
        container = SimpleNamespace(work_validation=SimpleNamespace(validate=validate))

        result = await work_validate.handler(container, slug="s", gate="someday")  # type: ignore[arg-type]

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_ok(self):
        async def validate(**kwargs: Any) -> Any:
            return self._report()
        container = SimpleNamespace(work_validation=SimpleNamespace(validate=validate))

        result = await work_validate.handler(container, slug="s", gate="freeze")

        assert result["gate"]["passed"] is True


class TestWorkTraceTool:
    @pytest.mark.asyncio
    async def test_needs_one_selector(self):
        async def trace(**kwargs: Any) -> Any:
            raise ValueError("exactly one selector")
        container = SimpleNamespace(work_trace=SimpleNamespace(trace=trace))

        result = await work_trace.handler(container)

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_ok(self):
        node = TraceNode(kind="work", id=WORK, label="s rev 1 (draft)")
        async def trace(**kwargs: Any) -> Any:
            return node
        container = SimpleNamespace(work_trace=SimpleNamespace(trace=trace))

        result = await work_trace.handler(container, slug="s")

        assert result["kind"] == "work"


class TestWorkFreezeTool:
    @pytest.mark.asyncio
    async def test_bad_waiver_shape(self):
        container = SimpleNamespace(work_publication=object())

        result = await work_freeze.handler(container, slug="s", waivers=[{"rule_id": "X"}])

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_blockers_refuse(self):
        async def freeze(**kwargs: Any) -> Any:
            raise FreezeBlocked(["AUTH_QUOTE_UNVERIFIED"])
        container = SimpleNamespace(work_publication=SimpleNamespace(freeze=freeze))

        result = await work_freeze.handler(container, slug="s")

        assert result["error"]["code"] == "validation_error"
        assert result["error"]["details"]["blockers"] == ["AUTH_QUOTE_UNVERIFIED"]

    @pytest.mark.asyncio
    async def test_ok(self):
        sealed = RevisionSealed(
            revision_id=UUID(KEY), revision_number=1,
            content_hash="ab" * 32, state="frozen",
        )
        async def freeze(**kwargs: Any) -> Any:
            return sealed
        container = SimpleNamespace(work_publication=SimpleNamespace(freeze=freeze))

        result = await work_freeze.handler(container, slug="s", message="first")

        assert result["state"] == "frozen"


class _FakeExportService:
    """WorkExportService double: per-method outcomes or exceptions."""

    def __init__(self, **behaviour: Any) -> None:
        self._behaviour = behaviour

    async def export_draft(self, **kwargs: Any) -> Any:
        return self._result("export_draft", **kwargs)

    async def import_draft(self, **kwargs: Any) -> Any:
        return self._result("import_draft", **kwargs)

    async def promote(self, **kwargs: Any) -> Any:
        return self._result("promote", **kwargs)

    def _result(self, name: str, **kwargs: Any) -> Any:
        outcome = self._behaviour[name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestWorkExportTool:
    @pytest.mark.asyncio
    async def test_ok(self):
        container = SimpleNamespace(
            work_export=_FakeExportService(export_draft="# Beat\n")
        )

        result = await work_export.handler(container, slug="s")

        assert result == {"markdown": "# Beat\n"}

    @pytest.mark.asyncio
    async def test_missing_revision(self):
        container = SimpleNamespace(
            work_export=_FakeExportService(
                export_draft=NotFoundError("work_revision", "s revision 9")
            )
        )

        result = await work_export.handler(container, slug="s", revision=9)

        assert result["error"]["code"] == "not_found"


class TestWorkImportTool:
    def _diff(self) -> ImportDiff:
        return ImportDiff(revision_id=UUID(KEY), revision_number=2)

    @pytest.mark.asyncio
    async def test_ok(self):
        container = SimpleNamespace(
            work_export=_FakeExportService(import_draft=self._diff())
        )

        result = await work_import.handler(container, slug="s", markdown="m")

        assert result["revision_number"] == 2
        assert result["changes"] == []

    @pytest.mark.asyncio
    async def test_refusal_carries_rule_id(self):
        refused = ImportRefused(
            "AUTH_PARENT_REVISION_MISMATCH",
            "the work changed since this file was exported",
            detail={"file_base": "old", "current_base": "new",
                    "current_revision": 2},
        )
        container = SimpleNamespace(
            work_export=_FakeExportService(import_draft=refused)
        )

        result = await work_import.handler(container, slug="s", markdown="m")

        assert result["error"]["code"] == "validation_error"
        assert result["error"]["details"]["rule_id"] == "AUTH_PARENT_REVISION_MISMATCH"
        assert result["error"]["details"]["current_base"] == "new"


class TestWorkPromoteTool:
    @pytest.mark.asyncio
    async def test_ok(self):
        diff = ImportDiff(revision_id=UUID(KEY), revision_number=1)
        container = SimpleNamespace(
            work_export=_FakeExportService(promote=diff)
        )

        result = await work_promote.handler(
            container, slug="s", title="T", work_type="script", markdown="m"
        )

        assert result["revision_number"] == 1

    @pytest.mark.asyncio
    async def test_taken_slug(self):
        container = SimpleNamespace(
            work_export=_FakeExportService(promote=ValueError("slug 's' is taken"))
        )

        result = await work_promote.handler(
            container, slug="s", title="T", work_type="script", markdown="m"
        )

        assert result["error"]["code"] == "invalid_input"

    @pytest.mark.asyncio
    async def test_refusal_carries_rule_id(self):
        refused = ImportRefused(
            "AUTH_CITATION_MARKER_DANGLING",
            "Marker {{cite:k}} matches no occurrence",
            detail={"citation_key": "k"},
        )
        container = SimpleNamespace(
            work_export=_FakeExportService(promote=refused)
        )

        result = await work_promote.handler(
            container, slug="s", title="T", work_type="script", markdown="m"
        )

        assert result["error"]["code"] == "validation_error"
        assert result["error"]["details"]["rule_id"] == "AUTH_CITATION_MARKER_DANGLING"
