"""Start the GPU host's embed unit before bulk work, and nothing else.

A bulk command (`ingest`, `embeddings backfill`, `reindex chunks`) calls
`ensure_gpu_host_ready` before it builds its container. If the configured
inference host's `marginalia-embed` unit is not active, this starts it — over
`ssh` when the caller is not on that host — and waits for `/health` to report
`warm=true`. Then the ingest runs. That is the whole bracket.

Deliberately, there is no stop on exit, and no refcount for overlapping
ingests. The goal allows the GPU to stay up for N minutes after a run, and a
client-side stop would need a lease that lives on the *host* to be correct:
two ingests from different machines (laptop over ssh, server locally) cannot
coordinate through files on either caller. A stale lease then either leaks the
server up forever or, worse, stops it under a run that is still embedding.
The server's `--idle-exit-after` already reaps an unused server with no
coordination at all, so the client starts and the server stops itself. A
stop-if-started policy can be added later behind a flag if immediate release
after ingest turns out to matter; until then this is the smallest correct
shape.

Queries never call this. They survive a stopped server by design — embedding
falls back to local, reranking is skipped and flagged — so waking the card
for an interactive search would spend 30–60 s of warm-up to save 66 ms.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import structlog

logger = structlog.get_logger()

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from research_engine.config.settings import Settings

#: The systemd user unit this manages, on the GPU host.
UNIT = "marginalia-embed.service"

#: Seconds between /health polls while waiting for warm=true.
POLL_INTERVAL = 2.0

#: Hostnames that mean "this machine" — no ssh needed.
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class GpuHostError(RuntimeError):
    """The embed unit could not be started, or never reported warm."""


@dataclass(frozen=True)
class HostControl:
    """How to reach the GPU host's systemctl."""

    kind: Literal["local", "ssh"]
    target: str | None = None


def resolve_control(base_url: str, ssh_target: str | None = None) -> HostControl:
    """Decide whether the host behind *base_url* is here or over ssh.

    An explicit *ssh_target* always wins — it names the box to dial when the
    URL itself does not (a reverse proxy, a name only the laptop knows).
    """
    if ssh_target:
        return HostControl("ssh", ssh_target)
    host = (urlsplit(base_url).hostname or "").lower()
    if host in _LOOPBACK or host in _local_names():
        return HostControl("local")
    return HostControl("ssh", host)


def _local_names() -> set[str]:
    """This machine's names, short and long, for the loopback-or-ssh decision."""
    try:
        full = socket.getfqdn().lower()
        short = socket.gethostname().lower()
    except OSError:
        return set()
    names = {full, short}
    names.add(full.split(".")[0])
    names.add(short.split(".")[0])
    return names


def control_command(control: HostControl, *unit_args: str) -> list[str]:
    """Render a `systemctl --user ...` invocation, locally or over ssh."""
    local = ["systemctl", "--user", *unit_args]
    if control.kind == "local":
        return local
    assert control.target is not None
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        control.target,
        *local,
    ]


def _default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


async def _default_wait_warm(base_url: str, timeout: float) -> None:
    """Poll /health until it reports warm, or raise GpuHostError."""
    import httpx

    from research_engine.adapters.embedding.wire import HealthResponse

    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10.0) as client:
        while True:
            try:
                resp = await client.get("/health")
                if resp.status_code == 200:
                    health = HealthResponse.model_validate(resp.json())
                    if health.warm:
                        logger.info(
                            "gpu_host_warm",
                            base_url=base_url,
                            model=health.model_name,
                            device=health.device,
                        )
                        return
            except Exception as exc:  # noqa: BLE001 - not warm yet, keep polling
                logger.debug("gpu_host_health_pending", error=str(exc))
            if time.monotonic() >= deadline:
                raise GpuHostError(
                    f"Embedding server at {base_url} did not report warm=true "
                    f"within {timeout:g}s of `systemctl --user start {UNIT}`. "
                    f"Check the unit on its host: "
                    f"`systemctl --user status {UNIT}`."
                )
            await asyncio.sleep(POLL_INTERVAL)


