"""Compare a screen against the text we intend to store, with Claude, diff-only.

Anthropic's output filter blocks verbatim reproduction of long passages, so
Claude cannot be a transcription reader of book pages (design §2). What it can
do is what the audit needs: look at one screen and one candidate text and say
where they differ. This module is that reader, in the shape D1 has to choose
between; WP5 productionizes the same thing inside the service.

Diff-only is enforced, not requested:

* the prompt asks for differences and nothing else, and a JSON schema fixes the
  response shape;
* every snippet and every reading is refused if it runs past eight words, and a
  response that carries more than ``MAX_DIFFERENCES`` of them is refused whole —
  a response that looks like a transcription is rejected, never trimmed;
* nothing here logs the image, the candidate text, or the snippets beyond a
  count, and the candidate text is passed to the process, never printed.

The runner is the Claude Code CLI in headless mode (``claude -p``) with the
operator's own subscription and only the ``Read`` tool available, which is how
the image gets seen: the CLI reads the file itself.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

MAX_SNIPPET_WORDS = 8
MAX_DIFFERENCES = 64
DEFAULT_MODEL = None  # the CLI's own default; the caller may pin one

DIFFERENCE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "differences": {
            "type": "array",
            "maxItems": MAX_DIFFERENCES,
            "items": {
                "type": "object",
                "properties": {
                    "snippet": {"type": "string"},
                    "reading": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["snippet", "reading"],
            },
        }
    },
    "required": ["differences"],
}

PROMPT = """\
You are auditing one screen of a book against the text an OCR pipeline intends \
to store for it.

The page image is the file {image}. Read it. The text below is the candidate \
storage text; it may be wrong.

The candidate text covers the page's body text only. Ignore the running head, \
the page number or footer label, and any popup such as "Back to 123": those are \
chrome the candidate is not supposed to contain.

Report ONLY the differences: places where the image says something the \
candidate text does not.

Rules, all of them binding:
- For each difference give `snippet` (the candidate's own words at that place, \
picking up to three words either side of the difference so the place is \
unambiguous) and `reading` (what the image actually shows there). Both are at \
most 8 words. Never longer.
- Do not transcribe the page. Do not summarise it. Do not quote more than 8 \
consecutive words of it anywhere in your answer.
- If the image and the candidate agree, return an empty list.
- Return only the JSON object the schema describes. No prose.

Answer with JSON: {{"differences": [{{"snippet": "...", "reading": "...", \
"confidence": 0.0}}]}}
"""


class ResponseRejected(ValueError):
    """The response is not a diff-only answer; the caller must not score it."""


@dataclass(frozen=True)
class Difference:
    snippet: str
    reading: str
    confidence: float | None = None
    final_start: int | None = None
    final_end: int | None = None

    @property
    def located(self) -> bool:
        return self.final_start is not None and self.final_end is not None


@dataclass(frozen=True)
class CheckResult:
    """What one screen's audit produced. Snippets are not kept beyond scoring."""

    differences: tuple[Difference, ...] = ()
    seconds: float = 0.0
    cost_usd: float | None = None
    model: str | None = None
    rejected: str | None = None
    raw_word_count: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def words(text: str) -> list[str]:
    return [word for word in re.split(r"\s+", text.strip()) if word]


def validate(payload: object) -> tuple[Difference, ...]:
    """Refuse anything that is not a diff-only answer."""
    if not isinstance(payload, dict) or "differences" not in payload:
        raise ResponseRejected("response is not an object with a 'differences' list")
    entries = payload["differences"]
    if not isinstance(entries, list):
        raise ResponseRejected("'differences' is not a list")
    if len(entries) > MAX_DIFFERENCES:
        raise ResponseRejected(f"{len(entries)} differences exceeds the limit of {MAX_DIFFERENCES}")
    out: list[Difference] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ResponseRejected("a difference is not an object")
        snippet, reading = entry.get("snippet"), entry.get("reading")
        if not isinstance(snippet, str) or not isinstance(reading, str):
            raise ResponseRejected("a difference needs string 'snippet' and 'reading'")
        for label, value in (("snippet", snippet), ("reading", reading)):
            if len(words(value)) > MAX_SNIPPET_WORDS:
                raise ResponseRejected(f"{label} runs past {MAX_SNIPPET_WORDS} words")
        confidence = entry.get("confidence")
        out.append(
            Difference(
                snippet=snippet,
                reading=reading,
                confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            )
        )
    return tuple(out)


def parse_envelope(stdout: str) -> tuple[object, float | None, str | None, int]:
    """Pull the structured answer, cost, model and word count out of the CLI's JSON."""
    envelope = json.loads(stdout)
    if not isinstance(envelope, dict):
        raise ResponseRejected("the CLI did not return a JSON object")
    cost = envelope.get("total_cost_usd")
    model = envelope.get("model")
    payload: object = envelope.get("structured_output")
    if payload is None:
        result = envelope.get("result")
        if not isinstance(result, str):
            raise ResponseRejected("the CLI returned no result")
        payload = json.loads(_first_json_object(result))
    return payload, float(cost) if isinstance(cost, (int, float)) else None, model, _text_volume(payload)


def _text_volume(payload: object) -> int:
    """How much text came back, in words: the number the diff-only rule is about."""
    if not isinstance(payload, dict) or not isinstance(payload.get("differences"), list):
        return 0
    total = 0
    for entry in payload["differences"]:
        if isinstance(entry, dict):
            total += len(words(str(entry.get("snippet", "")))) + len(words(str(entry.get("reading", ""))))
    return total


def _first_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ResponseRejected("no JSON object in the result")
    return text[start : end + 1]


def locate(difference: Difference, candidate: str) -> Difference:
    """Find the snippet in the candidate text so a difference can be scored."""
    snippet = difference.snippet.strip()
    if not snippet:
        return difference
    index = candidate.find(snippet)
    if index < 0:
        collapsed = " ".join(snippet.split())
        for match in re.finditer(r"\S+(?:\s+\S+)*", candidate):
            if " ".join(match.group(0).split()) == collapsed:
                index = match.start()
                break
    if index < 0:
        return difference
    return Difference(
        snippet=difference.snippet,
        reading=difference.reading,
        confidence=difference.confidence,
        final_start=index,
        final_end=index + len(snippet),
    )


def check(
    image_path: Path,
    candidate: str,
    *,
    model: str | None = DEFAULT_MODEL,
    timeout: float = 300.0,
    claude: str = "claude",
) -> CheckResult:
    """Ask Claude where this screen and this candidate text differ."""
    command = [
        claude,
        "-p",
        PROMPT.format(image=str(image_path)),
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(DIFFERENCE_SCHEMA),
        "--allowedTools",
        "Read",
    ]
    if model:
        command += ["--model", model]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            input=candidate,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(seconds=time.monotonic() - started, rejected="timeout")
    seconds = time.monotonic() - started
    if completed.returncode != 0:
        return CheckResult(seconds=seconds, rejected=f"exit {completed.returncode}")
    try:
        payload, cost, used_model, raw_words = parse_envelope(completed.stdout)
        differences = tuple(locate(difference, candidate) for difference in validate(payload))
    except (ResponseRejected, json.JSONDecodeError) as exc:
        return CheckResult(seconds=seconds, rejected=str(exc))
    return CheckResult(
        differences=differences,
        seconds=seconds,
        cost_usd=cost,
        model=used_model or model,
        raw_word_count=raw_words,
    )
