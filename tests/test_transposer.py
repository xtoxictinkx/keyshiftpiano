from pathlib import Path
import io
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
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


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_child(element, name: str):
    for child in list(element):
        if _xml_local_name(child.tag) == name:
            return child
    return None


def _find_children(element, name: str):
    return [child for child in list(element) if _xml_local_name(child.tag) == name]


def _xml_text_values(file_path: Path, element_name: str) -> list[str]:
    root = ET.parse(file_path).getroot()
    return [
        element.text or ""
        for element in root.iter()
        if _xml_local_name(element.tag) == element_name
    ]


def _measure_duration_info(file_path: Path, measure_number: str) -> dict:
    root = ET.parse(file_path).getroot()
    active_divisions = 1
    active_beats = 4
    active_beat_type = 4

    for measure in root.iter():
        if _xml_local_name(measure.tag) != "measure":
            continue

        attributes = _find_child(measure, "attributes")
        if attributes is not None:
            divisions = _find_child(attributes, "divisions")
            if divisions is not None and (divisions.text or "").strip():
                active_divisions = int((divisions.text or "").strip())

            time = _find_child(attributes, "time")
            if time is not None:
                beats = _find_child(time, "beats")
                beat_type = _find_child(time, "beat-type")
                if beats is not None and (beats.text or "").strip():
                    active_beats = int((beats.text or "").strip())
                if beat_type is not None and (beat_type.text or "").strip():
                    active_beat_type = int((beat_type.text or "").strip())

        if measure.attrib.get("number") != measure_number:
            continue

        actual_duration = 0
        rest_count = 0
        for note_element in _find_children(measure, "note"):
            if _find_child(note_element, "rest") is not None:
                rest_count += 1
            if _find_child(note_element, "chord") is not None or _find_child(note_element, "grace") is not None:
                continue
            duration = _find_child(note_element, "duration")
            if duration is not None and (duration.text or "").strip():
                actual_duration += int(float((duration.text or "").strip()))

        expected_duration = int(active_divisions * active_beats * 4 / active_beat_type)
        return {
            "actual_duration": actual_duration,
            "expected_duration": expected_duration,
            "rest_count": rest_count,
        }

    raise AssertionError(f"Measure {measure_number} was not found.")


def _measure_number_counts(file_path: Path) -> dict[str, int]:
    root = ET.parse(file_path).getroot()
    counts = {}
    for measure in root.iter():
        if _xml_local_name(measure.tag) != "measure":
            continue
        number = measure.attrib.get("number", "")
        if number:
            counts[number] = counts.get(number, 0) + 1
    return counts


def _measure_staff_duration_info(file_path: Path, measure_number: str) -> dict[str, int]:
    root = ET.parse(file_path).getroot()
    durations = {}
    for measure in root.iter():
        if _xml_local_name(measure.tag) != "measure" or measure.attrib.get("number") != measure_number:
            continue
        for note_element in _find_children(measure, "note"):
            if _find_child(note_element, "chord") is not None or _find_child(note_element, "grace") is not None:
                continue
            duration = _find_child(note_element, "duration")
            if duration is None or not (duration.text or "").strip():
                continue
            staff = _find_child(note_element, "staff")
            staff_number = (staff.text or "").strip() if staff is not None else "1"
            durations[staff_number] = durations.get(staff_number, 0) + int(float((duration.text or "").strip()))
        return durations
    raise AssertionError(f"Measure {measure_number} was not found.")


def _measure_rest_flags(file_path: Path, measure_number: str) -> list[str]:
    root = ET.parse(file_path).getroot()
    flags = []
    for measure in root.iter():
        if _xml_local_name(measure.tag) != "measure" or measure.attrib.get("number") != measure_number:
            continue
        for note_element in _find_children(measure, "note"):
            rest = _find_child(note_element, "rest")
            if rest is not None:
                flags.append(rest.attrib.get("measure", ""))
        return flags
    raise AssertionError(f"Measure {measure_number} was not found.")


