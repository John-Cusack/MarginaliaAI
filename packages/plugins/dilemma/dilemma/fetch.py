"""Fetch source artifacts politely, and keep the bytes by digest.

Three courtesies every acquisition goes through, because the sites holding
these texts are small archives and church or university servers, not CDNs:

- **robots.txt is obeyed.** A disallowed URL raises rather than being fetched;
  the source is then a candidate for `mark_blocked`, not a workaround. An
  unreachable or missing robots.txt allows everything, as the convention says.
- **One request per second per host by default**, through a token bucket
  (copied from the academic-journal pack's, which has no dependencies).
- **A descriptive User-Agent**, which names the tool and — only if the operator
  supplies one — a contact. Nobody's address is sent by default.

Fetched bytes land in ``<data_dir>/raw/<source_key>/<sha256>.<ext>`` — the
pack's data directory, never a repository — so a copyrighted text can sit in a
private library without ever being committed anywhere. A URL already fetched
for a source is served from there (``index.json`` remembers which file it
was), so restaging a parse never asks the site twice; ``refresh`` refetches.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

AGENT = "MarginaliaAI-dilemma/0.1"
PURPOSE = "private research library; polite crawler"
#: Operator-supplied contact appended to the User-Agent, e.g. a URL or address.
CONTACT_ENV = "RE_DILEMMA_CONTACT"
RETRY_STATUSES = frozenset({429, 502, 503, 504})


class RobotsDisallowed(PermissionError):
    """The site's robots.txt does not allow this URL for our agent."""


@dataclass
class TokenBucket:
    rate: float  # tokens per second
    max_tokens: float  # burst capacity
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._tokens = self.max_tokens
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.max_tokens, self._tokens + elapsed * self.rate)
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._last_refill = time.monotonic()
            else:
                self._tokens -= 1.0


@dataclass(frozen=True)
class Stored:
    """Bytes kept in the raw store."""

    path: Path
    sha256: str
    size: int
    url: str | None
    retrieved_at: datetime


def user_agent() -> str:
    contact = os.environ.get(CONTACT_ENV, "").strip()
    details = f"{PURPOSE}; +{contact}" if contact else PURPOSE
    return f"{AGENT} ({details})"


class PoliteFetcher:
    """GETs through the pack's gated HTTP client, then stores by digest."""

    def __init__(
        self,
        http: Any,
        data_dir: Path,
        *,
        rate: float = 1.0,
        max_retries: int = 3,
        backoff: float = 2.0,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._http = http
        self._raw = Path(data_dir) / "raw"
        self._rate = rate
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    async def get(self, url: str) -> bytes:
        """Fetch one URL, obeying robots.txt and the per-host rate."""
        host = _host(url)
        if not await self._allowed(url):
            raise RobotsDisallowed(f"robots.txt at {host} disallows {url}")
        for attempt in range(self._max_retries + 1):
            await self._bucket(host).acquire()
            try:
                return await self._http.get(url, headers={"User-Agent": user_agent()})
            except Exception as exc:
                status = _status(exc)
                if status not in RETRY_STATUSES or attempt == self._max_retries:
                    raise
                await self._sleep(self._backoff * (2**attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    async def fetch(
        self, url: str, *, source_key: str, ext: str, refresh: bool = False
    ) -> Stored:
        """Fetch a URL into the raw store, or return the copy already there."""
        if not refresh and (kept := self._remembered(url, source_key)) is not None:
            return kept
        body = await self.get(url)
        stored = self.keep(body, source_key=source_key, ext=ext, url=url)
        self._remember(stored, source_key)
        return stored

    def _index(self, source_key: str) -> Path:
        return self._raw / source_key / "index.json"

    def _remembered(self, url: str, source_key: str) -> Stored | None:
        try:
            entry = json.loads(self._index(source_key).read_text())[url]
        except (OSError, ValueError, KeyError):
            return None
        path = self._raw / source_key / entry["file"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            return None
        return Stored(path, entry["sha256"], path.stat().st_size, url,
                      datetime.fromisoformat(entry["retrieved_at"]))

    def _remember(self, stored: Stored, source_key: str) -> None:
        index = self._index(source_key)
        try:
            known = json.loads(index.read_text())
        except (OSError, ValueError):
            known = {}
        known[stored.url] = {
            "file": stored.path.name,
            "sha256": stored.sha256,
            "retrieved_at": stored.retrieved_at.isoformat(),
        }
        index.write_text(json.dumps(known, indent=2, sort_keys=True))

    def keep(
        self, body: bytes, *, source_key: str, ext: str, url: str | None = None
    ) -> Stored:
        """Store bytes by digest. The same bytes twice are one file."""
        digest = hashlib.sha256(body).hexdigest()
        directory = self._raw / source_key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.{ext.lstrip('.')}"
        # A file of this name whose bytes no longer match was altered on disk:
        # the name promises the digest, so the bytes are put back.
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            partial = path.with_suffix(path.suffix + ".partial")
            partial.write_bytes(body)
            partial.replace(path)
        return Stored(path, digest, len(body), url, datetime.now(UTC))

    def keep_file(self, file: Path, *, source_key: str) -> Stored:
        """Bring a local file — a scan, a purchased ebook — into the raw store."""
        return self.keep(
            Path(file).read_bytes(), source_key=source_key, ext=Path(file).suffix or ".bin"
        )

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = await self._load_robots(origin)
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(user_agent(), url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        await self._bucket(_host(origin)).acquire()
        try:
            body = await self._http.get(
                f"{origin}/robots.txt", headers={"User-Agent": user_agent()}
            )
        except Exception:  # noqa: BLE001 - see below
            # A missing robots.txt (4xx) means "no rules", per the convention.
            # An unreachable one is treated the same: the per-host rate still
            # applies, and a site that is down will fail the real request.
            return None
        parser = RobotFileParser()
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        return parser

    def _bucket(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(rate=self._rate, max_tokens=1.0)
        return self._buckets[host]


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)
