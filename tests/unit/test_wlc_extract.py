"""The editorial rules `scripts/wlc_extract` applies to morphhb's OSIS.

Each of these is a decision about what the Westminster Leningrad Codex *says*,
made once during the ingest and then frozen into 929 stored chapters. They are
not obvious from the XML and they are not recoverable from the rows, so they are
pinned here: a change to any of them changes what the corpus claims a witness
reads, and should have to break a test first.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import wlc_extract as W  # noqa: E402

pytestmark = pytest.mark.unit

OSIS = "http://www.bibletechnologies.net/2003/OSIS/namespace"


def verse(inner: str) -> ET.Element:
    return ET.fromstring(f'<verse xmlns="{OSIS}" osisID="Gen.1.1">{inner}</verse>')


def word(text: str, tail: str = "\n") -> str:
    return f'<w lemma="1" morph="HNcmsa" id="x">{text}</w>{tail}'


class TestWordText:
    def test_morpheme_boundaries_are_morphology_not_text(self):
        """morphhb writes "b/7225" as "בְּ/רֵאשִׁית"; the slash is a parse, not a letter."""
        text, pairs = W._verse_text(verse(word("בְּ/רֵאשִׁית", tail="")))
        assert text == "בְּרֵאשִׁית"
        assert pairs == []

    def test_words_are_separated_by_the_source_s_own_whitespace(self):
        text, _ = W._verse_text(verse(word("alpha") + word("beta", tail="")))
        assert text == "alpha beta"

    def test_a_maqqef_binds_the_words_it_joins(self):
        """No tail means no space, which is what makes "אֶת־הָאָרֶץ" one unit."""
        inner = (
            word("אֶת", tail="")
            + '<seg type="x-maqqef">־</seg>'
            + word("הָאָרֶץ", tail="")
        )
        text, _ = W._verse_text(verse(inner))
        assert text == "אֶת־הָאָרֶץ"

    def test_a_paseq_stands_apart_because_the_source_spaces_it(self):
        inner = word("alpha") + '<seg type="x-paseq">׀</seg>\n' + word("beta", tail="")
        text, _ = W._verse_text(verse(inner))
        assert text == "alpha ׀ beta"

    def test_a_scribal_paragraph_mark_is_kept(self):
        """Setumah and petuchah are Masoretic division, not markup."""
        inner = word("alpha") + '<seg type="x-samekh">ס</seg>'
        text, _ = W._verse_text(verse(inner))
        assert text.endswith("ס")

    def test_text_in_a_tail_is_refused_rather_than_silently_dropped(self):
        with pytest.raises(ValueError, match="unexpected text content"):
            W._verse_text(verse(word("alpha", tail=" stray ") + word("beta", tail="")))


class TestKetivQere:
    """morphhb writes the ketiv unpointed and hangs the pointed qere off a note.

    WLC carries the **qere**, because an unpointed word in a pointed edition
    witnesses nothing. The ketiv is never discarded — it goes to `Chapter.kq`.
    """

    def test_the_qere_replaces_the_ketiv_in_the_running_text(self):
        inner = (
            word("alpha")
            + '<w type="x-ketiv" lemma="1" morph="H" id="k">KTV</w>'
            + '<note type="variant"><catchWord>KTV</catchWord>'
            '<rdg type="x-qere"><w>QRE</w></rdg></note>'
        )
        text, pairs = W._verse_text(verse(inner))
        assert text == "alpha QRE"
        assert pairs == [("KTV", "QRE")]

    def test_a_ketiv_spanning_several_words_is_replaced_whole(self):
        """1 Kgs 17:15 reads two words, joined by a maqqef tagged as ketiv."""
        inner = (
            word("alpha")
            + '<w type="x-ketiv" lemma="1" morph="H" id="k">FIRST</w>'
            + '<seg type="x-maqqef" subType="x-ketiv">־</seg>'
            + '<w type="x-ketiv" lemma="1" morph="H" id="k2">SECOND</w>'
            + '<note type="variant"><rdg type="x-qere"><w>QRE</w></rdg></note>'
        )
        text, pairs = W._verse_text(verse(inner))
        assert text == "alpha QRE"
        assert pairs == [("FIRST־SECOND", "QRE")]

    def test_ketiv_velo_qere_removes_the_word_and_the_maqqef_holding_it(self):
        """2 Kgs 5:18: written but not read, and hyphenated to what precedes it.

        Leaving the maqqef behind would print "יִסְלַח־ יְהוָה" — a hyphen joining
        a word to nothing.
        """
        inner = (
            word("alpha", tail="")
            + '<seg type="x-maqqef">־</seg>'
            + '<w type="x-ketiv" lemma="1" morph="H" id="k">KTV</w>'
            + '<note type="variant"><rdg type="x-qere"></rdg></note>'
        )
        text, pairs = W._verse_text(verse(inner))
        assert text == "alpha"
        assert pairs == [("KTV", "")]

    def test_qere_velo_ketiv_is_read_though_nothing_is_written(self):
        """Nine verses read a word the codex does not write; it has no ketiv."""
        inner = (
            word("alpha")
            + '<note type="variant"><rdg type="x-qere"><w>QRE</w></rdg></note>'
        )
        text, pairs = W._verse_text(verse(inner))
        assert text == "alpha QRE"
        assert pairs == []


class TestApparatusIsNotText:
    def test_an_alternative_accentuation_is_not_a_reading(self):
        """Exod 20:2 carries the Decalogue's second cantillation tradition."""
        inner = (
            word("alpha")
            + '<note type="alternative"><catchWord>alpha</catchWord>'
            '<rdg type="x-accent">ALT</rdg></note>'
            + word("beta", tail="")
        )
        text, pairs = W._verse_text(verse(inner))
        assert text == "alpha beta"
        assert pairs == []

    def test_a_note_on_word_division_is_commentary(self):
        inner = (
            word("alpha")
            + '<note type="exegesis">WLC has this word divided as <rdg>X</rdg>.</note>'
            + word("beta", tail="")
        )
        text, _ = W._verse_text(verse(inner))
        assert text == "alpha beta"


class TestChapterLayout:
    """The rendered layout is LHB's, so the two editions diff line for line."""

    def make(self, verses):
        chapter = W.Chapter(book="Gen", number=1)
        chapter.verses = verses
        return chapter

    def test_the_first_verse_carries_the_chapter_number(self):
        text = W.render_chapter(self.make([(1, "alpha"), (2, "beta")]))
        assert text == "1 1 \talpha \n\n \n\n2 \tbeta"

    def test_spans_cover_the_words_and_not_their_marker(self):
        text, spans = W.render_chapter_with_spans(self.make([(1, "alpha"), (2, "beta")]))
        assert [text[s:e] for _, s, e in spans] == ["alpha", "beta"]
        assert [v for v, _, _ in spans] == [1, 2]

    def test_an_empty_verse_is_refused_rather_than_rendered(self):
        with pytest.raises(ValueError):
            W.render_chapter(self.make([(1, "alpha"), (2, "")]))
