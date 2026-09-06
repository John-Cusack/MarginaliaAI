"""Footnotes derive display strings from metadata; gaps are provisional, never silent."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from research_engine.services.verification.quote import Tier
from research_engine.services.works.render import WorkRenderer

pytestmark = pytest.mark.unit

DOC = uuid.uuid4()

ENTRY = """  - id: {cid}
    document_id: {doc}
    char_start: 34
    char_end: 62
    quoted_text: "The prophets pair two words."
    intent: quotation
    edition_key: DABAR_2026
    locator: {{volume: II, page: 64}}
"""


def _file(title_entries: str, body: str = "Reading [^c1] closely.\n") -> str:
    return (
        "---\n"
        'work: W-001\ntitle: "A fragment"\ntype: essay\nstatus: draft\n'
        "created: 2026-09-04\nclaims: []\n"
        f"citations:\n{title_entries}---\n\n{body}"
    )


class FakeDocuments:
    def __init__(self, docs: dict) -> None:
        self._docs = docs

    async def get(self, document_id):
        return self._docs.get(document_id)


class FakeVerification:
    def __init__(self, tier: Tier = Tier.NORMALIZED) -> None:
        self.tier = tier

    async def verify(self, quote, document_id=None, *, window=None):
        return SimpleNamespace(tier=self.tier)


def _renderer(tmp_path, docs, tier=Tier.NORMALIZED) -> WorkRenderer:
    return WorkRenderer(FakeDocuments(docs), FakeVerification(tier), tmp_path)


def _doc(metadata: dict, title: str = "Dabaris") -> SimpleNamespace:
    return SimpleNamespace(title=title, metadata=metadata)


class TestRender:
    @pytest.mark.asyncio
    async def test_full_metadata_renders_the_guide_example(self, tmp_path):
        (tmp_path / "essay.md").write_text(
            _file(ENTRY.format(cid="c1", doc=DOC)), encoding="utf-8"
        )
        renderer = _renderer(tmp_path, {DOC: _doc({
            "author": "Kittel",
            "title": "Theological Dictionary of the New Testament",
            "year": "1964",
        })})

        result = await renderer.render("essay.md")

        assert result["footnotes"] == [{
            "id": "c1",
            "text": "Kittel, *Theological Dictionary of the New Testament*, "
            "vol. II (1964), p. 64. [normalized]",
            "provisional": False,
            "tier": "normalized",
        }]
        assert result["rendered"].endswith(
            "[^c1]: Kittel, *Theological Dictionary of the New Testament*, "
            "vol. II (1964), p. 64. [normalized]\n"
        )

    @pytest.mark.asyncio
    async def test_missing_author_is_provisional(self, tmp_path):
        (tmp_path / "essay.md").write_text(
            _file(ENTRY.format(cid="c1", doc=DOC)), encoding="utf-8"
        )
        renderer = _renderer(tmp_path, {DOC: _doc({"title": "Dabaris", "year": "2020"})})

        (footnote,) = (await renderer.render("essay.md"))["footnotes"]

        assert footnote["provisional"] is True
        assert footnote["text"].startswith(f"document {DOC}, *Dabaris*")
        assert footnote["text"].endswith("[normalized] [provisional]")

    @pytest.mark.asyncio
    async def test_missing_year_is_provisional(self, tmp_path):
        (tmp_path / "essay.md").write_text(
            _file(ENTRY.format(cid="c1", doc=DOC)), encoding="utf-8"
        )
        renderer = _renderer(tmp_path, {DOC: _doc({"author": "Anon", "title": "Notes"})})

        (footnote,) = (await renderer.render("essay.md"))["footnotes"]

        assert footnote["provisional"] is True
        assert "(20" not in footnote["text"]

    @pytest.mark.asyncio
    async def test_title_falls_back_to_the_document_title(self, tmp_path):
        (tmp_path / "essay.md").write_text(
            _file(ENTRY.format(cid="c1", doc=DOC)), encoding="utf-8"
        )
        renderer = _renderer(
            tmp_path, {DOC: _doc({"author": "Anon", "year": "2020"}, title="Dabaris")}
        )

        (footnote,) = (await renderer.render("essay.md"))["footnotes"]

        assert "*Dabaris*" in footnote["text"]
        assert footnote["provisional"] is False

    @pytest.mark.asyncio
    async def test_unknown_document_is_provisional(self, tmp_path):
        (tmp_path / "essay.md").write_text(
            _file(ENTRY.format(cid="c1", doc=DOC)), encoding="utf-8"
        )

        (footnote,) = (await _renderer(tmp_path, {}).render("essay.md"))["footnotes"]

        assert footnote["provisional"] is True
        assert f"document {DOC}" in footnote["text"]

    @pytest.mark.asyncio
    async def test_footnotes_emit_in_id_order(self, tmp_path):
        entries = ENTRY.format(cid="c2", doc=DOC) + ENTRY.format(cid="c1", doc=DOC)
        (tmp_path / "essay.md").write_text(
            _file(entries, "First [^c2], then [^c1].\n"), encoding="utf-8"
        )
        renderer = _renderer(
            tmp_path, {DOC: _doc({"author": "Anon", "title": "N", "year": "2020"})}
        )

        result = await renderer.render("essay.md")

        assert [note["id"] for note in result["footnotes"]] == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_stale_definitions_are_replaced_not_doubled(self, tmp_path):
        (tmp_path / "essay.md").write_text(
            _file(
                ENTRY.format(cid="c1", doc=DOC),
                "Reading [^c1] closely.\n\n[^c1]: a stale hand-typed note.\n",
            ),
            encoding="utf-8",
        )
        renderer = _renderer(
            tmp_path, {DOC: _doc({"author": "Anon", "title": "N", "year": "2020"})}
        )

        result = await renderer.render("essay.md")

        assert "stale" not in result["rendered"]
        assert result["rendered"].count("[^c1]:") == 1
