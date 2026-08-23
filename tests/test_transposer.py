from pathlib import Path
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

from python.transposer import (
    TranspositionError,
    clean_imported_musicxml_layout,
    detect_key_name,
    get_last_transposition_report,
    transpose_to_key,
    validate_musicxml_path,
)
from python.transposer import (
    _apply_stored_measure_number_resets,
    _ensure_opening_time_signatures,
    _move_section_directions_to_target_part,
    _remove_redundant_tempo_word_directions,
    _remove_redundant_time_signatures,
    _remove_repeated_page_key_directions,
    _repair_ocr_ending_artifacts,
    _set_rights_metadata,
    _store_measure_number_resets,
    _transpose_musicxml_directly,
    _validate_musicxml_tree,
)
from python.pdf_recovery import (
    SystemRegion,
    _canonicalize_pdf_chord,
    _chord_word_candidates,
    _extract_pdf_section_captions,
    _extract_pdf_metadata,
    _extract_pdf_performance_directions,
    _infer_measure_number_resets,
    _merge_stacked_chord_candidates,
    _restore_pdf_performance_directions,
    _restore_pdf_section_captions,
    _restore_sparse_pdf_whole_note_measures,
    _select_chord_part_for_measures,
    _tokenize_pdf_lyric_words,
)
from python.converters import (
    ConversionResult,
    convert_source_to_musicxml,
    expand_mxl_to_musicxml,
)
from python.pipeline import run_pipeline
from python.pdf_conversion import STAFF_NOTATION_REQUIRED_MESSAGE, convert_pdf_to_musicxml


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


def _part_id_counts(file_path: Path) -> dict[str, int]:
    root = ET.parse(file_path).getroot()
    counts = {}
    for part in root.iter():
        if _xml_local_name(part.tag) != "part":
            continue
        part_id = part.attrib.get("id", "")
        if part_id:
            counts[part_id] = counts.get(part_id, 0) + 1
    return counts


def _part_measure_numbers(file_path: Path, part_id: str) -> list[str]:
    root = ET.parse(file_path).getroot()
    for part in root.iter():
        if _xml_local_name(part.tag) == "part" and part.attrib.get("id") == part_id:
            return [
                measure.attrib.get("number", "")
                for measure in _find_children(part, "measure")
                if measure.attrib.get("number")
            ]
    raise AssertionError(f"Part {part_id} was not found.")


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


def _part_measure_rest_details(file_path: Path, part_id: str, measure_number: str) -> list[dict]:
    root = ET.parse(file_path).getroot()
    for part in root.iter():
        if _xml_local_name(part.tag) != "part" or part.attrib.get("id") != part_id:
            continue
        for measure in _find_children(part, "measure"):
            if measure.attrib.get("number") != measure_number:
                continue
            details = []
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
    raise AssertionError(f"Part {part_id} measure {measure_number} was not found.")


def _part_measure_lyrics(file_path: Path, part_id: str, measure_number: str) -> list[str]:
    root = ET.parse(file_path).getroot()
    lyrics = []
    for part in root.iter():
        if _xml_local_name(part.tag) != "part" or part.attrib.get("id") != part_id:
            continue
        for measure in _find_children(part, "measure"):
            if measure.attrib.get("number") != measure_number:
                continue
            for note_element in _find_children(measure, "note"):
                for lyric in _find_children(note_element, "lyric"):
                    text = _find_child(lyric, "text")
                    if text is not None and (text.text or "").strip():
                        lyrics.append((text.text or "").strip())
            return lyrics
    raise AssertionError(f"Part {part_id} measure {measure_number} was not found.")


def _part_measure_note_sequence(file_path: Path, part_id: str, measure_number: str) -> list[dict]:
    root = ET.parse(file_path).getroot()
    sequence = []
    for part in root.iter():
        if _xml_local_name(part.tag) != "part" or part.attrib.get("id") != part_id:
            continue
        for measure in _find_children(part, "measure"):
            if measure.attrib.get("number") != measure_number:
                continue
            for note_element in _find_children(measure, "note"):
                duration = _find_child(note_element, "duration")
                lyrics = []
                for lyric in _find_children(note_element, "lyric"):
                    text = _find_child(lyric, "text")
                    if text is not None and (text.text or "").strip():
                        lyrics.append((text.text or "").strip())
                sequence.append(
                    {
                        "is_rest": _find_child(note_element, "rest") is not None,
                        "duration": (duration.text or "").strip() if duration is not None else "",
                        "lyrics": lyrics,
                        "default_x": note_element.attrib.get("default-x", ""),
                    }
                )
            return sequence
    raise AssertionError(f"Part {part_id} measure {measure_number} was not found.")


def _part_measure_pitches(file_path: Path, part_id: str, measure_number: str) -> list[str]:
    root = ET.parse(file_path).getroot()
    for part in root.iter():
        if _xml_local_name(part.tag) != "part" or part.attrib.get("id") != part_id:
            continue
        for measure in _find_children(part, "measure"):
            if measure.attrib.get("number") != measure_number:
                continue
            pitches = []
            for note_element in _find_children(measure, "note"):
                pitch = _find_child(note_element, "pitch")
                if pitch is None:
                    continue
                step = (_find_child(pitch, "step").text or "").strip()
                alter_element = _find_child(pitch, "alter")
                alter = int((alter_element.text or "0").strip()) if alter_element is not None else 0
                octave = (_find_child(pitch, "octave").text or "").strip()
                accidental = "#" * max(alter, 0) + "b" * max(-alter, 0)
                pitches.append(f"{step}{accidental}{octave}")
            return pitches
    raise AssertionError(f"Part {part_id} measure {measure_number} was not found.")


def _part_measure_rehearsals(file_path: Path, part_id: str, measure_number: str) -> list[str]:
    root = ET.parse(file_path).getroot()
    rehearsals = []
    for part in root.iter():
        if _xml_local_name(part.tag) != "part" or part.attrib.get("id") != part_id:
            continue
        for measure in _find_children(part, "measure"):
            if measure.attrib.get("number") != measure_number:
                continue
            for direction in _find_children(measure, "direction"):
                for direction_type in _find_children(direction, "direction-type"):
                    for rehearsal in _find_children(direction_type, "rehearsal"):
                        if rehearsal.text and rehearsal.text.strip():
                            rehearsals.append(rehearsal.text.strip())
            return rehearsals
    raise AssertionError(f"Part {part_id} measure {measure_number} was not found.")


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


def _measure_time_signature(file_path: Path, measure_number: str) -> tuple[str, str] | None:
    root = ET.parse(file_path).getroot()
    for measure in root.iter():
        if _xml_local_name(measure.tag) != "measure" or measure.attrib.get("number") != measure_number:
            continue
        attributes = _find_child(measure, "attributes")
        time = _find_child(attributes, "time") if attributes is not None else None
        if time is None:
            return None
        beats = _find_child(time, "beats")
        beat_type = _find_child(time, "beat-type")
        return (
            (beats.text or "").strip() if beats is not None else "",
            (beat_type.text or "").strip() if beat_type is not None else "",
        )
    raise AssertionError(f"Measure {measure_number} was not found.")


def _measure_staves(file_path: Path, measure_number: str) -> str:
    root = ET.parse(file_path).getroot()
    for measure in root.iter():
        if _xml_local_name(measure.tag) != "measure" or measure.attrib.get("number") != measure_number:
            continue
        attributes = _find_child(measure, "attributes")
        staves = _find_child(attributes, "staves") if attributes is not None else None
        return (staves.text or "").strip() if staves is not None else ""
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

    def test_rights_metadata_is_wrapped_once_for_pdf_footers(self):
        root = ET.fromstring(
            "<score-partwise><identification>"
            "<rights>old fragment</rights><rights>another fragment</rights>"
            "</identification><part-list /></score-partwise>"
        )

        _set_rights_metadata(root, "First footer line\nSecond footer line")

        rights = [
            element
            for element in root.iter()
            if _xml_local_name(element.tag) == "rights"
        ]
        self.assertEqual(len(rights), 1)
        self.assertEqual(rights[0].text, "First footer line\nSecond footer line")


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

    REST_ONLY_CARRY_TIME_MEASURES = """
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><rest measure="yes"/><duration>16</duration><voice>1</voice></note>
    </measure>
    <measure number="2">
      <note><rest measure="yes"/><duration>14</duration><voice>1</voice></note>
    </measure>
"""

    DUPLICATE_PARTS_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Full score</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="121">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <staves>4</staves>
      </attributes>
      <note><rest measure="yes"/><duration>16</duration><voice>1</voice></note>
    </measure>
  </part>
  <part id="P1">
    <measure number="121">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <staves>2</staves>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration><voice>1</voice><staff>1</staff></note>
      <backup><duration>16</duration></backup>
      <note><pitch><step>E</step><octave>3</octave></pitch><duration>16</duration><voice>5</voice><staff>2</staff></note>
    </measure>
  </part>
