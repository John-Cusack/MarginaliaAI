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


class TestWordSpans:
    """Where each word sits in the rendered text, and how that is proved.

    The index these produce is only worth as much as its offsets, and there is
    no external oracle for them — so the invariant is checked directly: a word's
    span must quote that word, and no text a word does not claim may contain a
    letter.
    """

    def make(self, verses):
        chapter = W.Chapter(book="Gen", number=1)
        for number, inner in verses:
            text, pairs, words = W._verse_parse(verse(inner))
            chapter.verses.append((number, text))
            for word in words:
                word.verse = number
            chapter.words.extend(words)
        return chapter

    def test_every_word_span_quotes_its_own_word(self):
        chapter = self.make([(1, word("alpha") + word("beta", tail="")),
                             (2, word("gamma", tail=""))])
        text, words = W.render_chapter_with_words(chapter)
        assert [w.surface for w in words] == ["alpha", "beta", "gamma"]
        assert W.misplaced_words(text, words) == []

    def test_a_repeated_word_gets_distinct_spans(self):
        """The offsets come from the walk, not from searching for the word.

        Searching would return the first match twice and silently mis-address
        the second occurrence — which is the failure this test exists to
        prevent, because nothing downstream could detect it.
        """
        chapter = self.make([(1, word("alpha") + word("alpha", tail=""))])
        text, words = W.render_chapter_with_words(chapter)
        assert len({(w.offset, w.length) for w in words}) == 2
        assert W.misplaced_words(text, words) == []

    def test_a_word_carries_its_lemma_and_morphology(self):
        chapter = self.make([(1, '<w lemma="c/4941" morph="HC/Ncmsa" id="x">וּמִשְׁפָּט</w>')])
        _, words = W.render_chapter_with_words(chapter)
        assert (words[0].lemma, words[0].morph) == ("c/4941", "HC/Ncmsa")

    def test_punctuation_is_not_a_word(self):
        """A maqqef is text but not a word, and must not become a row."""
        inner = (
            word("אֶת", tail="")
            + '<seg type="x-maqqef">־</seg>'
            + word("הָאָרֶץ", tail="")
        )
        chapter = self.make([(1, inner)])
        text, words = W.render_chapter_with_words(chapter)
        assert [w.surface for w in words] == ["אֶת", "הָאָרֶץ"]
        assert W.unclaimed_letters(text, words) == []

    def test_a_qere_reading_is_indexed_word_by_word(self):
        """The qere is what the text reads, so it is what the index holds."""
        inner = (
            word("alpha")
            + '<w type="x-ketiv" lemma="1" morph="H" id="k">KTV</w>'
            + '<note type="variant"><rdg type="x-qere">'
            '<w lemma="4941" morph="HNcmsa">QRE</w></rdg></note>'
        )
        chapter = self.make([(1, inner)])
        text, words = W.render_chapter_with_words(chapter)
        assert [w.surface for w in words] == ["alpha", "QRE"]
        assert [w.qere for w in words] == [False, True]
        assert words[1].lemma == "4941"
        assert W.misplaced_words(text, words) == []
        assert W.unclaimed_letters(text, words) == []

    def test_the_ketiv_is_not_indexed_because_it_is_not_in_the_text(self):
        inner = (
            word("alpha")
            + '<w type="x-ketiv" lemma="1" morph="H" id="k">KTV</w>'
            + '<note type="variant"><rdg type="x-qere">'
            '<w lemma="4941" morph="HNcmsa">QRE</w></rdg></note>'
        )
        _, words = W.render_chapter_with_words(self.make([(1, inner)]))
        assert "KTV" not in [w.surface for w in words]

    def test_a_missing_word_is_caught_by_the_gap_check(self):
        """The completeness proof, demonstrated by breaking it.

        A count would not notice this — it agrees with any consistent mistake.
        The gap does: the dropped word's letters turn up in text nothing claims.
        """
        inner = word("אֶרֶץ") + word("שָׁלוֹם", tail="")
        chapter = self.make([(1, inner)])
        text, words = W.render_chapter_with_words(chapter)
        assert W.unclaimed_letters(text, words) == []
        assert W.unclaimed_letters(text, words[:1]) != []

    def test_scribal_marks_are_not_words_and_are_not_gaps(self):
        """Samekh and pe are letters, but they mark a paragraph, not a word."""
        inner = word("alpha", tail="") + '<seg type="x-samekh">ס</seg>'
        chapter = self.make([(1, inner)])
        text, words = W.render_chapter_with_words(chapter)
        assert [w.surface for w in words] == ["alpha"]
        assert W.unclaimed_letters(text, words) == []


class TestLemmaParsing:
    """Splitting morphhb's compound lemma into the parts a survey queries.

    Every case here is one the corpus actually contains, and three of them
    break the obvious implementation.
    """

    @staticmethod
    def parse(lemma):
        from backfill_words import parse_lemma

        return parse_lemma(lemma)

    def test_a_plain_number(self):
        assert self.parse("4941") == ("4941", None, None)

    def test_a_prefixed_word_still_answers_to_its_number(self):
        """Half of all mishpat occurrences are prefixed; `= '4941'` must find them."""
        assert self.parse("c/4941") == ("4941", None, "c")

    def test_stacked_prefixes_are_kept_in_order(self):
        assert self.parse("c/d/4941") == ("4941", None, "c/d")

    def test_a_homograph_letter_is_kept_apart_from_the_number(self):
        """Strong's merged words that OSHB splits; 59,061 words carry a letter."""
        assert self.parse("834 a") == ("834", "a", None)
        assert self.parse("l/6213 a") == ("6213", "a", "l")

    def test_a_compound_name_keeps_its_number(self):
        assert self.parse("1008+")[0] == "1008"

    def test_a_bare_preposition_has_no_strongs_number(self):
        """5,966 words are a morpheme standing alone — hence the nullable column."""
        assert self.parse("l") == (None, None, None)
