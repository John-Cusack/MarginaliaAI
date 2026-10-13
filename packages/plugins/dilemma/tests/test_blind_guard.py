"""The blind rule holds: nothing in the pack points a blind run at its answer.

The structural checks run everywhere. The sealed-term check runs only where
the operator keeps the sealed file — never in CI, never inside a repository —
and reports where a match is, not what it matched. Fixtures that must look
like references are assembled at run time, so this file holds none itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from dilemma import guard

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[4]
#: Design records that describe the blind protocol, scanned like the pack.
DOCS = [REPO / "docs" / "design" / "decision-002-claim-layer-in-core.md",
        REPO / "docs" / "design" / "decision-003-dilemma-finder-v2.md"]


def ref(chapter: int, verse: int) -> str:
    return f"{chapter}" + ":" + f"{verse}"


def test_the_pack_and_its_design_records_pass_the_structural_checks() -> None:
    assert [str(v) for v in guard.structural_violations(DOCS)] == []


def test_a_qualified_reference_anywhere_is_caught(tmp_path: Path) -> None:
    planted = tmp_path / "prompt.j2"
    planted.write_text("Consider " + "Surah " + ref(9, 9) + " carefully.\n")

    found = guard.structural_violations([planted])

    assert [(v.line, v.rule) for v in found if v.path.endswith("prompt.j2")] == [
        (1, "Qur'an reference")
    ]


def test_a_bare_verse_reference_outside_a_blind_profile_is_fine(tmp_path: Path) -> None:
    other = tmp_path / "lds_gold.yaml"
    other.write_text("locator: Mormon " + ref(7, 9) + "\n")
    assert not [v for v in guard.structural_violations([other]) if v.path.endswith(".yaml")]


def test_sealed_terms_and_loci_are_found_without_being_named(tmp_path: Path) -> None:
    sealed = tmp_path / "sealed.yaml"
    sealed.write_text(
        "guard_terms: [zqxword, transmission_error]\n"
        "pairs:\n  - a: {locator: '" + ref(9, 9) + "'}\n"
    )
    leaky = tmp_path / "prompt.j2"
    leaky.write_text(
        "line one\n"
        "the ZQXWORD hatch\n"
        "see Qur'an " + ref(9, 9) + "\n"
        "a generic transmission_error type\n"
        "Mormon " + ref(9, 9) + " is another scripture's verse\n"
    )

    found = [v for v in guard.sealed_violations(sealed, [leaky]) if v.path.endswith("prompt.j2")]

    assert [(v.line, v.rule) for v in found] == [(2, "sealed term"), (3, "sealed locus")]
    assert all("zqx" not in str(v).lower() for v in found)


def test_no_sealed_term_or_locus_is_in_the_pack() -> None:
    sealed = guard.sealed_path()
    if sealed is None:
        pytest.skip("no sealed file on this machine; the structural checks still ran")
    assert [str(v) for v in guard.sealed_violations(sealed, DOCS)] == []


def test_the_sealed_file_is_never_inside_a_repository() -> None:
    sealed = guard.sealed_path()
    if sealed is None:
        pytest.skip("no sealed file on this machine")
    inside = subprocess.run(
        ["git", "-C", str(sealed.parent), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, check=False,
    )
    assert inside.stdout.strip() != "true", f"{sealed} sits inside a git work tree"
