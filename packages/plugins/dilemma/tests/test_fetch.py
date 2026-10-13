"""The fetcher is polite before it is useful.

robots.txt refusals stop a fetch; a missing robots.txt does not; overloaded
servers are retried with backoff, other errors are not; nobody's contact goes
out unless the operator sets one; and the same bytes are stored once.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from dilemma import fetch
from dilemma.fetch import PoliteFetcher, RobotsDisallowed

pytestmark = pytest.mark.unit


class HttpStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.response = SimpleNamespace(status_code=status)


class FakeHttp:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url: str, **kwargs: object) -> bytes:
        self.calls.append((url, kwargs))
        result = self.routes.get(url, HttpStatusError(404))
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


async def no_sleep(_seconds: float) -> None:
    return None


def fetcher(http: FakeHttp, tmp_path) -> PoliteFetcher:
    return PoliteFetcher(http, tmp_path, rate=1000.0, sleep=no_sleep)


async def test_robots_disallow_stops_the_fetch(tmp_path) -> None:
    http = FakeHttp({
        "https://x.org/robots.txt": b"User-agent: *\nDisallow: /private/\n",
        "https://x.org/private/a": b"secret",
    })

    with pytest.raises(RobotsDisallowed):
        await fetcher(http, tmp_path).get("https://x.org/private/a")
    assert [url for url, _ in http.calls] == ["https://x.org/robots.txt"]


async def test_a_missing_robots_txt_allows_everything(tmp_path) -> None:
    http = FakeHttp({"https://x.org/a": b"text"})

    assert await fetcher(http, tmp_path).get("https://x.org/a") == b"text"


async def test_robots_is_read_once_per_origin(tmp_path) -> None:
    http = FakeHttp({"https://x.org/a": b"1", "https://x.org/b": b"2"})
    polite = fetcher(http, tmp_path)

    await polite.get("https://x.org/a")
    await polite.get("https://x.org/b")

    assert [url for url, _ in http.calls].count("https://x.org/robots.txt") == 1


async def test_overload_is_retried_and_other_errors_are_not(tmp_path) -> None:
    http = FakeHttp({
        "https://x.org/busy": [HttpStatusError(503), HttpStatusError(429), b"ok"],
        "https://x.org/gone": HttpStatusError(410),
    })
    polite = fetcher(http, tmp_path)

    assert await polite.get("https://x.org/busy") == b"ok"
    with pytest.raises(HttpStatusError):
        await polite.get("https://x.org/gone")
    assert [url for url, _ in http.calls].count("https://x.org/gone") == 1


async def test_the_user_agent_carries_a_contact_only_when_given(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(fetch.CONTACT_ENV, raising=False)
    http = FakeHttp({"https://x.org/a": b"text"})
    await fetcher(http, tmp_path).get("https://x.org/a")
    agent = http.calls[-1][1]["headers"]["User-Agent"]
    assert agent.startswith("MarginaliaAI-dilemma/")
    assert "@" not in agent and "+" not in agent

    monkeypatch.setenv(fetch.CONTACT_ENV, "https://example.org/contact")
    assert fetch.user_agent().endswith("; +https://example.org/contact)")


async def test_bytes_are_stored_once_by_digest(tmp_path) -> None:
    http = FakeHttp({"https://x.org/a": b"Section V"})
    polite = fetcher(http, tmp_path)

    first = await polite.fetch("https://x.org/a", source_key="dc-1835", ext="html")
    second = polite.keep(b"Section V", source_key="dc-1835", ext=".html")

    digest = hashlib.sha256(b"Section V").hexdigest()
    assert first.path == tmp_path / "raw" / "dc-1835" / f"{digest}.html"
    assert second.path == first.path
    assert first.sha256 == digest and first.url == "https://x.org/a"
    assert sorted(p.name for p in first.path.parent.glob("*.html")) == [f"{digest}.html"]


async def test_a_url_already_fetched_is_served_from_the_store(tmp_path) -> None:
    http = FakeHttp({"https://x.org/a": [b"first", b"second"]})
    polite = fetcher(http, tmp_path)

    first = await polite.fetch("https://x.org/a", source_key="kjv", ext="zip")
    again = await polite.fetch("https://x.org/a", source_key="kjv", ext="zip")
    gets = [url for url, _ in http.calls if url.endswith("/a")]
    assert gets == ["https://x.org/a"]
    assert (again.path, again.sha256, again.retrieved_at) == (
        first.path, first.sha256, first.retrieved_at
    )

    fresh = await polite.fetch("https://x.org/a", source_key="kjv", ext="zip", refresh=True)
    assert fresh.sha256 == hashlib.sha256(b"second").hexdigest()


async def test_a_tampered_copy_is_fetched_again(tmp_path) -> None:
    http = FakeHttp({"https://x.org/a": [b"first", b"first"]})
    polite = fetcher(http, tmp_path)
    first = await polite.fetch("https://x.org/a", source_key="kjv", ext="zip")
    first.path.write_bytes(b"edited")

    again = await polite.fetch("https://x.org/a", source_key="kjv", ext="zip")

    assert [url for url, _ in http.calls if url.endswith("/a")] == ["https://x.org/a"] * 2
    assert again.sha256 == hashlib.sha256(b"first").hexdigest()
    assert again.path.read_bytes() == b"first"
