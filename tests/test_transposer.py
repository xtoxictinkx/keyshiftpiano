from pathlib import Path
import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

try:
    from music21 import converter, key, note, stream
except ImportError:
    converter = None
    key = None
    note = None
    stream = None

from python.transposer import TranspositionError, detect_key_name, get_last_transposition_report, transpose_to_key, validate_musicxml_path
from python.transposer import _transpose_musicxml_directly
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


class DirectMusicXmlFallbackTests(unittest.TestCase):
    SIMPLE_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
      </attributes>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>1</duration>
        <type>quarter</type>
      </note>
      <note>
        <pitch><step>E</step><octave>4</octave></pitch>
        <duration>1</duration>
        <type>quarter</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""

    LEAD_SHEET_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
  </part-list>
  <credit>
    <credit-words>(SATB) Key: G</credit-words>
  </credit>
  <identification>
    <creator type="composer">Composer Key:G</creator>
  </identification>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>1</fifths></key>
      </attributes>
      <direction>
        <direction-type><words>Am</words></direction-type>
      </direction>
      <harmony>
        <root><root-step>G</root-step></root>
        <kind text="">major</kind>
      </harmony>
      <harmony>
        <root><root-step>D</root-step></root>
        <kind text="">major</kind>
        <bass><bass-step>F</bass-step><bass-alter>1</bass-alter></bass>
      </harmony>
      <note>
        <pitch><step>G</step><octave>4</octave></pitch>
        <duration>1</duration>
        <type>quarter</type>
        <lyric><text>G</text></lyric>
      </note>
    </measure>
  </part>
