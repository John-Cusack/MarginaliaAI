"""The verse-boundary rules `scripts/bible_layout` recovers from stored text.

Every case here is one that the real corpus produced and an earlier version of
the parser got wrong. They are worth pinning because the parser has no source to
check itself against — LHB and ESV exist in this database as text and nothing
else — so a rule that quietly stops holding would be found only by noticing that
a citation names the wrong verse.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import bible_layout as layout  # noqa: E402

pytestmark = pytest.mark.unit

SEP = layout.VERSE_SEP
NBSP = layout.NBSP


def texts(chapter_text, regions, kind="verse"):
    return [chapter_text[r.start : r.end] for r in regions if r.kind == kind]


def numbers(regions, kind="verse"):
    return [r.number for r in regions if r.kind == kind]


class TestLexhamLayout:
    def test_reads_back_the_verses_it_was_rendered_from(self):
        text = SEP.join(["1 1 \talpha ", "2 \tbeta ", "3 \tgamma"])
        regions = layout.parse_lhb(text, 1)
        assert numbers(regions) == [1, 2, 3]
        assert texts(text, regions) == ["alpha", "beta", "gamma"]
        assert not layout.coverage_gaps(text, regions, layout.LHB_GAP)

    def test_a_one_chapter_book_names_no_chapter(self):
        """Obadiah opens "1 \tvision", not "1 1 \tvision"."""
        text = SEP.join(["1 \tvision ", "2 \tbehold"])
        regions = layout.parse_lhb(text, 1)
        assert numbers(regions) == [1, 2]
        assert texts(text, regions) == ["vision", "behold"]

    def test_a_chapter_it_cannot_read_is_refused_rather_than_guessed(self):
        assert layout.parse_lhb("no markers here at all", 1) is None

    def test_the_guard_notices_a_verse_left_behind(self):
        """The whole method rests on this: dropped text has to be loud."""
        text = SEP.join(["1 1 \talpha ", "2 \tbeta ", "3 \tgamma"])
        regions = layout.parse_lhb(text, 1)
        assert not layout.coverage_gaps(text, regions, layout.LHB_GAP)
        without_middle = [r for r in regions if r.number != 2]
        assert layout.coverage_gaps(text, without_middle, layout.LHB_GAP)

    def test_unnumbered_verses_are_counted_not_read(self):
        regions = layout.parse_unmarked("alpha\n\nbeta\n\ngamma")
        assert numbers(regions) == [1, 2, 3]
        assert [r.start for r in regions] == [0, 7, 13]


class TestEnglishStandardLayout:
    def test_prose_chapter_with_a_section_heading(self):
        text = SEP.join([
            "Naomi Widowed ",
            f"1{NBSP}In the days when the judges ruled. ",
            "Ruth Stays ",
            f"6{NBSP}Then she arose.",
        ])
        regions = layout.parse_esv(text, 1)
        assert numbers(regions) == [1, 6]
        assert texts(text, regions, "heading") == ["Naomi Widowed", "Ruth Stays"]
        assert not layout.coverage_gaps(text, regions, layout.ESV_GAP)

    def test_a_title_before_the_first_verse_is_a_title_however_it_reads(self):
        """"Job Replies: Where Is God?" ends in punctuation and starts a chapter.

        Nothing about the words marks it as a heading; its position does. There
        is no verse before it for it to be the continuation of.
        """
        text = SEP.join(["Job Replies: Where Is God? ", f"23{NBSP}Today also."])
        regions = layout.parse_esv(text, 23)
        assert texts(text, regions, "heading") == ["Job Replies: Where Is God?"]
        assert numbers(regions) == [1]

    def test_a_line_of_poetry_is_not_promoted_to_a_heading(self):
        """Isaiah 37: capitalised, unpunctuated, and the second half follows."""
        text = SEP.join([
            f"37{NBSP}Against whom have you raised your voice ",
            "and lifted your eyes to the heights? ",
            f"\t2{NBSP}\tAgainst the Holy One.",
        ])
        regions = layout.parse_esv(text, 37)
        assert texts(text, regions, "heading") == []
        assert numbers(regions) == [1, 2]
        assert "lifted your eyes" in texts(text, regions)[0]

    def test_a_heading_that_interrupts_a_verse_stays_inside_it(self):
        """The marker after the heading is not at the block's start.

        2 Samuel 19 puts a title in the middle of a sentence. Cutting the verse
        at the title would strand the half that follows it, so the title is left
        where it is — a span slightly too wide, rather than lost text.
        """
        text = SEP.join([
            f"1{NBSP}Now Israel had fled. ",
            "David Returns ",
            f"But the people came. 9{NBSP}And all the people argued.",
        ])
        regions = layout.parse_esv(text, 1)
        assert texts(text, regions, "heading") == []
        assert numbers(regions) == [1, 9]
        assert "David Returns" in texts(text, regions)[0]
        assert not layout.coverage_gaps(text, regions, layout.ESV_GAP)

    def test_a_psalm_superscription_is_not_verse_one(self):
        text = SEP.join([
            f"23{NBSP}A Psalm of David. ",
            f"\t1{NBSP}\tThe Lord is my shepherd. ",
            f"\t2{NBSP}\tHe makes me lie down.",
        ])
        regions = layout.parse_esv(text, 23)
        assert numbers(regions) == [1, 2]
        assert texts(text, regions, "superscription") == ["A Psalm of David."]
        assert texts(text, regions)[0] == "The Lord is my shepherd."

    def test_john_eight_keeps_the_verse_it_borrows_from_chapter_seven(self):
        """The ESV prints 7:53 at the head of chapter 8.

        Refusing the chapter would leave the whole pericope adulterae with no
        locators at all, so the borrowed verse is kept and marked as foreign.
        """
        text = SEP.join([
            f"8{NBSP}[[53{NBSP}They went each to his own house. ",
            f"1{NBSP}But Jesus went to the Mount of Olives. ",
            f"2{NBSP}Early in the morning.",
        ])
        regions = layout.parse_esv(text, 8)
        assert numbers(regions) == [1, 2]
        assert numbers(regions, "foreign_verse") == [53]
        assert texts(text, regions, "bracket") == ["[["]
        assert not layout.coverage_gaps(text, regions, layout.ESV_GAP)

    def test_verses_that_run_backwards_are_refused(self):
        text = SEP.join([f"5{NBSP}first. ", f"3{NBSP}second. ", f"2{NBSP}third."])
        assert layout.parse_esv(text, 5) is None

    def test_a_chapter_with_no_markers_is_refused(self):
        assert layout.parse_esv("The Resurrection", 16) is None
