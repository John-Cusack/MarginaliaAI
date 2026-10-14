"""The catalog and the profiles say only what is known, and hold their own rules.

Edition keys are unique; a blocked edition says how it could be had; nothing
claims a licence nobody checked; a site whose terms forbid robots is acquired
by hand and is not on the fetch allowlist. Profiles name only catalogued
editions, give each role a tier it may carry, and hash every file they ship.
"""

from __future__ import annotations

from importlib.resources import files

import pytest
import yaml
from dilemma import catalog

pytestmark = pytest.mark.unit

#: Hosts whose terms of use forbid automated access.
NO_ROBOTS = ("churchofjesuschrist.org", "josephsmithpapers.org")


def test_edition_keys_are_unique() -> None:
    keys = [entry.key for _, entry in catalog.entries()]
    assert len(keys) == len(set(keys))


def test_blocked_editions_say_how_they_could_be_had() -> None:
    blocked = [e for _, e in catalog.entries() if e.blocked]
    assert blocked
    assert all(e.blocked.reason and e.blocked.suggest for e in blocked)


def test_no_licence_is_asserted_without_a_check_or_a_known_status() -> None:
    """A licence is `unverified`, names a public-domain or copyright status, a
    permission the operator obtained, or the provider's own stated terms —
    never a bare guess like "CC BY"."""
    allowed = ("unverified", "public domain", "in copyright", "©", "permission:", "Tanzil")
    for _, entry in catalog.entries():
        assert entry.acquire.license.startswith(allowed), (entry.key, entry.acquire.license)


def test_sites_that_forbid_robots_are_acquired_by_hand() -> None:
    for _, entry in catalog.entries():
        if entry.acquire.url and any(host in entry.acquire.url for host in NO_ROBOTS):
            assert entry.acquire.method == "manual", entry.key


def test_the_fetch_allowlist_leaves_out_sites_that_forbid_robots() -> None:
    manifest = yaml.safe_load(files("dilemma").joinpath("plugin.yaml").read_text())
    allowlist = manifest["permissions"]["network_allowlist"]
    assert not set(NO_ROBOTS) & set(allowlist)


def test_the_heldout_collection_holds_only_evaluation_material() -> None:
    heldout = [e.key for _, e in catalog.entries() if e.collection == "heldout_eval"]
    assert heldout == ["christian-apologetics-to-islam-heldout"]


def test_every_profile_loads_and_names_only_catalogued_editions() -> None:
    assert catalog.profile_keys() == ["lds", "sunni_islam"]
    for key in catalog.profile_keys():
        loaded = catalog.profile(key)
        assert loaded.spec.key == key
        assert len(loaded.profile_hash) == 64


def test_the_hash_changes_with_any_file_of_the_profile(tmp_path, monkeypatch) -> None:
    root = files("dilemma").joinpath("profiles").joinpath("sunni_islam")
    present = sorted(p.name for p in root.iterdir() if p.is_file())
    before = catalog._hash(root, present)

    copy = tmp_path / "sunni_islam"
    copy.mkdir()
    for name in present:
        (copy / name).write_bytes(root.joinpath(name).read_bytes())
    assert catalog._hash(copy, present) == before
    (copy / "manifest.yaml").write_text(root.joinpath("manifest.yaml").read_text() + "\n# x\n")
    assert catalog._hash(copy, present) != before


def test_roles_carry_only_the_tiers_their_kind_allows() -> None:
    with pytest.raises(ValueError, match="takes tier"):
        catalog.RoleSpec(edition="x", role="binding", tier="T5")
    with pytest.raises(ValueError, match="has no tier"):
        catalog.RoleSpec(edition="x", role="external", tier="T1")
    assert catalog.RoleSpec(edition="x", role="defense", tier="T5").tier == 5


def test_a_reversed_window_is_refused() -> None:
    with pytest.raises(ValueError, match="not after"):
        catalog.RoleSpec(edition="x", role="binding", tier=1,
                         valid_from="1921-01-01", valid_to="1835-01-01")


def test_the_lectures_on_faith_window_is_recorded() -> None:
    (role,) = [
        r for r in catalog.profile("lds").spec.roles if r.edition == "lectures-on-faith-1835"
    ]
    assert (role.role, role.tier) == ("binding", 1)
    assert (role.valid_from.year, role.valid_to.year) == (1835, 1921)


def test_an_essay_can_bind_and_defend_at_once() -> None:
    roles = {
        r.role for r in catalog.profile("lds").spec.roles
        if r.edition == "gte-race-and-the-priesthood"
    }
    assert roles == {"binding", "defense"}


def test_hatch_instances_name_a_generic_type() -> None:
    types = {t.key for t in catalog.hatch_types()}
    hatches = catalog.profile("lds").hatches
    assert hatches is not None
    assert {h.type for h in hatches.hatches} <= types


def test_gold_has_both_splits_and_negatives() -> None:
    gold = catalog.profile("lds").gold
    assert gold is not None
    assert {item.split for item in gold.items} == {"tune", "test"}
    assert any(item.kind == "negative" for item in gold.items)
    ids = [item.id for item in gold.items]
    assert len(ids) == len(set(ids))


def test_a_blind_profile_ships_roles_manifest_and_entities_only() -> None:
    blind = catalog.profile("sunni_islam")
    assert blind.spec.blind
    assert set(blind.files) == {"profile.yaml", "manifest.yaml", "entities.yaml"}
    assert blind.topics is blind.hatches is blind.vouches is blind.gold is None


def test_a_slice_names_only_editions_the_profile_needs() -> None:
    for key in catalog.profile_keys():
        loaded = catalog.profile(key)
        for part in loaded.manifest.slices:
            assert set(part.sources) <= set(loaded.editions_needed())
    with pytest.raises(KeyError, match="no slice"):
        catalog.profile("lds").slice("absent")
