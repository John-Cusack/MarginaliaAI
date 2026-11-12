"""Read page images with a local vision-language model, then exit.

This runs on the machine with the GPU. It is deliberately the dumbest possible
client so that a candidate's *quality* is what gets measured, not its plumbing:

* vLLM's offline API, one process, model loaded, all pages read, process exits —
  nothing is left resident on the card;
* the prompt is a plain transcription request with no chain of thought;
* the output is ``{image name: text}`` and nothing else: no images are copied or
  kept, and no page text is printed.

The pages handed to it are synthetic (generated from the corpus's own truth), so
what leaves this machine is nothing a reader would recognise as a book.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROMPT = (
    "Transcribe all of the text visible in this page image, exactly as it appears, "
    "line by line. Keep the reading order: left column first, then the right column. "
    "Do not summarise, do not translate, do not add commentary. Output only the text."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="Qwen/Qwen2.5-VL-7B-Instruct-AWQ")
    parser.add_argument("--max-model-len", type=int, default=8192)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from vllm import LLM, SamplingParams

    images = sorted(args.images.glob("*.png"))
    if args.limit:
        images = images[: args.limit]
    if not images:
        raise SystemExit(f"no PNGs under {args.images}")
    llm = LLM(
        model=args.model,
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_memory_utilization,
        limit_mm_per_prompt={"image": 1},
    )
    sampling = SamplingParams(temperature=0.0, max_tokens=args.max_tokens)
    # The model's own chat template places the image token; the image itself
    # travels as multimodal data, so nothing is base64-ed into a prompt string.
    tokenizer = llm.get_tokenizer()
    prompt = tokenizer.apply_chat_template(
        [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": PROMPT}]}],
        tokenize=False,
        add_generation_prompt=True,
    )
    requests = [
        {"prompt": prompt, "multi_modal_data": {"image": str(path)}} for path in images
    ]
    outputs = llm.generate(requests, sampling)
    readings = {
        path.name: output.outputs[0].text.strip()
        for path, output in zip(images, outputs, strict=True)
    }
    args.out.write_text(json.dumps(readings, indent=2), encoding="utf-8")
    print(f"read {len(readings)} pages with {args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
