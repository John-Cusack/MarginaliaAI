"""Event writes need `write`; reading the timeline does not.

Every pack used to receive the event client ungated, so any pack could write
the timeline while edges, the same kind of derived assertion, required `write`.
"""

from __future__ import annotations

import pytest

from research_engine.plugins.permissions import GatedEventClient
from research_engine_sdk import PermissionDenied


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create(self, event):
        self.calls.append("create")
        return event

    async def upsert(self, event):
        self.calls.append("upsert")
        return event

    async def delete(self, event_id):
        self.calls.append("delete")
        return True

    async def query(self, filters, k=1000, group_by=None):
        self.calls.append("query")
        return [], []

    async def get_actors(self, event_id):
        self.calls.append("get_actors")
        return []

    async def get_actors_many(self, event_ids):
        self.calls.append("get_actors_many")
        return {}


@pytest.mark.parametrize("method", ["create", "upsert", "delete"])
async def test_writes_need_write(method):
    inner = Recorder()
    client = GatedEventClient(inner, can_write=False, plugin_name="history")

    with pytest.raises(PermissionDenied):
        await getattr(client, method)({})
    assert inner.calls == []


async def test_reads_stay_open():
    inner = Recorder()
    client = GatedEventClient(inner, can_write=False, plugin_name="history")

    await client.query(None)
    await client.get_actors("e")
    await client.get_actors_many(["e"])
    assert inner.calls == ["query", "get_actors", "get_actors_many"]


async def test_with_write_everything_passes_through():
    inner = Recorder()
    client = GatedEventClient(inner, can_write=True, plugin_name="history")

    await client.create({})
    await client.upsert({})
    await client.delete("e")
    assert inner.calls == ["create", "upsert", "delete"]
