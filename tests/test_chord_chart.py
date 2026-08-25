from pathlib import Path
import tempfile
import unittest

import pdfplumber
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from python.chord_chart import (
    _transpose_wrapped_chord,
    inspect_chord_chart_pdf,
    transpose_chord_chart_pdf,
)
from python.pipeline import run_pipeline


def create_chord_chart(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.setTitle("Example Chord Chart")
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(72, 735, "Example Song")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 710, "Key: G")
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(72, 670, "G        D        Em       C")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 652, "Grace has carried me this far")
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(72, 615, "G/B      C        D        G")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 597, "Love will lead me safely home")
    pdf.save()


def create_staff_score(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for staff_start in (650, 500):
        for offset in range(5):
            y = staff_start + offset * 9
            pdf.line(72, y, 540, y)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(90, 710, "G       D")
    pdf.save()


class ChordChartTests(unittest.TestCase):
    def test_wrapped_chord_symbols_preserve_barlines(self):
        self.assertEqual(_transpose_wrapped_chord("|:G/B|", 2, False), "|:A/C#|")

    def test_text_pdf_is_detected_and_transposed_without_external_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "chart.pdf"
            output = root / "chart-in-d.pdf"
            create_chord_chart(source)

            inspection = inspect_chord_chart_pdf(source)
            self.assertTrue(inspection.is_chord_chart)
            self.assertEqual(inspection.original_key, "G major")
            self.assertEqual(inspection.key_source, "printed key label")
            self.assertGreaterEqual(inspection.chord_count, 9)

            report = transpose_chord_chart_pdf(source, output, "D major", inspection=inspection)
            self.assertEqual(report["engine"], "new-key-scores-chord-chart")
            self.assertEqual(report["source_key"], "G major")
            self.assertEqual(report["target_key"], "D major")
            self.assertGreaterEqual(report["chords_transposed"], 9)
            self.assertTrue(output.is_file())

            with pdfplumber.open(output) as shifted:
                text = "\n".join(page.extract_text() or "" for page in shifted.pages)
            self.assertIn("Grace has carried me this far", text)
            self.assertIn("D", text)
            self.assertIn("A", text)
            self.assertIn("Bm", text)

    def test_staff_lines_keep_score_pdf_out_of_chord_chart_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "score.pdf"
            create_staff_score(source)

            inspection = inspect_chord_chart_pdf(source)

            self.assertFalse(inspection.is_chord_chart)
            self.assertEqual(inspection.kind, "score-pdf")
            self.assertTrue(inspection.staff_notation_detected)

    def test_pipeline_writes_chord_chart_pdf_and_reports_own_engines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "chart.pdf"
            output = root / "chart-in-bb.pdf"
            stages = []
            create_chord_chart(source)

            result = run_pipeline(
                source,
                output,
                "Bb major",
                "pdf",
                progress=lambda name, detail="": stages.append((name, detail)),
            )

            self.assertEqual(result, output)
            self.assertTrue(output.is_file())
            engine_detail = next(detail for name, detail in stages if name == "Engine report")
            self.assertIn("New Key Scores chord-chart PDF reader", engine_detail)
            self.assertIn("New Key Scores chord-chart PDF writer", engine_detail)
            self.assertNotIn("Audiveris", engine_detail)
            self.assertNotIn("MuseScore", engine_detail)


if __name__ == "__main__":
    unittest.main()
