"""Portable mocks and behavior contracts for plugin unit tests."""

from __future__ import annotations

import re
from typing import Any

from research_engine_sdk.chunking import ABSOLUTE_MAX_TOKENS
from research_engine_sdk.types import SearchQuery, SearchResult

OVERSHOOT_TOLERANCE = 1.5
MAX_AMPLIFICATION = 2.0
_SEAM = re.compile(r"\s+|[。！？；、，]")

CONTRACT_TEXTS: dict[str, str] = {
    "empty": "",
    "whitespace_only": "   \n\n\t  ",
    "single_sentence": "One sentence with no terminator",
    "prose": (
        "The archive holds letters. Each letter has a date. "
        "Some dates are approximate. Others are exact. "
    )
    * 40,
    "index": "Index\n\n" + "".join(f"entry {number}\n" for number in range(2400)),
    "no_boundaries": "word " * 2000,
    "cjk": "第一句話寫在這裡。第二句話也寫在這裡，內容比較長一些。" * 900,
    "greek": (
        "λόγος, ου, ὁ πρὸς τὸν θεόν, καὶ θεὸς ἦν ὁ λόγος· "
        "οὗτος ἦν ἐν ἀρχῇ πρὸς τὸν θεόν. "
    )
    * 600,
    "crlf": "First line.\r\nSecond line.\r\n\r\nThird.",
}


def _independent_token_estimate(text: str) -> int:
    if not text:
        return 1
    dense = sum(1 for char in text if ord(char) >= 128)
    return max(1, int((len(text) - dense) / 4.0 + dense / 1.5))


async def call_chunker(chunker: Any, text: str) -> list[Any]:
    if getattr(chunker, "consumes", "text") == "sections":
        if not text.strip():
            return []
        start, end = 0, len(text)
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        return await chunker.chunk(
            [{"char_start": start, "char_end": end, "level": 1}],
            None,
            full_text=text,
        )
    return await chunker.chunk(text)


async def assert_chunker_contract(
    chunker: Any,
    *,
    texts: dict[str, str] | None = None,
    absolute_max_tokens: int = ABSOLUTE_MAX_TOKENS,
) -> None:
    """Assert deterministic, gap-free, bounded, span-faithful chunk output."""

    label = getattr(chunker, "id", type(chunker).__name__)
    assert hasattr(chunker, "max_passage_tokens"), (
        f"{label} must declare max_passage_tokens (use None for deliberately unbounded)"
    )
    limit = chunker.max_passage_tokens
    for name, text in (texts or CONTRACT_TEXTS).items():
        drafts = await call_chunker(chunker, text)
        if not text.strip():
            assert drafts == [], f"{label}: emitted passages for empty input ({name})"
            continue
        assert drafts, f"{label}: emitted nothing for {name}"
        assert [draft.position for draft in drafts] == list(range(len(drafts)))
        assert [draft.char_start for draft in drafts] == sorted(
            draft.char_start for draft in drafts
        )
        assert len({(draft.char_start, draft.char_end) for draft in drafts}) == len(drafts)

        cursor = 0
        for draft in drafts:
            assert 0 <= draft.char_start <= draft.char_end <= len(text)
            assert draft.text == text[draft.char_start : draft.char_end]
            assert draft.text.strip()
            if draft.char_start > cursor:
                assert not text[cursor : draft.char_start].strip(), (
                    f"{label}: dropped content on {name}"
                )
            cursor = max(cursor, draft.char_end)
            tokens = _independent_token_estimate(draft.text)
            assert tokens <= absolute_max_tokens
            if limit is not None and len(_SEAM.findall(draft.text)) + 1 > 1:
                assert tokens <= limit * OVERSHOOT_TOLERANCE
        assert not text[cursor:].strip(), f"{label}: dropped tail on {name}"
        emitted = sum(len(draft.text) for draft in drafts)
        assert emitted / len(text.strip()) <= MAX_AMPLIFICATION

        again = await call_chunker(chunker, text)
        assert [(draft.char_start, draft.char_end) for draft in again] == [
            (draft.char_start, draft.char_end) for draft in drafts
        ], f"{label}: chunking is not deterministic on {name}"


