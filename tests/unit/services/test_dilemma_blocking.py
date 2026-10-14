"""Stage 0 horn eligibility, without a database.

The filter talks to plain role rows, so scripted namespaces stand in for
whatever a roles collaborator's ``roles_for_profile`` would return — the
profile repo itself is never touched. What is under test is the Stage 0
rule: a claim is horn-eligible iff its edition holds the ``binding`` role
over ``as_of`` and its modality is put forward as what is so. Storage,
pairing, and scoring belong to later stages.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_engine.domain.machine_claims import MachineClaim, Modality
from research_engine.domain.profiles import SourceRole
from research_engine.services.dilemma.blocking import (
    BLOCKED_REASONS,
    HornEligibility,
    eligible_claim_ids,
)

pytestmark = pytest.mark.unit

EDITION_A = uuid4()
EDITION_B = uuid4()
PROFILE_ID = uuid4()
AS_OF = date(2026, 10, 13)


def _claim(edition_id=EDITION_A, modality=Modality.asserts, statement=None):
    return MachineClaim(
        id=uuid4(),
        run_id=uuid4(),
        document_id=uuid4(),
        edition_id=edition_id,
        statement=statement or "The caravan paid no toll at the northern gate.",
        modality=modality,
        created_at=datetime.now(UTC),
    )


def _role(edition_id=EDITION_A, role="binding", valid_from=None, valid_to=None):
    return SimpleNamespace(
        edition_id=edition_id,
        role=role,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _blocked(**overrides):
    counts = dict.fromkeys(BLOCKED_REASONS, 0)
    counts.update(overrides)
    return counts


def test_binding_window_and_assertion_are_eligible() -> None:
    claim = _claim()
    roles = [_role(valid_from=date(2026, 1, 1), valid_to=date(2027, 1, 1))]

    eligible, blocked = eligible_claim_ids([claim], roles, AS_OF)

    assert eligible == [claim.id]
    assert blocked == _blocked()


def test_cross_edition_claims_are_each_eligible() -> None:
    first = _claim(edition_id=EDITION_A)
    second = _claim(
        edition_id=EDITION_B,
        statement="The caravan crossed the ford by daylight.",
    )
    roles = [_role(edition_id=EDITION_A), _role(edition_id=EDITION_B)]

    eligible, blocked = eligible_claim_ids([first, second], roles, AS_OF)

    assert eligible == [first.id, second.id]
    assert blocked == _blocked()


def test_same_edition_claims_are_both_eligible() -> None:
    first = _claim(statement="The caravan paid no toll at the northern gate.")
    second = _claim(statement="The caravan carried books and salt.")

    eligible, blocked = eligible_claim_ids([first, second], [_role()], AS_OF)

    assert eligible == [first.id, second.id]
    assert blocked == _blocked()


def test_open_window_with_no_bounds_is_eligible() -> None:
    claim = _claim()

    eligible, blocked = eligible_claim_ids([claim], [_role()], AS_OF)

    assert eligible == [claim.id]
    assert blocked == _blocked()


def test_null_valid_to_stays_open() -> None:
    claim = _claim()
    roles = [_role(valid_from=date(2020, 5, 1), valid_to=None)]

    eligible, blocked = eligible_claim_ids([claim], roles, date(2030, 5, 5))

    assert eligible == [claim.id]
    assert blocked == _blocked()


def test_window_start_is_inclusive_and_end_is_exclusive() -> None:
    opening = _claim(statement="The caravan set out at dawn.")
    closing = _claim(statement="The caravan rested at the well.")
    roles = [_role(valid_from=date(2026, 1, 1), valid_to=date(2026, 10, 13))]

    eligible, blocked = eligible_claim_ids([opening], roles, date(2026, 1, 1))
    assert eligible == [opening.id]

    eligible, blocked = eligible_claim_ids([closing], roles, AS_OF)
    assert eligible == []
    assert blocked == _blocked(outside_date_window=1)


def test_binding_outside_the_window_is_blocked() -> None:
    claim = _claim()
    roles = [_role(valid_from=date(2025, 1, 1), valid_to=date(2026, 1, 1))]

    eligible, blocked = eligible_claim_ids([claim], roles, AS_OF)

    assert eligible == []
    assert blocked == _blocked(outside_date_window=1)


@pytest.mark.parametrize("modality", [Modality.question, Modality.hypothetical])
def test_questions_and_hypotheses_are_blocked(modality) -> None:
    claim = _claim(modality=modality)

    eligible, blocked = eligible_claim_ids([claim], [_role()], AS_OF)

    assert eligible == []
    assert blocked == _blocked(excluded_modality=1)


@pytest.mark.parametrize(
    "role", ["external", "reference", "defense", "critical", "fact"]
)
def test_other_roles_are_never_horns(role) -> None:
    claim = _claim()
    roles = [_role(role=SourceRole(role))]

    eligible, blocked = eligible_claim_ids([claim], roles, AS_OF)

    assert eligible == []
    assert blocked == _blocked(non_binding_role=1)


def test_an_edition_with_no_role_is_blocked() -> None:
    claim = _claim(edition_id=EDITION_B)
    roles = [_role(edition_id=EDITION_A)]

    eligible, blocked = eligible_claim_ids([claim], roles, AS_OF)

    assert eligible == []
    assert blocked == _blocked(missing_role=1)


def test_a_claim_with_no_edition_is_blocked() -> None:
    claim = _claim(edition_id=None)

    eligible, blocked = eligible_claim_ids([claim], [_role()], AS_OF)

    assert eligible == []
    assert blocked == _blocked(missing_role=1)


def test_empty_claims_report_zeroed_counts() -> None:
    eligible, blocked = eligible_claim_ids([], [_role()], AS_OF)

    assert eligible == []
    assert blocked == _blocked()
    assert set(blocked) == set(BLOCKED_REASONS)


class _Roles:
    """Fake roles collaborator: answers `roles_for_profile` from a script."""

    def __init__(self, rows):
        self._rows = rows
        self.seen = []

    def roles_for_profile(self, profile_id):
        self.seen.append(profile_id)
        return list(self._rows)


class _AsyncRoles(_Roles):
    async def roles_for_profile(self, profile_id):
        self.seen.append(profile_id)
        return list(self._rows)


async def test_filter_reads_roles_through_the_collaborator() -> None:
    claim = _claim()
    roles = _Roles([_role()])
    filt = HornEligibility(roles)

    eligible, blocked = await filt.eligible_claim_ids([claim], PROFILE_ID, AS_OF)

    assert eligible == [claim.id]
    assert blocked == _blocked()
    assert roles.seen == [PROFILE_ID]


async def test_filter_also_accepts_an_async_collaborator() -> None:
    claim = _claim(edition_id=None)
    filt = HornEligibility(_AsyncRoles([_role()]))

    eligible, blocked = await filt.eligible_claim_ids([claim], PROFILE_ID, AS_OF)

    assert eligible == []
    assert blocked == _blocked(missing_role=1)
