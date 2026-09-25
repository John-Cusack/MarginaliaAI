"""Work files on disk — parsing the Phase-0 file contract.

A work is one markdown file: a `---` fenced YAML front matter block holding
structured span citations, then a body holding `[^cN]` markers. This module is
pure file reading plus validation; verification against the corpus lives in
`services/works/verify.py`, which is why a file with a bad entry still parses
— the failure becomes an `AUTH_ENTRY_INVALID` finding, not an exception.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import ValidationError

from research_engine.domain.errors import ResearchEngineError
from research_engine.domain.works_files import (
    CitationEntry,
    EntryError,
    WorkFile,
    WorkFrontMatter,
)

logger = structlog.get_logger()

#: Files that live in the works directory but are not works.
_NON_WORK_NAMES = frozenset({"README.md", "_TEMPLATE.md"})

#: A footnote definition line: `[^c1]: ...` at line start. Definitions are the
#: renderer's output, not markers, so they are excluded before markers are read.
_DEFINITION_RE = re.compile(r"^\[\^c\d+\]:[^\n]*\n?", re.MULTILINE)


def strip_definition_lines(body: str) -> str:
    """Remove rendered footnote definitions, leaving prose and markers."""
    return _DEFINITION_RE.sub("", body)

#: Every `[^cN]` marker left in the body once definitions are removed.
_MARKER_RE = re.compile(r"\[\^(c\d+)\]")

#: Top-level front-matter keys. Anything else is a typo for one of them, and a
#: typo that parses is worse than a header that refuses.
_HEADER_KEYS = frozenset(
    {"work", "title", "type", "status", "created", "claims", "citations"}
)


class WorkFileError(ResearchEngineError):
    """A work file cannot be parsed — bad fences, bad YAML, or a bad header."""


def parse_work_file(path: Path, works_dir: Path) -> WorkFile:
    """Parse one work file. A bad header is a hard error; a bad entry is not.

    Raises:
        WorkFileError: the file is outside *works_dir*, has no front matter,
            holds YAML that is not a mapping, or fails header validation.
    """
    works_dir = works_dir.resolve()
    resolved = (works_dir / path if not path.is_absolute() else path).resolve()
    try:
        relative = resolved.relative_to(works_dir)
    except ValueError:
        raise WorkFileError(f"{resolved} is outside the works directory") from None
    work_path = relative.as_posix()

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkFileError(f"Cannot read {work_path}: {exc}") from exc

    if not text.startswith("---\n"):
        raise WorkFileError(
            f"{work_path} does not start with a front-matter fence (`---`)"
        )
    end = text.find("\n---\n", 4)
    if end < 0:
        raise WorkFileError(f"{work_path} has no closing front-matter fence")
    yaml_block = text[4:end]
    body = text[end + len("\n---\n") :]

    try:
        raw = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        raise WorkFileError(f"{work_path} has invalid YAML front matter: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkFileError(f"{work_path} front matter must be a mapping")

    front_matter, entry_errors = _validate_header(work_path, raw)
    sha = hashlib.sha256(yaml_block.encode("utf-8")).hexdigest()
    markers = _MARKER_RE.findall(strip_definition_lines(body))
    return WorkFile(
        work_path=work_path,
        front_matter=front_matter,
        front_matter_sha=sha,
        body=body,
        markers=markers,
        entry_errors=entry_errors,
    )


def _validate_header(
    work_path: str, raw: dict[str, Any]
) -> tuple[WorkFrontMatter, list[EntryError]]:
    unknown = sorted(set(raw) - _HEADER_KEYS)
    if unknown:
        raise WorkFileError(f"{work_path} has unknown front-matter keys: {unknown}")
    entries = raw.get("citations", [])
    if not isinstance(entries, list):
        raise WorkFileError(f"{work_path} front matter `citations` must be a list")
    header_raw = {key: raw[key] for key in _HEADER_KEYS - {"citations"} if key in raw}
    try:
        header = WorkFrontMatter(**header_raw, citations=[])
    except ValidationError as exc:
        raise WorkFileError(
            f"{work_path} has an invalid front-matter header: {exc}"
        ) from exc

    valid: list[CitationEntry] = []
    errors: list[EntryError] = []
    for item in entries:
        if not isinstance(item, dict):
            errors.append(EntryError(message=f"citation entry must be a mapping: {item!r}"))
            continue
        try:
            valid.append(CitationEntry(**item))
        except ValidationError as exc:
            citation_id = item.get("id") if isinstance(item.get("id"), str) else None
            errors.append(EntryError(citation_id=citation_id, message=str(exc)))
    header.citations = valid
    return header, errors


class WorkFileReader:
    """Works on disk, addressed by path relative to the works directory."""

    def __init__(self, works_dir: Path) -> None:
        self._works_dir = works_dir.resolve()

    @property
    def works_dir(self) -> Path:
        return self._works_dir

    def list_works(self) -> list[str]:
        """Every work file, as forward-slash paths relative to the works dir.

        `README.md`, `_TEMPLATE.md`, and anything starting with `_` are the
        contract itself, not works. Sync conflict copies (Dropbox's
        `*conflicted copy*`, Syncthing's `*.sync-conflict-*`) are not works
        either: neither side of a sync conflict is authoritative.
        """
        found = []
        for path in sorted(self._works_dir.rglob("*.md")):
            if path.name in _NON_WORK_NAMES or path.name.startswith("_"):
                continue
            if (
                "conflicted copy" in path.name
                or fnmatch.fnmatch(path.name, "*.sync-conflict-*")
            ):
                logger.info(
                    "works_conflict_copy_skipped",
                    path=path.relative_to(self._works_dir).as_posix(),
                )
                continue
            found.append(path.relative_to(self._works_dir).as_posix())
        logger.debug("works_listed", count=len(found), works_dir=str(self._works_dir))
        return found

    def read(self, work_path: str) -> WorkFile:
        return parse_work_file(Path(work_path), self._works_dir)
