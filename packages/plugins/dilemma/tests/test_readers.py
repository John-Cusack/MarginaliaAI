"""Each source reader keeps the printed text verbatim and locates every unit.

Small public-domain fixtures in each source's own format, so the readers are
tested offline: what the edition prints is kept, what a site adds for study
is not, and a shape the reader does not know fails rather than leaking in.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from dilemma.fetch import Stored
from dilemma.sources import adapter_for, keys_with_adapters
from dilemma.sources.saved_pages import SavedPageError, study_page
from dilemma.sources.scriptures_json import ScripturesJson
from dilemma.sources.tanzil import PREFIX, TanzilError, surahs
from dilemma.sources.usfm import BOOKS, UsfmError, chapters, plain
from dilemma.sources.wikisource import WikisourceError, discourse

from research_engine_sdk.units import layout

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def stored(path: Path) -> Stored:
    return Stored(path, "0" * 64, path.stat().st_size, None, datetime.now(UTC))


def test_every_m1_edition_has_a_reader() -> None:
    from dilemma import catalog

    for key in ("lds", "sunni_islam"):
        needed = set(catalog.profile(key).slice("m1").sources)
        assert needed <= keys_with_adapters(), needed - keys_with_adapters()


# --- Scriptures in JSON -----------------------------------------------------


def test_scripture_json_keeps_verses_and_the_dc_reference_form(tmp_path: Path) -> None:
    volume = {"sections": [{"section": 130, "reference": "D&C 130", "verses": [
        {"reference": "D&C 130:22", "verse": 22,
         "text": "The Father has a body of flesh and bones as tangible as man's;"},
    ]}]}
    path = tmp_path / "dc.json"
    path.write_text(json.dumps(volume))

    (document,) = ScripturesJson().parse("dc-2013", [stored(path)])

    assert (document.title, document.node_type) == ("D&C 130", "section")
    (unit,) = document.units
    assert unit.ref == "D&C 130:22"
    assert unit.text.startswith("The Father has a body")


# --- USFM -------------------------------------------------------------------

GENESIS = (
    '\\id GEN Genesis \n\\h Genesis \n\\mt1 The First Book of Moses \n\\c 1  \n\\p\n'
    '\\v 1 In the \\w beginning|strong="H7225"\\w* God created the heaven.  \n'
    '\\v 2 And darkness \\add was\\add* upon the face of the deep.\\f + \\fr 1.2 '
    '\\ft a note\\f*  \n'
)
PSALM = (
    '\\id PSA Psalms \n\\c 3  \n\\d A Psalm of David.  \n\\q1\n'
    '\\v 1  \\nd \\+w LORD|strong="H3068"\\+w*\\nd*, how are they increased!   \n'
)
ROMANS = (
    '\\id ROM Romans \n\\c 16  \n\\p\n\\v 27 To God only wise, \\add be\\add* glory. Amen.   \n'
    '\\s1 Written to the Romans from Corinthus, \\add and sent\\add* by Phebe.  \n'
)


def test_usfm_keeps_the_verse_and_marks_the_supplied_word() -> None:
    (chapter,) = chapters("kjv", "Genesis", GENESIS)

    first, second = chapter.units
    assert first.text == "In the beginning God created the heaven."
    assert second.text == "And darkness was upon the face of the deep."
    (mark,) = second.marks
    assert second.text[mark.start:mark.end] == "was"
    laid = layout(chapter)
    (stored_mark,) = laid.marks
    assert laid.full_text[stored_mark.char_start:stored_mark.char_end] == "was"


def test_usfm_keeps_printed_text_outside_the_verse_numbering() -> None:
    (psalm,) = chapters("kjv", "Psalms", PSALM)
    (romans,) = chapters("kjv", "Romans", ROMANS)

    assert [(u.kind, u.text) for u in psalm.units] == [
        ("title", "A Psalm of David."),
        ("verse", "LORD, how are they increased!"),
    ]
    subscription = romans.units[-1]
    assert (subscription.kind, subscription.ref) == ("subscription", "Romans 16:subscription")
    assert [subscription.text[m.start:m.end] for m in subscription.marks] == ["and sent"]


def test_usfm_refuses_what_it_does_not_know() -> None:
    with pytest.raises(UsfmError, match="unknown paragraph marker"):
        list(chapters("kjv", "Genesis", "\\id GEN\n\\c 1\n\\zz odd\n"))
    with pytest.raises(UsfmError, match="without"):
        plain("an \\add open marker")


def test_usfm_needs_every_book_in_the_archive(tmp_path: Path) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("02-GENeng.usfm", GENESIS)
    path = tmp_path / "kjv.zip"
    path.write_bytes(archive.getvalue())

    with pytest.raises(UsfmError, match="lacks books"):
        list(adapter_for("kjv").parse("kjv", [stored(path)]))
    assert len(BOOKS) == 66


# --- Tanzil -----------------------------------------------------------------

TANZIL = (
    "1|1|In the name of Allah, the Beneficent, the Merciful.\n"
    "1|2|Praise be to Allah, Lord of the Worlds,\n"
    "2|1|Alif. Lam. Mim.\n"
    "\n#  Translation notice\n#  Source: Tanzil.net\n"
)


def test_tanzil_gives_one_document_per_surah_and_keeps_the_notice() -> None:
    first, second = surahs("quran-pickthall-1930", TANZIL, "en")

    assert [u.ref for u in first.units] == [f"{PREFIX} 1" + ":1", f"{PREFIX} 1" + ":2"]
    assert (second.node_type, second.metadata["surah"]) == ("surah", 2)
    assert "Source: Tanzil.net" in first.metadata["tanzil_notice"]
    assert first.metadata["unicode_normalization"] == "NFC"


def test_tanzil_refuses_a_line_out_of_shape() -> None:
    with pytest.raises(TanzilError, match="surah"):
        list(surahs("x", "1|one|text\n", "en"))


# --- Wikisource -------------------------------------------------------------


def page_break(page: int) -> str:
    return (
        f'<span class="wst-pagebreak"><span class="noprint ws-noexport">'
        f'<span id="page{page}">page {page}</span></span></span>'
    )


WIKISOURCE = (
    '<div class="mw-parser-output"><div class="ws-noexport"><p>A header note</p></div>'
    + page_break(46)
    + "<p>First paragraph, which runs on to the next "
    + page_break(47)
    + "page of the volume.</p><p>Second &amp; last paragraph.</p></div>"
)


def test_wikisource_paragraphs_carry_their_page_breaks() -> None:
    document = discourse("jd", WIKISOURCE, prefix="JD", volume=1, title="A sermon",
                         pages=[46, 47])

    first, second = document.units
    assert first.ref == "JD 1:46 ¶1"
    assert first.text == "First paragraph, which runs on to the next page of the volume."
    assert [(m.start, m.attrs["page"]) for m in first.marks] == [(0, 46), (43, 47)]
    assert first.text[43:].startswith("page of the volume")
    assert (second.ref, second.text) == ("JD 1:47 ¶2", "Second & last paragraph.")
    assert "header note" not in " ".join(u.text for u in document.units)


def test_wikisource_refuses_a_broken_page_run() -> None:
    with pytest.raises(WikisourceError, match="page breaks"):
        discourse("jd", WIKISOURCE, prefix="JD", volume=1, title="t", pages=[46, 48])


# --- Saved study pages ------------------------------------------------------

STUDY = (
    "<html><body><header><h1>Becoming Like God</h1></header>"
    '<div class="body-block"><p id="p1">Latter-day Saints believe '
    '<a class="note-ref"><sup class="marker">1</sup></a>this.</p>'
    '<p id="p2">A second paragraph.</p></div>'
    '<footer><p>Not the essay.</p></footer></body></html>'
)


def test_a_saved_study_page_yields_its_body_paragraphs() -> None:
    document = study_page("gte-becoming-like-god", STUDY, fallback_title="fallback")

    assert document.title == "Becoming Like God"
    assert [u.text for u in document.units] == [
        "Latter-day Saints believe this.",
        "A second paragraph.",
    ]
    with pytest.raises(SavedPageError, match="body-block"):
        study_page("gte-x", "<html><p>nothing</p></html>", fallback_title="t")
