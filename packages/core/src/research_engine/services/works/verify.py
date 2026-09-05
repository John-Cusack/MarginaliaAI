"""Verify a work file's citations against the corpus.

A work is review-ready when every entry verifies `exact` or `normalized`
against the span it names. This checks each entry in a fixed order — validity,
document, text, quote, span, region, edition, key, marker — stopping at the
first hard failure per entry, then the work-level checks (dangling markers,
claim refs, unearned status). Rule ids are the contract; messages are for
humans.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, Field

from research_engine.services.verification.quote import Tier
from research_engine.services.works.files import WorkFileReader

if TYPE_CHECKING:
    from pathlib import Path

    from research_engine.domain.works_files import CitationEntry, WorkFile

logger = structlog.get_logger()

#: A quotation or translation citing this many characters or more is a region
#: by size, not by intent — narrowed or not, nobody quotes a thousand
#: characters to say one thing.
MAX_QUOTE_CHARS = 1000

Severity = Literal["error", "warning", "info"]
GateName = Literal["none", "review", "publish"]

#: Intents that must cite a narrowed span, never a whole passage.
_NARROW_INTENTS = frozenset({"quotation", "translation"})
#: Intents warned when they cite a whole passage.
_REGION_WARN_INTENTS = frozenset({"support", "source", "definition"})


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    citation_id: str | None = None
    message: str
    detail: dict[str, Any] | None = None


class CitationReport(BaseModel):
    id: str
    intent: str
    tier: str | None = None
    document_id: str
    char_start: int
    char_end: int
    zotero_key: str | None = None
    findings: list[str] = Field(default_factory=list)


class GateResult(BaseModel):
    name: GateName
    passed: bool
    blockers: list[str] = Field(default_factory=list)


class WorkReport(BaseModel):
    work_path: str
    work: str
    status: str
    citations: list[CitationReport] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    gate: GateResult


class VerifyOutput(BaseModel):
    works: list[WorkReport] = Field(default_factory=list)
    unreadable: list[dict[str, str]] = Field(default_factory=list)
    summary: dict[str, int] = Field(
        default_factory=lambda: {"works": 0, "citations": 0, "errors": 0, "warnings": 0}
    )


class WorkVerifier:
    """Per-entry checks in guide order, stopping at the first hard failure."""

    def __init__(
        self,
        document_texts: Any,
        documents: Any,
        passages: Any,
        verification: Any,
        works_dir: Path,
    ) -> None:
        self._texts = document_texts
        self._documents = documents
        self._passages = passages
        self._verification = verification
        self._reader = WorkFileReader(works_dir)

    async def verify_all(self, gate: GateName = "none") -> VerifyOutput:
        output = VerifyOutput()
        for work_path in self._reader.list_works():
            try:
                work = self._reader.read(work_path)
            except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
                logger.warning("work_unreadable", work_path=work_path, error=str(exc))
                output.unreadable.append({"work_path": work_path, "message": str(exc)})
                continue
            output.works.append(await self.verify_work(work_path, gate, _work=work))
        output.summary = {
            "works": len(output.works),
            "citations": sum(len(work.citations) for work in output.works),
            "errors": sum(
                1
                for work in output.works
                for finding in work.findings
                if finding.severity == "error"
            ),
            "warnings": sum(
                1
                for work in output.works
                for finding in work.findings
                if finding.severity == "warning"
            ),
        }
        return output

    async def verify_work(
        self, work_path: str, gate: GateName = "none", *, _work: WorkFile | None = None
    ) -> WorkReport:
        work = _work if _work is not None else self._reader.read(work_path)
        findings: list[Finding] = []
        citations: list[CitationReport] = []

        for entry_error in work.entry_errors:
            findings.append(
                Finding(
                    rule_id="AUTH_ENTRY_INVALID",
                    severity="error",
                    citation_id=entry_error.citation_id,
                    message=f"Citation entry is invalid: {entry_error.message}",
                )
            )

        markers = set(work.markers)
        entry_ids = {entry.id for entry in work.front_matter.citations}
        for entry in work.front_matter.citations:
            report, entry_findings = await self._check_entry(entry, markers, gate)
            citations.append(report)
            findings.extend(entry_findings)

        for marker in work.markers:
            if marker not in entry_ids and marker not in {
                error.citation_id for error in work.entry_errors
            }:
                findings.append(
                    Finding(
                        rule_id="AUTH_CITATION_MARKER_DANGLING",
                        severity="error",
                        citation_id=marker,
                        message=f"Marker [^{marker}] has no citation entry",
                    )
                )

        for ref in work.front_matter.claims:
            findings.append(
                Finding(
                    rule_id="AUTH_CLAIM_UNRESOLVED",
                    severity="info",
                    message=(
                        f"Claim ref {ref} has no argument.claims row "
                        "(the ledger does not exist yet; grep-able only)"
                    ),
                )
            )

        if work.front_matter.status.value == "published" and not _gate_passes(
            findings, "publish"
        ):
            findings.append(
                Finding(
                    rule_id="AUTH_STATUS_UNEARNED",
                    severity="error",
                    message="The file says `published` but fails the publish gate",
                )
            )

        return WorkReport(
            work_path=work.work_path,
            work=work.front_matter.work,
            status=work.front_matter.status.value,
            citations=citations,
            findings=findings,
            gate=GateResult(
                name=gate,
                passed=_gate_passes(findings, gate),
                blockers=_blockers(findings, gate),
            ),
        )

    async def _check_entry(
        self, entry: CitationEntry, markers: set[str], gate: GateName
    ) -> tuple[CitationReport, list[Finding]]:
        """One entry's checks in order, stopping at the first error."""
        report = CitationReport(
            id=entry.id,
            intent=entry.intent.value,
            document_id=str(entry.document_id),
            char_start=entry.char_start,
            char_end=entry.char_end,
            zotero_key=entry.zotero_key,
        )
        findings: list[Finding] = []

        def fail(finding: Finding) -> tuple[CitationReport, list[Finding]]:
            findings.append(finding)
            report.findings = [finding.rule_id for finding in findings]
            return report, findings

        document = await self._documents.get(entry.document_id)
        if document is None:
            return fail(_error(entry, "AUTH_DOCUMENT_UNKNOWN",
                               f"Document {entry.document_id} does not exist"))
        if await self._texts.lengths(entry.document_id) is None:
            return fail(_error(entry, "AUTH_SOURCE_UNCHECKABLE",
                               f"Document {entry.document_id} has no canonical text, "
                               "so the quote cannot be checked"))

        result = await self._verification.verify(
            entry.quoted_text,
            entry.document_id,
            window=(entry.char_start, entry.char_end),
        )
        if result.tier in (Tier.NEAR, Tier.NOT_FOUND, Tier.NO_CANONICAL_TEXT):
            detail: dict[str, Any] = {"tier": result.tier.value}
            if result.matched_fraction is not None:
                detail["matched_fraction"] = result.matched_fraction
            if result.divergence is not None:
                detail["divergence"] = result.divergence.model_dump()
            return fail(_error(entry, "AUTH_QUOTE_UNVERIFIED",
                               f"Quote verifies {result.tier.value}, not "
                               "exact or normalized",
                               detail=detail))
        report.tier = result.tier.value
        location = result.location
        if location is None or (
            location.char_start,
            location.char_end,
        ) != (entry.char_start, entry.char_end):
            verified = (
                [location.char_start, location.char_end] if location else None
            )
            return fail(_error(entry, "AUTH_SOURCE_SPAN_STALE",
                               "The verified span moved under this entry — "
                               "re-anchor it",
                               detail={
                                   "entry_span": [entry.char_start, entry.char_end],
                                   "verified_span": verified,
                               }))

        region = await self._is_region(entry)
        if entry.intent.value in _NARROW_INTENTS and (
            region or entry.char_end - entry.char_start > MAX_QUOTE_CHARS
        ):
            return fail(_error(entry, "AUTH_SPAN_NOT_NARROWED",
                               "A quotation or translation must cite a narrowed "
                               "span, not a whole passage"))
        if entry.intent.value in _REGION_WARN_INTENTS and region:
            findings.append(
                Finding(
                    rule_id="AUTH_SPAN_REGION",
                    severity="warning",
                    citation_id=entry.id,
                    message="This span is exactly one passage — narrow it if "
                    "the point rests on less",
                )
            )

        if entry.zotero_key is None and entry.edition is None:
            findings.append(
                Finding(
                    rule_id="AUTH_CITATION_EDITION_MISSING",
                    severity="error" if gate == "publish" else "warning",
                    citation_id=entry.id,
                    message="Neither zotero_key nor edition is set; the "
                    "citation has no bibliographic identity",
                )
            )
        if entry.zotero_key is not None:
            document_key = (document.metadata or {}).get("zotero_key")
            if document_key is None:
                findings.append(
                    Finding(
                        rule_id="AUTH_ZOTERO_KEY_UNKNOWN",
                        severity="warning",
                        citation_id=entry.id,
                        message=f"zotero_key {entry.zotero_key} is on no "
                        "ingested document yet",
                    )
                )
            elif document_key != entry.zotero_key:
                findings.append(
                    Finding(
                        rule_id="AUTH_ZOTERO_KEY_MISMATCH",
                        severity="error",
                        citation_id=entry.id,
                        message=f"Entry key {entry.zotero_key} differs from the "
                        f"document's {document_key}",
                        detail={
                            "entry_key": entry.zotero_key,
                            "document_key": document_key,
                        },
                    )
                )
                report.findings = [finding.rule_id for finding in findings]
                return report, findings

        if entry.id not in markers:
            findings.append(
                Finding(
                    rule_id="AUTH_CITATION_MARKER_MISSING",
                    severity="warning",
                    citation_id=entry.id,
                    message=f"Entry {entry.id} never appears as [^{entry.id}] "
                    "in the body",
                )
            )

        report.findings = [finding.rule_id for finding in findings]
        return report, findings

    async def _is_region(self, entry: CitationEntry) -> bool:
        """Whether the entry's span coincides with one passage row's bounds."""
        covering = await self._passages.covering_span(
            entry.document_id, entry.char_start, entry.char_end
        )
        return any(
            passage.char_start == entry.char_start
            and passage.char_end == entry.char_end
            for passage in covering
        )


def _error(
    entry: CitationEntry,
    rule_id: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id, severity="error", citation_id=entry.id, message=message,
        detail=detail,
    )


def _blockers(findings: list[Finding], gate: GateName) -> list[str]:
    if gate == "none":
        return []
    blockers = [
        finding.rule_id
        for finding in findings
        if finding.severity == "error"
        or (gate == "publish" and finding.rule_id == "AUTH_CITATION_EDITION_MISSING")
    ]
    return sorted(set(blockers))


def _gate_passes(findings: list[Finding], gate: GateName) -> bool:
    return not _blockers(findings, gate)
