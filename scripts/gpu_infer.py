#!/usr/bin/env python3
"""Run the pinned Baidu GPU entrypoint with bounded PDF rasterization."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from pdf_raster import configured_max_page_pixels, pdf_to_images


UPSTREAM_INFER = Path("/opt/Unlimited-OCR/infer.py")


def load_upstream_infer():
    spec = importlib.util.spec_from_file_location("unlimited_ocr_upstream_infer", UPSTREAM_INFER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load pinned upstream runner: {UPSTREAM_INFER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    max_page_pixels = configured_max_page_pixels()
    upstream = load_upstream_infer()

    def bounded_pdf_to_images(pdf_path: str, dpi: int = 300) -> list[str]:
        return pdf_to_images(pdf_path, dpi=dpi, max_page_pixels=max_page_pixels)

    upstream.pdf_to_images = bounded_pdf_to_images
    upstream.main()


if __name__ == "__main__":
    main()
