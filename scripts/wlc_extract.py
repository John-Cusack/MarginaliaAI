"""Extract the Westminster Leningrad Codex from openscriptures/morphhb OSIS XML.

Produces one pointed-Hebrew string per chapter, laid out the way the Lexham
Hebrew Bible chapters already in this corpus are laid out, so the two editions
diff cleanly against each other.

Editorial decisions, all of them reversible and all of them recorded:

* ``<w>`` text carries morpheme boundaries as ``/`` (``בְּ/רֵאשִׁ֖ית``). Those are
  morphology, not text, so they come out.
* Ketiv/qere: morphhb writes the ketiv unpointed in the running text and hangs
  the pointed qere off a ``<note type="variant">``. This module emits the
  **qere**, because an unpointed word in a pointed edition is not a witness to
  anything. The ketiv is not discarded — `Chapter.kq` carries every pair.
* ``<note>`` content is apparatus (English commentary, KJV versification
  cross-references, alternative accentuations). None of it is text.
* Spacing follows the XML's own inter-element whitespace, which is what puts
  maqqef and sof-pasuq hard against their neighbours and leaves paseq free.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

NS = "{http://www.bibletechnologies.net/2003/OSIS/namespace}"

#: How LHB chapters are laid out: verse blocks joined by this, each block
#: ``"{verse} \t{text} "``, the first also prefixed with the chapter number.
VERSE_SEP = "\n\n \n\n"


def _tag(el: ET.Element) -> str:
    return el.tag.replace(NS, "")


def _sep_after(el: ET.Element) -> str:
    """One space where the source separates two elements, nothing where it does not."""
    tail = el.tail or ""
    if tail == "":
        return ""
    if tail.strip():
        raise ValueError(f"unexpected text content in tail: {tail!r}")
    return " "


def _word_text(el: ET.Element) -> str:
    text = "".join(el.itertext())
    return text.replace("/", "")


@dataclass
class _Token:
    text: str
    sep: str
    ketiv: bool = False


@dataclass
class KQ:
    """One ketiv/qere pair, kept so the reading the codex *writes* is recoverable."""

    verse: int
    ketiv: str
    qere: str


@dataclass
class Chapter:
    book: str          # osis book code, e.g. "Gen"
    number: int
    verses: list[tuple[int, str]] = field(default_factory=list)
    kq: list[KQ] = field(default_factory=list)


def _render_run(children: list[ET.Element]) -> str:
    """Flatten a qere reading (`<w>`/`<seg>` children of an `<rdg>`)."""
    out: list[str] = []
    for i, child in enumerate(children):
        out.append(_word_text(child) if _tag(child) == "w" else (child.text or ""))
        if i < len(children) - 1:
            out.append(_sep_after(child))
    return "".join(out)


def _verse_text(verse: ET.Element) -> tuple[str, list[tuple[str, str]]]:
    """Canonical text of one verse, plus its ketiv/qere pairs."""
    tokens: list[_Token] = []
    pairs: list[tuple[str, str]] = []

    for child in verse:
        tag, typ = _tag(child), child.get("type")
        sep = _sep_after(child)

        if tag == "w":
            tokens.append(
                _Token(_word_text(child), sep, ketiv=(typ == "x-ketiv"))
            )
        elif tag == "seg":
            tokens.append(
                _Token(child.text or "", sep, ketiv=(child.get("subType") == "x-ketiv"))
            )
        elif tag == "note":
            if typ != "variant":
                # Apparatus: KJV versification notes, English commentary,
                # `type="alternative"` accentuations. Not text.
                continue
            rdg = child.find(f"{NS}rdg[@type='x-qere']")
            qere = _render_run(list(rdg)) if rdg is not None else ""

            # The ketiv may be a run of words (1Kgs 17:15 reads two, joined by a
            # maqqef tagged `subType="x-ketiv"`), so take every trailing one.
            ketiv_parts: list[_Token] = []
            while tokens and tokens[-1].ketiv:
                ketiv_parts.insert(0, tokens.pop())
            ketiv = "".join(
                p.text + (p.sep if i < len(ketiv_parts) - 1 else "")
                for i, p in enumerate(ketiv_parts)
            )

            if qere:
                tokens.append(_Token(qere, sep))
            elif tokens and tokens[-1].sep == "" and tokens[-1].text == "־":
                # Ketiv velo qere: the word is written but not read, so it goes.
                # 2 Kgs 5:18 hyphenates it to the previous word — drop the
                # orphaned maqqef with it rather than leave `יִסְלַח־ יְהוָה`.
                tokens.pop()
            if ketiv:
                pairs.append((ketiv, qere))
        else:
            raise ValueError(f"unexpected element in verse: {tag}")

    text = "".join(
        tok.text + (tok.sep if i < len(tokens) - 1 else "")
        for i, tok in enumerate(tokens)
    )
    return text.strip(), pairs


def parse_book(path: Path) -> list[Chapter]:
    root = ET.parse(path).getroot()
    chapters: list[Chapter] = []
    for chap_el in root.iter(f"{NS}chapter"):
        osis = chap_el.get("osisID") or ""
        book, num = osis.rsplit(".", 1)
        chapter = Chapter(book=book, number=int(num))
        for verse_el in chap_el.iter(f"{NS}verse"):
            v_osis = verse_el.get("osisID") or ""
            verse_no = int(v_osis.rsplit(".", 1)[1])
            text, pairs = _verse_text(verse_el)
            chapter.verses.append((verse_no, text))
            chapter.kq.extend(KQ(verse_no, k, q) for k, q in pairs)
        chapters.append(chapter)
    return chapters


def render_chapter_with_spans(
    chapter: Chapter,
) -> tuple[str, list[tuple[int, int, int]]]:
    """Lay a chapter out as LHB lays one out, and say where each verse landed.

    Returns the text and one ``(verse, start, end)`` per verse, spanning the
    verse's *words* — not the ``"3 \t"`` that introduces them and not the
    separator that follows. Those bounds are what turns a passage's character
    span into the verses it actually quotes.
    """
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    pos = 0
    for i, (verse_no, text) in enumerate(chapter.verses):
        if not text:
            raise ValueError(f"empty verse {chapter.book} {chapter.number}:{verse_no}")
        if i:
            parts.append(VERSE_SEP)
            pos += len(VERSE_SEP)
        head = f"{chapter.number} " if i == 0 else ""
        head += f"{verse_no} \t"
        parts.append(head)
        pos += len(head)
        spans.append((verse_no, pos, pos + len(text)))
        # The trailing space is LHB's; only the last one is stripped below, and
        # it sits past the last verse's end, so no span moves.
        parts.append(f"{text} ")
        pos += len(text) + 1
    return "".join(parts).rstrip(" "), spans


def render_chapter(chapter: Chapter) -> str:
    """Lay a chapter out the way the LHB chapters in this corpus are laid out."""
    return render_chapter_with_spans(chapter)[0]


def load_all(wlc_dir: Path) -> list[Chapter]:
    chapters: list[Chapter] = []
    for path in sorted(wlc_dir.glob("*.xml")):
        if path.name == "VerseMap.xml":
            continue
        chapters.extend(parse_book(path))
    return chapters
