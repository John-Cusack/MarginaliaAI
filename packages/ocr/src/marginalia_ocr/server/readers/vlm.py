"""The local vision-language reader: Qwen2.5-VL-7B-AWQ, one region at a time.

The model Phase 0 measured as the best single-reader baseline, loaded on the 3090
under the GPU lease and **unloaded after every job**: the card is shared with the
operator's own work, and a model left resident is a model nobody else can use.

Everything heavy is imported lazily. The service's default venv has no torch, and
it must not need one to serve a job that never asks for the VLM - the GPU extra is
installed where the model lives, on the GPU host.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..gpu_lease import InsufficientVram, Lease, LeaseError, free_vram_mib
from .tesseract import RegionReading

#: The Phase 0 model. Changing it is a Phase 0 decision, not an implementation one.
MODEL = "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"
#: What the model needs on a 24 GB card, with room for the image at 3x.
BUDGET_MIB = 9000
#: The prompt is a transcription question and nothing else: the reader's job is to
#: read, and the alignment decides what to believe.
PROMPT = (
    "Transcribe every word of text in this image exactly as it appears, in reading "
    "order. Output only the transcription, no commentary."
)

#: The bug this bounds: `max_new_tokens=1024` on every region let the model run on
#: until it was cut off, and Phase 0's own measure showed it - a word count several
#: times the truth's, 4.35 word error rate on prose. The bound is what the region
#: can *plausibly* contain.
#: ~1 token per 3 characters of English, a generous 160 characters per line of a
#: 3x region, and ~1 character per 900 px^2 for regions whose lines we cannot count.
CHARS_PER_LINE = 160
CHARS_PER_3X_PIXEL = 1 / 900
MIN_NEW_TOKENS = 32
MAX_NEW_TOKENS = 1024
#: A loop is repetition, and repetition is not transcription: the answer is cut at
#: the third occurrence of any 6-gram, which is well past anything a page does.
REPEAT_NGRAM = 6
REPEAT_LIMIT = 3
#: Mild, and block a long n-gram outright: enough to stop a loop without making the
#: model avoid an ordinary repeated word like "the".
REPETITION_PENALTY = 1.15
NO_REPEAT_NGRAM = 12


def expected_tokens(width: int, height: int, *, line_height: int | None = None) -> int:
    """How many tokens a transcription of this region can plausibly need.

    Bounded by the region's own size: a single line gets a line's worth, a column
    gets its lines' worth, and nothing gets the whole budget by default.
    """
    lines = max(1, height // line_height) if line_height else None
    by_lines = lines * CHARS_PER_LINE if lines else 0
    by_area = width * height * CHARS_PER_3X_PIXEL
    estimate = max(by_lines, by_area) / 3  # tokens are ~3 characters of English
    return int(min(MAX_NEW_TOKENS, max(MIN_NEW_TOKENS, estimate)))


def trim_repetition(text: str, *, ngram: int = REPEAT_NGRAM, limit: int = REPEAT_LIMIT) -> str:
    """Cut an answer at the point it starts repeating itself.

    A transcriber that loops is not reading, and everything after the first loop is
    invented: keeping it would put the same words into the alignment many times and
    into the vocabulary once.
    """
    words = text.split()
    if len(words) < ngram * limit:
        return text
    seen: dict[tuple[str, ...], int] = {}
    for index in range(len(words) - ngram + 1):
        gram = tuple(words[index : index + ngram])
        seen[gram] = seen.get(gram, 0) + 1
        if seen[gram] >= limit:
            head = words[: index + ngram]
            return " ".join(head)
    return text


class VlmUnavailable(RuntimeError):
    """No torch, no weights, or no GPU: a missing reader is a gap, not empty text."""


@dataclass
class VlmReader:
    """A lazily loaded, lease-gated VLM. Unload it when the job is done."""

    model_name: str = MODEL
    budget_mib: int = BUDGET_MIB
    lease_path: Path = Path(os.environ.get("MARGINALIA_GPU_LEASE", "/tmp/marginalia-gpu.lease"))
    holder: str = "ocr-reader"
    device: str = "cuda:0"

    def __post_init__(self) -> None:
        self._model = None
        self._processor = None
        self._lease: Lease | None = None
        self.load_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:
            return
        started = time.monotonic()
        try:
            import torch  # noqa: F401
            from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
        except ImportError as exc:  # pragma: no cover - the GPU extra
            raise VlmUnavailable(f"the GPU extra is not installed: {exc}") from exc

        # The lease first, then the memory check it performs, then the load: a
        # holder that cannot prove it has the memory does not proceed.
        self._lease = Lease(
            path=Path(self.lease_path), holder=self.holder, budget_mib=self.budget_mib
        ).acquire()
        try:
            self._model = (
                Qwen2_5_VLForConditionalGeneration.from_pretrained(
                    self.model_name, dtype="auto", device_map={"": self.device}
                )
                .eval()
            )
            self._processor = AutoProcessor.from_pretrained(self.model_name)
        except BaseException:
            self.unload()
            raise
        self.load_seconds = time.monotonic() - started

    def unload(self) -> None:
        """Drop the model and give the lease back, whatever state we are in."""
        self._model = None
        self._processor = None
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        if self._lease is not None:
            self._lease.release()
            self._lease = None

    def free_vram_mib(self) -> int | None:
        return free_vram_mib()

    def read(self, image, box, *, region_id: str) -> RegionReading:
        """Read one region. An empty answer is an empty reading, never agreement."""
        self.load()
        import torch

        x0, y0, x1, y1 = (int(round(value)) for value in box)
        crop = image.crop((x0, y0, x1, y1))
        messages = [
            {
                "role": "user",
                "content": [{"type": "image", "image": crop}, {"type": "text", "text": PROMPT}],
            }
        ]
        started = time.monotonic()
        inputs = self._processor.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt"
        ).to(self.device)
        budget = expected_tokens(crop.width, crop.height)
        with torch.inference_mode():
            output = self._model.generate(
                **inputs,
                max_new_tokens=budget,
                do_sample=False,
                repetition_penalty=REPETITION_PENALTY,
                no_repeat_ngram_size=NO_REPEAT_NGRAM,
            )
        text = trim_repetition(
            self._processor.decode(
                output[0][inputs["input_ids"].shape[-1] :], skip_special_tokens=True
            ).strip()
        )
        return RegionReading(
            region_id=region_id, text=text, words=(), seconds=time.monotonic() - started, empty=not text
        )

    def __enter__(self) -> VlmReader:
        self.load()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.unload()


__all__ = ["MODEL", "BUDGET_MIB", "VlmReader", "VlmUnavailable", "LeaseError", "InsufficientVram"]
