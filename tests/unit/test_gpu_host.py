"""The ingest bracket starts the GPU host's unit and never stops it.

Starting is the bracket's only systemd effect: shutdown belongs to the
server's `--idle-exit-after`, which needs no coordination. These tests pin
that — especially that overlapping ingests cannot stop the server under each
other, because no path issues a stop at all.

The watcher exists because a run's own CPU phases (OCR on scanned pages) are
silent for longer than the idle timeout: startup-ensure alone lets the server
exit under its own ingest.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from research_engine.adapters.inference.gpu_host import (
    GpuHostError,
    control_command,
    ensure_gpu_host_ready,
    resolve_control,
    watch_gpu_host,
)
from research_engine.config.settings import Settings

HOST = "http://john-super-server:9882"

pytestmark = pytest.mark.unit


def _settings(**overrides):
    base = {
        "embedding_provider": "auto",
        "inference_base_url": HOST,
        "embed_start_timeout": 30.0,
    }
    base.update(overrides)
    return Settings(**base)


def _rc(returncode, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """A systemctl --user stand-in. First is-active answers can be scripted."""

    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]):
        self.calls.append(argv)
        if argv[-2] == "is-active":
            return _rc(0 if self.active else 3, stdout="active" if self.active else "inactive")
        return _rc(0)

    def stops(self) -> list[list[str]]:
        return [c for c in self.calls if "stop" in c]

    def starts(self) -> list[list[str]]:
        return [c for c in self.calls if "stop" not in c and c[-2] == "start"]


async def _warmed(url: str, timeout: float) -> None:
    return None


class TestResolveControl:
    def test_remote_host_goes_over_ssh(self):
        control = resolve_control(HOST)
        assert control.kind == "ssh"
        assert control.target == "john-super-server"

    def test_loopback_stays_local(self):
        assert resolve_control("http://localhost:9882").kind == "local"
        assert resolve_control("http://127.0.0.1:9882").kind == "local"

    def test_explicit_ssh_target_wins(self):
        control = resolve_control(HOST, ssh_target="gpu-box.tailnet")
        assert (control.kind, control.target) == ("ssh", "gpu-box.tailnet")

    def test_ssh_renders_noninteractive_systemctl(self):
        argv = control_command(resolve_control(HOST), "is-active", "marginalia-embed.service")
        assert argv == [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "john-super-server",
            "systemctl",
            "--user",
            "is-active",
            "marginalia-embed.service",
        ]

    def test_local_renders_plain_systemctl(self):
        argv = control_command(resolve_control("http://localhost:9882"), "start", "marginalia-embed.service")
        assert argv == ["systemctl", "--user", "start", "marginalia-embed.service"]


class TestEnsure:
    @pytest.mark.asyncio
    async def test_skipped_when_management_is_off(self):
        runner = FakeRunner()
        result = await ensure_gpu_host_ready(
            _settings(embed_manage_gpu="never"), run=runner, wait_warm=_warmed
        )
        assert result == "skipped"
        assert runner.calls == []

    @pytest.mark.asyncio
    async def test_skipped_when_bulk_embedding_is_local(self):
        runner = FakeRunner()
        result = await ensure_gpu_host_ready(
            Settings(embedding_provider="local_bge", inference_base_url=HOST),
            run=runner,
            wait_warm=_warmed,
        )
        assert result == "skipped"
        assert runner.calls == []

    @pytest.mark.asyncio
    async def test_skipped_without_a_configured_host(self):
        runner = FakeRunner()
        result = await ensure_gpu_host_ready(
            Settings(embedding_provider="auto"), run=runner, wait_warm=_warmed
        )
        assert result == "skipped"
        assert runner.calls == []

    @pytest.mark.asyncio
    async def test_already_running_issues_no_start(self):
        runner = FakeRunner(active=True)
        warmed = []

        async def watch(url: str, timeout: float) -> None:
            warmed.append(url)

        result = await ensure_gpu_host_ready(_settings(), run=runner, wait_warm=watch)
        assert result == "already-running"
        assert len(runner.calls) == 1
        assert warmed == []

    @pytest.mark.asyncio
    async def test_starts_and_waits_for_warm(self):
        runner = FakeRunner(active=False)
        seen: list[tuple[str, float]] = []

        async def watch(url: str, timeout: float) -> None:
            seen.append((url, timeout))

        result = await ensure_gpu_host_ready(_settings(), run=runner, wait_warm=watch)
        assert result == "started"
        assert len(runner.starts()) == 1
        assert seen == [(HOST, 30.0)]

    @pytest.mark.asyncio
    async def test_start_failure_is_one_loud_line(self):
        def failing(argv: list[str]):
            if argv[-2] == "is-active":
                return _rc(3, stdout="inactive")
            return _rc(1, stderr="Failed to start unit: not found")

        with pytest.raises(GpuHostError, match="Could not start"):
            await ensure_gpu_host_ready(_settings(), run=failing, wait_warm=_warmed)

    @pytest.mark.asyncio
    async def test_never_warm_aborts_before_the_database(self):
        async def cold(url: str, timeout: float) -> None:
            raise GpuHostError(f"{url} did not report warm=true")

        with pytest.raises(GpuHostError, match="did not report warm"):
            await ensure_gpu_host_ready(
                _settings(), run=FakeRunner(active=False), wait_warm=cold
            )

    @pytest.mark.asyncio
    async def test_overlapping_ingests_never_stop_the_server(self):
        """Two ingests racing the same cold host both proceed; neither stops.

        There is no refcount to get wrong because no path issues a stop —
        shutdown is the server's idle-exit, not the clients'.
        """
        runner = FakeRunner(active=False)

        first, second = await asyncio.gather(
            ensure_gpu_host_ready(_settings(), run=runner, wait_warm=_warmed),
            ensure_gpu_host_ready(_settings(), run=runner, wait_warm=_warmed),
        )
        assert {first, second} <= {"started", "already-running"}
        assert runner.stops() == []


class TestWatchGpuHost:
    @pytest.mark.asyncio
    async def test_rewakes_a_server_that_exited_mid_run(self):
        """The OCR-parse case: silent longer than the idle timeout."""
        runner = FakeRunner(active=False)
        warmed: list[str] = []

        async def watch(url: str, timeout: float) -> None:
            warmed.append(url)

        stop = asyncio.Event()
        polls = 0

        async def sleep(_: float) -> None:
            nonlocal polls
            polls += 1
            if polls >= 2:
                stop.set()

        await watch_gpu_host(
            _settings(), stop, run=runner, wait_warm=watch, sleep=sleep
        )
        assert len(runner.starts()) >= 1
        assert warmed
        assert runner.stops() == []

    @pytest.mark.asyncio
    async def test_quiet_while_the_server_is_up(self):
        runner = FakeRunner(active=True)
        warmed: list[str] = []

        async def watch(url: str, timeout: float) -> None:
            warmed.append(url)

        stop = asyncio.Event()
        polls = 0

        async def sleep(_: float) -> None:
            nonlocal polls
            polls += 1
            if polls >= 3:
                stop.set()

        await watch_gpu_host(
            _settings(), stop, run=runner, wait_warm=watch, sleep=sleep
        )
        assert runner.starts() == []
        assert warmed == []

    @pytest.mark.asyncio
    async def test_a_failed_rewake_does_not_kill_the_run(self):
        """The batch that needs the server fails loudly itself; the watcher
        only logs and keeps watching, so a transient systemctl failure is
        not a second, quieter way to die."""

        def flaky(argv: list[str]):
            if argv[-2] == "is-active":
                return _rc(3, stdout="inactive")
            return _rc(1, stderr="systemd is having a moment")

        stop = asyncio.Event()
        polls = 0

        async def sleep(_: float) -> None:
            nonlocal polls
            polls += 1
            if polls >= 2:
                stop.set()

        await watch_gpu_host(_settings(), stop, run=flaky, wait_warm=_warmed,
                             sleep=sleep)
