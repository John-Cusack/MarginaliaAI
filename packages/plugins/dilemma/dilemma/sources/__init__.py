"""Source readers: how each catalog edition is got and parsed into units.

One module per source format lives beside this file (`scriptures_json.py`,
`usfm.py`, `tanzil.py`, …). Each defines readers and registers them with
`@register`; the registry imports every module in this package on first use,
so adding a reader never touches `plugin.yaml` and never changes the pack's
approval hash.

A reader does two things and nothing else:

- `acquire` fetches the edition's raw artifacts through the polite fetcher —
  or, for a site whose terms forbid automated access, takes the files the
  operator saved into the edition's inbox — and returns them as stored by
  digest. It never parses.
- `parse` turns those artifacts into `UnitDocument`s: units verbatim, with
  references and locators, original spelling and punctuation untouched. It
  never fetches, so it can be tested on fixtures offline.

Everything after — layout (`research_engine_sdk.units`), ingest, coverage,
QA, status — is the pipeline's job, the same for every source.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from dilemma.catalog import EditionEntry
    from dilemma.fetch import PoliteFetcher, Stored
    from research_engine_sdk.units import UnitDocument


class AwaitingOperator(Exception):  # noqa: N818 - a state, not a failure
    """The edition can only come from files the operator saves; none are there yet."""


@dataclass(frozen=True)
class AcquireContext:
    entry: EditionEntry
    fetcher: PoliteFetcher
    #: Where an operator drops local files (saved pages, scans, purchases) for
    #: this edition: ``<data_dir>/inbox/<edition_key>/``.
    inbox: Path

    def inbox_files(self, *suffixes: str) -> list[Path]:
        """The operator's files for this edition, or `AwaitingOperator` if none.

        The message says exactly what to save and where, so the waiting list
        is a set of instructions rather than a status.
        """
        found = (
            sorted(
                p for p in self.inbox.iterdir()
                if p.is_file() and (not suffixes or p.suffix.lower() in suffixes)
            )
            if self.inbox.is_dir()
            else []
        )
        if not found:
            what = self.entry.acquire.url or "the source"
            raise AwaitingOperator(
                f"save {what} ({', '.join(suffixes) or 'any file'}) into {self.inbox}"
                + (f" — {self.entry.acquire.notes.strip()}" if self.entry.acquire.notes else "")
            )
        return found


@runtime_checkable
class SourceAdapter(Protocol):
    #: Catalog edition keys this reader handles.
    keys: ClassVar[tuple[str, ...]]
    #: Stamped on every document, its text and its spans: which code read the
    #: artifact. Bump the version whenever the parse would change any text.
    parser: ClassVar[str]
    parser_version: ClassVar[str]

    async def acquire(self, context: AcquireContext) -> list[Stored]: ...

    def parse(self, key: str, artifacts: list[Stored]) -> Iterable[UnitDocument]: ...


_registry: dict[str, type[SourceAdapter]] = {}
_discovered = False


def register(adapter: type[SourceAdapter]) -> type[SourceAdapter]:
    """Class decorator: make a reader the handler for its edition keys."""
    for key in adapter.keys:
        existing = _registry.get(key)
        if existing is not None and existing is not adapter:
            raise ValueError(
                f"source {key!r} already has adapter {existing.__name__}; "
                f"{adapter.__name__} cannot also claim it"
            )
        _registry[key] = adapter
    return adapter


def adapter_for(key: str) -> SourceAdapter | None:
    _discover()
    adapter = _registry.get(key)
    return adapter() if adapter is not None else None


def keys_with_adapters() -> set[str]:
    _discover()
    return set(_registry)


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")
    _discovered = True
