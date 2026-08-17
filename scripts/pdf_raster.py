#!/usr/bin/env python3
"""Bound PDF rasterization before images are materialized in memory."""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from dataclasses import dataclass


DEFAULT_MAX_PAGE_PIXELS = 25_000_000
MAX_PAGE_PIXELS_ENV = "OCR_MAX_PAGE_PIXELS"


@dataclass(frozen=True)
class RasterPlan:
    width_points: float
    height_points: float
    requested_dpi: float
    effective_dpi: float
    requested_width: int
    requested_height: int
    output_width: int
    output_height: int
    max_pixels: int

    @property
    def reduced(self) -> bool:
        return self.effective_dpi < self.requested_dpi


def parse_max_page_pixels(value: str | None) -> int:
    """Parse the environment value, using the default for missing/empty input."""
    if value is None or not value.strip():
        return DEFAULT_MAX_PAGE_PIXELS
    try:
        max_pixels = int(value)
    except ValueError as exc:
        raise ValueError(
            f"{MAX_PAGE_PIXELS_ENV} must be a positive integer; got {value!r}"
        ) from exc
    if max_pixels <= 0:
        raise ValueError(
            f"{MAX_PAGE_PIXELS_ENV} must be a positive integer; got {value!r}"
        )
    return max_pixels


def configured_max_page_pixels() -> int:
    return parse_max_page_pixels(os.environ.get(MAX_PAGE_PIXELS_ENV))


def _positive_finite(value: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"PDF page {label} must be finite and positive; got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"PDF page {label} must be finite and positive; got {value!r}")
    return number


def _raster_dimensions(width_points: float, height_points: float, dpi: float) -> tuple[int, int]:
    return (
        math.ceil(width_points * dpi / 72),
        math.ceil(height_points * dpi / 72),
    )


def plan_rasterization(
    width_points: float,
    height_points: float,
    requested_dpi: float,
    max_pixels: int,
) -> RasterPlan:
    """Return a per-page DPI whose ceiling-rounded dimensions fit the limit."""
    width_points = _positive_finite(width_points, "width")
    height_points = _positive_finite(height_points, "height")
    requested_dpi = _positive_finite(requested_dpi, "DPI")
    if isinstance(max_pixels, bool) or not isinstance(max_pixels, int) or max_pixels <= 0:
        raise ValueError(f"max_pixels must be a positive integer; got {max_pixels!r}")

    requested_width, requested_height = _raster_dimensions(
        width_points, height_points, requested_dpi
    )
    requested_pixels = requested_width * requested_height
    if requested_pixels <= max_pixels:
        return RasterPlan(
            width_points=width_points,
            height_points=height_points,
            requested_dpi=requested_dpi,
            effective_dpi=requested_dpi,
            requested_width=requested_width,
            requested_height=requested_height,
            output_width=requested_width,
            output_height=requested_height,
            max_pixels=max_pixels,
        )

    effective_dpi = requested_dpi * math.sqrt(max_pixels / requested_pixels)
    output_width, output_height = _raster_dimensions(
        width_points, height_points, effective_dpi
    )

    # Ceiling each axis can push the initial square-root estimate just over the
    # limit. Binary-search downward only in that case; the lower bound is always
    # safe because positive geometry tends to a 1 x 1 raster as DPI tends to 0.
    if output_width * output_height > max_pixels:
        safe_dpi = 0.0
        unsafe_dpi = effective_dpi
        for _ in range(80):
            candidate = (safe_dpi + unsafe_dpi) / 2
            candidate_width, candidate_height = _raster_dimensions(
                width_points, height_points, candidate
            )
            if candidate_width * candidate_height <= max_pixels:
                safe_dpi = candidate
            else:
                unsafe_dpi = candidate
        effective_dpi = safe_dpi
        output_width, output_height = _raster_dimensions(
            width_points, height_points, effective_dpi
        )

    if effective_dpi <= 0 or output_width * output_height > max_pixels:
        raise ValueError(
            f"Could not fit PDF page within max_pixels={max_pixels}"
        )

    return RasterPlan(
        width_points=width_points,
        height_points=height_points,
        requested_dpi=requested_dpi,
        effective_dpi=effective_dpi,
        requested_width=requested_width,
        requested_height=requested_height,
        output_width=output_width,
        output_height=output_height,
        max_pixels=max_pixels,
    )


def pdf_to_images(
    pdf_path: str,
    dpi: int = 300,
    max_page_pixels: int | None = None,
) -> list[str]:
    """Rasterize each PDF page independently without exceeding the pixel cap."""
    import fitz

    max_pixels = configured_max_page_pixels() if max_page_pixels is None else max_page_pixels
    if isinstance(max_pixels, bool) or not isinstance(max_pixels, int) or max_pixels <= 0:
        raise ValueError(f"max_page_pixels must be a positive integer; got {max_pixels!r}")

    doc = fitz.open(pdf_path)
    tmp_dir: str | None = None
    image_paths: list[str] = []
    try:
        if not doc.is_pdf:
            raise ValueError(f"Input is not a PDF: {pdf_path}")
        if doc.page_count == 0:
            raise ValueError(f"PDF contains no pages: {pdf_path}")

        tmp_dir = tempfile.mkdtemp(prefix="pdf_ocr_")
        for page_index, page in enumerate(doc):
            plan = plan_rasterization(
                page.rect.width,
                page.rect.height,
                dpi,
                max_pixels,
            )
            if plan.reduced:
                print(
                    "PDF raster limit: "
                    f"page={page_index + 1} "
                    f"requested={plan.requested_width}x{plan.requested_height}"
                    f"@{plan.requested_dpi:g}dpi "
                    f"effective={plan.output_width}x{plan.output_height}"
                    f"@{plan.effective_dpi:.6g}dpi "
                    f"max_pixels={plan.max_pixels}",
                    flush=True,
                )

            matrix = fitz.Matrix(plan.effective_dpi / 72, plan.effective_dpi / 72)
            pixmap = page.get_pixmap(matrix=matrix)
            actual_pixels = pixmap.width * pixmap.height
            if actual_pixels > max_pixels:
                raise RuntimeError(
                    "PDF raster dimensions exceeded the configured limit: "
                    f"page={page_index + 1} actual={pixmap.width}x{pixmap.height} "
                    f"max_pixels={max_pixels}"
                )
            output_path = os.path.join(tmp_dir, f"page_{page_index + 1:04d}.png")
            pixmap.save(output_path)
            image_paths.append(output_path)
    except Exception:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        doc.close()
    return image_paths
