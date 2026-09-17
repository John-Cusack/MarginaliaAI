"""Mechanical claim-ledger checks; never a reasoner or auto-fixer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from research_engine.domain.errors import NotFoundError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from research_engine.domain.claims import ClaimAuditReport
    from research_engine.ports.repositories import ClaimRepo


class ClaimAuditService:
    def __init__(self, claims: ClaimRepo) -> None:
        self._claims = claims

    async def audit(self, refs: Sequence[str] | None = None) -> ClaimAuditReport:
        """Run the repository checks over all claims or the named subjects."""
        checked: list[str] | None = None
        if refs is not None:
            checked = []
            seen = set()
            for ref in refs:
                if not isinstance(ref, str) or not ref.strip():
                    raise ValueError("refs must contain non-empty claim refs")
                normalized = ref.strip()
                if normalized not in seen:
                    seen.add(normalized)
                    checked.append(normalized)
        if checked:
            existing = await self._claims.existing_refs(checked)
            missing = [ref for ref in checked if ref not in existing]
            if missing:
                raise NotFoundError("claim", missing[0])
        return await self._claims.audit(checked)