def _measure_rest_details(file_path: Path, measure_number: str) -> list[dict]:
    root = ET.parse(file_path).getroot()
    details = []
    for measure in root.iter():
        if _xml_local_name(measure.tag) != "measure" or measure.attrib.get("number") != measure_number:
            continue
        for note_element in _find_children(measure, "note"):
            rest = _find_child(note_element, "rest")
            if rest is None:
                continue
            duration = _find_child(note_element, "duration")
            voice = _find_child(note_element, "voice")
            note_type = _find_child(note_element, "type")
            staff = _find_child(note_element, "staff")
            details.append(
                {
                    "measure": rest.attrib.get("measure", ""),
                    "duration": (duration.text or "").strip() if duration is not None else "",
                    "voice": (voice.text or "").strip() if voice is not None else "",
                    "type": (note_type.text or "").strip() if note_type is not None else "",
                    "staff": (staff.text or "").strip() if staff is not None else "",
                }
            )
        return details
    raise AssertionError(f"Measure {measure_number} was not found.")


def _measure_backup_durations(file_path: Path, measure_number: str) -> list[int]:
    root = ET.parse(file_path).getroot()
    durations = []
    for measure in root.iter():
        if _xml_local_name(measure.tag) != "measure" or measure.attrib.get("number") != measure_number:
            continue
        for backup in _find_children(measure, "backup"):
            duration = _find_child(backup, "duration")
            if duration is not None and (duration.text or "").strip():
                durations.append(int(float((duration.text or "").strip())))
        return durations
    raise AssertionError(f"Measure {measure_number} was not found.")


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

    DIAGNOSTIC_MEASURES = """
    <measure number="122">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration><voice>1</voice><type>half</type></note>
      <barline location="right"><repeat direction="forward"/></barline>
    </measure>
    <measure number="123">
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>2</duration><voice>1</voice><type>half</type></note>
      <backup><duration>2</duration></backup>
    </measure>
    <measure number="124">
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>2</duration><voice>1</voice><type>half</type></note>
      <note><pitch><step>G</step><octave>4</octave></pitch><duration>1</duration><voice>2</voice><type>quarter</type></note>
    </measure>
    <measure number="125">
      <note><pitch><step>F</step><octave>4</octave></pitch><duration>2</duration><voice>1</voice><type>half</type></note>
      <notations><ending number="1" type="start"/></notations>
    </measure>
    <measure number="126">
      <attributes><measure-style><multiple-rest>2</multiple-rest></measure-style></attributes>
      <note><rest/><duration>2</duration><type>half</type></note>
    </measure>
    <measure number="127">
      <note><pitch><step>A</step><octave>4</octave></pitch><duration>2</duration><voice>1</voice><type>half</type></note>
      <direction><direction-type><words>C</words></direction-type></direction>
    </measure>
    <measure number="128">
      <note><pitch><step>B</step><octave>4</octave></pitch><duration>5</duration><voice>1</voice><type>whole</type></note>
    </measure>
"""

    NO_TIME_SIGNATURE_MEASURE = """
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>
    </measure>
"""

    DUPLICATE_122_128_MEASURES = """
    <measure number="122">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
    <measure number="122"><note><rest/><duration>4</duration><type>whole</type></note></measure>
    <measure number="123"><note><pitch><step>D</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note></measure>
    <measure number="123"><note><rest/><duration>4</duration><type>whole</type></note></measure>
    <measure number="124"><note><pitch><step>E</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note></measure>
    <measure number="124"><note><rest/><duration>4</duration><type>whole</type></note></measure>
    <measure number="125"><note><pitch><step>F</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note></measure>
    <measure number="125"><note><rest/><duration>4</duration><type>whole</type></note></measure>
    <measure number="126"><note><pitch><step>G</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note></measure>
    <measure number="126"><note><rest/><duration>4</duration><type>whole</type></note></measure>
    <measure number="127"><note><pitch><step>A</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note></measure>
    <measure number="127"><note><rest/><duration>4</duration><type>whole</type></note></measure>
    <measure number="128"><note><pitch><step>B</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note></measure>
    <measure number="128"><note><rest/><duration>4</duration><type>whole</type></note></measure>
"""

    DUPLICATE_REAL_MEASURES = """
    <measure number="122">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
    <measure number="122">
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note>
    </measure>
"""

    EMPTY_FOUR_STAFF_MEASURES = """
    <measure number="121">
      <attributes>
        <divisions>8</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <staves>4</staves>
      </attributes>
    </measure>
    <measure number="122"></measure>
    <measure number="123">
      <attributes><time><beats>7</beats><beat-type>8</beat-type></time></attributes>
    </measure>
    <measure number="124">
      <attributes><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    </measure>
    <measure number="125"></measure>
    <measure number="126"></measure>
    <measure number="127"></measure>
    <measure number="128"></measure>
"""

    EMPTY_TWO_STAFF_MEASURE = """
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <staves>2</staves>
      </attributes>
      <note>
        <rest measure="yes"/>
        <duration>16</duration>
        <voice>1</voice>
        <type>whole</type>
      </note>
    </measure>
"""

    SINGLE_STAFF_EMPTY_MEASURE = """
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note>
        <rest measure="yes"/>
        <duration>16</duration>
        <voice>1</voice>
        <type>whole</type>
      </note>
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
            self.assertIn("(SATB) Key: C", _xml_text_values(output_path, "credit-words"))
            self.assertIn("Composer Key:C", _xml_text_values(output_path, "creator"))
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
            self.assertIn("(SATB) Key: C", _xml_text_values(output_path, "credit-words"))

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
            measure_info = _measure_duration_info(output_path, "2")
            self.assertGreaterEqual(measure_info["rest_count"], 1)
            self.assertEqual(measure_info["actual_duration"], measure_info["expected_duration"])
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

    def test_skipped_measure_diagnostics_include_exact_reasons(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "diagnostics.musicxml"
            output_path = tmp_path / "diagnostics-d.musicxml"
            input_path.write_text(self.MEASURE_TEMPLATE.format(measures=self.DIAGNOSTIC_MEASURES), encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            diagnostics = {item["measure_number"]: item for item in measure_report["bad_measures"]}
            self.assertEqual(diagnostics["122"]["skip_reason"], "repeat")
            self.assertTrue(diagnostics["123"]["backups_forwards_found"])
            self.assertEqual(diagnostics["123"]["skip_reason"], "backup/forward")
            self.assertEqual(diagnostics["124"]["skip_reason"], "multi-voice")
            self.assertEqual(diagnostics["124"]["voices_found"], ["1", "2"])
            self.assertEqual(diagnostics["125"]["skip_reason"], "ending")
            self.assertEqual(diagnostics["126"]["skip_reason"], "multi-measure rest")
            self.assertEqual(diagnostics["128"]["skip_reason"], "duration mismatch too complex")
            self.assertIn("122-126", measure_report["manual_review_measures"])
            self.assertIn("128", measure_report["manual_review_measures"])

    def test_simple_measure_with_direction_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "simple-direction.musicxml"
            output_path = tmp_path / "simple-direction-d.musicxml"
            input_path.write_text(self.MEASURE_TEMPLATE.format(measures=self.DIAGNOSTIC_MEASURES), encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            diagnostics = {item["measure_number"]: item for item in measure_report["bad_measures"]}
            self.assertTrue(diagnostics["127"]["rests_added"])
            self.assertEqual(diagnostics["127"]["missing_duration"], 2)
            self.assertNotIn("127", measure_report["manual_review_measures"])
            measure_info = _measure_duration_info(output_path, "127")
            self.assertGreaterEqual(measure_info["rest_count"], 1)
            self.assertEqual(measure_info["actual_duration"], measure_info["expected_duration"])

    def test_no_time_signature_diagnostic_is_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "no-time.musicxml"
            output_path = tmp_path / "no-time-d.musicxml"
            input_path.write_text(self.MEASURE_TEMPLATE.format(measures=self.NO_TIME_SIGNATURE_MEASURE), encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            self.assertEqual(measure_report["bad_measures"][0]["skip_reason"], "no time signature")

    def test_duplicate_rest_only_measures_122_128_are_removed_and_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "duplicates.musicxml"
            output_path = tmp_path / "duplicates-d.musicxml"
            input_path.write_text(
                self.MEASURE_TEMPLATE.format(measures=self.DUPLICATE_122_128_MEASURES),
                encoding="utf-8",
            )

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            counts = _measure_number_counts(output_path)
            for number in ["122", "123", "124", "125", "126", "127", "128"]:
                self.assertEqual(counts[number], 1)

            duplicate_report = get_last_transposition_report()["output_validation"]["duplicate_measure_validation"]
            self.assertEqual(duplicate_report["duplicate_measures_found"], 7)
            self.assertEqual(duplicate_report["duplicate_measures_removed"], 7)
            by_number = {item["measure_number"]: item for item in duplicate_report["duplicates"]}
            self.assertEqual(by_number["122"]["duplicate_count"], 2)
            self.assertEqual(by_number["122"]["removed_count"], 1)

    def test_duplicate_real_measures_are_reported_not_removed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "duplicate-real.musicxml"
            output_path = tmp_path / "duplicate-real-d.musicxml"
            input_path.write_text(
                self.MEASURE_TEMPLATE.format(measures=self.DUPLICATE_REAL_MEASURES),
                encoding="utf-8",
            )

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            counts = _measure_number_counts(output_path)
            self.assertEqual(counts["122"], 2)
            validation = get_last_transposition_report()["output_validation"]
            duplicate_report = validation["duplicate_measure_validation"]
            self.assertEqual(duplicate_report["duplicate_measures_found"], 1)
            self.assertEqual(duplicate_report["duplicate_measures_removed"], 0)
            self.assertTrue(duplicate_report["errors"])

    def test_empty_four_staff_measures_receive_staff_specific_rests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "empty-staves.musicxml"
            output_path = tmp_path / "empty-staves-d.musicxml"
            input_path.write_text(
                self.MEASURE_TEMPLATE.format(measures=self.EMPTY_FOUR_STAFF_MEASURES),
                encoding="utf-8",
            )

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            measure_121 = _measure_staff_duration_info(output_path, "121")
            measure_123 = _measure_staff_duration_info(output_path, "123")
            self.assertEqual(measure_121, {"1": 32, "2": 32, "3": 32, "4": 32})
            self.assertEqual(measure_123, {"1": 28, "2": 28, "3": 28, "4": 28})
            self.assertEqual(_measure_rest_flags(output_path, "121"), ["yes", "yes", "yes", "yes"])
            self.assertEqual(_measure_rest_flags(output_path, "123"), ["yes", "yes", "yes", "yes"])
            self.assertEqual(
                _measure_rest_details(output_path, "121"),
                [
                    {"measure": "yes", "duration": "32", "voice": "1", "type": "whole", "staff": "1"},
                    {"measure": "yes", "duration": "32", "voice": "1", "type": "whole", "staff": "2"},
                    {"measure": "yes", "duration": "32", "voice": "1", "type": "whole", "staff": "3"},
                    {"measure": "yes", "duration": "32", "voice": "1", "type": "whole", "staff": "4"},
                ],
            )
            self.assertEqual(_measure_backup_durations(output_path, "121"), [32, 32, 32])
            self.assertEqual(_measure_backup_durations(output_path, "123"), [28, 28, 28])

            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            diagnostics = {item["measure_number"]: item for item in measure_report["bad_measures"]}
            self.assertEqual(measure_report["empty_measures_found"], 8)
            self.assertEqual(measure_report["empty_staff_measures_repaired"], 32)
            self.assertEqual(measure_report["staff_duration_validation"], [])
            self.assertEqual(measure_report["voice_duration_validation"], [])
            self.assertEqual(diagnostics["121"]["staff_count"], 4)
            self.assertEqual(diagnostics["121"]["rests_added_count"], 4)
            self.assertEqual(diagnostics["121"]["empty_staffs_repaired"], ["1", "2", "3", "4"])
            self.assertEqual(diagnostics["123"]["expected_duration"], 28)
            self.assertEqual(diagnostics["123"]["rests_added_count"], 4)

    def test_unstaffed_two_staff_measure_rest_is_rebuilt_with_staffs_and_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "two-staff.musicxml"
            output_path = tmp_path / "two-staff-d.musicxml"
            input_path.write_text(
                self.MEASURE_TEMPLATE.format(measures=self.EMPTY_TWO_STAFF_MEASURE),
                encoding="utf-8",
            )

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_measure_staff_duration_info(output_path, "1"), {"1": 16, "2": 16})
            self.assertEqual(
                _measure_rest_details(output_path, "1"),
                [
                    {"measure": "yes", "duration": "16", "voice": "1", "type": "whole", "staff": "1"},
                    {"measure": "yes", "duration": "16", "voice": "1", "type": "whole", "staff": "2"},
                ],
            )
            self.assertEqual(_measure_backup_durations(output_path, "1"), [16])
            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            self.assertEqual(measure_report["staff_duration_validation"], [])
            self.assertEqual(measure_report["voice_duration_validation"], [])

    def test_single_staff_measure_rest_remains_single_staff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "single-staff.musicxml"
            output_path = tmp_path / "single-staff-d.musicxml"
            input_path.write_text(
                self.MEASURE_TEMPLATE.format(measures=self.SINGLE_STAFF_EMPTY_MEASURE),
                encoding="utf-8",
            )

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_measure_staff_duration_info(output_path, "1"), {"1": 16})
            self.assertEqual(_measure_backup_durations(output_path, "1"), [])
            self.assertEqual(_measure_rest_details(output_path, "1")[0]["staff"], "")

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

    def test_validation_report_warns_about_manual_review_measures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song-in-g.musicxml"
            stages = []
            input_path.write_text("<score-partwise />", encoding="utf-8")
            output_path.write_text("<?xml version=\"1.0\"?><score-partwise />", encoding="utf-8")

            with patch("python.pipeline.detect_key_name", return_value="C major"):
                with patch("python.pipeline.transpose_to_key", return_value=output_path):
                    with patch(
                        "python.pipeline.get_last_transposition_report",
                        return_value={
                            "source_key": "C major",
                            "target_key": "G major",
                            "interval": 7,
                            "note_transposition_count": 0,
                            "key_signature_update_count": 0,
                            "harmony_chord_update_count": 0,
                            "visible_key_label_update_count": 0,
                            "output_validation": {
                                "xml_valid": True,
                                "harmony_elements_checked": 0,
                                "metadata_updated": 0,
                                "musescore_compatibility_check": "failed",
                                "duplicate_measure_validation": {
                                    "duplicate_measures_found": 2,
                                    "duplicate_measures_removed": 2,
                                },
                                "measure_validation": {
                                    "total_measures_checked": 128,
                                    "incomplete_measures_found": 7,
                                    "measures_repaired": 0,
                                    "measures_skipped_as_intentional": 7,
                                    "empty_measures_found": 8,
                                    "empty_staff_measures_repaired": 32,
                                    "staff_duration_validation": [],
                                    "voice_duration_validation": [],
                                    "manual_review_measures": ["122-128"],
                                },
                            },
                        },
                    ):
                        run_pipeline(
                            input_path,
                            output_path,
                            "G major",
                            "musicxml",
                            progress=lambda name, detail="": stages.append((name, detail)),
                        )

            self.assertTrue(any("Measures 122-128 still need manual review." in detail for _name, detail in stages))
            self.assertTrue(any("Empty measures found: 8" in detail for _name, detail in stages))
            self.assertTrue(any("Empty staff measures repaired: 32" in detail for _name, detail in stages))
            self.assertTrue(any("Staff duration issues remaining: 0" in detail for _name, detail in stages))
            self.assertTrue(any("Voice duration issues remaining: 0" in detail for _name, detail in stages))
            self.assertTrue(any("Duplicate measures found: 2" in detail for _name, detail in stages))
            self.assertTrue(any("Duplicate measures removed: 2" in detail for _name, detail in stages))

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
