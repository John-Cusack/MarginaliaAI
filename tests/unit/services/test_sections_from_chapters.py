"""Recovering chapters from unmarked prose.

Library and e-reader exports arrive as flat text — no markdown, no tags — so
their structure layer was a single root node over the whole book. Sixteen such
books in this corpus had `nodes = 1` between them.

Finding `Chapter N` is easy. The difficulty, and the reason for every rule here,
is that a contents list, a back-of-book index, a notes section and a reference
list all contain `Chapter N` and look identical to a chapter start one line at a
time. Each case below was measured on a real book before it was written.
"""

from __future__ import annotations

import pytest

from research_engine.services.text.sections import (
    sections_from_chapters,
    sections_from_markdown,
)

PROSE = "Prose about the period, at some length. " * 200  # ~8k characters


def book(*headings: str, lead: str = "Front matter.\n\n") -> str:
    return lead + "".join(f"{heading}\n\n{PROSE}\n\n" for heading in headings)


class TestOrdinaryBooks:
    def test_a_plain_sequence_becomes_sections(self):
        text = book("Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4")

        sections = sections_from_chapters(text)

        assert [s["heading"] for s in sections] == [
            "Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4",
        ]

    @pytest.mark.parametrize(
        "headings",
        [
            ("CHAPTER ONE", "CHAPTER TWO", "CHAPTER THREE", "CHAPTER FOUR"),
            ("Chapter I", "Chapter II", "Chapter III", "Chapter IV"),
            ("Chapter 1: Your Self", "Chapter 2: Philosophy",
             "Chapter 3: Behaviour", "Chapter 4: Evolution"),
            ("CHAPTER ONE. Prelude", "CHAPTER TWO. Government",
             "CHAPTER THREE. Washington", "CHAPTER FOUR. Adams"),
        ],
    )
    def test_the_numbering_conventions_that_appear_in_this_corpus(self, headings):
        """Words, romans, colons and trailing titles — all real, all measured."""
        assert len(sections_from_chapters(book(*headings))) == 4

    def test_every_section_slices_back_to_real_text(self):
        text = book("Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4")

        for section in sections_from_chapters(text):
            assert 0 <= section["char_start"] < section["char_end"] <= len(text)
            assert text[section["char_start"] : section["char_end"]].strip()

    def test_sections_are_disjoint_and_in_order(self):
        sections = sections_from_chapters(
            book("Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4")
        )

        for earlier, later in zip(sections, sections[1:], strict=False):
            assert earlier["char_end"] <= later["char_start"]


class TestThingsThatLookLikeChapters:
    def test_a_contents_list_loses_to_the_body_it_lists(self):
        """The failure that made ascent necessary.

        A contents list and the chapters it lists are both numbered from one, so
        they are two ascending runs rather than one. The body wins because its
        entries are a book apart and the list's are one line apart — measured,
        85,932 characters against roughly forty.
        """
        contents = (
            "Table of Contents\n\nCHAPTER ONE. Prelude\nCHAPTER TWO. Government\n"
            "CHAPTER THREE. Washington\nCHAPTER FOUR. Adams\n\n"
        )
        text = contents + book(
            "Chapter One", "Chapter Two", "Chapter Three", "Chapter Four", lead=""
        )

        sections = sections_from_chapters(text)

        assert [s["heading"] for s in sections] == [
            "Chapter One", "Chapter Two", "Chapter Three", "Chapter Four",
        ]
        assert sections[0]["char_start"] > len(contents) - 1

    def test_a_back_of_book_index_is_not_a_chapter_sequence(self):
        """27 index entries are a longer run than the 8 chapters they list, and
        span 0.5% of the text. Width decides, not count."""
        text = book("Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4") + (
            "Index\n\n" + "".join(f"Chapter {n}\n" for n in range(1, 28))
        )

        sections = sections_from_chapters(text)

        assert [s["heading"] for s in sections] == [
            "Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4",
        ]

    def test_a_notes_section_crammed_into_the_tail_yields_nothing(self):
        """One book's only run sat at 84% through it, entries 3.4k apart. Using
        those as chapter bounds would carve up the notes, not the book."""
        filler = "Body text with no chapter markers at all. " * 4_000
        notes = "".join(f"Chapter {n}\n\n{'note text ' * 300}\n" for n in range(1, 11))

        assert sections_from_chapters(filler + notes) == []

    def test_a_sequence_that_stops_early_is_refused(self):
        """A section runs to the next heading or the end of the text, so a run
        that gives up at 8 of 27 titles chapter 8 over the rest of the book."""
        early = book("Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4")
        tail = "Unmarked prose continuing for a long while. " * 6_000

        assert sections_from_chapters(early + tail) == []

    def test_a_sentence_beginning_with_the_word_is_not_a_heading(self):
        text = book(
            "Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4"
        ).replace(
            "Chapter 3",
            "Chapter 3 of the present work argues at some length that the author "
            "has been consistently misread by his critics",
        )

        assert len(sections_from_chapters(text)) < 4

    def test_two_chapters_are_a_coincidence_not_a_sequence(self):
        assert sections_from_chapters(book("Chapter 1", "Chapter 2")) == []

    def test_a_book_with_no_chapters_yields_nothing(self):
        """Four activity books in this corpus have numbered steps and no
        chapters. Reading '1. Tap the rim of the mug' as a heading would give
        them eighty-six of them."""
        text = "1. Tap the rim of the mug.\n2. Pour hot water in.\n" * 500

        assert sections_from_chapters(text) == []


class TestAgreementWithMarkdown:
    def test_it_produces_the_shape_markdown_does(self):
        """Downstream must not be able to tell which extractor ran."""
        chapters = sections_from_chapters(
            book("Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4")
        )
        markdown = sections_from_markdown(
            book("# Chapter 1", "# Chapter 2", "# Chapter 3", "# Chapter 4")
        )

        assert {k for s in chapters for k in s} == {k for s in markdown for k in s}

    def test_a_run_broken_by_a_stray_reference_is_rejoined(self):
        """A list naming chapters 4-8 sits between chapter 8 and chapter 9, and
        without rejoining, one book reads as two shorter ones."""
        first = book("Chapter 1", "Chapter 2", "Chapter 3", lead="")
        stray = "See Chapter 2 for the argument.\n\n"
        second = book("Chapter 4", "Chapter 5", "Chapter 6", lead="")

        headings = [s["heading"] for s in sections_from_chapters(first + stray + second)]

        assert headings == [f"Chapter {n}" for n in range(1, 7)]
