"""The document structure tree: nesting, containment, and addressing."""

from __future__ import annotations

import pytest

from research_engine.domain.nodes import ROOT_PATH, DocumentNodeDraft, build_node_tree


def section(start: int, end: int, heading: str | None = None, level: int | None = None):
    out: dict = {"char_start": start, "char_end": end}
    if heading is not None:
        out["heading"] = heading
    if level is not None:
        out["level"] = level
    return out


def test_a_document_with_no_sections_still_has_a_root():
    drafts = build_node_tree([], text_length=500, title="Empty")

    assert len(drafts) == 1
    assert drafts[0].path == ROOT_PATH
    assert drafts[0].node_type == "document"
    assert (drafts[0].char_start, drafts[0].char_end) == (0, 500)


def test_flat_sections_all_parent_to_the_root():
    drafts = build_node_tree(
        [section(0, 10, "One", 1), section(12, 20, "Two", 1)],
        text_length=20,
    )

    assert [d.path for d in drafts] == [ROOT_PATH, "r.n0", "r.n1"]
    assert [d.parent_path for d in drafts[1:]] == [ROOT_PATH, ROOT_PATH]
    assert [d.depth for d in drafts] == [0, 1, 1]
    assert [d.position for d in drafts[1:]] == [0, 1]


def test_levels_nest():
    drafts = build_node_tree(
        [
            section(0, 10, "Chapter", 1),
            section(10, 20, "Section", 2),
            section(20, 30, "Subsection", 3),
            section(30, 40, "Next chapter", 1),
        ],
        text_length=40,
    )

    by_title = {d.title: d for d in drafts if d.title}
    assert by_title["Section"].parent_path == by_title["Chapter"].path
    assert by_title["Subsection"].parent_path == by_title["Section"].path
    assert by_title["Next chapter"].parent_path == ROOT_PATH
    assert by_title["Subsection"].depth == 3


def test_a_skipped_level_parents_to_the_nearest_shallower_node():
    """h1 -> h3 with no h2 is ordinary in real books and must not orphan."""
    drafts = build_node_tree(
        [section(0, 10, "Chapter", 1), section(10, 20, "Deep", 3)],
        text_length=20,
    )

    by_title = {d.title: d for d in drafts if d.title}
    assert by_title["Deep"].parent_path == by_title["Chapter"].path


def test_parents_are_widened_to_contain_their_children():
    """Parsers report a heading's own span, not its subtree's."""
    drafts = build_node_tree(
        [
            section(0, 10, "Chapter", 1),
            section(10, 25, "Section", 2),
            section(25, 60, "Deeper", 3),
        ],
        text_length=60,
    )

    chapter = next(d for d in drafts if d.title == "Chapter")
    assert chapter.char_start == 0
    assert chapter.char_end == 60


def test_every_child_span_sits_inside_its_parent():
    drafts = build_node_tree(
        [
            section(0, 10, "A", 1),
            section(10, 20, "A.1", 2),
            section(20, 30, "A.2", 2),
            section(30, 45, "B", 1),
            section(45, 50, "B.1", 2),
        ],
        text_length=50,
    )

    by_path = {d.path: d for d in drafts}
    for draft in drafts:
        if draft.parent_path is None:
            continue
        parent = by_path[draft.parent_path]
        assert parent.char_start <= draft.char_start
        assert draft.char_end <= parent.char_end


def test_parents_precede_children_so_inserts_can_resolve_ids():
    drafts = build_node_tree(
        [section(0, 10, "A", 1), section(10, 20, "A.1", 2), section(20, 30, "B", 1)],
        text_length=30,
    )

    seen: set[str] = set()
    for draft in drafts:
        if draft.parent_path is not None:
            assert draft.parent_path in seen
        seen.add(draft.path)


def test_sections_without_a_span_are_skipped_not_guessed():
    drafts = build_node_tree(
        [section(0, 10, "Real", 1), {"heading": "Spanless", "level": 1}],
        text_length=10,
    )

    assert [d.title for d in drafts] == [None, "Real"]


def test_extra_section_keys_survive_as_metadata():
    drafts = build_node_tree(
        [{"char_start": 0, "char_end": 5, "heading": "H", "level": 1, "href": "c1.xhtml"}],
        text_length=5,
    )

    assert drafts[1].metadata == {"href": "c1.xhtml"}
    assert drafts[1].title == "H"


def test_a_reversed_span_is_rejected():
    with pytest.raises(ValueError, match="precedes"):
        DocumentNodeDraft(
            path="r.n0", parent_path="r", depth=1, position=0,
            char_start=10, char_end=4,
        )


class _Node:
    """A stored node's shape, without needing a database to make one."""

    def __init__(self, node_id, depth, char_start, char_end):
        self.id = node_id
        self.depth = depth
        self.char_start = char_start
        self.char_end = char_end


def test_deepest_containing_prefers_the_innermost_node():
    from research_engine.domain.nodes import deepest_containing

    root = _Node("root", 0, 0, 100)
    chapter = _Node("chapter", 1, 0, 60)
    section = _Node("section", 2, 10, 40)

    found = deepest_containing([root, chapter, section], 15, 20)

    assert found.id == "section"


def test_a_passage_straddling_siblings_resolves_to_their_ancestor():
    from research_engine.domain.nodes import deepest_containing

    root = _Node("root", 0, 0, 100)
    first = _Node("first", 1, 0, 40)
    second = _Node("second", 1, 40, 80)

    assert deepest_containing([root, first, second], 30, 50).id == "root"


def test_a_span_outside_every_node_resolves_to_nothing():
    from research_engine.domain.nodes import deepest_containing

    assert deepest_containing([_Node("n", 1, 0, 10)], 50, 60) is None


def test_attach_nodes_stamps_each_draft_with_its_container():
    from research_engine.domain.nodes import attach_nodes
    from research_engine.domain.passages import PassageDraft

    def draft(start, end):
        return PassageDraft(
            position=0, char_start=start, char_end=end, text="x" * (end - start),
            chunker="structural", chunker_version="3.0",
        )

    nodes = [_Node("root", 0, 0, 100), _Node("inner", 1, 0, 50)]
    stamped = attach_nodes([draft(0, 10), draft(60, 70)], nodes)

    assert [d.node_id for d in stamped] == ["inner", "root"]


def test_attach_nodes_is_a_no_op_without_a_tree():
    from research_engine.domain.nodes import attach_nodes
    from research_engine.domain.passages import PassageDraft

    drafts = [
        PassageDraft(
            position=0, char_start=0, char_end=3, text="abc",
            chunker="prose_window", chunker_version="2.0",
        )
    ]

    assert attach_nodes(drafts, []) == drafts
