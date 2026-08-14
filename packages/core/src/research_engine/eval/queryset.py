"""A frozen set of queries with known-relevant passages."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID  # noqa: TC003 - pydantic resolves field types at runtime

import yaml
from pydantic import BaseModel, Field


class EvalQuery(BaseModel):
    """One query and the passages a human judged relevant to it."""

    query: str
    #: passage id -> graded relevance (1.0 = on point). A bare list in the YAML
    #: is read as all-1.0, so the simple case stays simple.
    relevant: dict[UUID, float] = Field(default_factory=dict)
    note: str | None = None
    #: Filters to apply, matching `SearchFilters`. Lets a query set exercise
    #: filtered retrieval, which is where ANN indexes regress.
    filters: dict[str, Any] | None = None

    @property
    def relevant_ids(self) -> set[UUID]:
        return set(self.relevant)


class QuerySet(BaseModel):
    """A named, versioned set of judged queries.

    Frozen on purpose: a regression set that drifts measures nothing. Add new
    queries as a new version rather than editing in place.
    """

    name: str
    version: str = "1"
    description: str | None = None
    queries: list[EvalQuery] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> QuerySet:
        raw = yaml.safe_load(Path(path).read_text())
        for query in raw.get("queries", []):
            relevant = query.get("relevant")
            if isinstance(relevant, list):
                query["relevant"] = {pid: 1.0 for pid in relevant}
        return cls.model_validate(raw)

    def save(self, path: str | Path) -> None:
        payload = self.model_dump(mode="json", exclude_none=True)
        Path(path).write_text(yaml.safe_dump(payload, sort_keys=False))

    def __len__(self) -> int:
        return len(self.queries)
