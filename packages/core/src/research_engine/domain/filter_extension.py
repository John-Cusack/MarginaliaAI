"""FilterExtension protocol — pluggable search filters for passage candidates."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import sqlalchemy as sa


@runtime_checkable
class FilterExtension(Protocol):
    """A pluggable filter that narrows passage candidates via a SQL subquery.

    Plugins implement this protocol and register instances in pack.yaml
    under ``provides.filter_extensions``.  Core composes these into the
    filter-pushdown stage of hybrid search.
    """

    @property
    def filter_id(self) -> str:
        """Unique ID, e.g. 'scripture_ref_range' or 'event_date_range'."""
        ...

    @property
    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for the filter value the LLM provides."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description so the LLM knows when to use this filter."""
        ...

    def build_clause(self, value: Any) -> sa.sql.expression.SelectBase:
        """Return a SELECT that yields passage_id rows matching *value*.

        Core uses::

            passages.c.id.in_(extension.build_clause(value))

        Must use bind parameters, never string interpolation.
        """
        ...
