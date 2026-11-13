"""A local model as the audit reader.

The audit reader D1 asks about is a local VLM, not Claude: it reads the screen
and reports where its reading differs. With ``--external`` the harness has that
model's readings in hand, so the pipeline and clean audits must measure *those*
and must not call any external model.

This is the measurement path the D1 numbers come from, so it is worth a test:
if the harness quietly fell back to Claude, a local model's false-alarm rate
would be reported as Claude's and D1 would be decided on the wrong numbers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import measure_phase0  # noqa: E402
from ocr_synth.corpus import build_corpus  # noqa: E402


def _readings(tmp_path: Path, *, wrong_words: int) -> tuple[Path, str]:
    screens = build_corpus(dprs=(1,), kinds=("chapter_opener",))
    screen = screens[0]
    words = screen.truth_text.split(" ")  # split/join on " " is the identity
    for index in range(wrong_words):
        words[index] = "zzq" + words[index]
    path = tmp_path / "readings.json"
    path.write_text(json.dumps({screen.screen_id: " ".join(words)}))
    return path, screen.truth_text


def test_the_clean_audit_measures_the_local_reading_not_claude(tmp_path):
    """Every difference from the exact truth is a false alarm, and it is counted.

    A local reader that gets one word wrong on a clean page must report exactly
    one false alarm, and the report must name the local transcriber.
    """
    readings, truth = _readings(tmp_path, wrong_words=1)
    out = tmp_path / "clean.json"
    measure_phase0.main(
        [
            "--dpr", "1",
            "--kinds", "chapter_opener",
            "--reader", "florence",
            "--external", str(readings),
            "--audit", "clean",
            "--json", str(out),
        ]
    )
    sensitivity = json.loads(out.read_text())["sensitivity"]
    assert sensitivity["false_alarms"] == 1, truth[:40]
    assert "local transcriber" in sensitivity["reader"]
    assert "claude" not in sensitivity["reader"]


def test_the_clean_audit_counts_nothing_for_a_reading_of_the_truth(tmp_path):
    readings, _ = _readings(tmp_path, wrong_words=0)
    out = tmp_path / "clean.json"
    measure_phase0.main(
        [
            "--dpr", "1",
            "--kinds", "chapter_opener",
            "--reader", "florence",
            "--external", str(readings),
            "--audit", "clean",
            "--json", str(out),
        ]
    )
    sensitivity = json.loads(out.read_text())["sensitivity"]
    assert sensitivity["false_alarms"] == 0
    assert sensitivity["screens_with_a_false_alarm"] == 0
