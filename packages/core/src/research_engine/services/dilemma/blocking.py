"""Stage 0 of the contradiction funnel: which claims may be horns.

A claim is horn-eligible iff its edition holds the ``binding`` role with
``valid_from <= as_of < valid_to`` (a NULL bound means open on that side)
and its modality is not ``question`` or ``hypothetical``. Every other role
(``external``, ``reference``, ``defense``, ``critical`` or ``fact``) can
never be a horn, whatever its window. Same- and cross-edition pairs are a
later stage's concern; this stage judges each claim on its own.

Needs no database: callers pass the edition roles (rows or namespaces
carrying ``edition_id``, ``role``, ``valid_from`` and ``valid_to``),
usually read through an injected roles collaborator exposing
``roles_for_profile`` rather than through the profile repo directly.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.domain.machine_claims import MachineClaim

#: Modalities that can never be horns: a question asks and a hypothesis
#: supposes, so neither is put forward as what is so.
EXCLUDED_MODALITIES = frozenset({"question", "hypothetical"})

#: Every reason a claim can be blocked, in the order it is checked: a claim
#: that cannot be placed under the profile first, then one whose content
#: disqualifies it whatever its standing, then standing of the wrong kind,
#: then standing of the right kind at the wrong date.
BLOCKED_REASONS = (
    "missing_role",
    "excluded_modality",
    "non_binding_role",
    "outside_date_window",
)

_MISSING = object()


def _field(row: Any, name: str) -> Any:
    """Read ``name`` off a row, namespace, or mapping, whichever it is."""
    value = getattr(row, name, _MISSING)
    if value is not _MISSING:
        return value
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return None


def _value(field: Any) -> Any:
    """Unwrap an enum to its value; plain values pass through untouched."""
    return getattr(field, "value", field)


def _as_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


def _covers(role: Any, as_of: date) -> bool:
    """Whether ``role``'s validity window holds ``as_of`` (NULL = open)."""
    start = _as_date(_field(role, "valid_from"))
    end = _as_date(_field(role, "valid_to"))
    return (start is None or start <= as_of) and (end is None or as_of < end)


def eligible_claim_ids(
    claims: list[MachineClaim],
    roles_for_editions: list[Any],
    as_of: date | datetime,
) -> tuple[list[UUID], dict[str, int]]:
    """Split ``claims`` into horn-eligible ids and blocked counts by reason.

    ``roles_for_editions`` is the edition roles in force to judge by — each
    a row or namespace with ``edition_id``, ``role``, ``valid_from`` and
    ``valid_to``. Returns the eligible claim ids in input order and a count
    per reason in :data:`BLOCKED_REASONS` (zero-filled, so every key is
    always present).
    """
    day = _as_date(as_of)
    if day is None:
        raise ValueError("as_of is required")
    by_edition: dict[Any, list[Any]] = {}
    for row in roles_for_editions:
        by_edition.setdefault(_field(row, "edition_id"), []).append(row)

    eligible: list[UUID] = []
    blocked: dict[str, int] = {reason: 0 for reason in BLOCKED_REASONS}
    for claim in claims:
        rows = by_edition.get(claim.edition_id) if claim.edition_id is not None else None
        if not rows:
            blocked["missing_role"] += 1
            continue
        if _value(claim.modality) in EXCLUDED_MODALITIES:
            blocked["excluded_modality"] += 1
            continue
        bindings = [row for row in rows if _value(_field(row, "role")) == "binding"]
        if not bindings:
            blocked["non_binding_role"] += 1
            continue
        if not any(_covers(row, day) for row in bindings):
            blocked["outside_date_window"] += 1
            continue
        eligible.append(claim.id)
    return eligible, blocked


class HornEligibility:
    """Stage 0 filter with a constructor-injected roles collaborator.

    The collaborator only needs ``roles_for_profile(profile_id)`` returning
    the edition-role rows (sync or async); never the profile repo itself.
    """

    def __init__(self, roles: Any) -> None:
        self._roles = roles

    async def eligible_claim_ids(
        self,
        claims: list[MachineClaim],
        profile_id: UUID,
        as_of: date | datetime,
    ) -> tuple[list[UUID], dict[str, int]]:
        """Eligible claim ids under ``profile_id`` at ``as_of`` (see above)."""
        rows = self._roles.roles_for_profile(profile_id)
        if inspect.isawaitable(rows):
            rows = await rows
        return eligible_claim_ids(claims, rows, as_of)
