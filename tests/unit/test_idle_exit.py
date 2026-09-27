"""The server's idle exit: die quietly when nobody needs the card.

Exit 0 matters more than the timer: the unit runs with
`Restart=on-failure`, and only a clean status keeps a reaped server stopped.
The tracker and watcher are clock-injectable so none of this sleeps for real.
"""

from __future__ import annotations

import asyncio

import pytest

from research_engine.adapters.embedding.server import (
    IdleTracker,
    idle_exit_watcher,
)

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestIdleTracker:
    def test_starts_at_zero_idle(self):
        assert IdleTracker(clock=Clock()).idle_seconds() == 0.0

    def test_idle_grows_with_the_clock(self):
        clock = Clock()
        tracker = IdleTracker(clock=clock)
        clock.now += 901.0
        assert tracker.idle_seconds() == pytest.approx(901.0)

    def test_activity_resets_the_clock(self):
        clock = Clock()
        tracker = IdleTracker(clock=clock)
        clock.now += 800.0
        tracker.note_activity()
        clock.now += 100.0
        assert tracker.idle_seconds() == pytest.approx(100.0)


class TestIdleExitWatcher:
    @pytest.mark.asyncio
    async def test_fires_once_past_the_threshold(self):
        clock = Clock()
        tracker = IdleTracker(clock=clock)
        shutdowns: list[None] = []

        async def sleep(_: float) -> None:
            clock.now += 30.0

        await idle_exit_watcher(
            tracker, 60.0, shutdown=lambda: shutdowns.append(None), sleep=sleep
        )
        assert len(shutdowns) == 1

    @pytest.mark.asyncio
    async def test_steady_traffic_never_fires(self):
        """Each request resets the timer, so a used server stays up."""
        clock = Clock()
        tracker = IdleTracker(clock=clock)
        shutdowns: list[None] = []
        polls = 0

        async def sleep(_: float) -> None:
            nonlocal polls
            polls += 1
            clock.now += 30.0
            tracker.note_activity()
            if polls >= 5:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await idle_exit_watcher(
                tracker, 60.0, shutdown=lambda: shutdowns.append(None), sleep=sleep
            )
        assert shutdowns == []

    @pytest.mark.asyncio
    async def test_lifespan_arms_the_watcher_only_when_configured(self, monkeypatch):
        from starlette.testclient import TestClient

        import research_engine.adapters.embedding.server as server_module
        from research_engine.adapters.embedding.server import create_app

        armed: list[float] = []

        async def fake_watcher(tracker, idle_exit_after, **kwargs):
            armed.append(idle_exit_after)

        monkeypatch.setattr(server_module, "idle_exit_watcher", fake_watcher)
        monkeypatch.setattr(
            "research_engine.adapters.embedding.local_bge.LocalBGEEmbedding",
            _FakeEmbedding,
        )

        with TestClient(create_app(idle_exit_after=60.0)):
            pass
        assert armed == [60.0]

        armed.clear()
        with TestClient(create_app()):
            pass
        assert armed == []


class _FakeEmbedding:
    def __init__(self, *args, **kwargs) -> None:
        pass
