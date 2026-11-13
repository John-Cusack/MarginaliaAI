"""The independent checks (design 7.5): what agreement cannot catch.

Each check is here because of a specific way three readers can be confidently
wrong together: a missing line, a note number read wrong, a word the book never
uses, a table cell that breaks its column's format, a heading the table of
contents does not know, and the reader's own chrome leaking into the text.
"""

from __future__ import annotations

from marginalia_ocr.server import checks


def test_a_missing_line_is_caught_even_when_every_reader_lost_it():
    """The layout knows how many lines are drawn; the readers agree on fewer."""
    layout_lines = [(0, i * 14, 100, i * 14 + 12) for i in range(20)]
    per_line_text = [f"line {i}" for i in range(19)]      # one line never made it
    spans = checks.line_count_mismatch(layout_lines, per_line_text)
    assert spans, "a line of the book vanished and every reader agreed about it"


def test_a_line_count_that_agrees_reports_nothing():
    layout_lines = [(0, i * 14, 100, i * 14 + 12) for i in range(6)]
    assert checks.line_count_mismatch(layout_lines, [f"l{i}" for i in range(6)]) == ()


def test_note_numbers_out_of_sequence_mark_the_number_that_broke_it():
    numbers = [(0, 3, 1), (20, 23, 2), (40, 43, 7)]
    spans = checks.note_sequence(numbers)
    assert spans == ((40, 43),), "the third note is not the second plus one"


def test_a_restart_is_allowed_only_where_the_caller_names_a_boundary():
    """Books restart note numbering at a part; treating that as a violation would
    mark the second half of every book."""
    numbers = [(0, 3, 1), (20, 23, 2), (900, 903, 1), (920, 923, 2)]
    assert checks.note_sequence(numbers) == ((900, 903),), "a restart without a boundary is a mark"
    assert checks.note_sequence(numbers, resets_at={900}) == ()


def test_a_table_column_keeps_its_number_format():
    rows = [
        ["1,234.50", "12%"],
        ["2,345.00", "13%"],
        ["3450.00", "14%"],      # the thousands separator went missing
        ["4,567.25", "15%"],
    ]
    violations = checks.table_number_formats(rows)
    assert (2, 0) in violations
    assert all(column == 0 for _row, column in violations), "the percent column is consistent"


def test_a_heading_the_table_of_contents_does_not_know_is_marked():
    labels = ["Chapter One", "The Debt Question", "Notes"]
    assert checks.heading_vs_toc("The Debt Question", labels) == ()
    assert checks.heading_vs_toc("The Dept Question", labels) == (), "one character of OCR noise is tolerated"
    assert checks.heading_vs_toc("A Chapter Nobody Listed", labels), "a heading the TOC never had"


def test_the_readers_own_chrome_is_a_leak_and_never_text():
    text = "Chapter One Back to Library the debt was 48.2%"
    spans = checks.chrome_leaks(text)
    assert spans and all(text[start:end].lower().startswith("back to") for start, end in spans)


def test_the_vocabulary_check_catches_a_confusion_the_readers_agreed_on():
    """`debr` for `debt`: three readers can be wrong the same way."""
    vocabulary = checks.book_vocabulary(["the debt was 48.2% " * 3])
    assert checks.unmatched_words("the debr was 48.2%", vocabulary)
    assert checks.unmatched_words("the debt was 48.2%", vocabulary) == ()
