"""Text drawn into the synthetic corpus.

Every string here is written for this file. None of it is book text, and none
of it is copied from a source: the corpus exists so that readings can be scored
against known truth without licensed material entering any machine, log, or
context window.

The content is chosen for the errors it can expose, not for its literary merit:

* ``form``/``from`` and ``then``/``than`` — real-word substitutions that a
  spell-checker cannot see.
* signed decimals and the U+2212 minus — ``-1.5`` and ``−1.5`` are different
  claims and different characters.
* units, thousands separators, currency, micro signs — digits are content.
* ``naïve``, ``café``, ``Émile`` — accents are significant in the normalization
  the pipeline uses.
* long hyphenatable words — the de-hyphenation rule needs a word the page
  splits across a line or a screen boundary.
* URLs and bracketed note references — the two things never joined by a rule.
"""

from __future__ import annotations

PROSE: tuple[str, ...] = (
    "The margin is not empty space. It is where the page admits that it cannot say "
    "everything at once, and where a reader, given room, writes back. A transcription "
    "that loses the margin loses the argument about the text as surely as one that "
    "loses a clause.",
    "The ratio fell from 48.2% to 40.7% between 1990 and 2019, then rose again; the sign "
    "matters more than the level, and −1.5% is not the same claim as 1.5%. An engine that "
    "drops the minus has not made a small error, it has reversed a finding.",
    "A clipboard of 2,340 tokens, a price of £1,024.50, a dose of 250 µg, a speed of "
    "12.5 km/h: units, signs and decimals carry content, and none of them is decoration. "
    "Note also the abbreviations that a reader must not expand, e.g. U.S. and i.e.",
    "Some words are one keystroke apart, and the pairs are cruel: form and from, then and "
    "than, causal and casual, affect and effect. Each survives a spell-checker, each "
    "changes the sentence, and each is exactly the error an audit is looking for.",
    "Émile said the café on the corner charged a naïve visitor a U.S. dollar price out of "
    "a British till, e.g. £1,024.50 for a coffee, i.e. an error no reader would forgive. "
    "Accents and currency symbols are not decoration either.",
    "The transformation of a page into text is a distribution of doubt across a thousand "
    "small decisions, and each one is invisible until it is wrong. The same is true of "
    "the distribution of a budget across a decade of commitments.",
    "A reader that reads a column and then the other column reads a page; a reader that "
    "reads across the gutter reads a nonsense, and the nonsense is fluent. Fluency is "
    "exactly what makes a scrambled page hard to notice, since every sentence still "
    "ends where a sentence can end.",
    "Consider the two claims that differ by a comma: a dollar price in a British till, "
    "and a dollar price, in a British till. One locates the till and the other locates "
    "the price, and a pipeline that drops the comma has chosen a meaning on the "
    "reader's behalf.",
    "Annotations are the page's memory of being argued with. A note number set as a "
    "superscript, a bracketed reference set in blue and underlined, a rule that runs "
    "the width of a table: each is a promise that something above can be checked, and "
    "each is invisible to a reader that only sees shapes.",
    "The last quibble is about order. Three engines that agree on every word can still "
    "disagree about where the words go, and order is not a detail: a list of revisions "
    "read out of sequence tells a different story than the one the page tells.",
)

# Explicit hyphenation: a page may break a word at these points. The dictionary is
# deliberately tiny — the point is a known break, not a general hyphenator.
HYPHEN_BREAKS: dict[str, tuple[str, str]] = {
    "transformation": ("transfor-", "mation"),
    "distribution": ("distribu-", "tion"),
    "transcription": ("transcrip-", "tion"),
}

# A long word that a page splits across the line break, used by the assembly
# tests: the joined form is confirmed elsewhere in the book.
SPLIT_WORD = "transformation"

RUNNING_HEADS: tuple[str, ...] = (
    "THE MEASURE OF MARGINS",
    "QUARRELS WITH SMALL DECISIONS",
)

# Notes pages: a raised note number, prose, and a bracketed blue link. The link
# is a sub-region of its note, read separately with a digit/bracket whitelist.
NOTES: tuple[tuple[int, str], ...] = (
    (1, "The series was revised twice; see [AR2] for the vintage dates."),
    (2, "Percentages are of gross domestic product, not of revenue."),
    (3, "The sign convention follows the statistical annex, [AR7]."),
    (4, "Two of the four decimal places here are reconstructions."),
    (5, "Units are metric throughout; the imperial figures are in an appendix."),
    (6, "Comparable medians for 2,340 municipalities were unavailable."),
    (7, "The £1,024.50 figure includes a rounding correction."),
    (8, "See https://example.invalid/series/1990-2019 for the raw table."),
)

# A TOC screen: label, page label. Roman and arabic both appear.
TOC_ENTRIES: tuple[tuple[str, str], ...] = (
    ("Prefatory Note", "ix"),
    ("1. A Quarrel with Small Decisions", "3"),
    ("2. The Margin as Evidence", "41"),
    ("3. Signed Decimal Places", "77"),
    ("4. Tables and Their Continuations", "118"),
    ("5. What the Audit Cannot See", "164"),
    ("Appendix A. Series and Vintages", "211"),
    ("Appendix B. Units and Conversions", "248"),
)

TABLE_CAPTION = "Table 4.3. Public debt and its revisions, 1990-2019"
TABLE_NUMBER = "4.3"
TABLE_HEADER: tuple[str, ...] = ("Year", "Debt (% GDP)", "Revision (pp)", "Notes")
TABLE_ROWS: tuple[tuple[str, ...], ...] = (
    ("1990", "48.2", "−1.5", "vintage 1995"),
    ("1995", "60.1", "+0.4", "vintage 2001"),
    ("2000", "51.9", "−0.9", "vintage 2006"),
    ("2005", "44.3", "0.0", "")
)
TABLE_ROWS_CONTINUATION: tuple[tuple[str, ...], ...] = (
    ("2010", "86.7", "+2.1", "vintage 2016"),
    ("2015", "101.4", "−0.3", "vintage 2021"),
    ("2019", "118.9", "+0.7", "provisional"),
)

FIGURE_CAPTION = "Figure 2.1. The same sentence, read by three engines"
FIGURE_LABELS: tuple[str, ...] = (
    "tesseract",
    "local vlm",
    "third reader",
    "agreement",
)

# Chrome: the popup the old pipeline leaked onto 20 of 21 pages, and the footer
# the reader must not treat as body text.
POPUP_TEMPLATE = "Back to {location}"
FOOTER_PAGE_TEMPLATE = "Page {page} of {total}"
FOOTER_LOCATION_TEMPLATE = "Location {location} of {total}"

# Page labels, in the three forms the corpus covers: arabic, roman, and the
# Location-only screens that never show "Page N".
ROMAN_LABELS: tuple[str, ...] = ("vi", "vii", "viii", "ix", "x", "xi")

BROKEN_LINE = "quarrel with small decisions, and a quarrel with the"
