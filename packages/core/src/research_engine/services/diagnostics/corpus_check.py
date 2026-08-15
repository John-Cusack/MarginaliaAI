"""Check the corpus against the invariants the test suite asserts on fixtures.

A fixture proves the code is right today. It says nothing about data written by
code that was wrong yesterday — and this corpus has lived through a chunker that
emitted `byte_start: 0` for every passage, a pre-offsets era, and more than one
chunker version. Those rows are still there, and every one of them is a citation
that will not verify or a passage that cannot be re-anchored.

So the same properties are asked of the database. Every check is a single SQL
statement evaluated server-side: span fidelity compares `substring(text ...)`
against the stored passage inside Postgres rather than pulling a corpus over the
wire, which is what makes running this against a real library affordable.

Checks report, they never repair. Each names the command that fixes it, because
a diagnostic that leaves you guessing at the remedy is only half a tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()

#: Passages this far over their chunker's cap are the shape of the defect that
#: prompted this tool: a section emitted whole rather than windowed. The
#: contract suite uses 1.5x against a *declared* cap; here the cap is not known
#: per row, so this is measured against the corpus norm instead.
OVERSIZED_TOKENS = 1200

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

_MISSING_TABLE = re.compile(r'relation "([^"]+)" does not exist')


def _reason(exc: Exception) -> str:
    """A one-line cause. The full SQL and traceback help nobody reading a report."""
    text = str(exc)
    if match := _MISSING_TABLE.search(text):
        return f"{match.group(1)} does not exist"
    return text.splitlines()[0][:160]


@dataclass
class Check:
    """One invariant, its verdict, and what to do about a failure."""

    name: str
    severity: str
    description: str
    count: int = 0
    total: int | None = None
    samples: list[str] = field(default_factory=list)
    remedy: str | None = None

    #: True when the query could not run at all — a missing table, most often
    #: an unapplied migration. Distinct from passing: nothing was verified.
    skipped: bool = False

    @property
    def failed(self) -> bool:
        return self.count > 0


@dataclass
class CorpusReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def critical(self) -> list[Check]:
        return [c for c in self.checks if c.failed and c.severity == "critical"]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.skipped]

    def sorted_checks(self) -> list[Check]:
        return sorted(
            self.checks, key=lambda c: (not c.failed, SEVERITY_ORDER[c.severity], c.name)
        )


#: (name, severity, description, remedy, sql). Each statement returns one row
#: per offender, with the offending id first; counting and sampling is uniform.
_CHECKS: list[tuple[str, str, str, str | None, str]] = [
    (
        "passage_text_matches_its_span",
        "critical",
        "Passage text differs from the canonical text at its own offsets",
        "research-engine reindex chunks",
        """
        SELECT p.id::text
        FROM core.passages p
        JOIN core.document_texts dt ON dt.document_id = p.document_id
        WHERE p.char_start IS NOT NULL AND p.char_end IS NOT NULL
          AND substring(dt.text FROM p.char_start + 1 FOR p.char_end - p.char_start)
              IS DISTINCT FROM p.text
        """,
    ),
    (
        "passage_span_within_the_text",
        "critical",
        "Passage span runs past the end of its document's canonical text",
        "research-engine reindex chunks",
        """
        SELECT p.id::text
        FROM core.passages p
        JOIN core.document_texts dt ON dt.document_id = p.document_id
        WHERE p.char_end > length(dt.text) OR p.char_start < 0
        """,
    ),
    (
        "passage_span_well_formed",
        "critical",
        "Passage span ends before it starts",
        "research-engine reindex chunks",
        "SELECT id::text FROM core.passages WHERE char_end < char_start",
    ),
    (
        "passage_has_offsets",
        "warning",
        "Passage carries no span, so it cannot be cited or re-anchored",
        "research-engine reindex chunks",
        """
        SELECT id::text FROM core.passages
        WHERE char_start IS NULL OR char_end IS NULL
        """,
    ),
    (
        "document_has_canonical_text",
        "warning",
        "Document has passages but no stored text for their offsets to address",
        "research-engine reindex text",
        """
        SELECT DISTINCT p.document_id::text
        FROM core.passages p
        LEFT JOIN core.document_texts dt ON dt.document_id = p.document_id
        WHERE dt.document_id IS NULL
        """,
    ),
    (
        "passage_is_not_empty",
        "warning",
        "Passage is empty or whitespace — an embedding and an index entry for nothing",
        "research-engine reindex chunks",
        "SELECT id::text FROM core.passages WHERE btrim(text) = ''",
    ),
    (
        "passage_is_not_oversized",
        "warning",
        f"Passage exceeds ~{OVERSIZED_TOKENS} tokens, diluting its own embedding",
        "research-engine reindex chunks",
        f"""
        SELECT id::text FROM core.passages
        WHERE length(text) / 4 > {OVERSIZED_TOKENS}
        """,
    ),
    (
        "passage_is_embedded",
        "warning",
        "Passage has no embedding, so it is invisible to semantic search",
        "research-engine embeddings backfill",
        """
        SELECT p.id::text
        FROM core.passages p
        LEFT JOIN core.passage_embeddings pe ON pe.passage_id = p.id
        WHERE pe.passage_id IS NULL
        """,
    ),
    (
        "passage_is_indexed_for_keywords",
        "warning",
        "Passage has no FTS row, so it is invisible to keyword search",
        "research-engine reindex chunks",
        """
        SELECT p.id::text
        FROM core.passages p
        LEFT JOIN core.passage_fts f ON f.passage_id = p.id
        WHERE f.passage_id IS NULL
        """,
    ),
    (
        "node_span_within_the_text",
        "critical",
        "Structural node runs past the end of its document's canonical text",
        "re-ingest the document",
        """
        SELECT n.id::text
        FROM core.document_nodes n
        JOIN core.document_texts dt ON dt.document_id = n.document_id
        WHERE n.char_end > length(dt.text)
        """,
    ),
    (
        "node_sits_inside_its_parent",
        "critical",
        "Structural node is not contained by its parent, breaking subtree queries",
        "re-ingest the document",
        """
        SELECT c.id::text
        FROM core.document_nodes c
        JOIN core.document_nodes p ON p.id = c.parent_id
        WHERE c.char_start < p.char_start OR c.char_end > p.char_end
        """,
    ),
    (
        "passage_node_is_in_the_same_document",
        "critical",
        "Passage points at a structural node belonging to a different document",
        "re-ingest the document",
        """
        SELECT p.id::text
        FROM core.passages p
        JOIN core.document_nodes n ON n.id = p.node_id
        WHERE n.document_id <> p.document_id
        """,
    ),
]


class CorpusChecker:
    """Runs the invariant checks. Reports; never writes."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def run(self, *, sample_size: int = 5) -> CorpusReport:
        report = CorpusReport()
        async with self._engine.connect() as conn:
            passage_total = (
                await conn.execute(sa.text("SELECT count(*) FROM core.passages"))
            ).scalar_one()

            for name, severity, description, remedy, sql in _CHECKS:
                check = Check(
                    name=name,
                    severity=severity,
                    description=description,
                    remedy=remedy,
                    total=passage_total,
                )
                try:
                    rows = (await conn.execute(sa.text(sql))).all()
                except Exception as exc:  # noqa: BLE001
                    # A check that cannot run is not a check that passed. Most
                    # often this is a table a migration has not created yet.
                    check.severity = "info"
                    check.skipped = True
                    check.description = f"{description} — could not run: {_reason(exc)}"
                    check.count = 0
                    report.checks.append(check)
                    logger.debug("corpus_check_skipped", check=name, reason=_reason(exc))
                    # Postgres aborts the whole transaction on a failed
                    # statement and refuses every one after it. Without this,
                    # a single missing table — an unapplied migration, say —
                    # takes down every check that follows and the report comes
                    # back mostly empty rather than mostly passing.
                    await conn.rollback()
                    continue

                check.count = len(rows)
                check.samples = [row[0] for row in rows[:sample_size]]
                report.checks.append(check)

            report.checks.append(await self._version_drift(conn))
        return report

    async def _version_drift(self, conn: Any) -> Check:
        """Passages written by a chunker version no longer emitted.

        Not a defect on its own — old passages are valid until re-chunked — but
        it is what `reindex chunks` exists to resolve, and it is invisible
        without asking.
        """
        from research_engine.services.ingestion.pipeline import current_chunker_versions

        current = current_chunker_versions()
        check = Check(
            name="passages_on_current_chunker_versions",
            severity="info",
            description="Passages written by a superseded chunker version",
            remedy="research-engine reindex chunks",
        )
        rows = (
            await conn.execute(
                sa.text(
                    "SELECT chunker, chunker_version, count(*) "
                    "FROM core.passages GROUP BY chunker, chunker_version"
                )
            )
        ).all()
        stale = 0
        for chunker, version, count in rows:
            expected = current.get(chunker)
            if expected is not None and version != expected:
                stale += count
                check.samples.append(f"{chunker} {version} -> {expected}: {count} passages")
        check.count = stale
        return check
