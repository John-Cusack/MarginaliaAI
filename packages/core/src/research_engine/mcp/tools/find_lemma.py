"""find_lemma tool -- every occurrence of a lemma, as citable verse references."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.mcp.errors import envelope, failed
from research_engine.services.words import LemmaLookup, LemmaQuery

logger = structlog.get_logger()

TOOL_NAME = "find_lemma"
TOOL_DESCRIPTION = (
    "Find every occurrence of a word by its Strong's number, using the "
    "morphological index rather than string matching. A pointed Hebrew word has "
    "no single searchable form — mishpat (4941) is written 204 distinct ways and "
    "its commonest spelling finds only 21 of 422 occurrences — so searching the "
    "text for a lemma does not work and this tool is how the question gets "
    "asked. Returns verse references, never character spans: the index is built "
    "on WLC, and a span into WLC does not address the same characters in LHB. "
    "Cite the reference in whichever edition you are quoting, then use "
    "verify_quote to resolve the span there. Each occurrence also carries the "
    "English-tradition reference, which differs for 1,978 verses."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strong": {
            "type": "string",
            "description": (
                "Strong's number, digits only — '4941' for mishpat, '6666' for "
                "tsedaqah. Do not prefix it with H or G; name the language "
                "instead."
            ),
        },
        "homograph": {
            "type": "string",
            "description": (
                "OSHB homograph letter, which splits an entry Strong's "
                "conflated ('834 a' and '834 b' are different words sharing a "
                "number). Omit for every homograph; pass a letter for one; pass "
                "an empty string for only the rows carrying no letter."
            ),
        },
        "language": {
            "type": "string",
            "default": "he",
            "description": (
                "Which lexicon the number belongs to. A Strong's number is "
                "unique only inside one language."
            ),
        },
        "book": {
            "type": "string",
            "description": (
                "Restrict to one OSIS book id: 'Isa', 'Ps', '1Kgs', 'Eccl'. "
                "Omit for the whole corpus."
            ),
        },
        "chapters": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "maxItems": 2,
            "description": (
                "Chapter range as [start, end], or [n] for a single chapter. "
                "Applies within `book`."
            ),
        },
        "include_occurrences": {
            "type": "boolean",
            "default": True,
            "description": (
                "False returns only the totals and the counts — cheaper when "
                "the question is about distribution rather than about places."
            ),
        },
    },
    "required": ["strong"],
}


async def handler(
    container: Any,
    *,
    strong: str,
    homograph: str | None = None,
    language: str = "he",
    book: str | None = None,
    chapters: list[int] | None = None,
    include_occurrences: bool = True,
) -> dict[str, Any]:
    """Look a lemma up by number and report where it occurs."""
    try:
        strong = str(strong).strip()
        # "H4941" is the form printed in every lexicon, so accept it and say
        # what happened rather than returning a confident zero.
        stripped_prefix = None
        if strong[:1].upper() in ("H", "G") and strong[1:].isdigit():
            stripped_prefix, strong = strong[0].upper(), strong[1:]
        if not strong.isdigit():
            return envelope("invalid_input", f"strong must be digits, got {strong!r}. Pass '4941', "
                        f"not 'H4941' or a lemma string.", None)

        chapter_start = chapter_end = None
        if chapters:
            if len(chapters) == 1:
                chapter_start = chapter_end = int(chapters[0])
            else:
                chapter_start, chapter_end = int(chapters[0]), int(chapters[1])
            if chapter_start > chapter_end:
                return envelope("invalid_input", f"chapters start {chapter_start} is after end "
                            f"{chapter_end}.", None)

        lookup = LemmaLookup(container.engine)

        if book:
            known = await lookup.known_books(language)
            if book not in known:
                match = [b for b in known if b.lower() == book.lower()]
                if not match:
                    return envelope("unknown_book", f"No book {book!r} in the {language!r} index. "
                                f"Use an OSIS id.", {"known_books": known})
                book = match[0]

        result = await lookup.find(
            LemmaQuery(
                strong=strong,
                language=language,
                homograph=homograph,
                book=book,
                chapter_start=chapter_start,
                chapter_end=chapter_end,
                include_occurrences=include_occurrences,
            )
        )

        notes = list(result.notes)
        if stripped_prefix:
            notes.insert(
                0,
                f"Read {stripped_prefix}{strong} as strong={strong!r} "
                f"language={language!r}.",
            )

        return {
            "query": result.query,
            "total": result.total,
            "books": result.books,
            # Stated on every response because it is the whole point of the
            # tool: what comes back is citable by reference, not by offset.
            "addressing": (
                "Verse references in the Hebrew scheme, citable in LHB and WLC. "
                "No character spans: resolve one with verify_quote against the "
                "edition you are quoting."
            ),
            "occurrences": result.occurrences,
            "counts": result.counts,
            "notes": notes,
        }
    except ValueError as e:
        return envelope("invalid_input", str(e), None)
    except Exception as e:
        logger.error("find_lemma_error", error=str(e))
        return failed(TOOL_NAME, e)
