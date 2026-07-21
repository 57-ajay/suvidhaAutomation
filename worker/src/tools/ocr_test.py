"""Replay the captcha OCR against saved PNGs — no portal needed.

    uv run python src/tools/ocr_test.py /tmp/captcha-debug
    uv run python src/tools/ocr_test.py captcha-debug/some_one.png
"""
from __future__ import annotations

import asyncio
import base64
import glob
import os
import sys

from llm.vertex import ocr_image


async def run(paths: list[str]) -> None:
    for p in paths:
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        text, cost = await ocr_image(b64)
        print(f"{os.path.basename(p):52s} -> {text!r:12s} (${cost:.6f})")


def main() -> None:
    args = sys.argv[1:] or ["."]
    paths: list[str] = []
    for a in args:
        paths += (
            sorted(glob.glob(os.path.join(a, "*.png")))
            if os.path.isdir(a)
            else sorted(glob.glob(a))
        )
    if not paths:
        print("no PNGs found")
        sys.exit(1)
    asyncio.run(run(paths))


if __name__ == "__main__":
    main()
