"""Sustained throughput for the reading lanes, measured where they run.

The 9 s/page number the gate mentions is about keeping up with capture, not about
one screen's latency: capture spends 9-10 s on each page turn regardless, so what
matters is screens per second *sustained*, with the lanes running in parallel on
the host that owns them (Tesseract on the 5950X's cores, EasyOCR and the VLM on
the 3090).

This tool measures that on whichever host it is run on. It takes a directory of
page PNGs (and, for the rect mode, the layout line rectangles that accompany
them, as the render tar supplies them) and reports wall time, screens/second and
screens/minute per mode:

    python tools/measure_throughput.py --images DIR [--rects rects.json] \
        --mode tesseract --workers 8

Modes: ``tesseract`` (parallel across workers), ``easyocr`` (its own detector),
``easyocr-rects`` (its recognizer on the supplied layout lines, skipping
detection), ``vlm`` (vLLM on the GPU). Nothing here writes book text or images;
the pages are synthetic, and only timings and word counts are printed.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path


def tesseract_one(path: str) -> tuple[str, float]:
    """Read one page the way the measurement tool does: psm 6, stdin/stdout."""
    import io
    import subprocess

    from PIL import Image

    buffer = io.BytesIO()
    with Image.open(path) as image:
        image.convert("RGB").save(buffer, format="PNG")
    started = time.perf_counter()
    completed = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng"],
        input=buffer.getvalue(),
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8", errors="replace").strip(), time.perf_counter() - started


def _easyocr_reader(gpu: bool):
    import easyocr

    return easyocr.Reader(["en"], gpu=gpu, verbose=False)


def easyocr_one(args: tuple[str, list | None, str, bool]) -> tuple[str, float]:
    path, rects, mode, gpu = args
    import numpy as np
    from PIL import Image

    reader = _easyocr_reader(gpu)
    with Image.open(path) as image:
        array = np.array(image.convert("RGB"))
    started = time.perf_counter()
    if mode == "easyocr-rects" and rects:
        # EasyOCR slices the crop with these numbers, so they must be integers.
        rects = [[int(value) for value in box] for box in rects]
        # EasyOCR's recognizer on boxes we already have: its detector does not
        # run at all. horizontal_list is [[x_min, x_max, y_min, y_max]].
        # horizontal_list: the layout lines, each [x_min, x_max, y_min, y_max].
        result = reader.recognize(array, horizontal_list=rects, free_list=[])
        text = "\n".join(read for _box, read, _conf in result)
    else:
        result = reader.readtext(array, paragraph=True)
        text = "\n".join(read for _box, read in result)
    return text, time.perf_counter() - started


def run_serial(function, items: list) -> tuple[list[str], float]:
    started = time.perf_counter()
    results = [function(item) for item in items]
    return [text for text, _seconds in results], time.perf_counter() - started


def run_parallel(function, items: list, workers: int) -> tuple[list[str], float]:
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(function, items))
    return [text for text, _seconds in results], time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--rects", type=Path, default=None, help="layout line rectangles by image stem")
    parser.add_argument("--mode", default="tesseract", choices=("tesseract", "easyocr", "easyocr-rects"))
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-gpu", action="store_true", help="EasyOCR on the CPU instead")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)

    images = sorted(args.images.glob("*.png"))
    if args.limit:
        images = images[: args.limit]
    if not images:
        raise SystemExit(f"no PNGs under {args.images}")

    rects = json.loads(args.rects.read_text()) if args.rects else {}

    if args.mode == "tesseract":
        items = [str(path) for path in images]
        texts, seconds = (
            run_parallel(tesseract_one, items, args.workers)
            if args.workers > 1
            else run_serial(tesseract_one, items)
        )
    else:
        items = [(str(path), rects.get(path.stem), args.mode, not args.no_gpu) for path in images]
        texts, seconds = run_serial(easyocr_one, items)

    words = sum(len(text.split()) for text in texts)
    report = {
        "label": args.label or args.mode,
        "mode": args.mode,
        "host": __import__("socket").gethostname(),
        "images": len(images),
        "workers": args.workers,
        "seconds": round(seconds, 3),
        "seconds_per_screen": round(seconds / len(images), 3),
        "screens_per_minute": round(60 * len(images) / seconds, 2),
        "words_read": words,
        "gpu": (not args.no_gpu) if args.mode.startswith("easyocr") else None,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
