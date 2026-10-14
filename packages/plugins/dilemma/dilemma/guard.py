"""The blind rule, as checks a build can fail on.

A blind profile is a validation target: the run must find what the profile's
tradition is known for without being pointed at it. So nothing that points —
a locus, a hatch instance, a vouch seed, a gold pair, a topic — may sit in the
profile, or in the prompts, config, code or tests the run is built from.

Two layers, because the answer itself cannot be in the repository:

- **Structural checks run everywhere, CI included.** A blind profile ships
  only `profile.yaml`, `manifest.yaml` and `entities.yaml`, and none of them
  holds a chapter:verse locator. Nothing scanned holds a chapter:verse
  reference qualified as a Qur'an reference by "Q", "Surah" or "Qur'an".
- **Sealed-term checks run only where the sealed file is.** The operator keeps
  it outside every repository (`RE_DILEMMA_SEALED`, by default
  `~/.research-engine/plugin-data/dilemma/sealed/`). Its terms and loci are
  read at check time and never written anywhere; a match is reported by file
  and line, not by the term matched.

The generic hatch types and the title mapping a blind profile may carry are
exempt: they are the brief's disclosed vocabulary, not the answer.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dilemma import catalog

#: Where the sealed file lives unless `RE_DILEMMA_SEALED` says otherwise.
DEFAULT_SEALED_DIR = Path("~/.research-engine/plugin-data/dilemma/sealed").expanduser()
SEALED_ENV = "RE_DILEMMA_SEALED"

#: Any chapter:verse-shaped locator ("2:1", "10 : 3", "4:1-5").
LOCATOR = re.compile(r"(?<![\w:])\d{1,3}\s*:\s*\d{1,3}(?:\s*[-–]\s*\d{1,3})?(?![\w:])")
#: A locator qualified as a Qur'an reference.
QURAN_REF = re.compile(
    r"\b(?:Q|Qur['’]?an|Quran|Koran|Surah?|Sura)\.?\s*\d{1,3}\s*[:.]\s*\d{1,3}",
    re.IGNORECASE,
)
#: Text files worth scanning; data the pack fetches is never in the tree.
SCANNED_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".txt", ".j2", ".jinja", ".toml", ".json"}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def pack_root() -> Path:
    return Path(__file__).resolve().parent


def blind_profiles() -> list[str]:
    return [key for key in catalog.profile_keys() if catalog.profile(key).spec.blind]


def structural_violations(extra_paths: list[Path] | None = None) -> list[Violation]:
    """What CI can check without the sealed file."""
    found: list[Violation] = []
    root = pack_root()
    for key in blind_profiles():
        directory = root / "profiles" / key
        for path in sorted(directory.iterdir()):
            if path.name in catalog.BLIND_FORBIDDEN:
                found.append(Violation(_rel(path), 0, "a blind profile may not ship this file"))
            for number, line in _lines(path):
                if LOCATOR.search(line):
                    found.append(Violation(_rel(path), number, "locator in a blind profile"))
    for path in scanned_files(extra_paths):
        for number, line in _lines(path):
            if QURAN_REF.search(line):
                found.append(Violation(_rel(path), number, "Qur'an reference"))
    return found


def sealed_path() -> Path | None:
    """The sealed file, if the operator has put one where it may be read."""
    configured = os.environ.get(SEALED_ENV)
    candidates = (
        [Path(configured).expanduser()]
        if configured
        else sorted(DEFAULT_SEALED_DIR.glob("*.yaml")) if DEFAULT_SEALED_DIR.is_dir() else []
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def sealed_violations(
    sealed: Path, extra_paths: list[Path] | None = None
) -> list[Violation]:
    """Every scanned line holding a sealed term or a sealed locus.

    Terms are the strings under any `terms` or `guard_terms` key, matched as
    whole words, case-insensitively. Loci are every chapter:verse string in the
    file; outside a blind profile they count only when qualified as Qur'an
    references, since "3:16" alone is a verse in every scripture.
    """
    data = yaml.safe_load(sealed.read_text(encoding="utf-8")) or {}
    exempt = _exempt_terms()
    terms = {
        t for t in _strings_under(data, {"terms", "guard_terms"})
        if t.strip() and t.strip().lower() not in exempt
    }
    loci = {_norm_locus(m.group(0)) for s in _strings(data) for m in LOCATOR.finditer(s)}
    term_patterns = [
        re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE) for term in terms
    ]
    found: list[Violation] = []
    blind_dirs = [pack_root() / "profiles" / key for key in blind_profiles()]
    for path in scanned_files(extra_paths):
        in_blind = any(path.is_relative_to(d) for d in blind_dirs)
        for number, line in _lines(path):
            if any(p.search(line) for p in term_patterns):
                found.append(Violation(_rel(path), number, "sealed term"))
            located = (
                [m.group(0) for m in LOCATOR.finditer(line)]
                if in_blind
                else [_strip_qualifier(m.group(0)) for m in QURAN_REF.finditer(line)]
            )
            if any(_norm_locus(text) in loci for text in located):
                found.append(Violation(_rel(path), number, "sealed locus"))
    return found


def scanned_files(extra_paths: list[Path] | None = None) -> list[Path]:
    """The pack — code, prompts, config, profiles, tests — plus anything given."""
    roots = [pack_root(), pack_root().parent / "tests", *(extra_paths or [])]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        if not root.is_dir():
            continue
        files += [
            path for path in sorted(root.rglob("*"))
            if path.is_file()
            and path.suffix in SCANNED_SUFFIXES
            and "__pycache__" not in path.parts
            and path.resolve() != Path(__file__).resolve()
        ]
    return files


def _exempt_terms() -> set[str]:
    """Lower-cased terms a sealed list may name but the repository may still hold."""
    exempt = {"torah", "gospel", "qur'an", "quran", "islam", "sunni", "bible"}
    for hatch in catalog.hatch_types():
        exempt |= {hatch.key.lower(), hatch.label.lower()}
    return exempt


def _strings_under(data: Any, keys: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if key in keys:
                found |= set(_strings(value))
            else:
                found |= _strings_under(value, keys)
    elif isinstance(data, list):
        for value in data:
            found |= _strings_under(value, keys)
    return found


def _strings(data: Any) -> list[str]:
    if isinstance(data, str):
        return [data]
    if isinstance(data, dict):
        return [s for key, value in data.items() for s in (*_strings(key), *_strings(value))]
    if isinstance(data, list):
        return [s for value in data for s in _strings(value)]
    return []


def _norm_locus(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("–", "-")


def _strip_qualifier(text: str) -> str:
    match = re.search(r"\d{1,3}\s*[:.]\s*\d{1,3}", text)
    return match.group(0).replace(".", ":") if match else text


def _lines(path: Path) -> list[tuple[int, str]]:
    try:
        return list(enumerate(path.read_text(encoding="utf-8").splitlines(), start=1))
    except UnicodeDecodeError:
        return []


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(pack_root().parent.parent.parent.parent))
    except ValueError:
        return str(path)
