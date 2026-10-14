"""Write a sample of loaded units for a person to read against the source.

Twenty units per source, chosen reproducibly from the source key, each with
its reference, locator and verbatim text. The file lives in the pack's data
directory (it quotes the source, which may be in copyright) at
``<data_dir>/qa/<source_key>.md``.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dilemma.catalog import EditionEntry
    from research_engine_sdk.units import UnitDocument

SAMPLE_SIZE = 20


def dump(
    data_dir: Path, entry: EditionEntry, documents: list[UnitDocument], *, parser: str
) -> Path:
    units = [(document, unit) for document in documents for unit in document.units]
    sample = sorted(
        random.Random(entry.key).sample(range(len(units)), min(SAMPLE_SIZE, len(units)))
    )
    lines = [
        f"# QA sample — {entry.title}",
        "",
        f"- source key: `{entry.key}`",
        f"- parser: `{parser}`",
        f"- documents: {len(documents)}; units: {len(units)}; sampled: {len(sample)}",
        "",
        "Check each unit against the artifact: wording, spelling, punctuation, "
        "and that the reference is the right one.",
        "",
    ]
    for index in sample:
        document, unit = units[index]
        lines += [
            f"## {unit.ref}",
            "",
            f"*{document.title}* · {unit.kind}"
            + (f" · label `{unit.label}`" if unit.label else "")
            + (f" · {unit.locator}" if unit.locator else ""),
            "",
            *(f"> {line}" if line else ">" for line in unit.text.splitlines()),
            "",
        ]
    path = Path(data_dir) / "qa" / f"{entry.key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
