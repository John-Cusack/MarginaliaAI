"""Recovering a section table from markdown headings."""

from __future__ import annotations

from research_engine.domain.nodes import build_node_tree
from research_engine.services.text.sections import sections_from_markdown

DOC = """Preamble prose with no heading above it.

# Alpha

First body.

## Alpha One

Nested body.

# Beta

Second body.
"""


def test_headings_become_sections_with_exact_offsets():
    sections = sections_from_markdown(DOC)

    assert [s["heading"] for s in sections] == ["Alpha", "Alpha One", "Beta"]
    assert [s["level"] for s in sections] == [1, 2, 1]
    for section in sections:
        span = DOC[section["char_start"] : section["char_end"]]
        assert span.startswith("#")
        assert section["heading"] in span.splitlines()[0]


def test_sections_are_disjoint_and_the_tree_supplies_containment():
    """Division of labour: this reports headings, the builder nests them."""
    alpha, alpha_one, _beta = sections_from_markdown(DOC)

    # Disjoint here — Alpha stops where Alpha One begins.
    assert alpha["char_end"] <= alpha_one["char_start"]

    # Containment appears only once the tree has widened parents.
    tree = build_node_tree(sections_from_markdown(DOC), text_length=len(DOC))
    by_title = {node.title: node for node in tree if node.title}
    assert by_title["Alpha"].char_start <= by_title["Alpha One"].char_start
    assert by_title["Alpha One"].char_end <= by_title["Alpha"].char_end


def test_prose_before_the_first_heading_gets_no_section():
    sections = sections_from_markdown(DOC)

    assert sections[0]["char_start"] == DOC.index("# Alpha")
    # Not lost: the document root spans everything.
    root = build_node_tree(sections, text_length=len(DOC))[0]
    assert (root.char_start, root.char_end) == (0, len(DOC))


def test_text_with_no_headings_yields_no_sections():
    assert sections_from_markdown("Just prose.\n\nMore prose.") == []


def test_spans_are_trimmed_of_trailing_blank_lines():
    sections = sections_from_markdown(DOC)

    for section in sections:
        span = DOC[section["char_start"] : section["char_end"]]
        assert span == span.strip()


def test_hash_that_is_not_a_heading_is_ignored():
    text = "# Real\n\nA C preprocessor line follows.\n\n#include <stdio.h>\n\n#\n"

    assert [s["heading"] for s in sections_from_markdown(text)] == ["Real"]


def test_sections_feed_the_tree_builder_with_correct_nesting():
    tree = build_node_tree(sections_from_markdown(DOC), text_length=len(DOC))

    by_title = {node.title: node for node in tree if node.title}
    assert by_title["Alpha One"].parent_path == by_title["Alpha"].path
    assert by_title["Beta"].parent_path == "r"