class MockCorpusClient:
    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self._docs = {str(document["id"]): document for document in documents or []}

    async def find_passages(
        self, query: str, filters: dict[str, Any] | None = None, k: int = 20
    ) -> SearchResult:
        return SearchResult(hits=[], total_candidates=0)

    async def find_passages_advanced(self, query: SearchQuery) -> SearchResult:
        return SearchResult(hits=[], total_candidates=0)

    async def get_document(self, document_id: Any) -> dict[str, Any] | None:
        return self._docs.get(str(document_id))

    async def get_document_outline(
        self, document_id: Any, dated_only: bool = False
    ) -> list[dict[str, Any]]:
        return []

    async def get_passage_context(
        self, passage_id: Any, before: int = 0, after: int = 0
    ) -> dict[str, Any]:
        return {"target": None, "before": [], "after": []}


class MockLLMClient:
    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or ["Mock response"])
        self._call_count = 0

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        index = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[index]

    async def structured(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"records": []}


class MockBibliographyClient:
    """An in-memory ``BibliographyClient`` that keeps what it is told.

    Mirrors the core adapter's rules that a plugin can trip over: a
    re-registration without ``status`` keeps the source's progress, a blocked
    source needs a reason, and nothing but ``register_source`` creates one.
    """

    _STATUSES = frozenset({"planned", "acquired", "loaded", "blocked", "failed"})

    def __init__(self) -> None:
        self.sources: dict[str, dict[str, Any]] = {}

    async def register_source(
        self, edition_key: str, *, status: str | None = None, **declared: Any
    ) -> dict[str, Any]:
        source = self.sources.setdefault(
            edition_key,
            {
                "edition_key": edition_key,
                "status": "planned",
                "collection": "main",
                "artifacts": [],
                "coverage": [],
            },
        )
        # Partial, like the real adapter: what is not passed is not erased.
        source.update({k: v for k, v in declared.items() if k != "csl" and v is not None})
        if status is not None:
            if status not in self._STATUSES:
                raise ValueError(f"unknown status {status!r}")
            if status == "blocked" and not declared.get("blocked_reason"):
                raise ValueError("a blocked acquisition needs a blocked_reason")
            source["status"] = status
        return self._public(source)

    async def mark_blocked(
        self, edition_key: str, *, reason: str, suggested_acquisition: str, **declared: Any
    ) -> dict[str, Any]:
        return await self.register_source(
            edition_key,
            status="blocked",
            blocked_reason=reason,
            suggested_acquisition=suggested_acquisition,
            **declared,
        )

    async def set_status(
        self, edition_key: str, status: str, *, retrieved_at: Any = None
    ) -> dict[str, Any]:
        source = self._require(edition_key)
        if status == "blocked":
            raise ValueError("use mark_blocked to block a source: it needs a reason")
        if status not in self._STATUSES:
            raise ValueError(f"unknown status {status!r}")
        source["status"] = status
        source["blocked_reason"] = None
        if retrieved_at is not None:
            source["retrieved_at"] = retrieved_at
        return self._public(source)

    async def attach_artifact(
        self, edition_key: str, path: Any, *, sha256: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        artifact = {"path": str(path), "sha256": sha256, **kwargs}
        self._require(edition_key)["artifacts"].append(artifact)
        return artifact

    async def report_coverage(
        self,
        edition_key: str,
        *,
        unit: str,
        loaded_units: int,
        checker: str,
        checker_version: str,
        expected_units: int | None = None,
        anomalies: list[Any] | None = None,
    ) -> dict[str, Any]:
        anomalies = list(anomalies or [])
        report = {
            "unit": unit,
            "loaded_units": loaded_units,
            "expected_units": expected_units,
            "anomalies": anomalies,
            "ok": (expected_units is None or loaded_units == expected_units)
            and not anomalies,
            "checker": checker,
            "checker_version": checker_version,
        }
        self._require(edition_key)["coverage"].append(report)
        return report

    async def get_source(self, edition_key: str) -> dict[str, Any] | None:
        source = self.sources.get(edition_key)
        return dict(source) if source is not None else None

    async def list_sources(
        self, *, status: str | None = None, collection: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            self._public(source)
            for key, source in sorted(self.sources.items())
            if (status is None or source["status"] == status)
            and (collection is None or source.get("collection") == collection)
        ]

    def _require(self, edition_key: str) -> dict[str, Any]:
        if edition_key not in self.sources:
            raise LookupError(
                f"edition {edition_key!r} has no acquisition; register_source first"
            )
        return self.sources[edition_key]

    @staticmethod
    def _public(source: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v for k, v in source.items()
            if k not in {"artifacts", "coverage"}
        }


class MockProfileClient:
    """An in-memory ``ProfileClient``.

    Mirrors the core adapter's rules: every role names a known edition (pass
    ``editions`` to say which exist, or ``None`` to accept any), loading
    replaces a profile's roles whole, and a run is closed once.
    """

    ROLES = frozenset({"binding", "external", "reference", "defense", "critical", "fact"})

    def __init__(self, editions: set[str] | None = None) -> None:
        self.editions = editions
        self.profiles: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}

    async def load_profile(
        self,
        key: str,
        *,
        version: int,
        profile_hash: str,
        roles: list[dict[str, Any]],
        title: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for role in roles:
            if role.get("role") not in self.ROLES:
                raise ValueError(f"unknown role {role.get('role')!r}")
            if self.editions is not None and role["edition_key"] not in self.editions:
                raise LookupError(f"edition {role['edition_key']!r} is not registered")
        self.profiles[key] = {
            "key": key,
            "version": version,
            "profile_hash": profile_hash,
            "title": title,
            "settings": dict(settings or {}),
            "roles": [dict(role) for role in roles],
        }
        return {**self._public(key), "roles": len(roles)}

    async def get_profile(self, key: str, *, on: Any = None) -> dict[str, Any] | None:
        if key not in self.profiles:
            return None
        return {**self._public(key), "roles": [dict(r) for r in self.profiles[key]["roles"]]}

    async def start_run(
        self, profile_key: str, *, stage: str, config: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if profile_key not in self.profiles:
            raise LookupError(f"profile {profile_key!r} is not loaded")
        run_id = f"run-{len(self.runs) + 1}"
        self.runs[run_id] = {
            "id": run_id,
            "profile_key": profile_key,
            "stage": stage,
            "status": "running",
            "profile_hash": self.profiles[profile_key]["profile_hash"],
            "config": dict(config or {}),
            **kwargs,
        }
        return dict(self.runs[run_id])

    async def finish_run(
        self, run_id: Any, *, status: str = "finished", **kwargs: Any
    ) -> dict[str, Any]:
        run = self.runs.get(str(run_id))
        if run is None or run["status"] != "running":
            raise ValueError(f"run {run_id} does not exist or is already closed")
        if status not in {"finished", "failed"}:
            raise ValueError("finish a run as finished or failed, not running")
        run.update({"status": status, **{k: v for k, v in kwargs.items() if v is not None}})
        return dict(run)

    def _public(self, key: str) -> dict[str, Any]:
        return {k: v for k, v in self.profiles[key].items() if k != "roles"}


__all__ = [
    "ABSOLUTE_MAX_TOKENS",
    "CONTRACT_TEXTS",
    "MAX_AMPLIFICATION",
    "OVERSHOOT_TOLERANCE",
    "MockBibliographyClient",
    "MockCorpusClient",
    "MockLLMClient",
    "MockProfileClient",
    "assert_chunker_contract",
    "call_chunker",
]
