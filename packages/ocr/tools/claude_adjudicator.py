"""A second, independent look at every difference the audit reports.

The audit reader's differences are not detections on their own. Two things about
them matter to the per-book rule (design §9.4): a difference has to survive an
automatic re-read, and — the operator's decision D1(b) — a difference counts as a
detection only after an *adjudication* that is not the first pass looking at
itself.

Everything here follows the operator's rules for that step:

* one call per screen, carrying every disputed place on that screen at once;
* the two readings of a place are given in random order, and the adjudicator is
  never told which of them the first pass produced;
* the adjudicator may answer "unsure", and an unsure answer is not a detection
  for sensitivity (the span is still marked — marking is unconditional);
* nothing here transcribes a page: every reading is a snippet of at most eight
  words, and a longer one is refused rather than scored.

Anchoring bias is what this step exists to measure and bound, not to hide.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import random
    from pathlib import Path

MAX_WORDS = 8
DEFAULT_MODEL = None

ADJUDICATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "choice": {"type": "string", "enum": ["a", "b", "unsure"]},
                },
                "required": ["index", "choice"],
            },
        }
    },
    "required": ["verdicts"],
}

PROMPT = """\
One screen of a book, and a list of disputed places on it.

The page image is the file {image}. Read it.

For each numbered dispute, two readings of the same place are given, in no
particular order: "a" is the first, "b" the second. Say which one the image
shows — "a", "b", or "unsure" when the image does not settle it.

Rules, all of them binding:
- Answer once per dispute, by index.
- Never transcribe the page. Never quote more than 8 consecutive words of it.
- The two readings are both plausible; pick the one the image supports. Do not
  assume either is right, and do not prefer one because of its order.
- If the place is too small, too blurred, or too degraded to decide, answer
  "unsure". That is a real answer, not a failure.
- Return only the JSON object the schema describes. No prose.

Disputes:
{disputes}
"""


class ResponseRejected(ValueError):
    """The response is not a usable adjudication; the caller must not score it."""


@dataclass(frozen=True)
class Dispute:
    """One disputed place, with its two readings in the order shown."""

    index: int
    first: str
    second: str
    auditor_side: str  # "a" or "b": which reading the audit pass produced
    span: tuple[int, int]


@dataclass(frozen=True)
class Verdict:
    index: int
    choice: str

    @property
    def is_detection(self) -> bool:
        return self.choice in {"a", "b"}


@dataclass(frozen=True)
class Adjudication:
    verdicts: tuple[Verdict, ...]
    seconds: float
    cost_usd: float | None = None
    model: str | None = None
    rejected: str | None = None


def words(text: str) -> list[str]:
    return text.split()


def disputes_for(
    places: list[tuple[tuple[int, int], str, str]],
    *,
    rng: random.Random,
) -> list[Dispute]:
    """Build disputes from (span, candidate words, auditor words), order shuffled.

    The shuffle is the point: the adjudicator must not be able to tell the first
    pass's answer from the pipeline's by position.
    """
    disputes: list[Dispute] = []
    for span, candidate, auditor in places:
        if not candidate.strip() or not auditor.strip():
            continue
        if rng.random() < 0.5:
            disputes.append(Dispute(len(disputes), candidate, auditor, "b", span))
        else:
            disputes.append(Dispute(len(disputes), auditor, candidate, "a", span))
    return disputes


def render_disputes(disputes: list[Dispute]) -> str:
    return "\n".join(
        f'{dispute.index}. a: "{dispute.first}"  |  b: "{dispute.second}"' for dispute in disputes
    )


def validate(payload: object, count: int) -> tuple[Verdict, ...]:
    if not isinstance(payload, dict) or "verdicts" not in payload:
        raise ResponseRejected("the answer needs a 'verdicts' array")
    entries = payload["verdicts"]
    if not isinstance(entries, list):
        raise ResponseRejected("'verdicts' must be an array")
    verdicts: dict[int, Verdict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ResponseRejected("a verdict must be an object")
        index, choice = entry.get("index"), entry.get("choice")
        if not isinstance(index, int) or choice not in {"a", "b", "unsure"}:
            raise ResponseRejected("a verdict needs an integer index and a choice of a/b/unsure")
        if not 0 <= index < count:
            raise ResponseRejected(f"verdict index {index} is not a dispute on this screen")
        verdicts[index] = Verdict(index=index, choice=choice)
    # A dispute the adjudicator skipped is not a detection: silence must never
    # become agreement with either side.
    return tuple(verdicts.get(index, Verdict(index=index, choice="unsure")) for index in range(count))


def parse_envelope(stdout: str) -> tuple[object, float | None, str | None]:
    envelope = json.loads(stdout)
    cost = envelope.get("total_cost_usd")
    return envelope.get("result"), float(cost) if isinstance(cost, (int, float)) else None, envelope.get(
        "model"
    )


def _first_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ResponseRejected("no JSON object in the answer")
    return text[start : end + 1]


def adjudicate(
    image_path: Path,
    disputes: list[Dispute],
    *,
    model: str | None = DEFAULT_MODEL,
    timeout: float = 300.0,
    claude: str = "claude",
) -> Adjudication:
    """Ask once, for one screen, which of each pair of readings the image shows."""
    if not disputes:
        return Adjudication(verdicts=(), seconds=0.0)
    prompt = PROMPT.format(image=str(image_path), disputes=render_disputes(disputes))
    command = [
        claude,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(ADJUDICATION_SCHEMA),
        "--allowedTools",
        "Read",
    ]
    if model:
        command += ["--model", model]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return Adjudication(verdicts=(), seconds=time.monotonic() - started, rejected="timeout")
    seconds = time.monotonic() - started
    if completed.returncode != 0:
        return Adjudication(verdicts=(), seconds=seconds, rejected=f"exit {completed.returncode}")
    try:
        payload, cost, used = parse_envelope(completed.stdout)
        if isinstance(payload, str):
            payload = json.loads(_first_json_object(payload))
        verdicts = validate(payload, len(disputes))
    except (ResponseRejected, json.JSONDecodeError) as exc:
        return Adjudication(verdicts=(), seconds=seconds, rejected=str(exc))
    return Adjudication(
        verdicts=verdicts, seconds=seconds, cost_usd=cost, model=used or model
    )


def confirmed_spans(disputes: list[Dispute], verdicts: tuple[Verdict, ...]) -> set[tuple[int, int]]:
    """The spans the adjudicator sided with the audit pass on, and no others."""
    by_index = {verdict.index: verdict for verdict in verdicts}
    return {
        dispute.span
        for dispute in disputes
        if by_index.get(dispute.index, Verdict(dispute.index, "unsure")).choice == dispute.auditor_side
    }
