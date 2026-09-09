"""Recover verse boundaries from the chapter text this corpus already stores.

The WLC backfill could re-render its chapters from morphhb and diff the result
against the database. LHB and ESV have no such source here — the text in
`core.document_texts` is all there is, so the verses have to be read back out of
it. That is safe only with a guard that fails loudly, so every parser below is
checked for *coverage*: blank out the spans it claims, and whatever is left must
be nothing but verse markers and whitespace. A parser that silently dropped half
a verse would leave that verse's text sitting in a gap, and the check would say
so. Chapters that fail are skipped, never guessed at.

Layouts differ completely between the two editions:

LHB is one verse per block, blocks joined by `VERSE_SEP`, each block
`"{verse} \t{text} "` and the first also carrying the chapter number. It is the
layout the WLC ingest copied, so this parser is the inverse of that renderer.

ESV runs verses together in flowing paragraphs, marks each with `"{n} "`,
breaks poetry across blocks, and puts editorial section headings between verses.
Headings have to be separated from verse text or they land inside a verse's
span, but nothing in the markup distinguishes them: `"David Anointed King"` and
`"Against whom have you raised your voice"` are both untabbed, capitalised and
unpunctuated. So the rule here is deliberately asymmetric — a block is a heading
only when several independent signals agree, and anything short of that stays
part of the verse. Absorbing a heading into a verse makes a span slightly too
wide; promoting a line of Isaiah to a heading would take real scripture out of
the verse that contains it. Only one of those is recoverable.
"""

from __future__ import annotations

import re

VERSE_SEP = "\n\n \n\n"
NBSP = " "

#: A verse marker in the ESV export: the number, then a non-breaking space.
#: Footnote numerals ("waters.1") carry no NBSP, which is what separates them.
ESV_MARK = re.compile(rf"(\d+){NBSP}")

#: What may sit between two spans once the parser has claimed everything it
#: recognises: separators, tabs, and a verse marker. Nothing else.
LHB_GAP = re.compile(r"^\s*(?:\d+ )?\d+ \t\s*$|^\s*$")
#: The double brackets around Mark 16:9-20 and John 7:53-8:11 are the ESV's own
#: text-critical marks. They belong to no verse, so they are allowed to sit in a
#: gap rather than being folded into the reading on either side of them.
ESV_GAP = re.compile(rf"^[\s]*(?:\[\[)?[\s]*(?:\d+{NBSP}[\s]*)?(?:\]\])?[\s]*$")

_SENTENCE_END = tuple(".!?;,:”’\"')-—")


class Region:
    """A claimed stretch of the chapter text."""

    __slots__ = ("kind", "number", "title", "start", "end")

    def __init__(self, kind, start, end, number=None, title=None):
        self.kind, self.start, self.end = kind, start, end
        self.number, self.title = number, title

    def __repr__(self):
        return f"Region({self.kind},{self.number},{self.start}:{self.end})"


def _strip_footnote_digits(s: str) -> str:
    """Drop the footnote numerals the ESV export glues onto a word."""
    return re.sub(r"\d+$", "", s)


def _confident_heading(block: str, following: str | None) -> bool:
    """True only when every signal agrees this block is an editorial heading.

    Four independent tests, all of which a section heading passes and a line of
    verse fails: it is not indented (poetry continuations are), it carries no
    verse marker, it begins a new sentence rather than continuing one, and it
    does not end mid-clause. The fourth test runs after footnote numerals are
    removed, because `"forever;6"` ends in a digit and would otherwise read as
    unpunctuated.

    The last test is the important one: a following block that begins lowercase
    is the rest of a sentence, which means this block was its first half and not
    a heading at all. That single check is what rejects `"Against whom have you
    raised your voice"` / `"and lifted your eyes to the heights?"`.
    """
    stripped = block.strip()
    if not stripped or block.startswith("\t") or ESV_MARK.search(block):
        return False
    if not stripped[0].isupper():
        return False
    if _strip_footnote_digits(stripped).endswith(_SENTENCE_END):
        return False
    tail = following.strip() if following else ""
    return not (tail and tail[0].islower())


def parse_lhb(text: str, chapter: int) -> list[Region] | None:
    """One region per verse, read back out of the renderer's own layout."""
    regions: list[Region] = []
    pos = 0
    for index, block in enumerate(text.split(VERSE_SEP)):
        # The first block normally carries the chapter number as well as the
        # verse ("1 1 \t"). A one-chapter book has none to carry, so Obadiah
        # opens "1 \t" and matches the same pattern as every later block.
        patterns = (
            [rf"^{chapter} (\d+) \t", r"^(\d+) \t"] if index == 0 else [r"^(\d+) \t"]
        )
        match = next((m for m in (re.match(p, block) for p in patterns) if m), None)
        if not match:
            return None
        body = block[match.end() :]
        start = pos + match.end()
        regions.append(
            Region("verse", start, start + len(body.rstrip()), number=int(match.group(1)))
        )
        pos += len(block) + len(VERSE_SEP)
    return regions


