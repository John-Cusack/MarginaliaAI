"""Check a parse against what the edition is known to contain.

A load that silently drops a chapter is worse than one that fails: every later
stage would treat the gap as absence of evidence. So each parse is counted
against the catalog's `expect` (31,102 verses, 239 chapters), and every
discrepancy is reported — a duplicate reference, a count off by one — rather
than repaired. Nothing here knows which verses any analysis needs: coverage
is about the edition, never about an answer.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dilemma.catalog import EditionEntry
    from research_engine_sdk.units import UnitDocument

CHECKER = "dilemma.coverage"
CHECKER_VERSION = "1"


@dataclass
class Count:
    unit: str
    loaded: int
    expected: int | None
    anomalies: list[Any] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.expected is None or self.loaded == self.expected) and not self.anomalies


def check(entry: EditionEntry, documents: list[UnitDocument]) -> list[Count]:
    """One count per unit kind seen or expected, plus duplicate references."""
    observed: Counter[str] = Counter()
    refs: Counter[str] = Counter()
    for document in documents:
        observed[document.node_type] += 1
        for unit in document.units:
            observed[unit.kind] += 1
            refs[unit.ref] += 1

    counts = [
        Count(unit=kind, loaded=observed.get(kind, 0), expected=entry.expect.get(kind))
        for kind in sorted(set(observed) | set(entry.expect))
    ]
    duplicates = sorted(ref for ref, n in refs.items() if n > 1)
    if duplicates:
        counts.append(
            Count(unit="duplicate_refs", loaded=len(duplicates), expected=0,
                  anomalies=[f"duplicate reference {ref}" for ref in duplicates[:50]])
        )
    return counts
