import contextlib
import io
import math
import tempfile
import unittest
from pathlib import Path

import fitz

from scripts.pdf_raster import (
    DEFAULT_MAX_PAGE_PIXELS,
    parse_max_page_pixels,
    pdf_to_images,
    plan_rasterization,
)


class RasterPlanTests(unittest.TestCase):
    def test_incident_page_fits_default_limit(self):
        plan = plan_rasterization(5669.29, 6803.15, 300, DEFAULT_MAX_PAGE_PIXELS)

        self.assertLess(plan.effective_dpi, 300)
        self.assertEqual((plan.requested_width, plan.requested_height), (23_623, 28_347))
        self.assertLessEqual(plan.output_width * plan.output_height, DEFAULT_MAX_PAGE_PIXELS)

    def test_common_a4_page_keeps_requested_dpi_and_dimensions(self):
        plan = plan_rasterization(595, 842, 300, DEFAULT_MAX_PAGE_PIXELS)

        self.assertEqual(plan.effective_dpi, 300)
        self.assertEqual(plan.output_width, math.ceil(595 * 300 / 72))
        self.assertEqual(plan.output_height, math.ceil(842 * 300 / 72))

    def test_ceiling_rounding_is_adjusted_below_limit(self):
        plan = plan_rasterization(101, 103, 300, 10_000)

        self.assertLessEqual(plan.output_width * plan.output_height, 10_000)

    def test_invalid_geometry_is_rejected(self):
        for value in (0, -1, math.inf, math.nan):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite and positive"):
                plan_rasterization(value, 72, 300, 100)


class ConfigurationTests(unittest.TestCase):
    def test_missing_and_empty_values_use_default(self):
        self.assertEqual(parse_max_page_pixels(None), DEFAULT_MAX_PAGE_PIXELS)
        self.assertEqual(parse_max_page_pixels("  "), DEFAULT_MAX_PAGE_PIXELS)

    def test_positive_custom_value_is_accepted(self):
        self.assertEqual(parse_max_page_pixels("12345"), 12_345)

    def test_invalid_values_fail_clearly(self):
        for value in ("0", "-1", "1.5", "abc"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "OCR_MAX_PAGE_PIXELS must be a positive integer"
            ):
                parse_max_page_pixels(value)


class PdfRasterTests(unittest.TestCase):
    @staticmethod
    def _empty_pdf_bytes() -> bytes:
        header = b"%PDF-1.4\n"
        objects = [
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
            b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n",
        ]
        offsets = []
        body = bytearray(header)
        for pdf_object in objects:
            offsets.append(len(body))
            body.extend(pdf_object)
        xref_offset = len(body)
        body.extend(b"xref\n0 3\n0000000000 65535 f \n")
        for offset in offsets:
            body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        body.extend(
            b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n"
            + str(xref_offset).encode("ascii")
            + b"\n%%EOF\n"
        )
        return bytes(body)

    def test_mixed_pdf_is_planned_per_page_and_large_page_is_reduced(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "mixed.pdf"
            document = fitz.open()
            document.new_page(width=20, height=20)
            document.new_page(width=72, height=72)
            document.save(pdf_path)
            document.close()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                image_paths = pdf_to_images(str(pdf_path), dpi=300, max_page_pixels=10_000)

            self.assertEqual(len(image_paths), 2)
            small = fitz.Pixmap(image_paths[0])
            large = fitz.Pixmap(image_paths[1])
            self.assertEqual((small.width, small.height), (84, 84))
            self.assertLessEqual(large.width * large.height, 10_000)
            self.assertIn("page=2", stdout.getvalue())
            self.assertNotIn("base64", stdout.getvalue().lower())

    def test_invalid_pdf_fails_without_creating_images(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "invalid.pdf"
            pdf_path.write_bytes(b"not a pdf")
            with self.assertRaises(Exception):
                pdf_to_images(str(pdf_path), max_page_pixels=100)

    def test_non_pdf_document_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "image.png"
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 1, 1), False)
            pixmap.save(image_path)

            with self.assertRaisesRegex(ValueError, "Input is not a PDF"):
                pdf_to_images(str(image_path), max_page_pixels=100)

    def test_zero_page_pdf_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "empty.pdf"
            pdf_path.write_bytes(self._empty_pdf_bytes())

            with self.assertRaisesRegex(ValueError, "PDF contains no pages"):
                pdf_to_images(str(pdf_path), max_page_pixels=100)


if __name__ == "__main__":
    unittest.main()
