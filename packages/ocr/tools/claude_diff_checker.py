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

# A difference is either a reading (the candidate's words and the image's, each at
# most eight words) or, when the place differs too much to describe in eight
# words, a *location only*: the line range that differs, with no reading at all.
# The second form exists because a heavily degraded region provokes long answers,
# and refusing the whole reply for one over-long reading threw away every
# difference on the screen. It carries no text, so the diff-only rule holds.
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
                    "lines": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "kind": {"type": "string", "enum": ["differs_extensively"]},
                    "confidence": {"type": "number"},
                },
                "required": [],
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
- **If your reading of a place would run past eight words, you MUST use the \
location-only form**: `{{"lines": [start, end], "kind": "differs_extensively"}}`, \
the character range in the candidate text that differs, and no reading at all. A \
reading longer than eight words is discarded and the difference is recorded from \
its location, so the form costs you nothing and costs the answer nothing. Use the \
reading form whenever eight words or fewer can describe the place.
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
    # True when this difference arrived as a reading over the eight-word cap and
    # was reduced to its location. The reading's text is not kept anywhere.
    downgraded: bool = False

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
    attempts: int = 1
    errors: tuple[str, ...] = field(default_factory=tuple)


def words(text: str) -> list[str]:
    return [word for word in re.split(r"\s+", text.strip()) if word]


def validate(payload: object, candidate_length: int | None = None) -> tuple[Difference, ...]:
    """Refuse anything that is not a diff-only answer.

    Two forms are accepted: a reading (the candidate's words and the image's, each
    at most eight words) and a *location only* — a line range with no text at all,
    for a place that differs too much to describe in eight words. The second form
    keeps the diff-only cap intact while still letting the difference be found.
    """
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
        if entry.get("kind") == "differs_extensively":
            lines = entry.get("lines")
            if (
                not isinstance(lines, list)
                or len(lines) != 2
                or not all(isinstance(value, int) for value in lines)
                or lines[0] < 0
                or lines[1] <= lines[0]
                or (candidate_length is not None and lines[1] > candidate_length)
            ):
                raise ResponseRejected("a differs_extensively difference needs a valid [start, end]")
            out.append(
                Difference(
                    snippet="",
                    reading="",
                    confidence=None,
                    final_start=lines[0],
                    final_end=lines[1],
                )
            )
            continue
        snippet, reading = entry.get("snippet"), entry.get("reading")
        if not isinstance(snippet, str) or not isinstance(reading, str):
            raise ResponseRejected("a difference needs string 'snippet' and 'reading'")
        if len(words(snippet)) > MAX_SNIPPET_WORDS:
            # The snippet is the *location* of the difference; without a usable
            # one there is nothing to place, so this is structural.
            raise ResponseRejected(f"snippet runs past {MAX_SNIPPET_WORDS} words")
        downgraded = False
        if len(words(reading)) > MAX_SNIPPET_WORDS:
            # The reading is the image side, and the operator's rule: an
            # individual reading over the cap is downgraded to a location-only
            # difference. Its location is the snippet's place in the candidate;
            # the over-long text is discarded here and never stored, logged or
            # scored. The rest of the reply stays valid.
            reading = ""
            downgraded = True
        confidence = entry.get("confidence")
        out.append(
            Difference(
                snippet=snippet,
                reading=reading,
                confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
                downgraded=downgraded,
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
            # Volume of what is *kept*: a downgraded reading is not stored, so it
            # is not counted either.
            total += len(words(str(entry.get("snippet", ""))))
            reading_words = words(str(entry.get("reading", "")))
            if len(reading_words) <= MAX_SNIPPET_WORDS:
                total += len(reading_words)
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
    attempts: int = 2,
) -> CheckResult:
    """Ask Claude where this screen and this candidate text differ.

    A reply that fails validation is **never** "no differences". It is re-asked
    once; if it fails again the result comes back with ``rejected`` set and the
    caller decides what that means: the release gate counts it as a detection
    (fail closed, the same rule as the adjudicator), and the sensitivity
    measurement counts it as a miss. Both are honest; treating it as agreement
    would not be.
    """
    last: CheckResult | None = None
    for attempt in range(max(1, attempts)):
        last = _check_once(image_path, candidate, model=model, timeout=timeout, claude=claude)
        last = CheckResult(
            differences=last.differences,
            seconds=last.seconds,
            cost_usd=last.cost_usd,
            model=last.model,
            raw_word_count=last.raw_word_count,
            rejected=last.rejected,
            attempts=attempt + 1,
        )
        if last.rejected is None:
            return last
    assert last is not None
    return last


def _check_once(
    image_path: Path,
    candidate: str,
    *,
    model: str | None = DEFAULT_MODEL,
    timeout: float = 300.0,
    claude: str = "claude",
) -> CheckResult:
    """One ask, one answer, validated or rejected."""
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
        differences = tuple(
            locate(difference, candidate)
            for difference in validate(payload, candidate_length=len(candidate))
        )
    except (ResponseRejected, json.JSONDecodeError) as exc:
        return CheckResult(seconds=seconds, rejected=str(exc))
    return CheckResult(
        differences=differences,
        seconds=seconds,
        cost_usd=cost,
        model=used_model or model,
        raw_word_count=raw_words,
    )
