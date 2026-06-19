from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from music21 import converter, key, note, stream
except ImportError:
    converter = None
    key = None
    note = None
    stream = None

from python.transposer import TranspositionError, transpose_to_key, validate_musicxml_path
from python.pipeline import run_pipeline
from python.pdf_conversion import convert_pdf_to_musicxml
from python.pdf_export import export_musicxml_to_pdf


@unittest.skipIf(stream is None, "music21 is not installed")
class TransposerTests(unittest.TestCase):
    def test_transposes_musicxml_to_target_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "simple.musicxml"
            output_path = tmp_path / "simple-in-d.musicxml"

            score = stream.Score()
            part = stream.Part()
            measure = stream.Measure(number=1)
            measure.insert(0, key.Key("C", "major"))
            measure.append(note.Note("C4", quarterLength=1))
            measure.append(note.Note("E4", quarterLength=1))
            measure.append(note.Note("G4", quarterLength=1))
            part.append(measure)
            score.append(part)
            score.write("musicxml", fp=str(input_path))

            transpose_to_key(input_path, output_path, "D major")

            shifted = converter.parse(str(output_path))
            pitches = [n.nameWithOctave for n in list(shifted.recurse().notes)[:3]]
            self.assertEqual(pitches, ["D4", "F#4", "A4"])
            self.assertEqual(shifted.analyze("key").tonic.name, "D")


class ValidationTests(unittest.TestCase):
    def test_rejects_non_musicxml_extension(self):
        with self.assertRaises(TranspositionError):
            validate_musicxml_path("song.pdf", must_exist=False)

    def test_accepts_musicxml_extensions(self):
        self.assertEqual(validate_musicxml_path("song.musicxml", must_exist=False).suffix, ".musicxml")
        self.assertEqual(validate_musicxml_path("song.xml", must_exist=False).suffix, ".xml")


class PipelineTests(unittest.TestCase):
    def test_musicxml_input_routes_to_transposer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song-in-d.musicxml"
            input_path.write_text("<score-partwise />", encoding="utf-8")

            with patch("python.pipeline.transpose_to_key", return_value=output_path) as transpose:
                result = run_pipeline(input_path, output_path, "D major", "musicxml")

            self.assertEqual(result, output_path)
            transpose.assert_called_once_with(input_path, output_path, "D major")

    def test_musicxml_input_still_works_without_pdf_tools_installed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.xml"
            output_path = tmp_path / "song-in-g.musicxml"
            input_path.write_text("<score-partwise />", encoding="utf-8")

            with patch("python.pipeline.transpose_to_key", return_value=output_path) as transpose:
                result = run_pipeline(
                    input_path,
                    output_path,
                    "G major",
                    "musicxml",
                    audiveris_path="",
                    musescore_path="",
                )

            self.assertEqual(result, output_path)
            transpose.assert_called_once_with(input_path, output_path, "G major")

    def test_pdf_input_routes_to_conversion_pre_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "scan.pdf"
            output_path = tmp_path / "scan.musicxml"
            audiveris_path = tmp_path / "audiveris.exe"
            converted_path = tmp_path / "converted.musicxml"
            input_path.write_bytes(b"%PDF-1.7")
            audiveris_path.write_text("", encoding="utf-8")
            converted_path.write_text("<score-partwise />", encoding="utf-8")

            with patch("python.pipeline.convert_pdf_to_musicxml", return_value=converted_path) as convert:
                with patch("python.pipeline.transpose_to_key", return_value=output_path) as transpose:
                    result = run_pipeline(input_path, output_path, "D major", "musicxml", audiveris_path=audiveris_path)

            self.assertEqual(result, output_path)
            convert.assert_called_once()
            transpose.assert_called_once_with(converted_path, output_path, "D major")

    def test_pdf_output_routes_to_export_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song.pdf"
            musescore_path = tmp_path / "MuseScore.exe"
            input_path.write_text("<score-partwise />", encoding="utf-8")
            musescore_path.write_text("", encoding="utf-8")

            with patch("python.pipeline.transpose_to_key") as transpose:
                with patch("python.pipeline.export_musicxml_to_pdf", return_value=output_path) as export:
                    result = run_pipeline(input_path, output_path, "D major", "pdf", musescore_path=musescore_path)

            self.assertEqual(result, output_path)
            transpose.assert_called_once()
            export.assert_called_once()