</score-partwise>
"""

    REST_ONLY_PART_WITH_GLOBAL_TIME_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Rest Part</part-name></score-part>
    <score-part id="P2"><part-name>Music Part</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="121">
      <attributes><divisions>4</divisions><key><fifths>0</fifths></key></attributes>
      <note><rest measure="yes"/><duration>16</duration><voice>1</voice></note>
    </measure>
    <measure number="123">
      <note><rest measure="yes"/><duration>14</duration><voice>1</voice></note>
    </measure>
  </part>
  <part id="P2">
    <measure number="121">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration><voice>1</voice></note>
    </measure>
    <measure number="123">
      <attributes><time><beats>7</beats><beat-type>8</beat-type></time></attributes>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>14</duration><voice>1</voice></note>
    </measure>
  </part>
</score-partwise>
"""

    REST_ONLY_PART_WITH_BAD_LOCAL_TIME_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Rest Part</part-name></score-part>
    <score-part id="P2"><part-name>Music Part</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="121">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>2</beats><beat-type>16</beat-type></time>
      </attributes>
      <note><rest measure="yes"/><duration>3</duration><voice>1</voice></note>
    </measure>
  </part>
  <part id="P2">
    <measure number="121">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration><voice>1</voice></note>
    </measure>
  </part>
</score-partwise>
"""

    REST_ONLY_PART_WITH_VALID_LOCAL_PICKUP_TIME_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Rest Part</part-name></score-part>
    <score-part id="P2"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>6</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>2</beats><beat-type>16</beat-type></time>
      </attributes>
      <note><rest measure="yes"/><duration>3</duration><voice>1</voice><type>eighth</type><staff>1</staff></note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1">
      <attributes>
        <divisions>6</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>24</duration><voice>1</voice></note>
    </measure>
  </part>
</score-partwise>
"""

    LEADING_INTRO_MISSING_IN_ONE_PART_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
    <score-part id="P2"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="9">
      <attributes>
        <divisions>12</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <staves>1</staves>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>48</duration><voice>1</voice><type>whole</type><staff>1</staff></note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1">
      <attributes>
        <divisions>6</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>2</beats><beat-type>16</beat-type></time>
        <staves>1</staves>
      </attributes>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>3</duration><voice>1</voice><type>eighth</type><staff>1</staff><lyric><syllabic>single</syllabic><text>IntroWords</text></lyric></note>
    </measure>
    <measure number="2"><note><rest measure="yes"/><duration>3</duration><voice>1</voice><type>whole</type><staff>1</staff></note></measure>
    <measure number="3"><note><rest measure="yes"/><duration>3</duration><voice>1</voice><type>whole</type><staff>1</staff></note></measure>
    <measure number="4"><note><rest measure="yes"/><duration>3</duration><voice>1</voice><type>whole</type><staff>1</staff></note></measure>
    <measure number="5"><note><rest measure="yes"/><duration>3</duration><voice>1</voice><type>whole</type><staff>1</staff></note></measure>
    <measure number="6"><note><rest measure="yes"/><duration>3</duration><voice>1</voice><type>whole</type><staff>1</staff></note></measure>
    <measure number="7"><note><rest measure="yes"/><duration>3</duration><voice>1</voice><type>whole</type><staff>1</staff></note></measure>
    <measure number="8"><note><rest measure="yes"/><duration>3</duration><voice>1</voice><type>whole</type><staff>1</staff></note></measure>
    <measure number="9">
      <attributes>
        <divisions>12</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <staves>1</staves>
      </attributes>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>48</duration><voice>1</voice><type>whole</type><staff>1</staff><lyric><syllabic>single</syllabic><text>KeepMe</text></lyric></note>
    </measure>
  </part>
</score-partwise>
"""

    PICKUP_VERSE_MARKER_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>6</divisions>
        <key><fifths>0</fifths></key>
      </attributes>
      <note>
        <pitch><step>F</step><alter>1</alter><octave>4</octave></pitch>
        <duration>3</duration>
        <voice>1</voice>
        <type>eighth</type>
        <lyric><syllabic>single</syllabic><text>I’ll</text></lyric>
      </note>
    </measure>
    <measure number="2">
      <direction placement="above">
        <direction-type>
          <rehearsal enclosure="rectangle">2 Verse</rehearsal>
        </direction-type>
      </direction>
      <note>
        <pitch><step>F</step><alter>1</alter><octave>4</octave></pitch>
        <duration>4</duration>
        <voice>1</voice>
        <type>eighth</type>
        <lyric><syllabic>single</syllabic><text>praise</text></lyric>
      </note>
    </measure>
  </part>
</score-partwise>
"""

    PICKUP_VERSE_MARKER_ALREADY_ALIGNED_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>6</divisions>
        <key><fifths>0</fifths></key>
      </attributes>
      <direction placement="above">
        <direction-type>
          <rehearsal enclosure="rectangle">2 Verse</rehearsal>
        </direction-type>
      </direction>
      <note>
        <pitch><step>F</step><alter>1</alter><octave>4</octave></pitch>
        <duration>3</duration>
        <voice>1</voice>
        <type>eighth</type>
        <lyric><syllabic>single</syllabic><text>1. I’ll</text></lyric>
      </note>
    </measure>
    <measure number="2">
      <note>
        <pitch><step>F</step><alter>1</alter><octave>4</octave></pitch>
        <duration>4</duration>
        <voice>1</voice>
        <type>eighth</type>
        <lyric><syllabic>single</syllabic><text>praise</text></lyric>
      </note>
    </measure>
  </part>
