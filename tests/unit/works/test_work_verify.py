"""One fixture entry per finding: each rule fires exactly, and only, when it should."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from research_engine.domain.works_files import (
    CitationEntry,
    EntryError,
    WorkFile,
    WorkFrontMatter,
)
from research_engine.services.verification.quote import Tier
from research_engine.services.works.verify import MAX_QUOTE_CHARS, WorkVerifier

pytestmark = pytest.mark.unit

DOC = uuid.uuid4()


class FakeTexts:
    def __init__(self, has_text: bool = True) -> None:
        self._has_text = has_text

    async def lengths(self, document_id):
        return (500, 450) if self._has_text else None


class FakeDocuments:
    """Maps document id to its metadata dict. Absent means unknown."""

    def __init__(self, docs: dict | None = None) -> None:
        self._docs = {DOC: {"edition_key": "DABAR_2026"}} if docs is None else docs

    async def get(self, document_id):
        if document_id not in self._docs:
            return None
        return SimpleNamespace(title="Dabaris", metadata=self._docs[document_id])


class FakePassages:
    """Passage rows as (char_start, char_end) bounds."""

    def __init__(self, regions: tuple = ()) -> None:
        self._regions = regions

    async def covering_span(self, document_id, char_start, char_end):
        return [
            SimpleNamespace(char_start=start, char_end=end)
            for start, end in self._regions
            if start < char_end and end > char_start
        ]

class FakeClaims:
    def __init__(self, existing: set[str] | None = None) -> None:
        self._existing = existing or set()
        self.calls: list[list[str]] = []

    async def existing_refs(self, refs):
        self.calls.append(list(refs))
        return set(refs) & self._existing




class FakeVerification:
    def __init__(
        self,
        tier: Tier = Tier.EXACT,
        span: tuple[int, int] | None = (10, 60),
    ) -> None:
        self.tier = tier
        self.span = span
        self.calls: list = []

    async def verify(self, quote, document_id=None, *, window=None):
        self.calls.append((quote, document_id, window))
        location = None
        if self.span is not None:
            location = SimpleNamespace(char_start=self.span[0], char_end=self.span[1])
        return SimpleNamespace(
            tier=self.tier, location=location, divergence=None, matched_fraction=None
        )


def _entry(**overrides) -> CitationEntry:
    fields = {
        "id": "c1",
        "intent": "quotation",
        "document_id": DOC,
        "char_start": 10,
        "char_end": 60,
        "quoted_text": "a fine sentence here",
        "edition_key": "DABAR_2026",
    }
    fields.update(overrides)
    return CitationEntry(**fields)


def _work(
    entries: list | None = None,
    *,
    markers: list[str] | None = None,
    claims: list[str] | None = None,
    status: str = "draft",
    entry_errors: list | None = None,
) -> WorkFile:
    entries = [_entry()] if entries is None else entries
    return WorkFile(
        work_path="essay.md",
        front_matter=WorkFrontMatter(
            work="W-001",
            title="A fragment",
            type="essay",
            status=status,
            created="2026-09-04",
            claims=claims or [],
            citations=entries,
        ),
        front_matter_sha="0" * 64,
        body="",
        markers=["c1"] if markers is None else markers,
        entry_errors=entry_errors or [],
    )


def _verifier(tmp_path: Path, **fakes) -> WorkVerifier:
    fakes.setdefault("document_texts", FakeTexts())
    fakes.setdefault("documents", FakeDocuments())
    fakes.setdefault("passages", FakePassages())
    fakes.setdefault("verification", FakeVerification())
    fakes.setdefault("claims", FakeClaims())
    return WorkVerifier(
        fakes["document_texts"],
        fakes["documents"],
        fakes["passages"],
        fakes["verification"],
        fakes["claims"],
        tmp_path,
    )


def _for(entry_id: str, report) -> list[str]:
    return [
        finding.rule_id for finding in report.findings if finding.citation_id == entry_id
    ]

class TestClaimRefs:
    @pytest.mark.asyncio
    async def test_known_refs_have_no_finding_and_resolve_as_one_set(self, tmp_path):
        claims = FakeClaims({"KNOWN-001", "KNOWN-002"})
        report = await _verifier(tmp_path, claims=claims).verify_work(
            "essay.md",
            "review",
            _work=_work(claims=["KNOWN-001", "KNOWN-002"]),
        )

        assert "AUTH_CLAIM_UNRESOLVED" not in {
            finding.rule_id for finding in report.findings
        }
        assert claims.calls == [["KNOWN-001", "KNOWN-002"]]

    @pytest.mark.asyncio
    async def test_unknown_ref_is_an_error(self, tmp_path):
        report = await _verifier(tmp_path).verify_work(
            "essay.md",
            "review",
            _work=_work(claims=["MISSING-001"]),
        )

        unresolved = [
            finding
            for finding in report.findings
            if finding.rule_id == "AUTH_CLAIM_UNRESOLVED"
        ]
        assert len(unresolved) == 1
        assert unresolved[0].severity == "error"
        assert report.gate.blockers == ["AUTH_CLAIM_UNRESOLVED"]


class TestEachFinding:
    @pytest.mark.asyncio
    async def test_invalid_entry(self, tmp_path):
        work = _work(
            entries=[], markers=[],
            entry_errors=[EntryError(citation_id="c9", message="boom")],
        )

        report = await _verifier(tmp_path).verify_work("essay.md", _work=work)

        assert _for("c9", report) == ["AUTH_ENTRY_INVALID"]

    @pytest.mark.asyncio
    async def test_unknown_document(self, tmp_path):
        verifier = _verifier(tmp_path, documents=FakeDocuments({}))

        report = await verifier.verify_work("essay.md", _work=_work())

        assert _for("c1", report) == ["AUTH_DOCUMENT_UNKNOWN"]

    @pytest.mark.asyncio
    async def test_uncheckable_source(self, tmp_path):
        verifier = _verifier(tmp_path, document_texts=FakeTexts(has_text=False))

        report = await verifier.verify_work("essay.md", _work=_work())

        assert _for("c1", report) == ["AUTH_SOURCE_UNCHECKABLE"]

    @pytest.mark.asyncio
    async def test_unverified_quote(self, tmp_path):
        verifier = _verifier(tmp_path, verification=FakeVerification(tier=Tier.NEAR))

        report = await verifier.verify_work("essay.md", _work=_work())

        assert _for("c1", report) == ["AUTH_QUOTE_UNVERIFIED"]

    @pytest.mark.asyncio
    async def test_stale_span(self, tmp_path):
        verifier = _verifier(tmp_path, verification=FakeVerification(span=(10, 80)))

        report = await verifier.verify_work("essay.md", _work=_work())

        assert _for("c1", report) == ["AUTH_SOURCE_SPAN_STALE"]

    @pytest.mark.asyncio
    async def test_quotation_on_a_region_is_not_narrowed(self, tmp_path):
        verifier = _verifier(tmp_path, passages=FakePassages(regions=((10, 60),)))

        report = await verifier.verify_work("essay.md", _work=_work())

        assert _for("c1", report) == ["AUTH_SPAN_NOT_NARROWED"]

    @pytest.mark.asyncio
    async def test_long_quotation_is_not_narrowed_without_any_region(self, tmp_path):
        entry = _entry(char_start=0, char_end=MAX_QUOTE_CHARS + 1)
        verifier = _verifier(
            tmp_path,
            verification=FakeVerification(span=(0, MAX_QUOTE_CHARS + 1)),
        )

        report = await verifier.verify_work("essay.md", _work=_work([entry]))

        assert _for("c1", report) == ["AUTH_SPAN_NOT_NARROWED"]

    @pytest.mark.asyncio
    async def test_support_on_a_region_is_a_warning(self, tmp_path):
        entry = _entry(intent="support")
        verifier = _verifier(tmp_path, passages=FakePassages(regions=((10, 60),)))

        report = await verifier.verify_work("essay.md", _work=_work([entry]))

        assert _for("c1", report) == ["AUTH_SPAN_REGION"]
        assert report.gate.passed

    @pytest.mark.asyncio
    async def test_background_on_a_region_is_fine(self, tmp_path):
        entry = _entry(intent="background")
        verifier = _verifier(tmp_path, passages=FakePassages(regions=((10, 60),)))

        report = await verifier.verify_work("essay.md", _work=_work([entry]))

        assert _for("c1", report) == []

    @pytest.mark.asyncio
    async def test_missing_edition_is_a_review_warning(self, tmp_path):
        entry = _entry(edition_key=None)

        report = await _verifier(tmp_path).verify_work("essay.md", _work=_work([entry]))

        assert _for("c1", report) == ["AUTH_CITATION_EDITION_MISSING"]
        assert report.gate.passed

    @pytest.mark.asyncio
    async def test_document_without_a_key_is_unknown(self, tmp_path):
        verifier = _verifier(tmp_path, documents=FakeDocuments({DOC: {}}))

        report = await verifier.verify_work("essay.md", _work=_work())

        assert _for("c1", report) == ["AUTH_EDITION_KEY_UNKNOWN"]

    @pytest.mark.asyncio
    async def test_differing_key_is_a_mismatch(self, tmp_path):
        verifier = _verifier(tmp_path, documents=FakeDocuments({DOC: {"edition_key": "OTHER"}}))

        report = await verifier.verify_work("essay.md", _work=_work())

        assert _for("c1", report) == ["AUTH_EDITION_KEY_MISMATCH"]

    @pytest.mark.asyncio
    async def test_missing_marker(self, tmp_path):
        report = await _verifier(tmp_path).verify_work(
            "essay.md", _work=_work(markers=[])
        )

        assert _for("c1", report) == ["AUTH_CITATION_MARKER_MISSING"]

    @pytest.mark.asyncio
    async def test_dangling_marker(self, tmp_path):
        report = await _verifier(tmp_path).verify_work(
            "essay.md", _work=_work(markers=["c1", "c7"])
        )

        assert _for("c7", report) == ["AUTH_CITATION_MARKER_DANGLING"]


    @pytest.mark.asyncio
    async def test_clean_entry_reports_its_tier(self, tmp_path):
        report = await _verifier(tmp_path).verify_work("essay.md", _work=_work())

        assert report.citations[0].tier == "exact"
        assert report.citations[0].findings == []
        assert report.gate.passed

    @pytest.mark.asyncio
    async def test_verify_receives_the_entry_span_as_its_window(self, tmp_path):
        verification = FakeVerification()
        verifier = _verifier(tmp_path, verification=verification)

        await verifier.verify_work("essay.md", _work=_work())

        assert verification.calls[0][2] == (10, 60)


class TestGates:
    @pytest.mark.asyncio
    async def test_review_fails_on_any_error(self, tmp_path):
        verifier = _verifier(tmp_path, verification=FakeVerification(tier=Tier.NEAR))

        report = await verifier.verify_work("essay.md", "review", _work=_work())

        assert not report.gate.passed
        assert report.gate.blockers == ["AUTH_QUOTE_UNVERIFIED"]

    @pytest.mark.asyncio
    async def test_publish_fails_on_a_missing_edition(self, tmp_path):
        entry = _entry(edition_key=None)

        report = await _verifier(tmp_path).verify_work(
            "essay.md", "publish", _work=_work([entry])
        )

        assert not report.gate.passed
        assert "AUTH_CITATION_EDITION_MISSING" in report.gate.blockers

    @pytest.mark.asyncio
    async def test_published_status_must_earn_it(self, tmp_path):
        entry = _entry(edition_key=None)

        report = await _verifier(tmp_path).verify_work(
            "essay.md", "review", _work=_work([entry], status="published")
        )

        assert "AUTH_STATUS_UNEARNED" in [
            finding.rule_id for finding in report.findings
        ]
        assert not report.gate.passed

    @pytest.mark.asyncio
    async def test_draft_with_warnings_passes_review(self, tmp_path):
        report = await _verifier(tmp_path).verify_work(
            "essay.md", "review", _work=_work(markers=[])
        )

        assert report.gate.passed
        assert report.gate.blockers == []
