"""Ask Claude about a table, cell by cell, and take only per-cell differences.

The page's canonical text for a table is Markdown and no reader writes Markdown,
so a difference against it is meaningless. This is the table half of the audit
sample: the image, plus the candidate grid as plain labelled cell text, and an
answer that names the cell each difference is in.

Same boundary as the page checker: differences only, snippets of at most eight
words, no transcription, and a refusal rather than a guess when the answer does
not fit the schema.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

MAX_WORDS = 8
DEFAULT_MODEL = None

CELL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "differences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row": {"type": "integer"},
                    "col": {"type": "integer"},
                    "snippet": {"type": "string"},
                    "reading": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["row", "col", "snippet", "reading"],
            },
        }
    },
    "required": ["differences"],
}

PROMPT = """\
You are auditing one table in a book, against the text an OCR pipeline intends to \
store for it.

The page image is the file {image}. Read it. The text below is that table as the \
pipeline stores it: one line per cell, labelled with its row and column, which \
are 1-based.

Report ONLY the differences, one per affected cell: places where the image shows \
something the stored text does not.

Rules, all of them binding:
- Give `row` and `col` (the cell's), `snippet` (the stored text's own words at \
that place, up to three words either side) and `reading` (what the image shows \
there). Snippet and reading are at most 8 words each. Never longer.
- Report a cell once, however wrong it is.
- Do not transcribe the table. Do not quote more than 8 consecutive words of it \
anywhere in your answer.
- If the image and the stored text agree, return an empty list.
- Return only the JSON object the schema describes. No prose.

Answer with JSON: {{"differences": [{{"row": 1, "col": 2, "snippet": "...", \
"reading": "...", "confidence": 0.0}}]}}
"""


class ResponseRejected(ValueError):
    """The response is not a usable per-cell answer; callers must not score it."""


@dataclass(frozen=True)
class CellDifference:
    row: int
    col: int
    snippet: str
    reading: str
    confidence: float | None = None


@dataclass(frozen=True)
class CellCheckResult:
    differences: tuple[CellDifference, ...] = ()
    seconds: float = 0.0
    cost_usd: float | None = None
    model: str | None = None
    raw_word_count: int = 0
    rejected: str | None = None


def words(text: str) -> list[str]:
    return text.split()


def validate(payload: object) -> tuple[CellDifference, ...]:
    if not isinstance(payload, dict) or "differences" not in payload:
        raise ResponseRejected("the answer needs a 'differences' array")
    entries = payload["differences"]
    if not isinstance(entries, list):
        raise ResponseRejected("'differences' must be an array")
    differences: list[CellDifference] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ResponseRejected("a difference must be an object")
        row, col = entry.get("row"), entry.get("col")
        snippet, reading = entry.get("snippet"), entry.get("reading")
        if not isinstance(row, int) or not isinstance(col, int) or row < 1 or col < 1:
            raise ResponseRejected("a difference needs 1-based integer row and col")
        if not isinstance(snippet, str) or not isinstance(reading, str):
            raise ResponseRejected("a difference needs string 'snippet' and 'reading'")
        for label, value in (("snippet", snippet), ("reading", reading)):
            if len(words(value)) > MAX_WORDS:
                raise ResponseRejected(f"{label} runs past {MAX_WORDS} words")
        confidence = entry.get("confidence")
        differences.append(
            CellDifference(
                row=row,
                col=col,
                snippet=snippet,
                reading=reading,
                confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            )
        )
    return tuple(differences)


def parse_envelope(stdout: str) -> tuple[object, float | None, str | None, int]:
    envelope = json.loads(stdout)
    cost = envelope.get("total_cost_usd")
    result = envelope.get("result")
    payload = result
    if isinstance(result, str):
        start, end = result.find("{"), result.rfind("}")
        if start < 0 or end <= start:
            raise ResponseRejected("no JSON object in the answer")
        payload = json.loads(result[start : end + 1])
    volume = sum(
        len(words(str(entry.get("snippet", "")))) + len(words(str(entry.get("reading", ""))))
        for entry in (payload.get("differences", []) if isinstance(payload, dict) else [])
        if isinstance(entry, dict)
    )
    return payload, float(cost) if isinstance(cost, (int, float)) else None, envelope.get("model"), volume


def check_cells(
    image_path: Path,
    grid_text: str,
    *,
    model: str | None = DEFAULT_MODEL,
    timeout: float = 300.0,
    claude: str = "claude",
) -> CellCheckResult:
    """Ask Claude which cells of this table disagree with the image."""
    command = [
        claude,
        "-p",
        PROMPT.format(image=str(image_path)),
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(CELL_SCHEMA),
        "--allowedTools",
        "Read",
    ]
    if model:
        command += ["--model", model]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, input=grid_text, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return CellCheckResult(seconds=time.monotonic() - started, rejected="timeout")
    seconds = time.monotonic() - started
    if completed.returncode != 0:
        return CellCheckResult(seconds=seconds, rejected=f"exit {completed.returncode}")
    try:
        payload, cost, used, volume = parse_envelope(completed.stdout)
        differences = validate(payload)
    except (ResponseRejected, json.JSONDecodeError) as exc:
        return CellCheckResult(seconds=seconds, rejected=str(exc))
    return CellCheckResult(
        differences=differences, seconds=seconds, cost_usd=cost, model=used or model, raw_word_count=volume
    )
