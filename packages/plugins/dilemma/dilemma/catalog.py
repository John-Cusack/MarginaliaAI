"""The pack's declared knowledge: which editions exist, and how readers hold them.

Two kinds of YAML ship with the pack and are validated here before anything
reaches the corpus:

- **The editions catalog** (`editions/*.yaml`): one entry per *edition*, shared
  by every profile. An entry says what the edition is, which work it is an
  edition of, where it is believed to be and on what terms, and what its known
  shape is (31,102 verses) so a load can be checked. Where something needs
  checking, the entry says so in `verify`, and a licence nobody has checked is
  `unverified` — never a guess. Nothing in it says who holds the edition
  authoritative: the King James Bible is one edition whoever reads it.
- **Profiles** (`profiles/<key>/`): one reader's view of the library. Its
  `profile.yaml` gives editions roles (`binding`, `external`, `reference`,
  `defense`, `critical`, `fact`) with tiers and dates; `manifest.yaml` groups
  the editions it needs into slices loaded in order; the remaining files —
  entities, topics, hatches, vouch seeds, gold — are optional, and a *blind*
  profile may not have the last four at all (see `guard.py`).

Hatch instances name a generic hatch type from `hatch_types.yaml`, so scoring
and evaluation work the same across traditions.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date  # noqa: TC003 - pydantic needs it at runtime
from functools import cache
from importlib.resources import files
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: How a blocked source could still be had. Mirrors what the scan/purchase
#: queue can act on: a scan of a book in hand, an ebook, a print copy, a
#: library loan, an API key, or permission from a rights holder.
SUGGESTIONS = (
    "scan",
    "purchase_ebook",
    "purchase_print",
    "library_borrow",
    "api_key",
    "permission",
)
METHODS = ("api", "bulk_download", "scrape", "scan", "purchase_ebook", "manual")
ROLES = ("binding", "external", "reference", "defense", "critical", "fact")
#: The tier each role may carry. Binding texts are T1–T3 (what counts as
#: "internal" is the profile's `internal_tiers`); reference, defense and
#: critical texts sit at T4, T5 and T6; external and fact sources have none.
ROLE_TIERS: dict[str, tuple[int, ...]] = {
    "binding": (1, 2, 3),
    "reference": (4,),
    "defense": (5,),
    "critical": (6,),
    "external": (),
    "fact": (),
}
VOUCH_KINDS = (
    "affirms_reliability",
    "affirms_authority",
    "claims_prophesied_by",
    "self_reliability",
    "affirms_fallibility",
)
#: Files a profile may carry beyond `profile.yaml` and `manifest.yaml`.
OPTIONAL_FILES = ("entities.yaml", "topics.yaml", "hatches.yaml", "vouches.yaml", "gold.yaml")
#: The files a blind profile must not carry: each would point the run at the
#: answer it is meant to find on its own.
BLIND_FORBIDDEN = ("topics.yaml", "hatches.yaml", "vouches.yaml", "gold.yaml")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")
_PROFILE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class _Strict(BaseModel):
    # A bare YAML year (`date_published: 1830`) is a number; read it as text.
    model_config = ConfigDict(extra="forbid", frozen=True, coerce_numbers_to_str=True)


# --- The editions catalog ---------------------------------------------------


class Acquire(_Strict):
    method: Literal[METHODS] | None = None  # type: ignore[valid-type]
    url: str | None = None
    license: str = "unverified"
    notes: str | None = None


class Blocked(_Strict):
    reason: str
    suggest: Literal[SUGGESTIONS]  # type: ignore[valid-type]


class EditionEntry(_Strict):
    key: str
    title: str
    #: The work this edition is an edition of. Seven Book of Mormon editions
    #: share one `work`; claims are read once per work and carried across.
    work: str
    author: str | None = None
    edition: str | None = None
    translator: str | None = None
    date_composed: str | None = None
    date_published: str | None = None
    language: str = "en"
    collection: Literal["main", "heldout_eval"] = "main"
    acquire: Acquire = Field(default_factory=Acquire)
    blocked: Blocked | None = None
    #: Already in the corpus from an earlier pipeline (e.g. WLC): register it,
    #: do not re-ingest it.
    existing: bool = False
    optional: bool = False
    #: Known shape, by unit kind: {"book": 66, "verse": 31102}.
    expect: dict[str, int] = Field(default_factory=dict)
    #: Which units of the source to load, when only part of it is wanted
    #: (e.g. some books of a translation). Interpreted by the source's reader.
    select: dict[str, Any] = Field(default_factory=dict)
    #: What must be checked rather than assumed.
    verify: tuple[str, ...] = ()
    notes: str | None = None

    @field_validator("key")
    @classmethod
    def _key_shape(cls, value: str) -> str:
        if not _KEY.match(value):
            raise ValueError(f"edition key {value!r} must be letters, digits, '.', '-'")
        return value

    @model_validator(mode="after")
    def _blocked_is_not_acquired(self) -> EditionEntry:
        if self.blocked and self.existing:
            raise ValueError(f"{self.key}: an existing source cannot also be blocked")
        return self


class EditionGroup(_Strict):
    group: str
    title: str
    brief: str
    sources: tuple[EditionEntry, ...]


@cache
def editions() -> tuple[EditionGroup, ...]:
    """Every group, validated, with edition keys unique across all of them."""
    loaded = tuple(
        EditionGroup.model_validate(_yaml(path))
        for path in sorted(_data("editions").iterdir(), key=str)
        if path.name.endswith(".yaml")
    )
    seen: dict[str, str] = {}
    for group in loaded:
        for entry in group.sources:
            if entry.key in seen:
                raise ValueError(
                    f"edition key {entry.key!r} appears in groups "
                    f"{seen[entry.key]} and {group.group}"
                )
            seen[entry.key] = group.group
    return loaded


def entries() -> list[tuple[str, EditionEntry]]:
    """`(group, entry)` for every edition in the catalog."""
    return [(group.group, entry) for group in editions() for entry in group.sources]


def entry(key: str) -> tuple[str, EditionEntry]:
    for group, found in entries():
        if found.key == key:
            return group, found
    raise KeyError(f"no edition {key!r} in the catalog")


# --- Profiles ---------------------------------------------------------------


class RoleSpec(_Strict):
    """What one edition is to the profile, when, and for which part of it."""

    edition: str
    role: Literal[ROLES]  # type: ignore[valid-type]
    tier: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    #: Narrows the role to part of the edition, by locator: {"books": [...]}.
    scope: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None

    @field_validator("tier", mode="before")
    @classmethod
    def _tier(cls, value: Any) -> Any:
        # Profiles write tiers as the brief does, "T1"; store the number.
        if isinstance(value, str) and re.fullmatch(r"T[1-9]", value):
            return int(value[1])
        return value

    @model_validator(mode="after")
    def _consistent(self) -> RoleSpec:
        allowed = ROLE_TIERS[self.role]
        if allowed and self.tier not in allowed:
            raise ValueError(
                f"{self.edition}: a {self.role} role takes tier "
                f"{'/'.join(f'T{t}' for t in allowed)}, not {self.tier}"
            )
        if not allowed and self.tier is not None:
            raise ValueError(f"{self.edition}: a {self.role} role has no tier")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError(
                f"{self.edition}: valid_to {self.valid_to} is not after {self.valid_from}"
            )
        return self

    def as_client_role(self) -> dict[str, Any]:
        return {
            "edition_key": self.edition,
            "role": self.role,
            "tier": self.tier,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "scope": dict(self.scope),
            "notes": self.notes,
        }


class ProfileSpec(_Strict):
    key: str
    version: int = Field(ge=1)
    title: str
    #: Blind profiles are validation targets: no topics, hatch instances,
    #: vouch seeds or gold may ship with them (enforced by `guard.py`).
    blind: bool = False
    #: Which binding tiers count as "internal" for a contradiction.
    internal_tiers: tuple[int, ...] = (1, 2, 3)
    roles: tuple[RoleSpec, ...]
    cost_notes: str | None = None
    notes: str | None = None

    @field_validator("internal_tiers", mode="before")
    @classmethod
    def _tiers(cls, value: Any) -> Any:
        return tuple(
            int(v[1]) if isinstance(v, str) and re.fullmatch(r"T[1-9]", v) else v
            for v in value
        )

    @field_validator("key")
    @classmethod
    def _key_shape(cls, value: str) -> str:
        if not _PROFILE_KEY.match(value):
            raise ValueError(f"profile key {value!r} must be lower_snake_case")
        return value


class Slice(_Strict):
    """Editions loaded together, in the order the work needs them."""

    name: str
    sources: tuple[str, ...]
    note: str | None = None


class ProfileManifest(_Strict):
    slices: tuple[Slice, ...]


class EntitySpec(_Strict):
    """A seeded entity with an exact id, so two Nephis are never one."""

    id: str
    label: str
    kind: str
    aliases: tuple[str, ...] = ()
    #: For a named text: the edition and part of it the name is taken to
    #: denote. A disclosed assumption, recorded as data.
    denotes: dict[str, Any] | None = None
    note: str | None = None


class Entities(_Strict):
    entities: tuple[EntitySpec, ...]


class Topic(_Strict):
    key: str
    label: str
    children: tuple[Topic, ...] = ()


class Topics(_Strict):
    topics: tuple[Topic, ...]


class HatchType(_Strict):
    key: str
    label: str
    description: str


class HatchInstance(_Strict):
    key: str
    type: str
    name: str
    description: str
    triggers: tuple[str, ...]
    defeaters: tuple[str, ...]
    #: Leads to texts that may close the hatch — locators, verified at load.
    known_closers: tuple[str, ...] = ()


class Hatches(_Strict):
    hatches: tuple[HatchInstance, ...]


class Locus(_Strict):
    edition: str
    locator: str


class VouchSeed(_Strict):
    at: Locus
    kind: Literal[VOUCH_KINDS]  # type: ignore[valid-type]
    #: What is vouched for: an edition key or a seeded entity id.
    target: str
    note: str | None = None


class Vouches(_Strict):
    vouches: tuple[VouchSeed, ...]


class GoldItem(_Strict):
    id: str
    split: Literal["tune", "test"]
    kind: Literal["contradiction", "tension", "negative"]
    pair_type: Literal[
        "internal", "vouched", "edition_change", "parallel_anomaly", "failed_prophecy"
    ]
    #: Well known enough to be in a model's training data; recall on famous
    #: pairs is reported apart from recall on the rest.
    famous: bool = False
    a: tuple[Locus, ...]
    b: tuple[Locus, ...]
    #: Editions or source kinds the item needs; skipped, with a note, until
    #: they are loaded.
    requires: tuple[str, ...] = ()
    notes: str | None = None

    @field_validator("a", "b", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return [value] if isinstance(value, dict) else value


class Gold(_Strict):
    items: tuple[GoldItem, ...]


class LoadedProfile(BaseModel):
    """A profile with every file it ships, validated against the catalog."""

    model_config = ConfigDict(frozen=True)

    spec: ProfileSpec
    manifest: ProfileManifest
    entities: Entities | None = None
    topics: Topics | None = None
    hatches: Hatches | None = None
    vouches: Vouches | None = None
    gold: Gold | None = None
    #: SHA-256 over every file of the profile and the shared hatch types: the
    #: identity every run records.
    profile_hash: str
    files: tuple[str, ...]

    def editions_needed(self) -> list[str]:
        """Every edition the profile names, in slice order, then by role."""
        keys: list[str] = []
        for part in self.manifest.slices:
            keys += [k for k in part.sources if k not in keys]
        keys += [r.edition for r in self.spec.roles if r.edition not in keys]
        return keys

    def slice(self, name: str) -> Slice:
        for part in self.manifest.slices:
            if part.name == name:
                return part
        known = [part.name for part in self.manifest.slices]
        raise KeyError(f"profile {self.spec.key!r} has no slice {name!r}; it has {known}")


@cache
def hatch_types() -> tuple[HatchType, ...]:
    return tuple(
        HatchType.model_validate(t) for t in _yaml(_data("hatch_types.yaml"))["types"]
    )


def profile_keys() -> list[str]:
    return sorted(p.name for p in _data("profiles").iterdir() if p.is_dir())


@cache
def profile(key: str) -> LoadedProfile:
    """Load and cross-check one profile against the catalog and hatch types."""
    root = _data("profiles").joinpath(key)
    if not root.is_dir():
        raise KeyError(f"no profile {key!r}; profiles are {profile_keys()}")
    present = sorted(p.name for p in root.iterdir() if p.is_file())
    unknown = set(present) - {"profile.yaml", "manifest.yaml", *OPTIONAL_FILES}
    if unknown:
        raise ValueError(f"profile {key!r} has unexpected files {sorted(unknown)}")
    spec = ProfileSpec.model_validate(_yaml(root.joinpath("profile.yaml")))
    if spec.key != key:
        raise ValueError(f"profiles/{key}/profile.yaml declares key {spec.key!r}")

    def optional(name: str, model: type[BaseModel]) -> Any:
        return model.model_validate(_yaml(root.joinpath(name))) if name in present else None

    loaded = LoadedProfile(
        spec=spec,
        manifest=ProfileManifest.model_validate(_yaml(root.joinpath("manifest.yaml"))),
        entities=optional("entities.yaml", Entities),
        topics=optional("topics.yaml", Topics),
        hatches=optional("hatches.yaml", Hatches),
        vouches=optional("vouches.yaml", Vouches),
        gold=optional("gold.yaml", Gold),
        profile_hash=_hash(root, present),
        files=tuple(present),
    )
    _cross_check(loaded, present)
    return loaded


def _cross_check(loaded: LoadedProfile, present: list[str]) -> None:
    key = loaded.spec.key
    catalog = {e.key for _, e in entries()}
    named = set(loaded.editions_needed())
    if loaded.vouches:
        named |= {v.at.edition for v in loaded.vouches.vouches}
    if loaded.gold:
        named |= {loc.edition for item in loaded.gold.items for loc in (*item.a, *item.b)}
    if loaded.entities:
        named |= {
            e.denotes["edition"] for e in loaded.entities.entities
            if e.denotes and "edition" in e.denotes
        }
    missing = sorted(named - catalog)
    if missing:
        raise ValueError(f"profile {key!r} names editions not in the catalog: {missing}")
    if loaded.hatches:
        types = {t.key for t in hatch_types()}
        bad = sorted({h.type for h in loaded.hatches.hatches} - types)
        if bad:
            raise ValueError(f"profile {key!r} uses unknown hatch types {bad}")
    if loaded.spec.blind:
        shipped = sorted(set(present) & set(BLIND_FORBIDDEN))
        if shipped:
            raise ValueError(
                f"profile {key!r} is blind and may not ship {shipped}: each would "
                f"point the run at what it must find on its own"
            )
    slices = [s.name for s in loaded.manifest.slices]
    if len(slices) != len(set(slices)):
        raise ValueError(f"profile {key!r} repeats a slice name: {slices}")


def _hash(root: Any, present: list[str]) -> str:
    digest = hashlib.sha256()
    for name in present:
        digest.update(f"profiles/{root.name}/{name}\0".encode())
        digest.update(root.joinpath(name).read_bytes())
        digest.update(b"\0")
    shared = _data("hatch_types.yaml")
    digest.update(b"hatch_types.yaml\0" + shared.read_bytes())
    return digest.hexdigest()


def _data(name: str) -> Any:
    return files("dilemma").joinpath(name)


def _yaml(path: Any) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