class PdfToolTests(unittest.TestCase):
    def test_missing_audiveris_path_has_clear_error(self):
        with self.assertRaisesRegex(TranspositionError, "PDF import requires the Audiveris OMR engine"):
            convert_pdf_to_musicxml("scan.pdf", tempfile.gettempdir(), "")

    def test_missing_musescore_path_has_clear_error(self):
        with self.assertRaisesRegex(TranspositionError, "PDF export requires MuseScore"):
            export_musicxml_to_pdf("score.musicxml", "score.pdf", "")

    def test_audiveris_command_is_called_and_musicxml_is_returned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            audiveris_path = tmp_path / "audiveris.exe"
            input_path = tmp_path / "scan.pdf"
            output_dir = tmp_path / "work" / "audiveris-output"
            converted_path = output_dir / "scan.musicxml"
            audiveris_path.write_text("", encoding="utf-8")
            input_path.write_bytes(b"%PDF-1.7")

            def fake_run(command, capture_output, text, check):
                output_dir.mkdir(parents=True, exist_ok=True)
                converted_path.write_text("<score-partwise />", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("python.pdf_conversion.subprocess.run", side_effect=fake_run) as run:
                result = convert_pdf_to_musicxml(input_path, tmp_path / "work", audiveris_path)

            self.assertEqual(result, converted_path)
            run.assert_called_once()
            self.assertIn(str(audiveris_path), run.call_args.args[0])
            self.assertIn(str(input_path), run.call_args.args[0])

    def test_audiveris_conversion_does_not_modify_original_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            audiveris_path = tmp_path / "audiveris.exe"
            input_path = tmp_path / "scan.pdf"
            work_path = tmp_path / "work"
            output_dir = work_path / "audiveris-output"
            converted_path = output_dir / "scan.musicxml"
            original_bytes = b"%PDF-1.7 original content"
            audiveris_path.write_text("", encoding="utf-8")
            input_path.write_bytes(original_bytes)

            def fake_run(command, capture_output, text, check):
                output_dir.mkdir(parents=True, exist_ok=True)
                converted_path.write_text("<score-partwise />", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("python.pdf_conversion.subprocess.run", side_effect=fake_run):
                convert_pdf_to_musicxml(input_path, work_path, audiveris_path)

            self.assertEqual(input_path.read_bytes(), original_bytes)

    def test_musescore_command_is_called_and_pdf_is_returned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            musescore_path = tmp_path / "MuseScore.exe"
            input_path = tmp_path / "score.musicxml"
            output_path = tmp_path / "score.pdf"
            musescore_path.write_text("", encoding="utf-8")
            input_path.write_text("<score-partwise />", encoding="utf-8")

            def fake_run(command, capture_output, text, check):
                output_path.write_bytes(b"%PDF-1.7")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("python.pdf_export.subprocess.run", side_effect=fake_run) as run:
                result = export_musicxml_to_pdf(input_path, output_path, musescore_path)

            self.assertEqual(result, output_path)
            run.assert_called_once()
            self.assertIn(str(musescore_path), run.call_args.args[0])
            self.assertIn(str(output_path), run.call_args.args[0])


class CliErrorTests(unittest.TestCase):
    def test_missing_audiveris_cli_error_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "scan.pdf"
            output_path = tmp_path / "scan.musicxml"
            input_path.write_bytes(b"%PDF-1.7")

            result = subprocess.run(
                [
                    sys.executable,
                    "python/transposer.py",
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--target-key",
                    "D major",
                    "--output-format",
                    "musicxml",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PDF import requires the Audiveris OMR engine. Please configure it in Settings.", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