async def watch_gpu_host(
    settings: Settings,
    stop: asyncio.Event,
    *,
    run: Callable[[list[str]], Any] | None = None,
    wait_warm: Callable[[str, float], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval: float = 60.0,
) -> None:
    """Re-wake the unit while a bulk run is still going. Runs until cancelled.

    The startup bracket covers a cold card at launch, but a run's own CPU
    phases (510 scanned pages of OCR) are silent for longer than
    `--idle-exit-after`, and the server cannot tell an ingest is coming back.
    So the client keeps watch for the run's duration: if the unit went down,
    start it again (and wait for warm, so it is ready before the next batch).
    Start-only like the bracket, so overlapping runs stay safe; a start that
    fails here only logs, and the batch that needs the server fails loudly
    itself. Cancel when the run ends.
    """
    base_url = settings.resolved_inference_base_url
    if base_url is None:  # pragma: no cover - callers skip watching then
        return
    control = resolve_control(base_url, settings.embed_ssh_target)
    runner = run or _default_run
    waiter = wait_warm or _default_wait_warm
    where = "this host" if control.kind == "local" else control.target
    while not stop.is_set():
        active = await asyncio.to_thread(
            runner, control_command(control, "is-active", UNIT)
        )
        if active.returncode != 0:
            logger.warning("gpu_host_went_down_mid_run", where=where, unit=UNIT)
            started = await asyncio.to_thread(
                runner, control_command(control, "start", UNIT)
            )
            if started.returncode != 0:
                logger.error(
                    "gpu_host_rewake_failed",
                    where=where,
                    detail=str(
                        getattr(started, "stderr", "")
                        or getattr(started, "stdout", "")
                    ).strip(),
                )
            else:
                try:
                    await waiter(base_url, settings.embed_start_timeout)
                except GpuHostError as exc:
                    logger.error("gpu_host_rewake_never_warmed", error=str(exc))
        await sleep(interval)


async def ensure_gpu_host_ready(
    settings: Settings,
    *,
    run: Callable[[list[str]], Any] | None = None,
    wait_warm: Callable[[str, float], Awaitable[None]] | None = None,
) -> str:
    """Start the embed unit if bulk embedding needs it and it is down.

    Returns `"started"`, `"already-running"`, or `"skipped"`. Never stops
    anything — see the module docstring for why. Raises GpuHostError when the
    unit cannot be started or never warms; callers fail fast before opening
    the database.
    """
    base_url = settings.resolved_inference_base_url
    mode = settings.embed_manage_gpu
    if mode == "never" or base_url is None:
        return "skipped"
    if mode == "auto" and settings.embedding_provider == "local_bge":
        # Bulk embedding runs on this machine; there is nothing to wake.
        return "skipped"

    control = resolve_control(base_url, settings.embed_ssh_target)
    runner = run or _default_run
    waiter = wait_warm or _default_wait_warm
    where = "this host" if control.kind == "local" else control.target

    active = await asyncio.to_thread(
        runner, control_command(control, "is-active", UNIT)
    )
    if active.returncode == 0:
        logger.info("gpu_host_already_running", base_url=base_url, where=where)
        return "already-running"

    logger.info("gpu_host_starting", base_url=base_url, where=where, unit=UNIT)
    started = await asyncio.to_thread(
        runner, control_command(control, "start", UNIT)
    )
    if started.returncode != 0:
        raise GpuHostError(
            f"Could not start {UNIT} on {where}: "
            f"`{' '.join(control_command(control, 'start', UNIT))}` "
            f"exited {started.returncode}: "
            f"{getattr(started, 'stderr', '') or getattr(started, 'stdout', '')}"
            .strip()
        )
    await waiter(base_url, settings.embed_start_timeout)
    return "started"