def parse_esv(text: str, chapter: int) -> list[Region] | None:
    """Verse regions plus the headings confidently separated from them.

    The first marker in a chapter is the chapter number, not the numeral 1 —
    except in the Psalms, where it introduces an unnumbered superscription and
    verse 1 follows with its own marker. The two cases are told apart by what
    comes next: a following marker of 1 means the first was a superscription.

    Two structural facts do most of the work that guessing at prose cannot.
    Anything before the first verse marker has no verse to belong to and is a
    heading whatever it looks like — which is how the question-form titles
    ("Job Replies: Where Is God?") are caught, since they end in punctuation and
    fail every textual test. And a heading always introduces a verse, so a
    candidate not followed by a marker is a line of verse that merely reads like
    a title; those stay inside their verse, where they can do no harm.
    """
    blocks = text.split(VERSE_SEP)
    offsets, pos = [], 0
    for block in blocks:
        offsets.append(pos)
        pos += len(block) + len(VERSE_SEP)

    marked = [bool(ESV_MARK.search(b)) for b in blocks]
    if not any(marked):
        return None
    opening = marked.index(True)

    heads = {i for i in range(opening) if blocks[i].strip()}
    for i, block in enumerate(blocks):
        if i < opening or marked[i] or not block.strip():
            continue
        nxt = next((j for j in range(i + 1, len(blocks)) if blocks[j].strip()), None)
        # A heading introduces a verse, so the block after it opens with that
        # verse's marker. When the marker sits further in, the text before it is
        # the second half of a verse the heading interrupted, and cutting the
        # verse at the heading would strand it.
        if nxt is None or not re.match(rf"^\t*\d+{NBSP}", blocks[nxt]):
            continue
        if _confident_heading(block, blocks[nxt]):
            heads.add(i)

    marks: list[tuple[int, int]] = []  # (absolute end of marker, number)
    for i, block in enumerate(blocks):
        if i in heads:
            continue
        for m in ESV_MARK.finditer(block):
            marks.append((offsets[i] + m.end(), int(m.group(1))))
    if not marks or marks[0][1] != chapter:
        return None

    # John 8 opens with 7:53, which the ESV prints at the head of the chapter
    # rather than the foot of the previous one. It is a verse, it is citable,
    # and it belongs to the chapter before this one — so it is kept and marked
    # as foreign rather than forcing the whole chapter to be skipped.
    foreign = (
        len(marks) > 2 and marks[1][1] > marks[2][1] and marks[2][1] == 1
    )
    body = marks[2:] if foreign else marks[1:]

    superscription = not foreign and len(marks) > 1 and marks[1][1] == 1
    numbers = [n for _, n in body] if (superscription or foreign) else [1] + [n for _, n in body]
    if numbers != sorted(set(numbers)) or numbers[0] != 1:
        return None

    head_starts = sorted(offsets[h] for h in heads)
    regions: list[Region] = []
    for j, (mark_end, number) in enumerate(marks):
        if j + 1 < len(marks):
            nxt_end, nxt_num = marks[j + 1]
            stop = nxt_end - len(str(nxt_num)) - len(NBSP)
        else:
            stop = len(text)
        for h in head_starts:
            if mark_end < h < stop:
                stop = h
                break
        # Poetry markers are written "\t2\xa0\t", so the verse's own text starts
        # a tab after the marker ends. The span covers the words, not the
        # indentation that lays them out.
        body = text[mark_end:stop]
        begin = mark_end + (len(body) - len(body.lstrip()))
        end = mark_end + len(body.rstrip())
        if begin > end:
            begin = end = mark_end
        if j == 0 and superscription:
            regions.append(Region("superscription", begin, end, title="superscription"))
        elif j == 0 and foreign:
            # The chapter marker itself, carrying only the "[[" that opens the
            # disputed passage. No verse text of its own.
            regions.append(Region("bracket", begin, end))
        elif j == 1 and foreign:
            regions.append(Region("foreign_verse", begin, end, number=number))
        else:
            regions.append(Region("verse", begin, end, number=1 if j == 0 else number))

    for h in sorted(heads):
        block = blocks[h]
        lead = len(block) - len(block.lstrip())
        begin = offsets[h] + lead
        regions.append(Region("heading", begin, begin + len(block.strip()), title=block.strip()))

    regions.sort(key=lambda r: r.start)
    return regions


def coverage_gaps(text: str, regions: list[Region], gap_re: re.Pattern) -> list[str]:
    """Everything the parser did not claim, minus what is allowed to be there.

    This is the guard the whole module rests on. If a parser loses a verse, the
    verse's text turns up here and does not match `gap_re`, so the chapter is
    skipped rather than written with offsets that point at the wrong words.
    """
    bad, cursor = [], 0
    for region in sorted(regions, key=lambda r: r.start):
        if region.start < cursor:
            bad.append(f"overlap at {region.start}")
            continue
        gap = text[cursor : region.start]
        if not gap_re.match(gap):
            bad.append(repr(gap[:60]))
        cursor = region.end
    if not gap_re.match(text[cursor:]):
        bad.append(repr(text[cursor:][:60]))
    return bad


def parse_unmarked(text: str) -> list[Region]:
    """Verses of a chapter that carries no verse numbers at all.

    Leviticus 25 was ingested separately for the dossier work and its verses are
    divided by a bare blank line with nothing to number them. Counting blocks
    from one recovers the numbering only if no verse is missing and none holds a
    blank line of its own, neither of which the text can confirm — so a caller
    must check the result against locators recorded elsewhere before trusting
    it. `backfill_editions` refuses the chapter when it cannot.
    """
    regions, pos = [], 0
    for block in text.split("\n\n"):
        stripped = block.strip()
        if stripped:
            lead = len(block) - len(block.lstrip())
            regions.append(
                Region("verse", pos + lead, pos + lead + len(stripped),
                       number=len(regions) + 1)
            )
        pos += len(block) + 2
    return regions
