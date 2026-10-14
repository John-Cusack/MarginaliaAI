"""The bibliography and profile clients cross the SDK boundary under `ingest`.

A pack records where its editions came from, and how a reader holds them,
through these clients, so each must conform to its SDK protocol, reach a pack
only when the pack may ingest, and — in the in-memory mocks packs test
against — keep the same rules the real adapter enforces.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_engine.adapters.bibliography_client import BibliographyServiceAdapter
from research_engine.adapters.ingestion_client import IngestionServiceAdapter
from research_engine.adapters.profile_client import ProfileServiceAdapter, config_hash
from research_engine.domain.acquisitions import DocumentMarkDraft
from research_engine.plugins.loader import LoadedPlugin, PluginLoader
from research_engine.plugins.permissions import (
    DeniedBibliographyClient,
    DeniedProfileClient,
)
from research_engine.plugins.registry import PluginRegistry
from research_engine_sdk import (
    BibliographyClient,
    MarkDraft,
    PassageDraft,
    PermissionDenied,
    PluginPermissions,
    ProfileClient,
)
from research_engine_sdk.testing import MockBibliographyClient, MockProfileClient

pytestmark = pytest.mark.unit


def _adapter(tmp_path) -> BibliographyServiceAdapter:
    return BibliographyServiceAdapter(
        editions=AsyncMock(),
        acquisitions=AsyncMock(),
        transaction_factory=lambda: None,
        data_dir=tmp_path,
    )


def _profiles() -> ProfileServiceAdapter:
    return ProfileServiceAdapter(
        editions=AsyncMock(),
        profiles=AsyncMock(),
        runs=AsyncMock(),
        transaction_factory=lambda: None,
    )


def test_adapter_denial_and_mock_all_conform(tmp_path) -> None:
    assert isinstance(_adapter(tmp_path), BibliographyClient)
    assert isinstance(DeniedBibliographyClient("p"), BibliographyClient)
    assert isinstance(MockBibliographyClient(), BibliographyClient)
    assert isinstance(_profiles(), ProfileClient)
    assert isinstance(DeniedProfileClient("p"), ProfileClient)
    assert isinstance(MockProfileClient(), ProfileClient)


def _loader_with(tmp_path, permissions: PluginPermissions) -> PluginLoader:
    loader = PluginLoader(
        AsyncMock(),
        PluginRegistry(),
        tmp_path / "plugin-data",
        bibliography=_adapter(tmp_path),
        profiles=_profiles(),
    )
    discovery = SimpleNamespace(
        manifest=SimpleNamespace(permissions=permissions),
        plugin_id="sample",
        distribution_name="marginalia-ai-plugin-sample",
        distribution_version="1.0.0",
    )
    loader._loaded["sample"] = LoadedPlugin(discovery)
    return loader


async def test_a_pack_without_ingest_is_refused(tmp_path) -> None:
    clients = _loader_with(tmp_path, PluginPermissions()).build_plugin_clients("sample")

    with pytest.raises(PermissionDenied):
        await clients["bibliography"].register_source("X")
    with pytest.raises(PermissionDenied):
        await clients["profiles"].load_profile(
            "p", version=1, profile_hash="0" * 64, roles=[]
        )


def test_a_pack_with_ingest_gets_the_real_client(tmp_path) -> None:
    clients = _loader_with(
        tmp_path, PluginPermissions(ingest=True)
    ).build_plugin_clients("sample")

    assert isinstance(clients["bibliography"], BibliographyServiceAdapter)
    assert isinstance(clients["profiles"], ProfileServiceAdapter)


async def test_blocking_through_set_status_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="mark_blocked"):
        await _adapter(tmp_path).set_status("X", "blocked")


async def test_an_artifact_must_exist(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        await _adapter(tmp_path).attach_artifact("X", tmp_path / "absent.html")


async def test_ingest_adapter_passes_parser_identity_and_marks_through() -> None:
    registry = PluginRegistry()
    registry.register_document_type("verses", {"default_chunker": "prose_window"}, "p")
    orchestrator = AsyncMock()
    orchestrator.ingest_drafts.return_value = {"document_id": "d", "passage_count": 1}
    text = "1 In the beginning."

    await IngestionServiceAdapter(orchestrator, registry).ingest_drafts(
        "Genesis 1",
        "verses",
        [
            PassageDraft(
                position=0, char_start=0, char_end=len(text), text=text,
                chunker="p.verse", chunker_version="1",
            )
        ],
        full_text=text,
        parser="p.kjv",
        parser_version="3",
        mark_drafts=[MarkDraft(kind="supplied", char_start=2, char_end=4)],
    )

    call = orchestrator.ingest_drafts.await_args
    assert (call.kwargs["parser"], call.kwargs["parser_version"]) == ("p.kjv", "3")
    (mark,) = call.kwargs["mark_drafts"]
    assert isinstance(mark, DocumentMarkDraft)
    assert (mark.kind, mark.char_start, mark.char_end) == ("supplied", 2, 4)


async def test_mock_keeps_progress_on_reregistration() -> None:
    client = MockBibliographyClient()
    await client.register_source("BoM-1830", url="https://a")
    await client.set_status("BoM-1830", "loaded")

    again = await client.register_source("BoM-1830", url="https://b")

    assert again["status"] == "loaded"
    assert again["url"] == "https://b"


async def test_mock_enforces_blocked_reason_and_registration_first() -> None:
    client = MockBibliographyClient()
    with pytest.raises(ValueError, match="blocked_reason"):
        await client.register_source("X", status="blocked")
    with pytest.raises(LookupError, match="register_source first"):
        await client.report_coverage(
            "Y", unit="verse", loaded_units=1, checker="c", checker_version="1"
        )

    await client.mark_blocked("X", reason="print only", suggested_acquisition="scan")
    assert [s["edition_key"] for s in await client.list_sources(status="blocked")] == ["X"]


async def test_a_role_outside_the_vocabulary_is_refused_before_the_database() -> None:
    with pytest.raises(ValueError, match="role"):
        await _profiles().load_profile(
            "p", version=1, profile_hash="0" * 64,
            roles=[{"edition_key": "kjv", "role": "canon"}],
        )


def test_config_hash_ignores_key_order_and_spacing() -> None:
    assert config_hash({"tau": 0.5, "k": 50}) == config_hash({"k": 50, "tau": 0.5})
    assert config_hash({"tau": 0.5}) != config_hash({"tau": 0.6})


async def test_mock_profile_replaces_roles_and_closes_runs_once() -> None:
    client = MockProfileClient(editions={"kjv", "bom"})
    await client.load_profile(
        "p", version=1, profile_hash="a" * 64,
        roles=[{"edition_key": "kjv", "role": "binding"}],
    )
    with pytest.raises(LookupError, match="not registered"):
        await client.load_profile(
            "p", version=2, profile_hash="b" * 64,
            roles=[{"edition_key": "absent", "role": "binding"}],
        )
    run = await client.start_run("p", stage="ingest")
    assert run["profile_hash"] == "a" * 64
    await client.finish_run(run["id"])
    with pytest.raises(ValueError, match="already closed"):
        await client.finish_run(run["id"])