</score-partwise>
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

    def test_a_major_to_e_major_uses_the_nearest_register(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><key><fifths>3</fifths></key></attributes>
    <note><pitch><step>A</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "a-major.musicxml"
            output_path = tmp_path / "e-major.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            transpose_to_key(input_path, output_path, "E major")

            root = ET.parse(output_path).getroot()
            pitch = next(element for element in root.iter() if _xml_local_name(element.tag) == "pitch")
            self.assertEqual((_find_child(pitch, "step").text, _find_child(pitch, "octave").text), ("E", "4"))
            self.assertEqual(get_last_transposition_report()["interval"], -5)

    def test_pdf_chord_reader_preserves_compound_suspended_chords(self):
        self.assertEqual(_canonicalize_pdf_chord("F♯7sus"), "F#7sus")

    def test_pdf_chord_reader_joins_small_suffixes_and_stacked_bass_notes(self):
        def word(text_value, x0, x1, top, bottom, size=10):
            return {
                "text": text_value,
                "x0": x0,
                "x1": x1,
                "top": top,
                "bottom": bottom,
                "chars": [
                    {
                        "text": character,
                        "x0": x0,
                        "fontname": "ChordFont",
                        "size": size,
                    }
                    for character in text_value
                ],
            }

        candidates, suffixes_merged = _chord_word_candidates(
            [
                word("Dm", 100, 115, 20, 30),
                word("7(4)", 114.5, 132, 17, 28, size=8),
                word("C", 113, 120, 31, 41),
            ]
        )
        normalized = [
            {
                "text": candidate["text"],
                "x0": candidate["x0"],
                "x1": candidate["x1"],
                "top": candidate["top"],
            }
            for candidate in candidates
        ]
        merged, stacked_count = _merge_stacked_chord_candidates(normalized)

        self.assertEqual(suffixes_merged, 1)
        self.assertEqual(stacked_count, 1)
        self.assertEqual([candidate["text"] for candidate in merged], ["Dm7(4)/C"])

    def test_pdf_section_caption_reader_preserves_word_spacing(self):
        def word(text_value, x0):
            return {
                "text": text_value,
                "x0": x0,
                "x1": x0 + max(8, len(text_value) * 5),
                "top": 100,
                "bottom": 112,
                "chars": [
                    {
                        "text": character,
                        "x0": x0 + index * 5,
                        "fontname": "TextFont",
                        "size": 10,
                    }
                    for index, character in enumerate(text_value)
                ],
            }

        captions = _extract_pdf_section_captions(
            None,
            [
                word("3", 10),
                word("Verse", 20),
                word('"I', 55),
                word("know", 68),
                word("who", 94),
                word("I", 114),
                word('am..."', 124),
            ],
        )

        self.assertEqual(
            [(caption["label"], caption["text"]) for caption in captions],
            [("3 Verse", '"I know who I am..."')],
        )

    def test_pdf_section_caption_replaces_corrupted_ocr_direction(self):
        root = ET.fromstring(
            """<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
    <score-part id="P2"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1"><measure number="37">
    <direction><direction-type><words>3 Verse</words></direction-type></direction>
    <note><rest/><duration>4</duration></note>
  </measure></part>
  <part id="P2"><measure number="37">
    <direction><direction-type><words>"IknowwhoIam..."</words></direction-type></direction>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration></note>
  </measure></part>
</score-partwise>"""
        )

        report = _restore_pdf_section_captions(
            root,
            {
                "section_captions": [
                    {
                        "measure_number": "36",
                        "label": "3 Verse",
                        "text": '"I know who I am..."',
                    }
                ]
            },
            {"37": "P2"},
        )
        words = [
            element.text or ""
            for element in root.iter()
            if _xml_local_name(element.tag) == "words"
        ]

        self.assertEqual(report["section_caption_artifacts_removed"], 1)
        self.assertEqual(report["section_captions_restored"], 1)
        self.assertIn('"I know who I am..."', words)
        self.assertNotIn('"IknowwhoIam..."', words)

    def test_pdf_chords_follow_the_active_score_section(self):
        root = ET.fromstring(
            """<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
    <score-part id="P2"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1"><note><rest/><duration>1</duration></note></measure>
    <measure number="2"><note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note></measure>
  </part>
  <part id="P2">
    <measure number="1"><note><pitch><step>C</step><octave>3</octave></pitch><duration>1</duration></note></measure>
    <measure number="2"><note><pitch><step>C</step><octave>3</octave></pitch><duration>1</duration></note></measure>
  </part>
</score-partwise>"""
        )

        self.assertEqual(
            _select_chord_part_for_measures(root, ["1"]).attrib["id"],
            "P2",
        )
        self.assertEqual(
            _select_chord_part_for_measures(root, ["2"]).attrib["id"],
            "P1",
        )

    def test_pdf_lyric_reader_preserves_words_punctuation_and_syllables(self):
        def word(text_value, x0):
            return {
                "text": text_value,
                "x0": x0,
                "chars": [
                    {"text": character, "x0": x0 + index}
                    for index, character in enumerate(text_value)
                ],
            }

        tokens = _tokenize_pdf_lyric_words(
            [
                word("Some-thing", 10),
                word("isn't", 30),
                word("add", 45),
                word("-", 50),
                word("ing,", 54),
                word("up,", 70),
            ]
        )

        self.assertEqual(
            [token["text"] for token in tokens],
            ["Some", "thing", "isn't", "add", "ing,", "up,"],
        )
        self.assertEqual(
            [token["syllabic"] for token in tokens],
            ["begin", "end", "single", "begin", "end", "single"],
        )

    def test_import_cleanup_reflows_hard_page_breaks(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <defaults>
    <scaling><millimeters>6.4347</millimeters><tenths>40</tenths></scaling>
    <page-layout><page-height>1736.833</page-height><page-width>1342.098</page-width></page-layout>
  </defaults>
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <print new-page="yes"><system-layout><top-system-distance>180</top-system-distance></system-layout></print>
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note><rest/><duration>4</duration><type>whole</type></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "hard-page-break.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)

            root = ET.parse(input_path).getroot()
            print_element = next(element for element in root.iter() if _xml_local_name(element.tag) == "print")
            self.assertNotIn("new-page", print_element.attrib)
            self.assertEqual(print_element.attrib.get("new-system"), "yes")
            self.assertFalse(any(_xml_local_name(element.tag) == "top-system-distance" for element in print_element.iter()))
            self.assertEqual(report["hard_page_breaks_removed"], 1)
            self.assertEqual(_xml_text_values(input_path, "millimeters"), ["6"])
            self.assertAlmostEqual(float(_xml_text_values(input_path, "page-width")[0]), 1439.333, places=3)
            self.assertAlmostEqual(float(_xml_text_values(input_path, "page-height")[0]), 1862.667, places=3)

    def test_import_cleanup_removes_punctuation_only_word_directions(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <direction><direction-type><words>_</words></direction-type></direction>
    <note><rest measure="yes"/><duration>4</duration><type>whole</type></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "punctuation-direction.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)

            self.assertEqual(report["punctuation_only_directions_removed"], 1)
            self.assertNotIn("_", _xml_text_values(input_path, "words"))

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

    def test_malformed_minor_slash_chord_text_is_normalized(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>1</fifths></key>
      </attributes>
      <direction>
        <direction-type><words>A_mG</words></direction-type>
      </direction>
      <note><pitch><step>G</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "malformed-chord.musicxml"
            output_path = tmp_path / "malformed-chord-d.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")
            transpose_to_key(input_path, output_path, "D major")

            output_text = output_path.read_text(encoding="utf-8")
            self.assertIn("<words>Em/D</words>", output_text)
            self.assertNotIn("A_mG", output_text)
            validation = get_last_transposition_report()["output_validation"]
            self.assertEqual(validation["rendering_artifact_repair"]["malformed_chord_text_remaining"], [])

    def test_pdf_style_chord_suffixes_are_transposed(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><key><fifths>2</fifths></key></attributes>
    <direction><direction-type><words>D(no3)</words></direction-type></direction>
    <direction><direction-type><words>D2</words></direction-type></direction>
    <direction><direction-type><words>Em7(4)</words></direction-type></direction>
    <direction><direction-type><words>Dsus/F#</words></direction-type></direction>
    <direction><direction-type><words>D Dsus</words></direction-type></direction>
    <barline location="right"><ending number="1,3" type="start">D(n°3)</ending></barline>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "pdf-chords.musicxml"
            output_path = tmp_path / "pdf-chords-a.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")
            transpose_to_key(input_path, output_path, "A major")

            words = _xml_text_values(output_path, "words")
            self.assertIn("A(no3)", words)
            self.assertIn("A2", words)
            self.assertIn("Bm7(4)", words)
            self.assertIn("Asus/C#", words)
            self.assertIn("A Asus", words)
            self.assertIn("A(no3)", _xml_text_values(output_path, "ending"))

    def test_import_cleanup_repairs_known_amazing_grace_sat_scan_before_transposition(self):
        root = ET.Element("score-partwise", {"version": "4.0"})
        movement_title = ET.SubElement(root, "movement-title")
        movement_title.text = "Amazing Grace (My Chains Are Gone)"
        part_list = ET.SubElement(root, "part-list")
        score_part = ET.SubElement(part_list, "score-part", {"id": "P1"})
        part_name = ET.SubElement(score_part, "part-name")
        part_name.text = "Voice"
        part = ET.SubElement(root, "part", {"id": "P1"})

        def add_note(measure, step="D", octave=5, *, duration=1, rest=False, chord=False, default_x=None):
            attributes = {"default-x": str(default_x)} if default_x is not None else {}
            note_element = ET.SubElement(measure, "note", attributes)
            if chord:
                ET.SubElement(note_element, "chord")
            if rest:
                ET.SubElement(note_element, "rest")
            else:
                pitch = ET.SubElement(note_element, "pitch")
                step_element = ET.SubElement(pitch, "step")
                step_element.text = step
                octave_element = ET.SubElement(pitch, "octave")
                octave_element.text = str(octave)
            duration_element = ET.SubElement(note_element, "duration")
            duration_element.text = str(duration)
            voice = ET.SubElement(note_element, "voice")
            voice.text = "1"
            note_type = ET.SubElement(note_element, "type")
            note_type.text = "16th" if duration == 1 else "quarter"
            return note_element

        def add_words(measure, text_value):
            direction = ET.SubElement(measure, "direction", {"placement": "above"})
            direction_type = ET.SubElement(direction, "direction-type")
            words = ET.SubElement(direction_type, "words")
            words.text = text_value

        measures = {}
        for number in range(1, 47):
            measure = ET.SubElement(part, "measure", {"number": str(number)})
            measures[str(number)] = measure
            if number == 1:
                attributes = ET.SubElement(measure, "attributes")
                divisions = ET.SubElement(attributes, "divisions")
                divisions.text = "4"
                time = ET.SubElement(attributes, "time")
                beats = ET.SubElement(time, "beats")
                beats.text = "4"
                beat_type = ET.SubElement(time, "beat-type")
                beat_type.text = "4"
            add_note(measure, rest=True, duration=16)

        for number in ("4", "5", "34", "41", "42", "43", "44", "45", "46"):
            for note_element in list(_find_children(measures[number], "note")):
                measures[number].remove(note_element)

        add_note(measures["4"], default_x=207)
        add_note(measures["4"], step="A", default_x=241)
        add_note(measures["5"], step="E", default_x=117)
        add_words(measures["5"], "1x - Piano only")
        add_words(measures["5"], "2 2x - Add E.G. - light fills")
        add_words(measures["5"], "3x - Add A.G.")

        for number in ("12", "21"):
            barline = ET.SubElement(measures[number], "barline")
            ET.SubElement(barline, "ending", {"number": "1", "type": "start"})

        pickup = add_note(measures["34"], step="A")
        lyric = ET.SubElement(pickup, "lyric", {"number": "1"})
        text = ET.SubElement(lyric, "text")
        text.text = "The"

        for step, duration in zip(
            ("E", "D", "E", "D", "F", "E", "E", "D", "D"),
            (4, 6, 1, 1, 1, 3, 2, 1, 1),
        ):
            add_note(measures["41"], step=step, duration=duration)
        for step, duration, rest in (
            ("D", 8, False), ("D", 4, True), ("D", 2, True), ("D", 1, False), ("D", 1, False)
        ):
            add_note(measures["42"], step=step, duration=duration, rest=rest)
        for step, duration in zip(
            ("D", "E", "D", "F", "E", "E", "D", "D"),
            (6, 1, 1, 1, 3, 2, 1, 1),
        ):
            add_note(measures["43"], step=step, duration=duration)
        add_note(measures["44"], duration=8)
        add_note(measures["44"], rest=True, duration=4)
        add_note(measures["44"], rest=True, duration=2)
        add_note(measures["44"], duration=2)
        for step, duration in zip(
            ("D", "E", "D", "F", "E", "E", "D"),
            (6, 1, 1, 1, 3, 2, 2),
        ):
            add_note(measures["45"], step=step, duration=duration)
        add_note(measures["46"], duration=8)
        final_cue = add_note(measures["46"], step="A", octave=5, duration=8)
        notations = ET.SubElement(final_cue, "notations")
        ET.SubElement(notations, "fermata", {"type": "upright"})
        add_note(measures["46"], step="C", octave=6, duration=8, chord=True)
        add_words(measures["44"], "for - ev")
        add_words(measures["45"], "er mine;")
        add_words(measures["46"], "will be")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "amazing-grace-audiveris.musicxml"
            output_path = tmp_path / "amazing-grace-a.musicxml"
            ET.ElementTree(root).write(input_path, encoding="utf-8", xml_declaration=True)

            report = clean_imported_musicxml_layout(input_path)

            self.assertEqual(report["known_score_repairs_applied"], 1)
            self.assertEqual(report["title_block_items_rebuilt"], 12)
            self.assertEqual(_part_measure_numbers(input_path, "P1")[-6:], ["42", "43", "44", "45", "46", "47"])
            self.assertEqual(
                _part_measure_pitches(input_path, "P1", "42"),
                ["D4", "E4", "D4", "F#4", "E4", "E4", "D4", "D4"],
            )
            self.assertEqual(_part_measure_lyrics(input_path, "P1", "42"), ["be", "for", "ev", "er", "mine;"])
            self.assertIn("1x - Piano only\n2x - Add E.G. - light fills\n3x - Add A.G.", _xml_text_values(input_path, "words"))
            self.assertIn("All Xs - Parts\nMel. on top", _xml_text_values(input_path, "words"))
            self.assertIn("A/D", _xml_text_values(input_path, "words"))
            self.assertIn("G/D", _xml_text_values(input_path, "words"))
            self.assertIn("D2/F#", _xml_text_values(input_path, "words"))
            self.assertIn("Add A.G. - light fills", _xml_text_values(input_path, "words"))
            self.assertGreaterEqual(_xml_text_values(input_path, "notehead").count("slash"), 8)
            self.assertIn("diamond", _xml_text_values(input_path, "notehead"))
            credit_texts = _xml_text_values(input_path, "credit-words")
            self.assertIn("Arr and orch. by Dan Galbraith", credit_texts)
            self.assertIn("SATB Vocals by Shane Ohlson", credit_texts)
            self.assertIn("Amazing Grace (My Chains Are Gone) - page 2 of 2", credit_texts)
            self.assertTrue(any("Duplication of this music" in text for text in credit_texts))
            self.assertEqual(detect_key_name(input_path), "D major")

            transpose_to_key(input_path, output_path, "A major")

            self.assertEqual(_part_measure_pitches(output_path, "P1", "42")[0], "A3")
            self.assertEqual(_part_measure_pitches(output_path, "P1", "47")[-1], "F#4")
            self.assertIn("E/A", _xml_text_values(output_path, "words"))
            self.assertIn("D/A", _xml_text_values(output_path, "words"))
            self.assertIn("A2/C#", _xml_text_values(output_path, "words"))
            self.assertIn("Rit.", _xml_text_values(output_path, "words"))

    def test_import_cleanup_repairs_high_confidence_ocr_chords_and_section_labels(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><key><fifths>2</fifths></key></attributes>
    <direction><direction-type><words font-weight="bold">62</words></direction-type></direction>
    <direction><direction-type><words>G 2</words></direction-type></direction>
    <direction><direction-type><rehearsal>23 Chorus</rehearsal></direction-type></direction>
    <direction><direction-type><rehearsal>1 3 Verse</rehearsal></direction-type></direction>
    <direction><direction-type><rehearsal>to1</rehearsal></direction-type></direction>
    <barline><ending number="2,3" type="start">02</ending></barline>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration></note>
  </measure>
  <measure number="2">
    <barline><ending number="1,,,,3" type="start">D(nÂ°3)</ending></barline>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><lyric><text>v‘ '</text></lyric></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "audiveris-ocr.musicxml"
            output_path = tmp_path / "audiveris-ocr-a.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)
            self.assertEqual(report["ocr_chord_labels_repaired"], 2)
            self.assertEqual(report["ocr_section_labels_repaired"], 3)
            self.assertEqual(report["ocr_ending_labels_repaired"], 3)
            self.assertEqual(report["ocr_ending_chords_promoted"], 2)
            self.assertEqual(report["ocr_text_fragments_repaired"], 1)
            self.assertEqual(_xml_text_values(input_path, "words"), ["G2", "G2"])
            self.assertEqual(_xml_text_values(input_path, "rehearsal"), ["2a Chorus", "1a Verse", "to 1"])
            self.assertEqual(_xml_text_values(input_path, "ending"), ["2,3  D2", "1,3  D(no3)"])
            self.assertEqual(_xml_text_values(input_path, "text"), [""])

            transpose_to_key(input_path, output_path, "A major")
            self.assertEqual(_xml_text_values(output_path, "words"), ["D2", "D2"])
            self.assertEqual(_xml_text_values(output_path, "ending"), ["2,3  A2", "1,3  A(no3)"])

    def test_import_cleanup_repairs_common_text_and_conflicting_octave_clefs(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <print><system-layout><top-system-distance>315</top-system-distance></system-layout></print>
      <attributes><divisions>1</divisions><clef><sign>G</sign><line>2</line><clef-octave-change>-1</clef-octave-change></clef></attributes>
      <direction><direction-type><words>D Add A.G. -Iight fills</words></direction-type></direction>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><lyric><text>Iieved!</text></lyric></note>
    </measure>
    <measure number="2">
      <print><system-layout><top-system-distance>193</top-system-distance><system-distance>152</system-distance></system-layout></print>
      <attributes><clef><sign>G</sign><line>2</line><clef-octave-change>1</clef-octave-change></clef></attributes>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration><lyric><text>Iow,</text></lyric></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "audiveris-text.musicxml"
            output_path = tmp_path / "audiveris-text-a.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)

            self.assertEqual(report["ocr_text_fragments_repaired"], 3)
            self.assertEqual(report["ocr_clef_octave_changes_removed"], 2)
            self.assertEqual(report["system_distances_tightened"], 2)
            self.assertEqual(_xml_text_values(input_path, "words"), ["D Add A.G. -light fills"])
            self.assertEqual(_xml_text_values(input_path, "text"), ["lieved!", "low,"])
            self.assertEqual(_xml_text_values(input_path, "clef-octave-change"), [])
            self.assertEqual(_xml_text_values(input_path, "top-system-distance"), ["315", "120"])
            self.assertEqual(_xml_text_values(input_path, "system-distance"), ["75"])

            class Tonic:
                name = "A"

            class TargetKey:
                sharps = 3
                tonic = Tonic()
                mode = "major"

            _transpose_musicxml_directly(input_path, output_path, 7, TargetKey(), source_key_name="D major")
            self.assertEqual(_xml_text_values(output_path, "words"), ["A Add A.G. -light fills"])
            self.assertEqual(_xml_text_values(output_path, "fifths"), ["3"])

    def test_direct_transposition_inserts_missing_initial_key_signature(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>4</duration></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "missing-key.musicxml"
            output_path = tmp_path / "missing-key-a.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            class Tonic:
                name = "A"

            class TargetKey:
                sharps = 3
                tonic = Tonic()
                mode = "major"

            _transpose_musicxml_directly(input_path, output_path, 7, TargetKey(), source_key_name="D major")

            self.assertEqual(_xml_text_values(output_path, "fifths"), ["3"])
            self.assertEqual(get_last_transposition_report()["key_signature_update_count"], 1)

    def test_direct_transposition_preserves_internal_key_changes(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions><key><fifths>3</fifths></key></attributes>
      <note><pitch><step>A</step><octave>4</octave></pitch><duration>4</duration></note>
    </measure>
    <measure number="2">
      <attributes><key><fifths>0</fifths></key></attributes>
      <note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "modulating-a.musicxml"
            output_path = tmp_path / "modulating-e.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            transpose_to_key(input_path, output_path, "E major")

            self.assertEqual(_xml_text_values(output_path, "fifths"), ["4", "1"])

    def test_import_cleanup_recovers_numbered_chord_row_without_moving_it(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><key><fifths>2</fifths></key></attributes>
    <note default-x="285"><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="1"><text>my</text></lyric>
      <lyric number="2"><text>D2</text></lyric></note>
    <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="2"><text>A</text></lyric></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "chord-as-lyric.musicxml"
            output_path = tmp_path / "chord-as-lyric-a.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)
            self.assertEqual(report["ocr_chord_lyrics_recovered"], 2)
            self.assertEqual(report["ocr_chord_lyrics_promoted"], 0)
            self.assertIn("my", _xml_text_values(input_path, "text"))
            self.assertIn("D2", _xml_text_values(input_path, "text"))
            self.assertIn("A", _xml_text_values(input_path, "text"))

            transpose_to_key(input_path, output_path, "A major")
            self.assertIn("A2", _xml_text_values(output_path, "text"))
            self.assertIn("E", _xml_text_values(output_path, "text"))
            self.assertEqual(get_last_transposition_report()["recovered_chord_lyric_update_count"], 2)

    def test_chord_recovery_handles_verse_one_ocr_and_preserves_sung_single_letter(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <work><work-title>Context Test — page 9 of 9</work-title></work>
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
    <score-part id="P2"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><key><fifths>3</fifths></key></attributes>
    <direction><direction-type><rehearsal>Verse</rehearsal></direction-type></direction>
    <note><pitch><step>A</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="1" default-y="-100"><text>A</text></lyric></note>
    <note><pitch><step>B</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="1" default-y="-100"><text>dream</text></lyric></note>
  </measure></part>
  <part id="P2">
    <measure number="1">
      <attributes><divisions>1</divisions><key><fifths>3</fifths></key></attributes>
      <direction><direction-type><rehearsal>Verse</rehearsal></direction-type></direction>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration>
        <lyric number="1" default-y="-92"><text>A</text></lyric></note>
      <note><pitch><step>F</step><alter>1</alter><octave>4</octave></pitch><duration>1</duration>
        <lyric number="1" default-y="-88"><text>ESUS</text></lyric></note>
    </measure>
    <measure number="2">
      <print new-system="yes"/>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration>
        <lyric number="1" default-y="-91"><text>Fﬁm7</text></lyric></note>
      <note><pitch><step>F</step><alter>1</alter><octave>4</octave></pitch><duration>1</duration>
        <lyric number="1" default-y="-89"><text>Dma9</text></lyric></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "verse-one-chords.musicxml"
            output_path = tmp_path / "verse-one-chords-e.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)
            self.assertEqual(report["ocr_chord_lyrics_recovered"], 4)
            self.assertEqual(report["ocr_chord_lyrics_ambiguous"], 1)
            self.assertEqual(report["duplicate_rehearsal_marks_removed"], 1)
            self.assertEqual(report["rehearsal_marks_moved_to_top"], 1)
            self.assertEqual(report["rehearsal_marks_converted_to_top_text"], 1)
            self.assertEqual(_xml_text_values(input_path, "work-title"), ["Context Test"])
            self.assertEqual(_xml_text_values(input_path, "rehearsal"), [])
            self.assertIn("Verse", _xml_text_values(input_path, "words"))
            self.assertEqual(
                _xml_text_values(input_path, "text"),
                ["A", "dream", "A", "Esus", "F#m7", "Dmaj9"],
            )

            transpose_to_key(input_path, output_path, "E major")
            self.assertEqual(
                _xml_text_values(output_path, "text"),
                ["A", "dream", "E", "Bsus", "C#m7", "Amaj9"],
            )

    def test_import_cleanup_removes_only_unmatched_slurs(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><voice>1</voice>
      <notations><slur type="start" number="1"/><slur type="start" number="2"/></notations></note>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><voice>1</voice>
      <notations><slur type="stop" number="1"/><slur type="stop" number="3"/></notations></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "orphan-slurs.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)
            slurs = list(ET.parse(input_path).getroot().iter("slur"))

            self.assertEqual([(slur.attrib["type"], slur.attrib["number"]) for slur in slurs], [("start", "1"), ("stop", "1")])
            self.assertEqual(report["rendering_artifact_repair"]["unmatched_slurs_removed"], 2)

    def test_copyright_lyrics_are_hidden_and_preserved_as_footer_credit(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>1</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note>
        <pitch><step>G</step><octave>4</octave></pitch><duration>4</duration><type>whole</type>
        <lyric><text>Elevation Publishing CMG permission</text></lyric>
      </note>
      <note>
        <pitch><step>A</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type>
        <lyric><text>Savior has ransomed me</text></lyric>
      </note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "copyright-lyric.musicxml"
            output_path = tmp_path / "copyright-lyric-d.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")
            transpose_to_key(input_path, output_path, "D major")

            output_text = output_path.read_text(encoding="utf-8")
            self.assertNotIn("Elevation Publishing CMG permission</text></lyric>", output_text)
            self.assertIn("Savior has ransomed me", output_text)
            self.assertTrue(
                any("Elevation Publishing CMG permission" in text for text in _xml_text_values(output_path, "rights"))
            )
            validation = get_last_transposition_report()["output_validation"]["rendering_artifact_repair"]
            self.assertGreaterEqual(validation["copyright_lyric_artifacts_hidden"], 1)
            self.assertEqual(validation["copyright_metadata_added"], 1)

    def test_fragmented_copyright_tracks_are_removed_without_removing_real_lyrics(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><key><fifths>0</fifths></key></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="1"><text>Savior</text></lyric>
      <lyric number="2"><text>copyright rights</text></lyric>
      <lyric number="3"><text>CCLI permission</text></lyric></note>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="1"><text>has ransomed me</text></lyric>
      <lyric number="2"><text>Publishing worldwide</text></lyric>
      <lyric number="3"><text>CMG Publishing</text></lyric></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "fragmented-copyright.musicxml"
            output_path = tmp_path / "fragmented-copyright-d.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")
            transpose_to_key(input_path, output_path, "D major")

            lyric_texts = _xml_text_values(output_path, "text")
            self.assertIn("Savior", lyric_texts)
            self.assertIn("has ransomed me", lyric_texts)
            self.assertNotIn("copyright rights", lyric_texts)
            self.assertNotIn("CMG Publishing", lyric_texts)

    def test_copyright_cleanup_preserves_real_lyrics_in_the_same_track(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="2" default-y="-101"><text>grace</text></lyric></note>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="2" default-y="-181"><text>copyright rights</text></lyric></note>
    <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="2" default-y="-181"><text>CMG Publishing permission</text></lyric></note>
    <note><pitch><step>F</step><octave>4</octave></pitch><duration>1</duration>
      <lyric number="2" default-y="-84"><text>You are mine.</text></lyric></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "mixed-copyright-track.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)
            lyric_texts = _xml_text_values(input_path, "text")

            self.assertIn("grace", lyric_texts)
            self.assertIn("You are mine.", lyric_texts)
            self.assertNotIn("copyright rights", lyric_texts)
            self.assertNotIn("CMG Publishing permission", lyric_texts)
            self.assertEqual(report["rendering_artifact_repair"]["copyright_lyric_artifacts_hidden"], 2)

    def test_import_cleanup_normalizes_metadata_and_rebuilds_clean_title_block(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <work><work-title>Praise - page 1 of 12</work-title></work>
  <movement-title>Praise - page 1 of 12</movement-title>
  <identification><creator type="composer">Writer Name</creator></identification>
  <credit page="1"><credit-words>Praise - page 1 of 12</credit-words></credit>
  <credit page="1"><credit-words>Elevation Publishing permission</credit-words></credit>
  <part-list>
    <score-part id="P1"><part-name>Voice Voice Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>1</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><rest/><duration>4</duration><type>whole</type></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "audiveris.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path)
            root = ET.parse(input_path).getroot()
            credit_texts = _xml_text_values(input_path, "credit-words")

            self.assertGreaterEqual(report["metadata_normalized"], 1)
            self.assertGreaterEqual(report["duplicate_first_page_credits_removed"], 1)
            self.assertIn("Praise", _xml_text_values(input_path, "work-title"))
            self.assertIn("Praise", _xml_text_values(input_path, "movement-title"))
            self.assertTrue(any("Key: G" in text for text in credit_texts))
            self.assertIn("Voice", _xml_text_values(input_path, "part-name"))
            self.assertNotIn("Voice Voice Voice", ET.tostring(root, encoding="unicode"))
            self.assertNotIn("Praise - page 1 of 12", credit_texts)
            self.assertTrue(any("Elevation Publishing permission" in text for text in _xml_text_values(input_path, "rights")))

    def test_import_cleanup_deduplicates_staff_labels(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1">
      <part-name>Piano Piano Piano</part-name>
      <part-abbreviation>Voice Voice</part-abbreviation>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      <note><rest/><duration>4</duration><type>whole</type></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "labels.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            report = clean_imported_musicxml_layout(input_path, rebuild_title_block=False)

            self.assertEqual(report["staff_labels_cleaned"], 2)
            self.assertIn("Piano", _xml_text_values(input_path, "part-name"))
            self.assertIn("Voice", _xml_text_values(input_path, "part-abbreviation"))

    def test_detect_key_uses_musicxml_key_signature_without_music21(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "simple.musicxml"
            input_path.write_text(self.LEAD_SHEET_MUSICXML, encoding="utf-8")

            with patch("python.transposer._require_music21") as require_music21:
                self.assertEqual(detect_key_name(input_path), "G major")

            require_music21.assert_not_called()

    def test_detect_key_prefers_explicit_minor_label_over_relative_major_signature(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <credit page="1"><credit-words>Key: Am</credit-words></credit>
  <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><key><fifths>0</fifths></key></attributes>
    <note><pitch><step>A</step><octave>4</octave></pitch><duration>4</duration></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "minor-label.musicxml"
            output_path = tmp_path / "minor-label-e.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            self.assertEqual(detect_key_name(input_path), "A minor")
            transpose_to_key(input_path, output_path, "E minor")

            self.assertEqual(_xml_text_values(output_path, "fifths"), ["1"])
            self.assertEqual(_xml_text_values(output_path, "credit-words"), ["Key: Em"])

    def test_transposition_rejects_major_minor_mode_conversion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "lead-sheet.musicxml"
            output_path = tmp_path / "lead-sheet-minor.musicxml"
            input_path.write_text(self.LEAD_SHEET_MUSICXML, encoding="utf-8")

            with self.assertRaisesRegex(TranspositionError, "choose a major target key"):
                transpose_to_key(input_path, output_path, "D minor")

    def test_detect_key_ignores_blank_fifths_and_uses_filename_key(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><key><fifths></fifths></key></attributes>
      <note><rest/><duration>4</duration><type>whole</type></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "Amazing Grace - D - Lead Sheet.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            with patch("python.transposer._require_music21") as require_music21:
                self.assertEqual(detect_key_name(input_path), "D major")

            require_music21.assert_not_called()

    def test_detect_key_returns_unknown_when_music21_analysis_fails(self):
        musicxml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Voice</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><key><fifths></fifths></key></attributes>
      <note><rest/><duration>4</duration><type>whole</type></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "no-key-hint.musicxml"
            input_path.write_text(musicxml, encoding="utf-8")

            class BrokenConverter:
                @staticmethod
                def parse(_path):
                    raise ValueError("invalid literal for int() with base 10: ''")

            with patch("python.transposer._require_music21", return_value=(BrokenConverter, None, None)):
                self.assertEqual(detect_key_name(input_path), "unknown")

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

    def test_duplicate_rest_only_part_is_removed_when_real_part_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "duplicate-parts.musicxml"
            output_path = tmp_path / "duplicate-parts-d.musicxml"
            input_path.write_text(self.DUPLICATE_PARTS_MUSICXML, encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_part_id_counts(output_path), {"P1": 1})
            duplicate_part_report = get_last_transposition_report()["output_validation"]["duplicate_part_validation"]
            self.assertEqual(duplicate_part_report["duplicate_parts_found"], 1)
            self.assertEqual(duplicate_part_report["duplicate_parts_removed"], 1)
            self.assertFalse(duplicate_part_report["errors"])

    def test_rest_only_part_uses_global_time_signature_for_repair(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "global-time.musicxml"
            output_path = tmp_path / "global-time-d.musicxml"
            input_path.write_text(self.REST_ONLY_PART_WITH_GLOBAL_TIME_MUSICXML, encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_measure_rest_details(output_path, "121")[0], {
                "measure": "yes",
                "duration": "16",
                "voice": "1",
                "type": "whole",
                "staff": "1",
            })
            self.assertEqual(_measure_rest_details(output_path, "123")[0]["duration"], "14")
            self.assertEqual(_measure_rest_details(output_path, "123")[0]["staff"], "1")
            self.assertEqual(_measure_time_signature(output_path, "121"), ("4", "4"))
            self.assertEqual(_measure_time_signature(output_path, "123"), ("7", "8"))
            self.assertEqual(_measure_staves(output_path, "121"), "1")
            self.assertEqual(_measure_staves(output_path, "123"), "1")
            measure_report = get_last_transposition_report()["output_validation"]["measure_validation"]
            self.assertEqual(measure_report["time_signatures_inferred"], 2)
            self.assertEqual(measure_report["staff_duration_validation"], [])

    def test_rest_only_part_bad_local_time_is_overridden_by_global_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "bad-local-time.musicxml"
            output_path = tmp_path / "bad-local-time-d.musicxml"
            input_path.write_text(self.REST_ONLY_PART_WITH_BAD_LOCAL_TIME_MUSICXML, encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_measure_time_signature(output_path, "121"), ("4", "4"))
            self.assertEqual(_measure_rest_details(output_path, "121")[0]["duration"], "16")
            self.assertEqual(_measure_rest_details(output_path, "121")[0]["staff"], "1")
            self.assertEqual(_measure_staves(output_path, "121"), "1")

    def test_valid_local_pickup_time_is_not_overridden_by_global_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "valid-local-pickup.musicxml"
            output_path = tmp_path / "valid-local-pickup-d.musicxml"
            input_path.write_text(self.REST_ONLY_PART_WITH_VALID_LOCAL_PICKUP_TIME_MUSICXML, encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_measure_time_signature(output_path, "1"), ("2", "16"))
            self.assertEqual(_measure_rest_details(output_path, "1")[0]["duration"], "3")
            self.assertEqual(_measure_rest_details(output_path, "1")[0]["staff"], "1")

    def test_missing_leading_part_measures_are_filled_with_rests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "missing-intro.musicxml"
            output_path = tmp_path / "missing-intro-d.musicxml"
            input_path.write_text(self.LEADING_INTRO_MISSING_IN_ONE_PART_MUSICXML, encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_part_measure_numbers(output_path, "P1")[:9], [str(number) for number in range(1, 10)])
            rest_details = _part_measure_rest_details(output_path, "P1", "1")[0]
            self.assertEqual(rest_details["measure"], "yes")
            self.assertTrue(rest_details["duration"])
            self.assertEqual(rest_details["voice"], "1")
            self.assertEqual(rest_details["type"], "whole")
            self.assertEqual(rest_details["staff"], "1")
            self.assertEqual(_part_measure_lyrics(output_path, "P2", "1"), ["IntroWords"])
            self.assertEqual(_part_measure_lyrics(output_path, "P2", "9"), ["KeepMe"])
            leading_report = get_last_transposition_report()["output_validation"]["leading_part_alignment"]
            self.assertEqual(leading_report["missing_leading_measures_found"], 8)
            self.assertEqual(leading_report["leading_rest_measures_added"], 8)
            self.assertEqual(leading_report["parts"][0]["part_id"], "P1")

    def test_pickup_verse_marker_moves_to_pickup_measure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "pickup-verse.musicxml"
            output_path = tmp_path / "pickup-verse-d.musicxml"
            input_path.write_text(self.PICKUP_VERSE_MARKER_MUSICXML, encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_part_measure_lyrics(output_path, "P1", "1"), ["1. I’ll"])
            self.assertEqual(_part_measure_lyrics(output_path, "P1", "2"), ["praise"])
            self.assertEqual(_part_measure_rehearsals(output_path, "P1", "1"), ["2 Verse"])
            self.assertEqual(_part_measure_rehearsals(output_path, "P1", "2"), [])
            pickup_sequence = _part_measure_note_sequence(output_path, "P1", "1")
            self.assertEqual(pickup_sequence[0]["is_rest"], True)
            self.assertEqual(pickup_sequence[0]["duration"], "3")
            self.assertEqual(pickup_sequence[0]["default_x"], "")
            self.assertEqual(pickup_sequence[1]["is_rest"], False)
            self.assertEqual(pickup_sequence[1]["lyrics"], ["1. I’ll"])
            self.assertEqual(pickup_sequence[1]["default_x"], "")
            pickup_report = get_last_transposition_report()["output_validation"]["pickup_marker_alignment"]
            self.assertEqual(pickup_report["pickup_rehearsal_markers_moved"], 1)
            self.assertEqual(pickup_report["pickup_verse_numbers_added"], 1)
            self.assertEqual(pickup_report["pickup_leading_rests_added"], 1)
            self.assertEqual(pickup_report["moves"][0]["part_id"], "P1")

    def test_aligned_pickup_verse_marker_still_gets_leading_rest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "pickup-verse-aligned.musicxml"
            output_path = tmp_path / "pickup-verse-aligned-d.musicxml"
            input_path.write_text(self.PICKUP_VERSE_MARKER_ALREADY_ALIGNED_MUSICXML, encoding="utf-8")

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            pickup_sequence = _part_measure_note_sequence(output_path, "P1", "1")
            self.assertEqual(pickup_sequence[0]["is_rest"], True)
            self.assertEqual(pickup_sequence[0]["duration"], "3")
            self.assertEqual(pickup_sequence[0]["default_x"], "")
            self.assertEqual(pickup_sequence[1]["is_rest"], False)
            self.assertEqual(pickup_sequence[1]["lyrics"], ["1. I’ll"])
            self.assertEqual(pickup_sequence[1]["default_x"], "")
            pickup_report = get_last_transposition_report()["output_validation"]["pickup_marker_alignment"]
            self.assertEqual(pickup_report["pickup_rehearsal_markers_moved"], 0)
            self.assertEqual(pickup_report["pickup_leading_rests_added"], 1)

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
            self.assertEqual(_measure_staves(output_path, "121"), "4")
            self.assertEqual(_measure_staves(output_path, "123"), "4")
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
            self.assertEqual(_measure_rest_details(output_path, "1")[0]["staff"], "1")
            self.assertEqual(_measure_staves(output_path, "1"), "1")

    def test_missing_time_signature_carries_previous_time_signature(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "carry-time.musicxml"
            output_path = tmp_path / "carry-time-d.musicxml"
            input_path.write_text(
                self.MEASURE_TEMPLATE.format(measures=self.REST_ONLY_CARRY_TIME_MEASURES),
                encoding="utf-8",
            )

            class TargetKey:
                sharps = 2

            _transpose_musicxml_directly(input_path, output_path, 2, TargetKey(), source_key_name="C major")

            self.assertEqual(_measure_time_signature(output_path, "2"), ("4", "4"))
            self.assertEqual(_measure_rest_details(output_path, "2")[0]["duration"], "16")

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


class PdfImportCleanupRegressionTests(unittest.TestCase):
    def test_measure_number_reset_is_applied_without_duplicate_validation_error(self):
        root = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
              <part id="P1">
                <measure number="95"/>
                <measure number="96"/>
                <measure number="117"/>
                <measure number="118"/>
                <measure number="119"/>
                <measure number="120"/>
              </part>
            </score-partwise>
            """
        )
        self.assertEqual(
            _store_measure_number_resets(
                root,
                [{"boundary_measure": "118", "printed_measure": "95", "offset": -23}],
            ),
            1,
        )
        self.assertEqual(_apply_stored_measure_number_resets(root), 3)
        measures = next(element for element in root if _xml_local_name(element.tag) == "part")
        self.assertEqual(
            [measure.attrib["number"] for measure in _find_children(measures, "measure")],
            ["95", "96", "117", "95", "96", "97"],
        )
        self.assertFalse(
            any("duplicate measure number" in error.lower() for error in _validate_musicxml_tree(root)["errors"])
        )

    def test_nonzero_printed_measure_run_infers_reset(self):
        self.assertEqual(
            _infer_measure_number_resets(
                [
                    {"recognized_measure": "116", "printed_measure": "116"},
                    {"recognized_measure": "118", "printed_measure": "95"},
                    {"recognized_measure": "121", "printed_measure": "98"},
                    {"recognized_measure": "124", "printed_measure": "101"},
                ]
            ),
            [{"boundary_measure": "118", "printed_measure": "95", "offset": -23}],
        )

    def test_redundant_visible_time_signatures_are_removed(self):
        root = ET.fromstring(
            """
            <score-partwise>
              <part id="P1">
                <measure number="1"><attributes><time><beats>6</beats><beat-type>8</beat-type></time></attributes></measure>
                <measure number="2"><attributes><time><beats>6</beats><beat-type>8</beat-type></time></attributes></measure>
              </part>
            </score-partwise>
            """
        )
        self.assertEqual(_remove_redundant_time_signatures(root), 1)
        self.assertEqual(sum(1 for element in root.iter() if _xml_local_name(element.tag) == "time"), 1)

    def test_late_entering_part_hides_repeated_opening_time_signature(self):
        root = ET.fromstring(
            """
            <score-partwise>
              <part id="P1">
                <measure number="1"><attributes><time><beats>4</beats><beat-type>4</beat-type></time></attributes></measure>
              </part>
              <part id="P2">
                <measure number="10"><attributes><time><beats>4</beats><beat-type>4</beat-type></time></attributes></measure>
              </part>
            </score-partwise>
            """
        )
        self.assertEqual(_remove_redundant_time_signatures(root), 1)
        parts = [element for element in root if _xml_local_name(element.tag) == "part"]
        late_time = next(
            element
            for element in parts[1].iter()
            if _xml_local_name(element.tag) == "time"
        )
        self.assertEqual(late_time.attrib.get("print-object"), "no")

    def test_import_cleanup_removes_page_key_and_duplicate_tempo_words(self):
        root = ET.fromstring(
            """
            <score-partwise>
              <part id="P1">
                <measure number="1">
                  <attributes><key><fifths>-2</fifths></key></attributes>
                  <direction>
                    <direction-type>
                      <metronome><beat-unit>quarter</beat-unit><per-minute>72</per-minute></metronome>
                    </direction-type>
                  </direction>
                  <direction><direction-type><words>= 72</words></direction-type></direction>
                </measure>
                <measure number="2">
                  <attributes><key><fifths>-2</fifths></key></attributes>
                  <direction><direction-type><rehearsal>Key: Bb</rehearsal></direction-type></direction>
                </measure>
                <measure number="3">
                  <attributes><key><fifths>-3</fifths></key></attributes>
                  <direction><direction-type><words>Key: Eb</words></direction-type></direction>
                </measure>
              </part>
            </score-partwise>
            """
        )
        self.assertEqual(_remove_redundant_tempo_word_directions(root), 1)
        self.assertEqual(_remove_repeated_page_key_directions(root), 1)
        words = [
            (element.text or "").strip()
            for element in root.iter()
            if _xml_local_name(element.tag) == "words"
        ]
        self.assertEqual(words, ["Key: Eb"])

    def test_section_cleanup_keeps_the_most_specific_label(self):
        root = ET.fromstring(
            """
            <score-partwise>
              <part id="P1">
                <measure number="40">
                  <direction><direction-type><words>5 Chorus 2</words></direction-type></direction>
                </measure>
              </part>
              <part id="P2">
                <measure number="40">
                  <direction><direction-type><words>Chorus</words></direction-type></direction>
                </measure>
              </part>
            </score-partwise>
            """
        )
        report = _move_section_directions_to_target_part(
            root,
            "P1",
            {"40": "P1"},
        )
        self.assertEqual(report["duplicates_removed"], 1)
        words = [
            (element.text or "").strip()
            for element in root.iter()
            if _xml_local_name(element.tag) == "words"
        ]
        self.assertEqual(words, ["5 Chorus 2"])

    def test_pdf_header_columns_do_not_merge_url_with_composers(self):
        class FakePage:
            width = 612
            height = 792
            rects = []

            def extract_words(self, **_kwargs):
                def word(text, x0, x1, top, size=10):
                    return {
                        "text": text,
                        "x0": x0,
                        "x1": x1,
                        "top": top,
                        "bottom": top + size,
                        "height": size,
                        "size": size,
                        "chars": [],
                    }

                return [
                    word("Holy", 255, 285, 20, 16),
                    word("Forever", 290, 345, 20, 16),
                    word("www.praisecharts.com/79148", 235, 370, 62, 7),
                    word("Chris", 480, 510, 62, 7),
                    word("Tomlin,", 514, 550, 62, 7),
                ]

            def extract_text(self):
                return "quarter = 72"

        metadata = _extract_pdf_metadata(FakePage())
        self.assertEqual(metadata["source_url"], "www.praisecharts.com/79148")
        self.assertNotIn("Chris", metadata["source_url"])

    def test_italic_performance_cue_is_mapped_to_its_measure(self):
        class FakePage:
            width = 600
            height = 400
            rects = []
            lines = [
                {"x0": 40, "x1": 40, "top": 100, "bottom": 300, "width": 0, "height": 200},
                {"x0": 220, "x1": 220, "top": 100, "bottom": 300, "width": 0, "height": 200},
                {"x0": 390, "x1": 390, "top": 100, "bottom": 300, "width": 0, "height": 200},
                {"x0": 560, "x1": 560, "top": 100, "bottom": 300, "width": 0, "height": 200},
            ]

        word = {
            "text": "mel.",
            "x0": 132,
            "x1": 148,
            "top": 150,
            "bottom": 158,
            "chars": [
                {"text": character, "fontname": "Subset+Arial,Italic", "size": 8}
                for character in "mel."
            ],
        }
        directions = _extract_pdf_performance_directions(
            FakePage(),
            [word],
            [SystemRegion(100, 300, 40, 560)],
            [["87", "88", "89"]],
        )
        self.assertEqual(len(directions), 1)
        self.assertEqual(directions[0]["text"], "mel.")
        self.assertEqual(directions[0]["measure_number"], "87")
        self.assertEqual(directions[0]["system_measures"], ["87", "88", "89"])

    def test_performance_cue_is_restored_to_the_correct_voice_staff(self):
        root = ET.fromstring(
            """
            <score-partwise>
              <part id="P1"><measure number="87"/><measure number="88"/><measure number="89"/></part>
              <part id="P2"><measure number="87"/><measure number="88"/><measure number="89"/></part>
              <part id="P3">
                <measure number="87"><attributes><staves>2</staves></attributes></measure>
                <measure number="88"/><measure number="89"/>
              </part>
            </score-partwise>
            """
        )
        report = _restore_pdf_performance_directions(
            root,
            {
                "performance_directions": [
                    {
                        "text": "mel.",
                        "measure_number": "87",
                        "system_measures": ["87", "88", "89"],
                        "relative_y": 0.26,
                    }
                ]
            },
        )
        self.assertEqual(report["performance_directions_restored"], 1)
        parts = [element for element in root if _xml_local_name(element.tag) == "part"]
        self.assertEqual(
            [
                (element.text or "").strip()
                for element in parts[1].iter()
                if _xml_local_name(element.tag) == "words"
            ],
            ["mel."],
        )
        self.assertFalse(
            any(_xml_local_name(element.tag) == "words" for element in parts[0].iter())
        )
        words = next(
            element for element in parts[1].iter() if _xml_local_name(element.tag) == "words"
        )
        self.assertEqual(words.attrib.get("font-style"), "italic")

    def test_missing_opening_meter_is_copied_to_each_part(self):
        root = ET.fromstring(
            """
            <score-partwise>
              <part id="P1">
                <measure number="1">
                  <attributes>
                    <divisions>4</divisions>
                    <time><beats>4</beats><beat-type>4</beat-type></time>
                    <clef><sign>G</sign><line>2</line></clef>
                  </attributes>
                </measure>
              </part>
              <part id="P2">
                <measure number="1">
                  <attributes><clef><sign>F</sign><line>4</line></clef></attributes>
                </measure>
              </part>
            </score-partwise>
            """
        )
        self.assertEqual(_ensure_opening_time_signatures(root), 1)
        parts = [element for element in root if _xml_local_name(element.tag) == "part"]
        attributes = _find_child(_find_children(parts[1], "measure")[0], "attributes")
        time_element = _find_child(attributes, "time")
        self.assertIsNotNone(time_element)
        self.assertNotIn("print-object", time_element.attrib)
        self.assertLess(list(attributes).index(time_element), list(attributes).index(_find_child(attributes, "clef")))

    def test_sparse_pdf_whole_note_chords_restore_empty_piano_measure(self):
        root = ET.fromstring(
            """
            <score-partwise>
              <part id="P1">
                <measure number="1">
                  <attributes>
                    <divisions>1</divisions>
                    <key><fifths>-2</fifths></key>
                    <time><beats>4</beats><beat-type>4</beat-type></time>
                    <staves>2</staves>
                    <clef number="1"><sign>G</sign><line>2</line></clef>
                    <clef number="2"><sign>F</sign><line>4</line></clef>
                  </attributes>
                  <note><rest measure="yes"/><duration>4</duration><staff>1</staff></note>
                  <backup><duration>4</duration></backup>
                  <note><rest measure="yes"/><duration>4</duration><staff>2</staff></note>
                </measure>
              </part>
            </score-partwise>
            """
        )

        class FakePage:
            width = 600
            height = 400
            rects = []

            def __init__(self):
                self.lines = [
                    {"x0": 40, "x1": 40, "top": 100, "bottom": 300, "width": 0, "height": 200},
                    {"x0": 560, "x1": 560, "top": 100, "bottom": 300, "width": 0, "height": 200},
                ]
                for top in (150, 155, 160, 165, 170, 230, 235, 240, 245, 250):
                    self.lines.append(
                        {"x0": 40, "x1": 560, "top": top, "bottom": top, "width": 520, "height": 0}
                    )

                def glyph(text, font, x0, top):
                    return {
                        "text": text,
                        "fontname": font,
                        "x0": x0,
                        "x1": x0 + 7,
                        "top": top,
                        "bottom": top + 17,
                    }

                self.chars = [
                    glyph("w", "Subset+Jazz", 200, 159.5),  # C5
                    glyph("w", "Subset+Jazz", 200, 164.5),  # A4
                    glyph("w", "Subset+Jazz", 200, 169.5),  # F4
                    glyph("w", "Subset+Jazz", 200, 244.5),  # C3
                    glyph("w", "Subset+Jazz", 200, 262.0),  # C2
                    glyph("U", "Subset+Maestro", 198, 147),
                    glyph("U", "Subset+Maestro", 198, 227),
                ]

        class FakePdf:
            pages = [FakePage()]

        report = _restore_sparse_pdf_whole_note_measures(
            root,
            FakePdf(),
            [[["1"]]],
        )
        self.assertEqual(report["sparse_whole_note_measures_restored"], 1)
        self.assertEqual(report["sparse_whole_notes_restored"], 5)
        pitches = {
            (
                next(child for child in pitch if _xml_local_name(child.tag) == "step").text,
                next(child for child in pitch if _xml_local_name(child.tag) == "octave").text,
            )
            for pitch in root.iter()
            if _xml_local_name(pitch.tag) == "pitch"
        }
        self.assertEqual(pitches, {("F", "4"), ("A", "4"), ("C", "5"), ("C", "3"), ("C", "2")})
        self.assertEqual(
            sum(1 for element in root.iter() if _xml_local_name(element.tag) == "fermata"),
            2,
        )

    def test_adjacent_duplicate_closed_ending_is_removed(self):
        root = ET.fromstring(
            """
            <score-partwise>
              <part id="P1">
                <measure number="1"><barline><ending number="2" type="start"/><ending number="2" type="stop"/></barline></measure>
                <measure number="2"><barline><ending number="2" type="start"/><ending number="2" type="discontinue"/></barline></measure>
              </part>
            </score-partwise>
            """
        )
        report = _repair_ocr_ending_artifacts(root)
        self.assertEqual(report["duplicate_ending_groups_removed"], 1)
        endings = [element for element in root.iter() if _xml_local_name(element.tag) == "ending"]
        self.assertEqual(len(endings), 2)


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
                    )

            self.assertEqual(result, output_path)
            transpose.assert_called_once_with(input_path, output_path, "G major")

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
                                "musicxml_compatibility_check": "failed",
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

            conversion = ConversionResult(
                source_path=input_path,
                musicxml_path=converted_path,
                input_format="pdf",
                engine="audiveris",
            )
            with patch("python.pipeline.convert_source_to_musicxml", return_value=conversion) as convert:
                with patch("python.pipeline.detect_key_name", return_value="C major"):
                    with patch("python.pipeline.transpose_to_key", return_value=output_path) as transpose:
                        result = run_pipeline(input_path, output_path, "D major", "musicxml", audiveris_path=audiveris_path)

            self.assertEqual(result, output_path)
            convert.assert_called_once()
            transpose.assert_called_once_with(converted_path, output_path, "D major")

    def test_pdf_word_recovery_runs_when_layout_cleanup_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "scan.pdf"
            output_path = tmp_path / "scan.musicxml"
            converted_path = tmp_path / "converted.musicxml"
            input_path.write_bytes(b"%PDF-1.7")
            converted_path.write_text("<score-partwise />", encoding="utf-8")
            conversion = ConversionResult(input_path, converted_path, "pdf", "audiveris")

            with patch("python.pipeline.convert_source_to_musicxml", return_value=conversion):
                with patch("python.pipeline.clean_imported_musicxml_layout") as cleanup:
                    with patch("python.pipeline.detect_key_name", return_value="C major"):
                        with patch("python.pipeline.transpose_to_key", return_value=output_path):
                            run_pipeline(
                                input_path,
                                output_path,
                                "E major",
                                "musicxml",
                                clean_export_layout=False,
                            )

            cleanup.assert_called_once_with(
                converted_path,
                source_pdf_path=input_path,
                rebuild_title_block=False,
                apply_layout_cleanup=False,
            )

    def test_pdf_mxl_import_is_expanded_before_layout_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "scan.pdf"
            output_path = tmp_path / "scan.musicxml"
            converted_path = tmp_path / "converted.mxl"
            expanded_path = tmp_path / "expanded.musicxml"
            input_path.write_bytes(b"%PDF-1.7")
            converted_path.write_bytes(b"mxl")
            expanded_path.write_text("<score-partwise />", encoding="utf-8")
            conversion = ConversionResult(input_path, converted_path, "pdf", "audiveris")

            with patch("python.pipeline.convert_source_to_musicxml", return_value=conversion):
                with patch("python.pipeline.expand_mxl_to_musicxml", return_value=expanded_path) as expand:
                    with patch("python.pipeline.clean_imported_musicxml_layout") as cleanup:
                        with patch("python.pipeline.detect_key_name", return_value="D major"):
                            with patch("python.pipeline.transpose_to_key", return_value=output_path) as transpose:
                                run_pipeline(input_path, output_path, "A major", "musicxml", audiveris_path="Audiveris.exe")

            expand.assert_called_once()
            cleanup.assert_called_once_with(
                expanded_path,
                source_pdf_path=input_path,
                rebuild_title_block=True,
                apply_layout_cleanup=True,
            )
            transpose.assert_called_once_with(expanded_path, output_path, "A major")

    def test_pdf_output_stops_before_transposition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song.pdf"
            input_path.write_text("<score-partwise />", encoding="utf-8")

            with patch("python.pipeline.detect_key_name", return_value="C major"):
                with patch("python.pipeline.transpose_to_key", return_value=tmp_path / "song.musicxml") as transpose:
                    with self.assertRaisesRegex(TranspositionError, "PDF export is handled by the desktop app"):
                        run_pipeline(input_path, output_path, "D major", "pdf")

            transpose.assert_not_called()

    def test_pipeline_reports_processing_stages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            output_path = tmp_path / "song.musicxml"
            stages = []
            input_path.write_text("<score-partwise />", encoding="utf-8")

            def write_valid(_source, destination, _target_key):
                Path(destination).write_text("<?xml version=\"1.0\"?><score-partwise />", encoding="utf-8")

            with patch("python.pipeline.detect_key_name", return_value="C major"):
                with patch("python.pipeline.transpose_to_key", side_effect=write_valid):
                    run_pipeline(
                        input_path,
                        output_path,
                        "D major",
                        "musicxml",
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
                    "Complete",
                ],
            )


class PdfToolTests(unittest.TestCase):
    def test_missing_audiveris_path_has_clear_error(self):
        with self.assertRaisesRegex(TranspositionError, "PDF import requires the Audiveris OMR engine"):
            convert_pdf_to_musicxml("scan.pdf", tempfile.gettempdir(), "")

    def test_converter_layer_passes_musicxml_through(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "song.musicxml"
            input_path.write_text("<score-partwise />", encoding="utf-8")

            result = convert_source_to_musicxml(input_path, tmp_path / "work")

            self.assertEqual(result.source_path, input_path)
            self.assertEqual(result.musicxml_path, input_path)
            self.assertEqual(result.input_format, "musicxml")
            self.assertEqual(result.engine, "none")

    def test_converter_expands_compressed_mxl_for_cleanup(self):
        container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="scores/main.musicxml"/></rootfiles>
</container>
"""
        score_xml = b"<?xml version='1.0'?><score-partwise version='4.0'/>"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "audiveris.mxl"
            with zipfile.ZipFile(input_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("META-INF/container.xml", container_xml)
                archive.writestr("scores/main.musicxml", score_xml)

            expanded = expand_mxl_to_musicxml(input_path, tmp_path / "work")

            self.assertEqual(expanded.suffix, ".musicxml")
            self.assertEqual(expanded.read_bytes(), score_xml)

    def test_converter_layer_routes_pdf_to_audiveris(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "scan.pdf"
            converted_path = tmp_path / "converted.musicxml"
            input_path.write_bytes(b"%PDF-1.7")
            converted_path.write_text("<score-partwise />", encoding="utf-8")

            with patch("python.converters.convert_pdf_to_musicxml", return_value=converted_path) as convert:
                result = convert_source_to_musicxml(input_path, tmp_path / "work", audiveris_path="audiveris.exe")

            self.assertEqual(result.source_path, input_path)
            self.assertEqual(result.musicxml_path, converted_path)
            self.assertEqual(result.input_format, "pdf")
            self.assertEqual(result.engine, "audiveris")
            convert.assert_called_once_with(input_path, tmp_path / "work", "audiveris.exe")

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

    def test_staffless_chord_chart_has_clear_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            audiveris_path = tmp_path / "audiveris.exe"
            input_path = tmp_path / "chord-chart.pdf"
            audiveris_path.write_text("", encoding="utf-8")
            input_path.write_bytes(b"%PDF-1.7")
            failure_log = """\
INFO [] StepMonitoring 98 | SCALE
INFO [] Book 543 | Created scores: []
INFO [] SheetStub 1194 | Sheet chord-chart#1 flagged as invalid.
java.lang.Exception: Error in export
"""

            with patch(
                "python.pdf_conversion.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", failure_log),
            ):
                with self.assertRaises(TranspositionError) as raised:
                    convert_pdf_to_musicxml(input_path, tmp_path / "work", audiveris_path)

            self.assertEqual(str(raised.exception), STAFF_NOTATION_REQUIRED_MESSAGE)
            self.assertNotIn("java.lang.Exception", str(raised.exception))


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
