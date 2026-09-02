"""Announcing that a document asked for structural chunking and did not get it.

Three built-in modules shipped for months returning `"structural"` from
`default_chunker()` and handing the pipeline no section table. The fallback to
prose windows is silent by design and produces perfectly good passages, so
nothing looked wrong — the structure was simply gone, and stayed gone until
someone went looking.

The hard part is not logging it. It is not crying wolf: a markdown file with no
`#`, or an HTML page with no `<h1>`, genuinely has no structure, and prose
windows are the right answer. What separates the two is that a parser which
*counted* headings and then supplied none has dropped them on the floor.
"""

from __future__ import annotations

import pytest
import structlog

from research_engine.services.ingestion.pipeline import run_chunking

TEXT = "Some prose that is long enough to chunk. " * 20


@pytest.fixture
def events():
    """Capture structlog events for the duration of one test."""
    captured: list[dict] = []

    def sink(_logger, method_name, event_dict):
        # The sink runs before `add_log_level`, so the level comes from the
        # method that was called rather than from the event dict.
        captured.append({"level": method_name, **event_dict})
        raise structlog.DropEvent

    original = structlog.get_config()["processors"]
    structlog.configure(processors=[sink])
    try:
        yield captured
    finally:
        structlog.configure(processors=original)


async def test_a_parser_that_counted_structure_and_dropped_it_warns(events):
    """The defect. `heading_count` says the headings were found."""
    await run_chunking(
        TEXT, "structural", {"heading_count": 39, "file_name": "a.md"}, parser_id="markdown"
    )

    [event] = [e for e in events if e["event"] == "structural_sections_missing"]
    assert event["level"] == "warning"
    assert event["parser"] == "markdown"
    assert event["reported"] == {"heading_count": 39}


async def test_a_document_that_really_has_none_does_not_warn(events):
    """The false alarm this must not raise: a flat file is not a bug."""
    await run_chunking(
        TEXT, "structural", {"heading_count": 0, "file_name": "flat.md"}, parser_id="markdown"
    )

    assert not [e for e in events if e["event"] == "structural_sections_missing"]
    [event] = [e for e in events if e["event"] == "structural_sections_absent"]
    assert event["level"] == "info"


async def test_no_metadata_at_all_is_reported_as_absent_not_broken(events):
    await run_chunking(TEXT, "structural")

    assert [e["event"] for e in events if e["event"].startswith("structural_")] == [
        "structural_sections_absent"
    ]


@pytest.mark.parametrize(
    "key", ["heading_count", "section_count", "chapter_count", "div_count"]
)
async def test_any_structure_count_is_enough_to_warn(events, key):
    """Each module names its own: EPUB counts chapters, TEI counts divs."""
    await run_chunking(TEXT, "structural", {key: 7}, parser_id="p")

    assert [e for e in events if e["event"] == "structural_sections_missing"]


async def test_a_document_with_sections_says_nothing(events):
    await run_chunking(
        TEXT,
        "structural",
        {"heading_count": 1, "sections": [{"char_start": 0, "char_end": 40, "heading": "H"}]},
        parser_id="markdown",
    )

    assert not [e for e in events if e["event"].startswith("structural_")]


async def test_a_text_chunker_is_not_reported_on(events):
    """Prose windows asked for directly are not a demotion."""
    await run_chunking(TEXT, "prose_window", {"heading_count": 5})

    assert not [e for e in events if e["event"].startswith("structural_")]