</score-partwise>
"""

    MEASURE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    {measures}
  </part>
</score-partwise>
"""

    COMPLETE_MEASURE = """
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
"""

    INCOMPLETE_SECOND_MEASURES = """
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
    <measure number="2">
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>2</duration><type>half</type></note>
    </measure>
"""

    PICKUP_MEASURES = """
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>
    </measure>
    <measure number="2">
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
"""

    def test_direct_musicxml_fallback_transposes_pitches_and_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "simple.musicxml"
            output_path = tmp_path / "simple-d.musicxml"
            input_path.write_text(self.SIMPLE_MUSICXML, encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey())

            output_text = output_path.read_text(encoding="utf-8")
            self.assertIn("<fifths>2</fifths>", output_text)
            self.assertIn("<step>D</step>", output_text)
            self.assertIn("<step>F</step>", output_text)
            self.assertIn("<alter>1</alter>", output_text)

    def test_direct_musicxml_fallback_transposes_harmony_and_visible_key_labels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "lead-sheet.musicxml"
            output_path = tmp_path / "lead-sheet-c.musicxml"
            input_path.write_text(self.LEAD_SHEET_MUSICXML, encoding="utf-8")

            class Tonic:
                name = "C"

            class TargetKey:
                sharps = 0
                tonic = Tonic()
                mode = "major"

            _transpose_musicxml_directly(input_path, output_path, 5, TargetKey(), source_key_name="G major")

            output_text = output_path.read_text(encoding="utf-8")
            self.assertIn("<root-step>C</root-step>", output_text)
            self.assertIn("<root-step>G</root-step>", output_text)
            self.assertIn("<bass-step>B</bass-step>", output_text)
            self.assertNotIn("<bass-alter>", output_text)
            self.assertIn("<credit-words>(SATB) Key: C</credit-words>", output_text)
            self.assertIn("<creator type=\"composer\">Composer Key:C</creator>", output_text)
            self.assertIn("<words>Dm</words>", output_text)
            self.assertIn("<lyric><text>G</text></lyric>", output_text)
            report = get_last_transposition_report()
            self.assertTrue(report["output_validation"]["xml_valid"])
            self.assertEqual(report["output_validation"]["harmony_elements_checked"], 2)
            self.assertEqual(report["output_validation"]["metadata_updated"], 1)

    def test_detect_key_uses_musicxml_key_signature_without_music21(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "simple.musicxml"
            input_path.write_text(self.LEAD_SHEET_MUSICXML, encoding="utf-8")

            with patch("python.transposer._require_music21") as require_music21:
                self.assertEqual(detect_key_name(input_path), "G major")

            require_music21.assert_not_called()

    def test_transpose_to_key_uses_fast_xml_path_when_key_signature_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "lead-sheet.musicxml"
            output_path = tmp_path / "lead-sheet-c.musicxml"
            input_path.write_text(self.LEAD_SHEET_MUSICXML, encoding="utf-8")

            with patch("python.transposer._require_music21") as require_music21:
                with patch("python.transposer._validate_and_repair_musicxml", return_value={"xml_valid": True}):
                    transpose_to_key(input_path, output_path, "C major")

            require_music21.assert_not_called()
            output_text = output_path.read_text(encoding="utf-8")
            self.assertIn("<root-step>C</root-step>", output_text)
            self.assertIn("<credit-words>(SATB) Key: C</credit-words>", output_text)

    def test_complete_measure_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "complete.musicxml"
            output_path = tmp_path / "complete-d.musicxml"
            input_path.write_text(self.MEASURE_TEMPLATE.format(measures=self.COMPLETE_MEASURE), encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            output_text = output_path.read_text(encoding="utf-8")
            self.assertNotIn("<rest", output_text)
            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            self.assertEqual(measure_report["total_measures_checked"], 1)
            self.assertEqual(measure_report["incomplete_measures_found"], 0)
            self.assertEqual(measure_report["measures_repaired"], 0)

    def test_incomplete_measure_receives_rest_padding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "incomplete.musicxml"
            output_path = tmp_path / "incomplete-d.musicxml"
            input_path.write_text(
                self.MEASURE_TEMPLATE.format(measures=self.INCOMPLETE_SECOND_MEASURES),
                encoding="utf-8",
            )

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            output_text = output_path.read_text(encoding="utf-8")
            self.assertIn("<rest", output_text)
            self.assertIn("<duration>2</duration>", output_text)
            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            self.assertEqual(measure_report["total_measures_checked"], 2)
            self.assertEqual(measure_report["incomplete_measures_found"], 1)
            self.assertEqual(measure_report["measures_repaired"], 1)

    def test_pickup_measure_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "pickup.musicxml"
            output_path = tmp_path / "pickup-d.musicxml"
            input_path.write_text(self.MEASURE_TEMPLATE.format(measures=self.PICKUP_MEASURES), encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            output_text = output_path.read_text(encoding="utf-8")
            self.assertNotIn("<rest", output_text)
            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            self.assertEqual(measure_report["incomplete_measures_found"], 1)
            self.assertEqual(measure_report["measures_repaired"], 0)
            self.assertEqual(measure_report["measures_skipped_as_intentional"], 1)

    def test_direct_mxl_fallback_transposes_inner_musicxml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "simple.mxl"
            output_path = tmp_path / "simple-d.mxl"
            container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="score.musicxml" media-type="application/vnd.recordare.musicxml+xml"/>
  </rootfiles>
</container>
"""

            with zipfile.ZipFile(input_path, "w", compression=zipfile.ZIP_DEFLATED) as mxl:
                mxl.writestr("META-INF/container.xml", container_xml)
                mxl.writestr("score.musicxml", self.SIMPLE_MUSICXML)

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey())

            with zipfile.ZipFile(output_path, "r") as mxl:
                output_text = mxl.read("score.musicxml").decode("utf-8")

            self.assertIn("<fifths>2</fifths>", output_text)
            self.assertIn("<step>D</step>", output_text)
            self.assertIn("<step>F</step>", output_text)
            self.assertIn("<alter>1</alter>", output_text)

    def test_direct_mxl_fallback_can_write_plain_musicxml_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "simple.mxl"
            output_path = tmp_path / "simple-d.musicxml"
            container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="score.musicxml" media-type="application/vnd.recordare.musicxml+xml"/>
  </rootfiles>
</container>
"""

            with zipfile.ZipFile(input_path, "w", compression=zipfile.ZIP_DEFLATED) as mxl:
                mxl.writestr("META-INF/container.xml", container_xml)
                mxl.writestr("score.musicxml", self.SIMPLE_MUSICXML)

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey())

            output_text = output_path.read_text(encoding="utf-8")
            self.assertTrue(output_text.lstrip().startswith("<?xml"))
            self.assertIn("<score-partwise", output_text)
            self.assertIn("<fifths>2</fifths>", output_text)


class PipelineTests(unittest.TestCase):
    def test_musicxml_input_routes_to_transposer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song-in-d.musicxml"
            input_path.write_text("<score-partwise />", encoding="utf-8")

            with patch("python.pipeline.detect_key_name", return_value="C major"):
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

            with patch("python.pipeline.detect_key_name", return_value="C major"):
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

    def test_musicxml_output_runs_silent_musescore_validation_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song-in-g.musicxml"
            musescore_path = tmp_path / "MuseScore.exe"
            stages = []
            input_path.write_text("<score-partwise />", encoding="utf-8")
            output_path.write_text("<?xml version=\"1.0\"?><score-partwise />", encoding="utf-8")
            musescore_path.write_text("", encoding="utf-8")

            with patch("python.pipeline.detect_key_name", return_value="C major"):
                with patch("python.pipeline.transpose_to_key", return_value=output_path):
                    with patch("python.pipeline.export_musicxml_to_pdf", return_value=tmp_path / "validation.pdf") as export:
                        result = run_pipeline(
                            input_path,
                            output_path,
                            "G major",
                            "musicxml",
                            musescore_path=musescore_path,
                            progress=lambda name, detail="": stages.append((name, detail)),
                        )

            self.assertEqual(result, output_path)
            export.assert_called_once()
            self.assertTrue(any("MuseScore silent validation/export test: passed" in detail for _name, detail in stages))

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
                with patch("python.pipeline.detect_key_name", return_value="C major"):
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

            def write_valid(_source, destination, _target_key):
                Path(destination).write_text("<?xml version=\"1.0\"?><score-partwise />", encoding="utf-8")

            with patch("python.pipeline.transpose_to_key", side_effect=write_valid) as transpose:
                with patch("python.pipeline.detect_key_name", return_value="C major"):
                    with patch("python.pipeline.export_musicxml_to_pdf", return_value=output_path) as export:
                        result = run_pipeline(input_path, output_path, "D major", "pdf", musescore_path=musescore_path)

            self.assertEqual(result, output_path)
            transpose.assert_called_once()
            export.assert_called_once()

    def test_pdf_output_retries_direct_transpose_when_temp_musicxml_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song.pdf"
            musescore_path = tmp_path / "MuseScore.exe"
            input_path.write_text("<score-partwise />", encoding="utf-8")
            musescore_path.write_text("", encoding="utf-8")

            def write_invalid(_source, destination, _target_key):
                Path(destination).write_text("", encoding="utf-8")

            def write_valid(_source, destination, _target_key):
                Path(destination).write_text("<?xml version=\"1.0\"?><score-partwise />", encoding="utf-8")

            with patch("python.pipeline.detect_key_name", return_value="C major"):
                with patch("python.pipeline.transpose_to_key", side_effect=write_invalid):
                    with patch("python.pipeline.transpose_to_key_direct", side_effect=write_valid) as direct:
                        with patch("python.pipeline.export_musicxml_to_pdf", return_value=output_path) as export:
                            result = run_pipeline(input_path, output_path, "D major", "pdf", musescore_path=musescore_path)

            self.assertEqual(result, output_path)
            direct.assert_called_once()
            export.assert_called_once()

    def test_pdf_output_returns_saved_musicxml_when_pdf_export_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song.pdf"
            musescore_path = tmp_path / "MuseScore.exe"
            fallback_path = output_path.with_suffix(".musicxml")
            input_path.write_text("<score-partwise />", encoding="utf-8")
            musescore_path.write_text("", encoding="utf-8")

            def write_valid(_source, destination, _target_key):
                Path(destination).write_text("<?xml version=\"1.0\"?><score-partwise />", encoding="utf-8")

            with patch("python.pipeline.detect_key_name", return_value="C major"):
                with patch("python.pipeline.transpose_to_key", side_effect=write_valid):
                    with patch("python.pipeline.export_musicxml_to_pdf", side_effect=TranspositionError("MuseScore failed")):
                        result = run_pipeline(input_path, output_path, "D major", "pdf", musescore_path=musescore_path)

            self.assertEqual(result, fallback_path)
            self.assertTrue(fallback_path.is_file())

    def test_pipeline_reports_processing_stages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song.pdf"
            musescore_path = tmp_path / "MuseScore.exe"
            stages = []
            input_path.write_text("<score-partwise />", encoding="utf-8")
            musescore_path.write_text("", encoding="utf-8")

            def write_valid(_source, destination, _target_key):
                Path(destination).write_text("<?xml version=\"1.0\"?><score-partwise />", encoding="utf-8")

            with patch("python.pipeline.detect_key_name", return_value="C major"):
                with patch("python.pipeline.transpose_to_key", side_effect=write_valid):
                    with patch("python.pipeline.export_musicxml_to_pdf", return_value=output_path):
                        run_pipeline(
                            input_path,
                            output_path,
                            "D major",
                            "pdf",
                            musescore_path=musescore_path,
                            progress=lambda name, detail="": stages.append((name, detail)),
                        )

            stage_names = [name for name, _detail in stages]
            self.assertEqual(
                stage_names,
                [
                    "Loading file",
                    "Detecting key",
                    "Detecting key",
                    "Transposing",
                    "Validation report",
                    "Validate Output",
                    "Exporting output",
                    "Complete",
                ],
            )


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

    def test_audiveris_accepts_quoted_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            audiveris_path = tmp_path / "Audiveris Tool.exe"
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
                result = convert_pdf_to_musicxml(input_path, tmp_path / "work", f'"{audiveris_path}"')

            self.assertEqual(result, converted_path)
            self.assertEqual(run.call_args.args[0][0], str(audiveris_path))

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

            class FakeProcess:
                def __init__(self, command):
                    self.command = command
                    self.stdout = io.StringIO("")
                    self.stderr = io.StringIO("")
                    self._polls = 0

                def poll(self):
                    self._polls += 1
                    if self._polls == 1:
                        output_path.write_bytes(b"%PDF-1.7")
                    return 0

                def communicate(self, timeout=None):
                    return "", ""

                def terminate(self):
                    pass

                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    pass

            def fake_popen(command, stdout, stderr, text):
                output_path.write_bytes(b"%PDF-1.7")
                return FakeProcess(command)

            with patch("python.pdf_export.subprocess.Popen", side_effect=fake_popen) as popen:
                result = export_musicxml_to_pdf(input_path, output_path, musescore_path)

            self.assertEqual(result, output_path)
            popen.assert_called_once()
            self.assertIn(str(musescore_path), popen.call_args.args[0])
            self.assertIn("-n", popen.call_args.args[0])
            self.assertIn(str(output_path), popen.call_args.args[0])

    def test_musescore_export_rejects_empty_musicxml_before_launch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            musescore_path = tmp_path / "MuseScore.exe"
            input_path = tmp_path / "empty.musicxml"
            output_path = tmp_path / "score.pdf"
            musescore_path.write_text("", encoding="utf-8")
            input_path.write_text("", encoding="utf-8")

            with patch("python.pdf_export.subprocess.Popen") as popen:
                with self.assertRaisesRegex(TranspositionError, "empty or invalid"):
                    export_musicxml_to_pdf(input_path, output_path, musescore_path)

            popen.assert_not_called()

    def test_musescore_retries_without_n_when_option_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            musescore_path = tmp_path / "MuseScore4.exe"
            input_path = tmp_path / "score.musicxml"
            output_path = tmp_path / "score.pdf"
            musescore_path.write_text("", encoding="utf-8")
            input_path.write_text("<score-partwise />", encoding="utf-8")
            calls = []

            class FakeProcess:
                def __init__(self, command):
                    self.command = command
                    self.stdout = io.StringIO("")
                    self.stderr = io.StringIO("MuseScore4: Unknown option 'n'." if "-n" in command else "")

                def poll(self):
                    if "-n" in self.command:
                        return 1
                    output_path.write_bytes(b"%PDF-1.7")
                    return 0

                def communicate(self, timeout=None):
                    if "-n" in self.command:
                        return "", "MuseScore4: Unknown option 'n'."
                    return "", ""

                def terminate(self):
                    pass

                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    pass

            def fake_popen(command, stdout, stderr, text):
                calls.append(command)
                return FakeProcess(command)

            with patch("python.pdf_export.subprocess.Popen", side_effect=fake_popen):
                result = export_musicxml_to_pdf(input_path, output_path, musescore_path)

            self.assertEqual(result, output_path)
            self.assertIn("-n", calls[0])
            self.assertNotIn("-n", calls[1])


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
