"""One edition from catalog to corpus, through the same steps every time.

Driven with the SDK's in-memory clients, a fake ingestion client and a test
reader, so it runs offline: registration never resets progress, blocked
editions stay out, robots.txt blocks rather than fails, a staged run writes
nothing, a load whose counts are wrong is refused unless accepted, a loaded
edition is never loaded twice, and a full run hands core the parser
identity, tree, windows and marks, then reports coverage.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from dilemma import catalog, pipeline, sources, tools
from dilemma.catalog import Acquire, EditionEntry
from dilemma.fetch import RobotsDisallowed

from research_engine_sdk.testing import MockBibliographyClient, MockProfileClient
from research_engine_sdk.units import Unit, UnitDocument, UnitMark

pytestmark = pytest.mark.unit

ENTRY = EditionEntry(
    key="test-kjv",
    title="Test KJV",
    work="bible",
    acquire=Acquire(method="bulk_download", url="https://ebible.org/x", license="unverified"),
    expect={"verse": 2},
)


class FakeIngestion:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.duplicate = False

    async def ingest_drafts(self, title, document_type, passages, **kwargs):
        self.calls.append({"title": title, "document_type": document_type,
                           "passages": passages, **kwargs})
        result = {"document_id": "d", "passage_count": len(passages)}
        return {**result, "skipped": "duplicate"} if self.duplicate else result


class TestReader:
    keys: ClassVar[tuple[str, ...]] = ("test-kjv",)
    parser: ClassVar[str] = "dilemma.test-usfm"
    parser_version: ClassVar[str] = "1"
    refuse = False
    verses = 2

    async def acquire(self, context):
        if self.refuse:
            raise RobotsDisallowed("robots.txt at ebible.org disallows /x")
        return [context.fetcher.keep(b"\\v 1 In the beginning", source_key="test-kjv",
                                     ext="usfm", url="https://ebible.org/x")]

    def parse(self, key, artifacts):
        units = (
            Unit(ref="Genesis 1:1", label="1", text="In the beginning God created."),
            Unit(ref="Genesis 1:2", label="2", text="And the earth was void.",
                 marks=(UnitMark("supplied", 14, 17),)),
        )
        yield UnitDocument(title="Genesis 1", source="test:kjv:Gen.1",
                           units=units[: self.verses])


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(catalog, "entry", lambda key: ("bible", ENTRY))
    monkeypatch.setattr(sources, "_registry", {"test-kjv": TestReader})
    monkeypatch.setattr(sources, "_discovered", True)
    monkeypatch.setattr(TestReader, "refuse", False)
    monkeypatch.setattr(TestReader, "verses", 2)
    return MockBibliographyClient(), FakeIngestion()


async def run(wired, tmp_path, **kwargs):
    bibliography, ingestion = wired
    return await pipeline.ingest_source(
        "test-kjv", bibliography=bibliography, ingestion=ingestion, http=None,
        data_dir=tmp_path, **kwargs,
    )


async def test_a_full_run_hands_core_everything_it_needs(wired, tmp_path) -> None:
    bibliography, ingestion = wired

    outcome = await run(wired, tmp_path)

    assert outcome.status == "loaded"
    assert outcome.coverage_ok is True
    (call,) = ingestion.calls
    assert (call["parser"], call["parser_version"]) == ("dilemma.test-usfm", "1")
    assert call["metadata"]["edition_key"] == "test-kjv"
    assert call["metadata"]["work"] == "bible"
    assert {p.chunker for p in call["passages"]} == {"unit_window"}
    assert [m.kind for m in call["mark_drafts"]] == ["supplied"]
    assert call["full_text"][call["mark_drafts"][0].char_start:
                             call["mark_drafts"][0].char_end] == "was"
    source = bibliography.sources["test-kjv"]
    assert source["status"] == "loaded"
    assert source["work_key"] == "bible"
    assert {r["unit"] for r in source["coverage"]} >= {"verse", "chapter"}
    assert source["artifacts"][0]["url"] == "https://ebible.org/x"
    assert (tmp_path / "qa" / "test-kjv.md").read_text().count("## Genesis") == 2


async def test_a_staged_run_writes_nothing_to_the_corpus(wired, tmp_path) -> None:
    bibliography, ingestion = wired

    outcome = await run(wired, tmp_path, stage_only=True)

    assert outcome.status == "staged"
    assert ingestion.calls == []
    assert bibliography.sources["test-kjv"]["coverage"] == []
    assert outcome.passages >= 1


async def test_a_count_mismatch_is_refused_and_recorded(wired, tmp_path,
                                                       monkeypatch) -> None:
    bibliography, ingestion = wired
    monkeypatch.setattr(TestReader, "verses", 1)

    outcome = await run(wired, tmp_path)

    assert outcome.status == "refused"
    assert ingestion.calls == []
    source = bibliography.sources["test-kjv"]
    assert source["status"] == "acquired"
    assert any(not r["ok"] for r in source["coverage"])


async def test_an_accepted_mismatch_loads_and_says_so(wired, tmp_path, monkeypatch) -> None:
    bibliography, ingestion = wired
    monkeypatch.setattr(TestReader, "verses", 1)

    outcome = await run(wired, tmp_path, accept_mismatch=True)

    assert outcome.status == "loaded"
    assert len(ingestion.calls) == 1
    verse = next(r for r in bibliography.sources["test-kjv"]["coverage"] if r["unit"] == "verse")
    assert "mismatch accepted by the operator at load" in verse["anomalies"]


async def test_a_loaded_edition_is_not_loaded_again(wired, tmp_path) -> None:
    _, ingestion = wired
    await run(wired, tmp_path)

    again = await run(wired, tmp_path)

    assert again.status == "loaded"
    assert "not loaded again" in again.detail
    assert len(ingestion.calls) == 1


async def test_documents_already_stored_count_as_present(wired, tmp_path) -> None:
    _, ingestion = wired
    ingestion.duplicate = True

    outcome = await run(wired, tmp_path)

    assert outcome.status == "loaded"
    assert outcome.documents_already_present == 1


async def test_robots_blocks_the_source_rather_than_failing(wired, tmp_path,
                                                            monkeypatch) -> None:
    bibliography, ingestion = wired
    monkeypatch.setattr(TestReader, "refuse", True)

    outcome = await run(wired, tmp_path)

    assert outcome.status == "blocked"
    assert bibliography.sources["test-kjv"]["suggested_acquisition"] == "permission"
    assert ingestion.calls == []


async def test_a_manual_edition_waits_for_the_operator(wired, tmp_path, monkeypatch) -> None:
    bibliography, ingestion = wired

    class HandSaved(TestReader):
        async def acquire(self, context):
            return [context.fetcher.keep_file(f, source_key="test-kjv")
                    for f in context.inbox_files(".html")]

    monkeypatch.setattr(sources, "_registry", {"test-kjv": HandSaved})
    waiting = await run(wired, tmp_path)
    assert waiting.status == "awaiting_operator"
    assert str(tmp_path / "inbox" / "test-kjv") in waiting.detail
    assert bibliography.sources["test-kjv"]["status"] == "planned"

    inbox = tmp_path / "inbox" / "test-kjv"
    inbox.mkdir(parents=True)
    (inbox / "page.html").write_text("<p>saved by hand</p>")
    assert (await run(wired, tmp_path)).status == "loaded"


async def test_reregistering_after_a_load_keeps_it_loaded(wired, tmp_path) -> None:
    bibliography, _ = wired
    await run(wired, tmp_path)

    await pipeline.register_entry(bibliography, "bible", ENTRY)

    assert bibliography.sources["test-kjv"]["status"] == "loaded"


async def test_a_catalog_block_is_lifted_by_the_catalog_alone(wired) -> None:
    bibliography, _ = wired
    blocked = ENTRY.model_copy(
        update={"blocked": catalog.Blocked(reason="needs a key", suggest="api_key")}
    )

    await pipeline.register_entry(bibliography, "islam", blocked)
    assert bibliography.sources["test-kjv"]["status"] == "blocked"

    await pipeline.register_entry(bibliography, "islam", ENTRY)
    assert bibliography.sources["test-kjv"]["status"] == "planned"


async def test_a_robots_block_is_not_the_catalogs_to_lift(wired, tmp_path,
                                                         monkeypatch) -> None:
    bibliography, _ = wired
    monkeypatch.setattr(TestReader, "refuse", True)
    await run(wired, tmp_path)

    await pipeline.register_entry(bibliography, "bible", ENTRY)

    assert bibliography.sources["test-kjv"]["status"] == "blocked"


async def test_an_existing_edition_is_registered_not_reingested(tmp_path) -> None:
    bibliography, ingestion = MockBibliographyClient(), FakeIngestion()

    outcome = await pipeline.ingest_source(
        "WLC", bibliography=bibliography, ingestion=ingestion, http=None,
        data_dir=tmp_path,
    )

    assert outcome.status == "loaded"
    assert ingestion.calls == []
    assert bibliography.sources["WLC"]["status"] == "loaded"


async def test_loading_a_profile_registers_its_editions_and_roles() -> None:
    bibliography, profiles = MockBibliographyClient(), MockProfileClient()

    report = await tools.profile_load(bibliography, profiles, "sunni_islam")

    declared = catalog.profile("sunni_islam")
    assert report["profile_hash"] == declared.profile_hash
    assert report["editions"] == len(declared.editions_needed())
    stored = profiles.profiles["sunni_islam"]
    assert stored["settings"]["blind"] is True
    externals = [r for r in stored["roles"] if r["role"] == "external"]
    assert {tuple(r["scope"]["books"])[0] for r in externals} == {"Genesis", "Matthew"}


async def test_ingest_refuses_a_profile_that_is_stale_or_not_loaded(tmp_path) -> None:
    bibliography, profiles = MockBibliographyClient(), MockProfileClient()
    context = type("Context", (), {"data_dir": tmp_path})()

    with pytest.raises(ValueError, match="not loaded"):
        await tools.ingest(bibliography, None, None, profiles, context,
                           profile="sunni_islam", slice="m1")

    await tools.profile_load(bibliography, profiles, "sunni_islam")
    profiles.profiles["sunni_islam"]["profile_hash"] = "0" * 64
    with pytest.raises(ValueError, match="changed"):
        await tools.ingest(bibliography, None, None, profiles, context,
                           profile="sunni_islam", slice="m1")


async def test_an_ingest_is_one_recorded_run(tmp_path, monkeypatch) -> None:
    bibliography, profiles = MockBibliographyClient(), MockProfileClient()
    await tools.profile_load(bibliography, profiles, "sunni_islam")
    context = type("Context", (), {"data_dir": tmp_path})()
    monkeypatch.setattr(sources, "_registry", {})
    monkeypatch.setattr(sources, "_discovered", True)

    report = await tools.ingest(bibliography, None, None, profiles, context,
                                profile="sunni_islam", slice="m1")

    (run,) = profiles.runs.values()
    assert run["status"] == "finished"
    assert run["stage"] == "ingest"
    assert run["config"]["editions"] == list(catalog.profile("sunni_islam").slice("m1").sources)
    assert report["run_id"] == run["id"]
    assert report["by_status"] == {"planned": 4}


async def test_the_queue_puts_the_highest_tier_first() -> None:
    bibliography = MockBibliographyClient()
    for group, entry in catalog.entries():
        if entry.blocked:
            await pipeline.register_entry(bibliography, group, entry)

    queue = (await tools.blocked_queue(bibliography))["queue"]

    tiers = [row["tier"] or "T9" for row in queue]
    assert tiers == sorted(tiers)
    assert all(row["suggested_acquisition"] for row in queue)
