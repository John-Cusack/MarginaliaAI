"""Validate a revision's rows — the Phase-1 `work_validate`.

Findings carry Appendix A rule ids; messages are for humans. Severities are
the core floor, moved by the per-work-type policy (`settings.works_policy`)
between error, warn, and allow. At gate `none` the report lists and passes;
at `freeze` and `publish` every unwaived error blocks, and waivers — rows
naming a rule, an optional subject, and who answers for it — clear the
finding they name.

What is deliberately absent: claim refs. Rows hold no `claims:` — the ledger
has no `claim_upsert` yet, and the only claim refs in the system are front
matter carried in `metadata.port` — so `AUTH_CLAIM_UNRESOLVED` has nothing to
resolve against and is not emitted (guide Appendix F).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

import structlog
from pydantic import BaseModel, Field

from research_engine.domain.errors import NotFoundError
from research_engine.services.works.assembly import (
    AssembledRevision,
    assemble_revision,
    hash_assembled,
)
from research_engine.services.works.markers import find_markers
from research_engine.services.works.verify import MAX_QUOTE_CHARS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from research_engine.domain.works import Work, WorkRevision

logger = structlog.get_logger()

GateName = Literal["none", "freeze", "publish"]

#: Intents that must cite a narrowed span, never a whole passage.
_NARROW_INTENTS = frozenset({"quotation", "translation"})
#: Intents warned when they cite a whole passage.
_REGION_WARN_INTENTS = frozenset({"support", "source", "definition"})
#: Block types that must lean on the corpus or say nothing.
_GROUNDED_TYPES = frozenset({"translation_unit", "quotation"})

#: The core floor: rule id to severity before the work-type policy moves it.
_DEFAULT_SEVERITY: dict[str, str] = {
    "AUTH_DOCUMENT_UNKNOWN": "error",
    "AUTH_SOURCE_UNCHECKABLE": "error",
    "AUTH_QUOTE_UNVERIFIED": "error",
    "AUTH_SOURCE_SPAN_STALE": "error",
    "AUTH_SPAN_NOT_NARROWED": "error",
    "AUTH_SPAN_REGION": "warning",
    "AUTH_CITATION_EDITION_MISSING": "warning",
    "AUTH_CITATION_EDITION_MISMATCH": "error",
    "AUTH_ZOTERO_KEY_UNKNOWN": "warning",
    "AUTH_CITATION_MARKER_MISSING": "error",
    "AUTH_CITATION_MARKER_DANGLING": "error",
    "AUTH_PARENT_REVISION_MISMATCH": "error",
    "AUTH_REVISION_MUTATED": "error",
    "AUTH_BIBLIOGRAPHY_ONLY": "warning",
    "AUTH_BLOCK_UNGROUNDED": "warning",
    "AUTH_UNUSED_CITATION": "warning",
    "AUTH_FILE_DRIFT": "warning",
}


class ValidationFinding(BaseModel):
    rule_id: str
    severity: str
    block_key: str | None = None
    citation_key: str | None = None
    message: str
    detail: dict[str, Any] | None = None


class CitationCheck(BaseModel):
    citation_key: str
    block_key: str
    intent: str
    tier: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    findings: list[str] = Field(default_factory=list)


class GateResult(BaseModel):
    name: GateName
    passed: bool
    blockers: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    work: str
    revision_number: int
    state: str
    citations: list[CitationCheck] = Field(default_factory=list)
    findings: list[ValidationFinding] = Field(default_factory=list)
    gate: GateResult


class WorkValidationService:
    """Structural and corpus checks over one revision, keyed by block and citation."""

    def __init__(
        self,
        *,
        works: Any,
        revisions: Any,
        blocks: Any,
        citations: Any,
        links: Any,
        editions: Any,
        waivers: Any,
        spans: Any,
        documents: Any,
        document_texts: Any,
        passages: Any,
        policy: dict[str, dict[str, str]] | None = None,
        works_dir: Path | None = None,
        export_markdown: Callable[[UUID], Any] | None = None,
    ) -> None:
        self._works = works
        self._revisions = revisions
        self._blocks = blocks
        self._citations = citations
        self._links = links
        self._editions = editions
        self._waivers = waivers
        self._spans = spans
        self._documents = documents
        self._texts = document_texts
        self._passages = passages
        self._policy = policy or {}
        self._works_dir = works_dir
        #: Renders the work's current draft to markdown for the drift check.
        #: A callable (not the export service) so this service stays
        #: constructible wherever the exporter is unavailable.
        self._export_markdown = export_markdown

    async def validate(
        self,
        *,
        slug: str,
        revision: int | None = None,
        gate: GateName = "none",
        prospective: set[tuple[str, str | None]] | None = None,
    ) -> ValidationReport:
        """Check a revision and judge it against the gate.

        `prospective` waivers — (rule_id, subject) pairs the caller is about
        to insert in the same transaction, as `freeze` does — clear blockers
        like stored rows. Without this the freeze gate could never pass on
        the call that earns its waivers.
        """
        if gate not in ("none", "freeze", "publish"):
            raise ValueError(f"Unknown gate {gate!r}")
        work = await self._works.get_by_slug(slug)
        if work is None:
            raise NotFoundError("work", slug)
        resolved = await self._resolve_revision(work, revision)
        view = await assemble_revision(
            work,
            resolved,
            blocks=self._blocks,
            citations=self._citations,
            links=self._links,
            spans=self._spans,
        )
        checker = _Checker(
            view,
            editions=self._editions,
            documents=self._documents,
            texts=self._texts,
            passages=self._passages,
        )
        findings = await checker.run()
        if (
            self._works_dir is not None
            and self._export_markdown is not None
            and revision is None
        ):
            drift = await self._check_drift(work, view)
            if drift is not None:
                findings.append(drift)
        for finding in findings:
            finding.severity = resolve_severity(
                self._policy, work.work_type, finding.rule_id, finding.severity
            )
        findings = [
            finding for finding in findings
            if finding.severity in ("error", "warning")
        ]
        waived = await self._waiver_index(resolved.id)
        if prospective:
            waived |= prospective
        blockers = _blockers(findings, gate, waived)
        for finding in findings:
            if (finding.rule_id, finding.citation_key or finding.block_key) in waived:
                detail = dict(finding.detail or {})
                detail["waived"] = True
                finding.detail = detail
        citations = _citation_checks(view, findings)
        return ValidationReport(
            work=work.slug,
            revision_number=resolved.revision_number,
            state=resolved.state.value,
            citations=citations,
            findings=findings,
            gate=GateResult(
                name=gate, passed=True if gate == "none" else not blockers,
                blockers=blockers,
            ),
        )

    async def _resolve_revision(
        self, work: Work, revision_number: int | None
    ) -> WorkRevision:
        if revision_number is None:
            if work.current_revision_id is None:
                raise NotFoundError("work_revision", f"current of {work.slug}")
            revision = await self._revisions.get(work.current_revision_id)
            if revision is None:
                raise NotFoundError("work_revision", work.current_revision_id)
            return revision
        latest = await self._revisions.latest(work.id)
        if latest is None:
            raise NotFoundError("work_revision", f"{work.slug} revision 1")
        current = latest
        while current.revision_number != revision_number:
            if current.parent_revision_id is None:
                raise NotFoundError(
                    "work_revision", f"{work.slug} revision {revision_number}"
                )
            parent = await self._revisions.get(current.parent_revision_id)
            if parent is None:  # pragma: no cover - FK keeps the chain whole
                raise NotFoundError(
                    "work_revision", f"{work.slug} revision {revision_number}"
                )
            current = parent
        return current

    async def _waiver_index(self, revision_id: UUID) -> set[tuple[str, str | None]]:
        rows = await self._waivers.for_revision(revision_id)
        return {(row.rule_id, row.subject) for row in rows}

    async def _check_drift(
        self, work: Work, view: AssembledRevision
    ) -> ValidationFinding | None:
        """A flipped work whose file moved since its last export drifts.

        The flip records its file under `metadata.port.file`; until flip code
        exists that key is absent and the check is skipped, never failed.
        """
        assert self._works_dir is not None and self._export_markdown is not None
        port = (view.revision.metadata or {}).get("port") or {}
        rel = port.get("file")
        if not rel:
            return None
        path = self._works_dir / str(rel)
        if not path.is_file():
            return None
        rendered = await self._export_markdown(work.id)
        if path.read_text(encoding="utf-8") != rendered:
            return ValidationFinding(
                rule_id="AUTH_FILE_DRIFT",
                severity="warning",
                message=f"File {rel} differs from the last export of {work.slug}",
            )
        return None

def resolve_severity(
    policy: dict[str, dict[str, str]] | None,
    work_type: str,
    rule_id: str,
    default: str,
) -> str:
    """The work-type policy moves a rule between error, warn, and allow.

    Unknown values fall back to the core floor: a typo must not silence a
    check, and `allow` is the only way to drop a finding.
    """
    override = (policy or {}).get(work_type, {}).get(rule_id)
    if override == "allow":
        return "allow"
    if override in ("error", "warning"):
        return override
    if override == "warn":
        return "warning"
    return default


class _Checker:
    """The per-revision checks, holding the read caches."""

    def __init__(
        self, view: AssembledRevision, *, editions: Any, documents: Any,
        texts: Any, passages: Any,
    ) -> None:
        self._view = view
        self._editions = editions
        self._documents = documents
        self._texts = texts
        self._passages = passages
        self._findings: list[ValidationFinding] = []
        self._documents_cache: dict[str, Any] = {}
        self._edition_keys: set[str] | None = None
        self._parser_versions: dict[str, str] = {}

    async def run(self) -> list[ValidationFinding]:
        view = self._view
        await self._prime_corpus_caches(view)
        markers = self._markers_by_block(view)
        for block_key, (_keys, invalid) in markers.items():
            for raw in invalid:
                self._add(
                    "AUTH_CITATION_MARKER_DANGLING", "error", block_key=block_key,
                    message=f"Marker {raw} names no citation: keys are UUIDs",
                    detail={"marker": raw},
                )
        occurrence_blocks: dict[str, str] = {}
        for item in view.blocks:
            for entry in item.citations:
                occurrence_blocks[str(entry.occurrence.citation_key)] = str(
                    item.block.block_key
                )
        for item in view.blocks:
            block_key = str(item.block.block_key)
            keys, _ = markers[block_key]
            for entry in item.citations:
                citation_key = str(entry.occurrence.citation_key)
                if citation_key not in keys:
                    self._add(
                        "AUTH_CITATION_MARKER_MISSING", "error",
                        block_key=block_key, citation_key=citation_key,
                        message=f"Occurrence {citation_key} has no "
                        "{{cite:…}} marker in its block",
                    )
                for row in entry.items:
                    await self._check_item(
                        block_key, citation_key,
                        entry.occurrence.intent.value, row,
                    )
            for key in keys:
                if key not in occurrence_blocks:
                    self._add(
                        "AUTH_CITATION_MARKER_DANGLING", "error",
                        block_key=block_key,
                        message=f"Marker {{{{cite:{key}}}}} matches no "
                        "occurrence on this block",
                        detail={"citation_key": key},
                    )
                elif occurrence_blocks[key] != block_key:
                    self._add(
                        "AUTH_UNUSED_CITATION", "warning",
                        block_key=occurrence_blocks[key], citation_key=key,
                        message=f"Occurrence {key} is marked in another "
                        "block, not the one rendered with it",
                    )
            await self._check_block(item)
        await self._check_revision()
        return self._findings

    async def _prime_corpus_caches(self, view: AssembledRevision) -> None:
        doc_ids = {
            span.document_id
            for span in view.spans.values()
        }
        for doc_id in doc_ids:
            document = await self._documents.get(doc_id)
            if document is not None:
                self._documents_cache[str(doc_id)] = document
        if doc_ids:
            versions = await self._texts.parser_versions(list(doc_ids))
            self._parser_versions = {str(key): value for key, value in versions.items()}
        self._edition_keys = set(await self._editions.list_keys())

    @staticmethod
    def _markers_by_block(
        view: AssembledRevision,
    ) -> dict[str, tuple[set[str], list[str]]]:
        return {
            str(item.block.block_key): find_markers(item.block.body_markdown or "")
            for item in view.blocks
        }

    async def _check_item(
        self, block_key: str, citation_key: str, intent: str, row: Any
    ) -> None:
        if row.edition_id is None and row.zotero_key is None:
            self._add(
                "AUTH_CITATION_EDITION_MISSING", "warning",
                block_key=block_key, citation_key=citation_key,
                message="The citation names no edition: zotero_key or "
                "edition_id",
            )
        if row.zotero_key is not None:
            assert self._edition_keys is not None
            if row.zotero_key not in self._edition_keys:
                self._add(
                    "AUTH_ZOTERO_KEY_UNKNOWN", "warning",
                    block_key=block_key, citation_key=citation_key,
                    message=f"zotero_key {row.zotero_key} has no "
                    "bibliography.editions row",
                )
        if row.source_span_id is None:
            self._add(
                "AUTH_BIBLIOGRAPHY_ONLY", "warning",
                block_key=block_key, citation_key=citation_key,
                message="The citation has identity but no span: bibliography, "
                "not evidence",
            )
            return
        span = self._view.spans.get(row.source_span_id)
        if span is None:  # pragma: no cover - RESTRICT keeps spans under rows
            return
        document = self._documents_cache.get(str(span.document_id))
        if document is None:
            self._add(
                "AUTH_DOCUMENT_UNKNOWN", "error",
                block_key=block_key, citation_key=citation_key,
                message=f"Document {span.document_id} does not exist",
            )
            return
        if str(span.document_id) not in self._parser_versions:
            self._add(
                "AUTH_SOURCE_UNCHECKABLE", "error",
                block_key=block_key, citation_key=citation_key,
                message=f"Document {span.document_id} has no canonical text",
            )
            return
        if span.parser_version != self._parser_versions[str(span.document_id)]:
            self._add(
                "AUTH_SOURCE_SPAN_STALE", "error",
                block_key=block_key, citation_key=citation_key,
                message="The document was re-parsed since this span resolved: "
                "re-verify it",
                detail={
                    "span_parser_version": span.parser_version,
                    "document_parser_version": self._parser_versions[
                        str(span.document_id)
                    ],
                },
            )
        await self._check_identity(block_key, citation_key, document, row)
        if row.verify_status not in ("exact", "normalized"):
            self._add(
                "AUTH_QUOTE_UNVERIFIED", "error",
                block_key=block_key, citation_key=citation_key,
                message=f"Quote verifies {row.verify_status}, not exact or "
                "normalized: earn a waiver or re-anchor it",
                detail={"verify_status": row.verify_status},
            )
        region = await self._is_region(span)
        length = span.char_end - span.char_start
        if intent in _NARROW_INTENTS and (region or length > MAX_QUOTE_CHARS):
            self._add(
                "AUTH_SPAN_NOT_NARROWED", "error",
                block_key=block_key, citation_key=citation_key,
                message="A quotation or translation must cite a narrowed "
                "span, not a whole passage",
            )
        elif intent in _REGION_WARN_INTENTS and region:
            self._add(
                "AUTH_SPAN_REGION", "warning",
                block_key=block_key, citation_key=citation_key,
                message="This span is exactly one passage: narrow it if the "
                "point rests on less",
            )

    async def _check_identity(
        self, block_key: str, citation_key: str, document: Any, row: Any
    ) -> None:
        """The item's key against its span's document key, until P3 joins them."""
        item_key = row.zotero_key
        if item_key is None and row.edition_id is not None:
            edition = await self._editions.get(row.edition_id)
            item_key = edition.zotero_key if edition is not None else None
        if item_key is None:
            return
        document_key = (document.metadata or {}).get("zotero_key")
        if document_key is None:
            self._add(
                "AUTH_ZOTERO_KEY_UNKNOWN", "warning",
                block_key=block_key, citation_key=citation_key,
                message=f"zotero_key {item_key} is on no ingested document yet",
            )
        elif document_key != item_key:
            self._add(
                "AUTH_CITATION_EDITION_MISMATCH", "error",
                block_key=block_key, citation_key=citation_key,
                message=f"Citation key {item_key} differs from the span's "
                f"document key {document_key}",
                detail={"item_key": item_key, "document_key": document_key},
            )

    async def _check_block(self, item: Any) -> None:
        block = item.block
        if block.block_type in _GROUNDED_TYPES:
            has_grounding = bool(item.citations) or bool(
                item.links and item.links.sources
            )
            if not has_grounding:
                self._add(
                    "AUTH_BLOCK_UNGROUNDED", "warning",
                    block_key=str(block.block_key),
                    message=f"A {block.block_type} block with no citation or "
                    "source link leans on nothing",
                )
        if block.parent_id is not None:
            ids = {entry.block.id for entry in self._view.blocks}
            if block.parent_id not in ids:
                self._add(
                    "AUTH_PARENT_REVISION_MISMATCH", "error",
                    block_key=str(block.block_key),
                    message="The block's parent is in another revision",
                )

    async def _check_revision(self) -> None:
        revision = self._view.revision
        if revision.state.value == "draft" or revision.content_hash is None:
            return
        if hash_assembled(self._view) != revision.content_hash:
            self._add(
                "AUTH_REVISION_MUTATED", "error",
                message=f"Revision {revision.revision_number} no longer "
                "matches its frozen hash",
            )

    async def _is_region(self, span: Any) -> bool:
        covering = await self._passages.covering_span(
            span.document_id, span.char_start, span.char_end
        )
        return any(
            passage.char_start == span.char_start
            and passage.char_end == span.char_end
            for passage in covering
        )

    def _add(
        self, rule_id: str, severity: str, *, block_key: str | None = None,
        citation_key: str | None = None, message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._findings.append(
            ValidationFinding(
                rule_id=rule_id, severity=severity, block_key=block_key,
                citation_key=citation_key, message=message, detail=detail,
            )
        )


def _blockers(
    findings: list[ValidationFinding], gate: GateName,
    waived: set[tuple[str, str | None]],
) -> list[str]:
    """Rule ids that fail the gate: unwaived errors, plus edition identity at publish."""
    if gate == "none":
        return []
    blockers = set()
    for finding in findings:
        if finding.severity not in ("error", "warning"):
            continue
        subject = finding.citation_key or finding.block_key
        if (finding.rule_id, subject) in waived or (finding.rule_id, None) in waived:
            continue
        if finding.severity == "error" or (
            gate == "publish" and finding.rule_id == "AUTH_CITATION_EDITION_MISSING"
        ):
            blockers.add(finding.rule_id)
    return sorted(blockers)


def _citation_checks(
    view: AssembledRevision, findings: list[ValidationFinding]
) -> list[CitationCheck]:
    by_citation: dict[str, list[str]] = {}
    for finding in findings:
        if finding.citation_key is not None:
            by_citation.setdefault(finding.citation_key, []).append(finding.rule_id)
    checks = []
    for item in view.blocks:
        for entry in item.citations:
            key = str(entry.occurrence.citation_key)
            first_span = next(
                (
                    view.spans[row.source_span_id]
                    for row in entry.items
                    if row.source_span_id in view.spans
                ),
                None,
            )
            tier = next(
                (row.verify_status for row in entry.items if row.verify_status),
                None,
            )
            checks.append(
                CitationCheck(
                    citation_key=key,
                    block_key=str(item.block.block_key),
                    intent=entry.occurrence.intent.value,
                    tier=tier,
                    char_start=first_span.char_start if first_span else None,
                    char_end=first_span.char_end if first_span else None,
                    findings=by_citation.get(key, []),
                )
            )
    return checks
