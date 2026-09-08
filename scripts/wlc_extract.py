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

import re
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
    #: `(offset within this token, surface, lemma, morph, from a qere)` for the
    #: `<w>` elements this token contributes. A `<seg>` contributes none: a
    #: maqqef is punctuation, not a word, and the coverage check below depends
    #: on that distinction being exact.
    words: list = field(default_factory=list)


@dataclass
class Word:
    """One word of the running text, and where in that text it sits.

    `offset` is relative to the start of its verse; `render_chapter_with_words`
    turns that into an offset into the chapter, which is what the corpus stores.
    The invariant worth stating, because everything downstream rests on it:
    `text[offset:offset + length] == surface`, exactly, for every word.
    """

    verse: int
    offset: int
    length: int
    surface: str
    lemma: str
    morph: str
    qere: bool = False


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
    words: list[Word] = field(default_factory=list)


def _render_run(children: list[ET.Element]) -> str:
    """Flatten a qere reading (`<w>`/`<seg>` children of an `<rdg>`)."""
    out: list[str] = []
    for i, child in enumerate(children):
        out.append(_word_text(child) if _tag(child) == "w" else (child.text or ""))
        if i < len(children) - 1:
            out.append(_sep_after(child))
    return "".join(out)


def _render_run_words(children: list[ET.Element]) -> tuple[str, list]:
    """`_render_run`, additionally reporting where each word landed in the run.

    A qere may read several words (Josh 8:16 reads two). Flattening it to one
    string is right for the text and wrong for an index, so the offsets are
    kept here rather than recovered by searching for the word afterwards —
    searching would pick the wrong one whenever a run repeats a word.
    """
    out: list[str] = []
    words: list = []
    pos = 0
    for i, child in enumerate(children):
        if _tag(child) == "w":
            piece = _word_text(child)
            words.append(
                (pos, piece, child.get("lemma", ""), child.get("morph", ""), True)
            )
        else:
            piece = child.text or ""
        out.append(piece)
        pos += len(piece)
        if i < len(children) - 1:
            sep = _sep_after(child)
            out.append(sep)
            pos += len(sep)
    return "".join(out), words


def _verse_text(verse: ET.Element) -> tuple[str, list[tuple[str, str]]]:
    """Canonical text of one verse, plus its ketiv/qere pairs."""
    text, pairs, _ = _verse_parse(verse)
    return text, pairs


def _verse_parse(verse: ET.Element) -> tuple[str, list[tuple[str, str]], list[Word]]:
    """Canonical text of one verse, its ketiv/qere pairs, and its words."""
    tokens: list[_Token] = []
    pairs: list[tuple[str, str]] = []

    for child in verse:
        tag, typ = _tag(child), child.get("type")
        sep = _sep_after(child)

        if tag == "w":
            surface = _word_text(child)
            tokens.append(
                _Token(
                    surface,
                    sep,
                    ketiv=(typ == "x-ketiv"),
                    words=[
                        (0, surface, child.get("lemma", ""), child.get("morph", ""), False)
                    ],
                )
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
            qere, qere_words = (
                _render_run_words(list(rdg)) if rdg is not None else ("", [])
            )

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
                tokens.append(_Token(qere, sep, words=qere_words))
            elif tokens and tokens[-1].sep == "" and tokens[-1].text == "־":
                # Ketiv velo qere: the word is written but not read, so it goes.
                # 2 Kgs 5:18 hyphenates it to the previous word — drop the
                # orphaned maqqef with it rather than leave `יִסְלַח־ יְהוָה`.
                tokens.pop()
            if ketiv:
                pairs.append((ketiv, qere))
        else:
            raise ValueError(f"unexpected element in verse: {tag}")

    raw = "".join(
        tok.text + (tok.sep if i < len(tokens) - 1 else "")
        for i, tok in enumerate(tokens)
    )
    # Offsets are taken against the same string the text is built from, then
    # shifted by whatever `strip()` removes, so they cannot drift from it.
    shift = len(raw) - len(raw.lstrip())
    words: list[Word] = []
    pos = 0
    for i, tok in enumerate(tokens):
        for offset, surface, lemma, morph, from_qere in tok.words:
            words.append(
                Word(
                    verse=0,
                    offset=pos + offset - shift,
                    length=len(surface),
                    surface=surface,
                    lemma=lemma,
                    morph=morph,
                    qere=from_qere,
                )
            )
        pos += len(tok.text) + (len(tok.sep) if i < len(tokens) - 1 else 0)
    return raw.strip(), pairs, words


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
            text, pairs, words = _verse_parse(verse_el)
            chapter.verses.append((verse_no, text))
            chapter.kq.extend(KQ(verse_no, k, q) for k, q in pairs)
            for word in words:
                word.verse = verse_no
            chapter.words.extend(words)
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


def render_chapter_with_words(chapter: Chapter) -> tuple[str, list[Word]]:
    """The chapter text and every word in it, addressed against that text.

    The offsets `parse_book` recorded are relative to each verse; the corpus
    stores one string per chapter, so they are rebased here onto the spans
    `render_chapter_with_spans` already computes. Nothing re-derives a position
    by searching the text, which is the only way this stays correct through a
    verse that repeats a word.
    """
    text, spans = render_chapter_with_spans(chapter)
    starts = {verse: start for verse, start, _ in spans}
    words = [
        Word(
            verse=word.verse,
            offset=starts[word.verse] + word.offset,
            length=word.length,
            surface=word.surface,
            lemma=word.lemma,
            morph=word.morph,
            qere=word.qere,
        )
        for word in chapter.words
    ]
    return text, words


def misplaced_words(text: str, words: list[Word]) -> list[str]:
    """Words whose recorded span does not quote them. Empty means all of them do."""
    return [
        f"{w.verse}:{w.offset} expected {w.surface!r} got {text[w.offset:w.offset + w.length]!r}"
        for w in words
        if text[w.offset : w.offset + w.length] != w.surface
    ]


#: Hebrew letters. What may be left over once every word span is removed is
#: separators, verse numbers, and the pointing on the scribal marks — never a
#: letter belonging to a word nobody indexed.
HEBREW_LETTER = re.compile(r"[\u05d0-\u05ea]")

#: The scribal marks `_verse_text` keeps as text: maqqef, sof pasuq, paseq, and
#: the setumah/petuchah letters, which are letters but are not words.
SCRIBAL = set("\u05be\u05c3\u05c0\u05e1\u05e4\u05e0")


def unclaimed_letters(text: str, words: list[Word]) -> list[str]:
    """Stretches of text no word claims that still contain a Hebrew letter.

    This is the completeness proof. A word missing from the index leaves its
    letters sitting in a gap, and they show up here. Scribal marks are excluded
    because samekh and pe *are* letters but stand for a paragraph break rather
    than a word.
    """
    covered = bytearray(len(text))
    for w in words:
        for i in range(w.offset, min(w.offset + w.length, len(text))):
            covered[i] = 1
    gaps, run = [], []
    for i, ch in enumerate(text):
        if covered[i]:
            if run:
                gaps.append("".join(run))
                run = []
        else:
            run.append(ch)
    if run:
        gaps.append("".join(run))
    return [
        g for g in gaps
        if HEBREW_LETTER.search("".join(c for c in g if c not in SCRIBAL))
    ]
