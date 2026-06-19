from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.modules["python.transposer"] = sys.modules[__name__]

VALID_SUFFIXES = {".musicxml", ".xml", ".mxl"}
PITCH_CLASSES = {
    "C": 0,
    "D": 2,
    "E": 4,
    "F": 5,
    "G": 7,
    "A": 9,
    "B": 11,
}
SHARP_SPELLINGS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_SPELLINGS = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
LAST_TRANSPOSITION_REPORT = None
MAJOR_KEY_BY_FIFTHS = {
    -7: "Cb",
    -6: "Gb",
    -5: "Db",
    -4: "Ab",
    -3: "Eb",
    -2: "Bb",
    -1: "F",
    0: "C",
    1: "G",
    2: "D",
    3: "A",
    4: "E",
    5: "B",
    6: "F#",
    7: "C#",
}
MINOR_KEY_BY_FIFTHS = {
    -7: "Ab",
    -6: "Eb",
    -5: "Bb",
    -4: "F",
    -3: "C",
    -2: "G",
    -1: "D",
    0: "A",
    1: "E",
    2: "B",
    3: "F#",
    4: "C#",
    5: "G#",
    6: "D#",
    7: "A#",
}
FIFTHS_BY_MAJOR_KEY = {name: fifths for fifths, name in MAJOR_KEY_BY_FIFTHS.items()}
FIFTHS_BY_MINOR_KEY = {name: fifths for fifths, name in MINOR_KEY_BY_FIFTHS.items()}

KEY_LABEL_PATTERN = re.compile(r"(?i)(\bKey\s*:?\s*)([A-G](?:#|b)?)(\b)")
CHORD_TEXT_PATTERN = re.compile(
    r"^([A-G](?:#|b)?)(m|maj7|maj|min|dim|aug|sus\d*|add\d*|m\d+|maj\d+|\d+)?(?:/([A-G](?:#|b)?))?$"
)
VISIBLE_TEXT_ELEMENTS = {"words", "credit-words", "rehearsal"}
METADATA_TEXT_ELEMENTS = {"creator", "rights", "movement-title", "work-title", "miscellaneous-field"}


class TranspositionError(Exception):
    """Raised when a MusicXML file cannot be transposed."""


class SimpleKey:
    def __init__(self, tonic: str, mode: str = "major"):
        self.tonic = type("Tonic", (), {"name": tonic})()
        self.mode = mode
        fifths_by_key = FIFTHS_BY_MINOR_KEY if mode == "minor" else FIFTHS_BY_MAJOR_KEY
        self.sharps = fifths_by_key.get(tonic)
        if self.sharps is None:
            raise TranspositionError(f"Unsupported target key: {tonic} {mode}")


def get_last_transposition_report() -> dict | None:
    return LAST_TRANSPOSITION_REPORT


def _require_music21():
    try:
        from music21 import converter, interval, key
    except ImportError as exc:
        raise TranspositionError(
            "Python package 'music21' is required. Install it with: pip install -r requirements.txt"
        ) from exc

    return converter, interval, key


def validate_musicxml_path(file_path: str | Path, *, must_exist: bool) -> Path:
    path = Path(file_path).expanduser()
    if path.suffix.lower() not in VALID_SUFFIXES:
        raise TranspositionError("MusicXML files must end in .musicxml, .xml, or .mxl.")

    if must_exist and not path.is_file():
        raise TranspositionError(f"Input file was not found: {path}")

    return path


def transpose_to_key(input_path: str | Path, output_path: str | Path, target_key_name: str) -> Path:
    global LAST_TRANSPOSITION_REPORT

    LAST_TRANSPOSITION_REPORT = None

    source_path = validate_musicxml_path(input_path, must_exist=True)
    destination_path = validate_musicxml_path(output_path, must_exist=False)

    try:
        target_key = _parse_simple_key(target_key_name)
    except Exception as exc:
        raise TranspositionError(f"Unsupported target key: {target_key_name}") from exc

    fast_source_key = _detect_key_name_from_xml(source_path)
    if fast_source_key:
        source_tonic = fast_source_key.split()[0]
        target_tonic = _target_key_label(target_key)
        source_pitch_class = _pitch_name_to_class(source_tonic)
        target_pitch_class = _pitch_name_to_class(target_tonic)
        if source_pitch_class is not None and target_pitch_class is not None:
            semitones = (target_pitch_class - source_pitch_class) % 12
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            return _transpose_musicxml_directly(
                source_path,
                destination_path,
                semitones,
                target_key,
                source_key_name=fast_source_key,
            )

    converter, interval, key = _require_music21()

    try:
        target_key = key.Key(_target_key_label(target_key), target_key.mode)
        score = converter.parse(str(source_path))
        source_key = score.analyze("key")
        transposition_interval = interval.Interval(source_key.tonic, target_key.tonic)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _transpose_musicxml_directly(
                source_path,
                destination_path,
                transposition_interval.semitones,
                target_key,
                source_key_name=f"{source_key.tonic.name} {source_key.mode}",
            )
        except Exception as direct_exc:
            try:
                shifted_score = score.transpose(transposition_interval)
                _set_initial_key_signatures(shifted_score, target_key)
                shifted_score.write("musicxml", fp=str(destination_path))
                LAST_TRANSPOSITION_REPORT = {
                    "source_key": f"{source_key.tonic.name} {source_key.mode}",
                    "target_key": _target_key_name(target_key),
                    "interval": int(round(transposition_interval.semitones)),
                    "note_transposition_count": "music21",
                    "key_signature_update_count": "music21",
                    "harmony_chord_update_count": "music21",
                    "visible_key_label_update_count": "not handled by music21 writer",
                }
            except Exception as write_exc:
                raise TranspositionError(
                    "MusicXML export failed while writing the transposed score. "
                    f"direct XML error: {direct_exc}; music21 writer error: {write_exc}"
                ) from write_exc
    except TranspositionError:
        raise
    except Exception as exc:
        detail = str(exc).strip()
        if detail.startswith("'") and detail.endswith("'"):
            detail = "MusicXML export failed while writing the transposed score."
        raise TranspositionError(f"Could not transpose this MusicXML file. {detail}") from exc

    return destination_path


def transpose_to_key_direct(input_path: str | Path, output_path: str | Path, target_key_name: str) -> Path:
    global LAST_TRANSPOSITION_REPORT

    _converter, interval, key = _require_music21()
    LAST_TRANSPOSITION_REPORT = None

    source_path = validate_musicxml_path(input_path, must_exist=True)
    destination_path = validate_musicxml_path(output_path, must_exist=False)

    try:
        parts = target_key_name.strip().split()
        tonic = parts[0]
        mode = parts[1] if len(parts) > 1 else "major"
        target_key = key.Key(tonic, mode)
        source_key_name = detect_key_name(source_path)
        source_parts = source_key_name.strip().split()
        source_key = key.Key(source_parts[0], source_parts[1] if len(source_parts) > 1 else "major")
        transposition_interval = interval.Interval(source_key.tonic, target_key.tonic)
        return _transpose_musicxml_directly(
            source_path,
            destination_path,
            transposition_interval.semitones,
            target_key,
            source_key_name=source_key_name,
        )
    except TranspositionError:
        raise
    except Exception as exc:
        raise TranspositionError(f"Could not transpose this MusicXML file with the direct XML fallback. {exc}") from exc


def detect_key_name(input_path: str | Path) -> str:
    source_path = validate_musicxml_path(input_path, must_exist=True)
    fast_key = _detect_key_name_from_xml(source_path)
    if fast_key:
        return fast_key

    converter, _interval, _key = _require_music21()

    try:
        score = converter.parse(str(source_path))
        detected_key = score.analyze("key")
    except Exception as exc:
        raise TranspositionError(f"Could not detect the original key. {exc}") from exc

    return f"{detected_key.tonic.name} {detected_key.mode}"


def emit_stage(name: str, detail: str = "") -> None:
    print(json.dumps({"type": "stage", "name": name, "detail": detail}), flush=True)


def _set_initial_key_signatures(score, target_key):
    from music21 import key

    for part in score.parts:
        first_measure = part.measure(1)
        if first_measure is None:
            continue

        key_signatures = first_measure.getElementsByClass("KeySignature")
        for existing in list(key_signatures):
            first_measure.remove(existing)

        first_measure.insert(0, key.KeySignature(int(target_key.sharps)))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_child(element, name: str):
    for child in list(element):
        if _local_name(child.tag) == name:
            return child

    return None


def _find_children(element, name: str):
    return [child for child in list(element) if _local_name(child.tag) == name]


def _iter_elements(root, name: str):
    for element in root.iter():
        if _local_name(element.tag) == name:
            yield element


def _pitch_to_midi(step: str, alter: int, octave: int) -> int:
    return ((octave + 1) * 12) + PITCH_CLASSES[step] + alter


def _parse_simple_key(key_name: str) -> SimpleKey:
    parts = key_name.strip().replace("-", "b").split()
    if not parts:
        raise TranspositionError("Target key is required.")

    tonic = parts[0]
    mode = parts[1].lower() if len(parts) > 1 else "major"
    if mode in {"maj"}:
        mode = "major"
    if mode in {"min", "m"}:
        mode = "minor"
    if mode not in {"major", "minor"}:
        raise TranspositionError(f"Unsupported target key: {key_name}")

    return SimpleKey(tonic, mode)


def _detect_key_name_from_xml(input_path: Path) -> str | None:
    try:
        if input_path.suffix.lower() == ".mxl":
            with zipfile.ZipFile(input_path, "r") as source_zip:
                rootfile_path = _get_mxl_rootfile(source_zip)
                root = ET.fromstring(source_zip.read(rootfile_path))
        else:
            root = ET.parse(input_path).getroot()
    except Exception:
        return None

    for key_element in _iter_elements(root, "key"):
        fifths_element = _find_child(key_element, "fifths")
        if fifths_element is None:
            continue

        try:
            fifths = int((fifths_element.text or "").strip())
        except ValueError:
            continue

        mode_element = _find_child(key_element, "mode")
        mode = (mode_element.text or "major").strip().lower() if mode_element is not None else "major"
        if mode == "minor":
            tonic = MINOR_KEY_BY_FIFTHS.get(fifths)
        else:
            mode = "major"
            tonic = MAJOR_KEY_BY_FIFTHS.get(fifths)

        if tonic:
            return f"{tonic} {mode}"

    return None


def _spell_pitch(midi_value: int, prefer_flats: bool) -> tuple[str, int, int]:
    octave = (midi_value // 12) - 1
    pitch_class = midi_value % 12
    spelling = (FLAT_SPELLINGS if prefer_flats else SHARP_SPELLINGS)[pitch_class]
    step = spelling[0]
    accidental = spelling[1:] if len(spelling) > 1 else ""
    alter = {"#": 1, "b": -1}.get(accidental, 0)
    return step, alter, octave


def _spell_pitch_class(pitch_class: int, prefer_flats: bool) -> tuple[str, int]:
    spelling = (FLAT_SPELLINGS if prefer_flats else SHARP_SPELLINGS)[pitch_class % 12]
    step = spelling[0]
    accidental = spelling[1:] if len(spelling) > 1 else ""
    alter = {"#": 1, "b": -1}.get(accidental, 0)
    return step, alter


def _pitch_name_to_class(pitch_name: str) -> int | None:
    match = re.fullmatch(r"([A-Ga-g])([#b-]?)", (pitch_name or "").strip())
    if not match:
        return None

    step = match.group(1).upper()
    accidental = match.group(2)
    alter = {"#": 1, "b": -1, "-": -1, "": 0}[accidental]
    return (PITCH_CLASSES[step] + alter) % 12


def _target_key_label(target_key) -> str:
    tonic = getattr(getattr(target_key, "tonic", None), "name", None)
    if tonic is None:
        tonic = str(target_key).strip().split()[0] if str(target_key).strip() else "C"
    return tonic.replace("-", "b")


def _target_key_name(target_key) -> str:
    tonic = _target_key_label(target_key)
    mode = getattr(target_key, "mode", "")
    return f"{tonic} {mode}".strip()


def _transpose_pitch_name(pitch_name: str, semitones: int, prefer_flats: bool) -> str | None:
    pitch_class = _pitch_name_to_class(pitch_name)
    if pitch_class is None:
        return None

    step, alter = _spell_pitch_class(pitch_class + semitones, prefer_flats)
    accidental = {1: "#", -1: "b"}.get(alter, "")
    return f"{step}{accidental}"


def _transpose_chord_text(text: str, semitones: int, prefer_flats: bool) -> str | None:
    stripped = (text or "").strip()
    match = CHORD_TEXT_PATTERN.fullmatch(stripped)
    if not match:
        return None

    root = _transpose_pitch_name(match.group(1), semitones, prefer_flats)
    if root is None:
        return None

    suffix = match.group(2) or ""
    bass = match.group(3)
    transposed = f"{root}{suffix}"
    if bass:
        new_bass = _transpose_pitch_name(bass, semitones, prefer_flats)
        if new_bass is None:
            return None
        transposed = f"{transposed}/{new_bass}"

    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()) :]
    return f"{leading}{transposed}{trailing}"


def _set_text(parent, child_name: str, value: str):
    child = _find_child(parent, child_name)
    if child is None:
        child = ET.SubElement(parent, child_name)
    child.text = value
    return child


def _update_harmony_pitch(parent, prefix: str, semitones: int, prefer_flats: bool) -> bool:
    step_element = _find_child(parent, f"{prefix}-step")
    if step_element is None:
        return False

    step = (step_element.text or "").strip().upper()
    if step not in PITCH_CLASSES:
        return False

    alter_element = _find_child(parent, f"{prefix}-alter")
    try:
        alter = int(float((alter_element.text if alter_element is not None else "0") or "0"))
    except ValueError:
        return False

    new_step, new_alter = _spell_pitch_class(PITCH_CLASSES[step] + alter + semitones, prefer_flats)
    step_element.text = new_step
    if new_alter:
        _set_text(parent, f"{prefix}-alter", str(new_alter))
    elif alter_element is not None:
        parent.remove(alter_element)

    return True


def _transpose_harmonies(root, semitones: int, prefer_flats: bool) -> int:
    updated_count = 0
    for harmony in _iter_elements(root, "harmony"):
        updated = False
        root_element = _find_child(harmony, "root")
        if root_element is not None:
            updated = _update_harmony_pitch(root_element, "root", semitones, prefer_flats) or updated

        bass_element = _find_child(harmony, "bass")
        if bass_element is not None:
            updated = _update_harmony_pitch(bass_element, "bass", semitones, prefer_flats) or updated

        if updated:
            updated_count += 1

    return updated_count


def _replace_key_label_text(text: str, target_label: str) -> tuple[str, bool]:
    updated = False

    def replace_key_label(match):
        nonlocal updated
        updated = True
        return f"{match.group(1)}{target_label}{match.group(3)}"

    return KEY_LABEL_PATTERN.sub(replace_key_label, text, count=1), updated


def _update_visible_text(root, semitones: int, target_key, prefer_flats: bool) -> tuple[int, int, int]:
    target_label = _target_key_label(target_key)
    key_label_count = 0
    chord_text_count = 0
    metadata_count = 0

    for element in root.iter():
        local_name = _local_name(element.tag)
        if local_name not in VISIBLE_TEXT_ELEMENTS and local_name not in METADATA_TEXT_ELEMENTS:
            continue
        if element.text is None:
            continue

        original = element.text
        updated, key_updated = _replace_key_label_text(original, target_label)
        if key_updated:
            element.text = updated
            if local_name in METADATA_TEXT_ELEMENTS:
                metadata_count += 1
            else:
                key_label_count += 1
            continue

        if local_name not in VISIBLE_TEXT_ELEMENTS:
            continue

        chord_text = _transpose_chord_text(original, semitones, prefer_flats)
        if chord_text is not None and chord_text != original:
            element.text = chord_text
            chord_text_count += 1

    return key_label_count, chord_text_count, metadata_count


def _qualified_child_name(parent, child_name: str) -> str:
    if parent.tag.startswith("{"):
        namespace = parent.tag.split("}", 1)[0][1:]
        return f"{{{namespace}}}{child_name}"
    return child_name


def _measure_markers(measure) -> set[str]:
    markers = set()
    for element in measure.iter():
        local_name = _local_name(element.tag)
        if local_name in {"backup", "forward"}:
            markers.add("backup/forward")
        if local_name == "repeat":
            markers.add("repeat")
        if local_name == "ending":
            markers.add("ending")
        if local_name == "multiple-rest":
            markers.add("multi-measure rest")
    return markers


def _active_time_signature(measure, current: tuple[int, int | None, int | None]) -> tuple[int, int | None, int | None]:
    divisions, beats, beat_type = current
    attributes = _find_child(measure, "attributes")
    if attributes is None:
        return current

    divisions_element = _find_child(attributes, "divisions")
    if divisions_element is not None:
        try:
            divisions = int((divisions_element.text or "").strip())
        except ValueError:
            pass

    time_element = _find_child(attributes, "time")
    if time_element is not None:
        beats_element = _find_child(time_element, "beats")
        beat_type_element = _find_child(time_element, "beat-type")
        try:
            beats = int((beats_element.text or "").strip()) if beats_element is not None else beats
            beat_type = int((beat_type_element.text or "").strip()) if beat_type_element is not None else beat_type
        except ValueError:
            pass

    return divisions, beats, beat_type


def _active_staff_count(measure, current: int) -> int:
    attributes = _find_child(measure, "attributes")
    if attributes is not None:
        staves_element = _find_child(attributes, "staves")
        if staves_element is not None:
            try:
                return max(1, int((staves_element.text or "").strip()))
            except ValueError:
                pass

    staff_numbers = []
    for note_element in _find_children(measure, "note"):
        staff_element = _find_child(note_element, "staff")
        if staff_element is None:
            continue
        try:
            staff_numbers.append(int((staff_element.text or "").strip()))
        except ValueError:
            continue

    return max([current, *staff_numbers])


def _part_staff_count(part) -> int:
    count = 1
    for measure in _find_children(part, "measure"):
        count = max(count, _active_staff_count(measure, count))
    return count


def _measure_expected_duration(divisions: int, beats: int | None, beat_type: int | None) -> int | None:
    if divisions <= 0 or beats is None or beat_type is None or beats <= 0 or beat_type <= 0:
        return None
    expected = divisions * beats * 4 / beat_type
    if expected != int(expected):
        return None
    return int(expected)


def _measure_actual_duration(measure) -> dict:
    total = 0
    has_content = False
    voices = set()
    staff_durations = {}
    voice_durations = {}
    staves_found = set()
    valid = True
    for child in list(measure):
        if _local_name(child.tag) != "note":
            continue
        if _find_child(child, "grace") is not None:
            continue

        duration_element = _find_child(child, "duration")
        if duration_element is None:
            continue

        has_content = True
        voice_element = _find_child(child, "voice")
        voice_number = (voice_element.text or "").strip() if voice_element is not None else "1"
        if voice_element is not None and (voice_element.text or "").strip():
            voices.add(voice_number)

        if _find_child(child, "chord") is not None:
            continue

        staff_element = _find_child(child, "staff")
        staff_number = (staff_element.text or "").strip() if staff_element is not None else "1"
        staves_found.add(staff_number)

        try:
            duration = int(float((duration_element.text or "").strip()))
            total += duration
            staff_durations[staff_number] = staff_durations.get(staff_number, 0) + duration
            voice_durations[voice_number] = voice_durations.get(voice_number, 0) + duration
        except ValueError:
            valid = False

    return {
        "actual_duration": total,
        "has_content": has_content,
        "voices": sorted(voices),
        "staff_durations": staff_durations,
        "voice_durations": voice_durations,
        "staves_found": sorted(staves_found),
        "valid": valid,
    }


def _duration_type(duration: int, divisions: int) -> str | None:
    if divisions <= 0:
        return None
    ratios = {
        divisions * 4: "whole",
        divisions * 2: "half",
        divisions: "quarter",
        divisions // 2 if divisions % 2 == 0 else -1: "eighth",
        divisions // 4 if divisions % 4 == 0 else -1: "16th",
    }
    return ratios.get(duration)


def _append_backup(measure, duration: int) -> None:
    backup_element = ET.Element(_qualified_child_name(measure, "backup"))
    duration_element = ET.SubElement(backup_element, _qualified_child_name(measure, "duration"))
    duration_element.text = str(duration)
    measure.append(backup_element)


def _append_padding_rest(
    measure,
    duration: int,
    divisions: int,
    staff_number: int | None = None,
    *,
    measure_rest: bool = False,
) -> None:
    note_element = ET.Element(_qualified_child_name(measure, "note"))
    rest_element = ET.SubElement(note_element, _qualified_child_name(measure, "rest"))
    if measure_rest:
        rest_element.attrib["measure"] = "yes"
    duration_element = ET.SubElement(note_element, _qualified_child_name(measure, "duration"))
    duration_element.text = str(duration)
    voice_element = ET.SubElement(note_element, _qualified_child_name(measure, "voice"))
    voice_element.text = "1"
    rest_type = "whole" if measure_rest else _duration_type(duration, divisions)
    if rest_type:
        type_element = ET.SubElement(note_element, _qualified_child_name(measure, "type"))
        type_element.text = rest_type
    if staff_number is not None:
        staff_element = ET.SubElement(note_element, _qualified_child_name(measure, "staff"))
        staff_element.text = str(staff_number)
    measure.append(note_element)


def _compact_measure_list(measure_numbers: list[str]) -> list[str]:
    numeric = []
    other = []
    for number in dict.fromkeys(measure_numbers):
        try:
            numeric.append(int(str(number)))
        except ValueError:
            other.append(str(number))

    numeric.sort()
    ranges = []
    index = 0
    while index < len(numeric):
        start = numeric[index]
        end = start
        while index + 1 < len(numeric) and numeric[index + 1] == end + 1:
            index += 1
            end = numeric[index]
        ranges.append(str(start) if start == end else f"{start}-{end}")
        index += 1

    return ranges + other


def _measure_contains_real_music(measure) -> bool:
    for note_element in _find_children(measure, "note"):
        if _find_child(note_element, "rest") is None:
            return True
    return False


def _clear_rest_timeline(measure) -> None:
    for child in list(measure):
        local_name = _local_name(child.tag)
        if local_name in {"backup", "forward"}:
            measure.remove(child)
            continue
        if local_name == "note" and _find_child(child, "rest") is not None:
            measure.remove(child)


def _rebuild_empty_staff_rests(measure, expected: int, divisions: int, staff_count: int) -> int:
    _clear_rest_timeline(measure)
    for staff_number in range(1, staff_count + 1):
        _append_padding_rest(measure, expected, divisions, staff_number, measure_rest=True)
        if staff_number < staff_count:
            _append_backup(measure, expected)
    return staff_count


def _detect_and_repair_duplicate_measures(root) -> dict:
    report = {
        "duplicate_measures_found": 0,
        "duplicate_measures_removed": 0,
        "duplicates": [],
        "errors": [],
    }

    for part in _iter_elements(root, "part"):
        part_id = part.attrib.get("id", "")
        by_number = {}
        for measure in _find_children(part, "measure"):
            number = measure.attrib.get("number", "")
            if not number:
                continue
            by_number.setdefault(number, []).append(measure)

        for number, measures in by_number.items():
            if len(measures) <= 1:
                continue

            report["duplicate_measures_found"] += len(measures) - 1
            duplicate_entry = {
                "part_id": part_id,
                "measure_number": number,
                "duplicate_count": len(measures),
                "removed_count": 0,
            }

            keep = next((measure for measure in measures if _measure_contains_real_music(measure)), measures[0])
            for measure in measures:
                if measure is keep:
                    continue
                if _measure_contains_real_music(measure):
                    continue
                part.remove(measure)
                duplicate_entry["removed_count"] += 1
                report["duplicate_measures_removed"] += 1

            remaining = [measure for measure in _find_children(part, "measure") if measure.attrib.get("number", "") == number]
            if len(remaining) > 1:
                report["errors"].append(
                    f"Part {part_id or '?'} measure {number} appears {len(remaining)} times."
                )

            report["duplicates"].append(duplicate_entry)

    return report


def _repair_measure_durations(root) -> dict:
    report = {
        "total_measures_checked": 0,
        "incomplete_measures_found": 0,
        "measures_repaired": 0,
        "measures_skipped_as_intentional": 0,
        "empty_measures_found": 0,
        "empty_staff_measures_repaired": 0,
        "bad_measures": [],
        "staff_duration_validation": [],
        "voice_duration_validation": [],
        "manual_review_measures": [],
        "errors": [],
    }

    for part in _iter_elements(root, "part"):
        active = (1, None, None)
        active_staff_count = _part_staff_count(part)
        measures = _find_children(part, "measure")
        for index, measure in enumerate(measures):
            active = _active_time_signature(measure, active)
            active_staff_count = _active_staff_count(measure, active_staff_count)
            expected = _measure_expected_duration(*active)
            number = measure.attrib.get("number", "?")
            marker_reasons = _measure_markers(measure)
            duration_info = _measure_actual_duration(measure)
            actual = duration_info["actual_duration"]
            voices = duration_info["voices"]
            staff_durations = duration_info["staff_durations"]
            expected_total = None if expected is None else expected * active_staff_count
            diagnostic = {
                "measure_number": number,
                "expected_duration": expected,
                "actual_duration": actual,
                "expected_total_duration": expected_total,
                "missing_duration": None if expected_total is None else expected_total - actual,
                "staff_count": active_staff_count,
                "staff_durations": staff_durations,
                "voices_found": voices,
                "backups_forwards_found": "backup/forward" in marker_reasons,
                "rests_added": False,
                "rests_added_count": 0,
                "empty_staffs_repaired": [],
                "skip_reason": "",
            }

            if expected is None:
                diagnostic["skip_reason"] = "no time signature"
                report["bad_measures"].append(diagnostic)
                report["manual_review_measures"].append(number)
                continue

            report["total_measures_checked"] += 1
            if marker_reasons:
                diagnostic["skip_reason"] = ", ".join(sorted(marker_reasons))
                report["measures_skipped_as_intentional"] += 1
                if expected_total is not None and actual != expected_total:
                    report["incomplete_measures_found"] += 1
                    report["bad_measures"].append(diagnostic)
                    report["manual_review_measures"].append(number)
                continue

            if not duration_info["valid"]:
                diagnostic["skip_reason"] = "duration mismatch too complex"
                report["measures_skipped_as_intentional"] += 1
                report["bad_measures"].append(diagnostic)
                report["manual_review_measures"].append(number)
                continue

            if len(voices) > 1:
                diagnostic["skip_reason"] = "multi-voice"
                report["measures_skipped_as_intentional"] += 1
                if expected_total is not None and actual != expected_total:
                    report["incomplete_measures_found"] += 1
                    report["bad_measures"].append(diagnostic)
                    report["manual_review_measures"].append(number)
                continue

            if expected_total is None:
                continue

            has_real_music = _measure_contains_real_music(measure)
            staff_incomplete = any(
                staff_durations.get(str(staff_number), 0) != expected
                for staff_number in range(1, active_staff_count + 1)
            )
            if active_staff_count > 1 and not has_real_music and staff_incomplete:
                report["incomplete_measures_found"] += 1
                report["empty_measures_found"] += 1
                rests_added = _rebuild_empty_staff_rests(measure, expected, active[0], active_staff_count)
                diagnostic["rests_added"] = True
                diagnostic["rests_added_count"] = rests_added
                diagnostic["empty_staffs_repaired"] = [str(number) for number in range(1, active_staff_count + 1)]
                diagnostic["skip_reason"] = ""
                report["empty_staff_measures_repaired"] += rests_added
                report["bad_measures"].append(diagnostic)
                report["measures_repaired"] += 1
                continue

            if actual == expected_total:
                continue

            if actual < expected_total:
                report["incomplete_measures_found"] += 1
                if actual == 0 or (active_staff_count > 1 and not has_real_music):
                    report["empty_measures_found"] += 1
                if index == 0 and actual > 0:
                    diagnostic["skip_reason"] = "pickup-like"
                    report["measures_skipped_as_intentional"] += 1
                    report["bad_measures"].append(diagnostic)
                    continue

                rests_added = 0
                rest_specs = []
                for staff_number in range(1, active_staff_count + 1):
                    staff_key = str(staff_number)
                    staff_actual = staff_durations.get(staff_key, 0)
                    if staff_actual >= expected:
                        continue
                    rest_specs.append((staff_number, expected - staff_actual, staff_actual == 0))

                for rest_index, (staff_number, rest_duration, is_empty_staff) in enumerate(rest_specs):
                    _append_padding_rest(
                        measure,
                        rest_duration,
                        active[0],
                        staff_number if active_staff_count > 1 else None,
                        measure_rest=is_empty_staff,
                    )
                    if active_staff_count > 1 and rest_index < len(rest_specs) - 1:
                        _append_backup(measure, rest_duration)
                    rests_added += 1
                    if is_empty_staff:
                        report["empty_staff_measures_repaired"] += 1
                        diagnostic["empty_staffs_repaired"].append(str(staff_number))
                diagnostic["rests_added"] = True
                diagnostic["rests_added_count"] = rests_added
                diagnostic["skip_reason"] = ""
                report["bad_measures"].append(diagnostic)
                report["measures_repaired"] += 1
                continue

            report["incomplete_measures_found"] += 1
            diagnostic["skip_reason"] = "duration mismatch too complex"
            report["bad_measures"].append(diagnostic)
            report["manual_review_measures"].append(number)
            report["errors"].append(f"Measure {number} is longer than the active time signature.")

    report["staff_duration_validation"] = _validate_staff_duration_totals(root)
    report["voice_duration_validation"] = _validate_voice_duration_totals(root)
    report["manual_review_measures"] = _compact_measure_list(report["manual_review_measures"])
    return report


def _validate_staff_duration_totals(root) -> list[dict]:
    diagnostics = []
    for part in _iter_elements(root, "part"):
        active = (1, None, None)
        active_staff_count = 1
        for measure in _find_children(part, "measure"):
            active = _active_time_signature(measure, active)
            active_staff_count = _active_staff_count(measure, active_staff_count)
            expected = _measure_expected_duration(*active)
            if expected is None:
                continue

            duration_info = _measure_actual_duration(measure)
            staff_durations = duration_info["staff_durations"]
            for staff_number in range(1, active_staff_count + 1):
                found = staff_durations.get(str(staff_number), 0)
                if found != expected:
                    diagnostics.append(
                        {
                            "part_id": part.attrib.get("id", ""),
                            "measure_number": measure.attrib.get("number", "?"),
                            "staff_number": str(staff_number),
                            "expected_duration": expected,
                            "found_duration": found,
                        }
                    )
    return diagnostics


def _validate_voice_duration_totals(root) -> list[dict]:
    diagnostics = []
    for part in _iter_elements(root, "part"):
        active = (1, None, None)
        active_staff_count = _part_staff_count(part)
        for measure in _find_children(part, "measure"):
            active = _active_time_signature(measure, active)
            active_staff_count = _active_staff_count(measure, active_staff_count)
            expected = _measure_expected_duration(*active)
            if expected is None:
                continue

            duration_info = _measure_actual_duration(measure)
            for voice_number, found in duration_info["voice_durations"].items():
                expected_voice_duration = expected * active_staff_count if active_staff_count > 1 else expected
                if found != expected_voice_duration:
                    diagnostics.append(
                        {
                            "part_id": part.attrib.get("id", ""),
                            "measure_number": measure.attrib.get("number", "?"),
                            "voice_number": str(voice_number),
                            "expected_duration": expected_voice_duration,
                            "found_duration": found,
                        }
                    )
    return diagnostics


def _validate_pitch_step(step: str | None) -> bool:
    return (step or "").strip().upper() in PITCH_CLASSES


def _validate_alter_text(text: str | None) -> bool:
    if text is None or str(text).strip() == "":
        return True
    try:
        int(float(str(text).strip()))
        return True
    except ValueError:
        return False


def _validate_musicxml_tree(root) -> dict:
    errors = []
    harmony_checked = 0
    key_signature_checked = 0
    pitch_checked = 0

    score_part_ids = {
        element.attrib.get("id")
        for element in _iter_elements(root, "score-part")
        if element.attrib.get("id")
    }
    part_ids = {
        element.attrib.get("id")
        for element in _iter_elements(root, "part")
        if element.attrib.get("id")
    }
    missing_parts = sorted(score_part_ids - part_ids)
    extra_parts = sorted(part_ids - score_part_ids)
    if missing_parts:
        errors.append(f"Part list references missing parts: {', '.join(missing_parts)}")
    if extra_parts:
        errors.append(f"Parts missing from part list: {', '.join(extra_parts)}")

    for measure in _iter_elements(root, "measure"):
        if not (measure.attrib.get("number") or "").strip():
            errors.append("A measure is missing its number.")

    for part in _iter_elements(root, "part"):
        seen = {}
        part_id = part.attrib.get("id", "")
        for measure in _find_children(part, "measure"):
            number = measure.attrib.get("number", "")
            if not number:
                continue
            seen[number] = seen.get(number, 0) + 1
        for number, count in seen.items():
            if count > 1:
                errors.append(f"Part {part_id or '?'} has duplicate measure number {number} ({count} copies).")

    for key_element in _iter_elements(root, "key"):
        fifths_element = _find_child(key_element, "fifths")
        if fifths_element is None:
            errors.append("A key signature is missing fifths.")
            continue
        try:
            fifths = int((fifths_element.text or "").strip())
            if fifths < -7 or fifths > 7:
                errors.append(f"Key signature fifths is outside MusicXML's usual range: {fifths}.")
        except ValueError:
            errors.append("A key signature has non-numeric fifths.")
        key_signature_checked += 1

    for pitch in _iter_elements(root, "pitch"):
        pitch_checked += 1
        step_element = _find_child(pitch, "step")
        octave_element = _find_child(pitch, "octave")
        alter_element = _find_child(pitch, "alter")
        if step_element is None or not _validate_pitch_step(step_element.text):
            errors.append("A note pitch has an invalid step.")
        if octave_element is None:
            errors.append("A note pitch is missing octave.")
        else:
            try:
                int((octave_element.text or "").strip())
            except ValueError:
                errors.append("A note pitch has a non-numeric octave.")
        if alter_element is not None and not _validate_alter_text(alter_element.text):
            errors.append("A note pitch has a non-numeric alter value.")

    for harmony in _iter_elements(root, "harmony"):
        harmony_checked += 1
        root_element = _find_child(harmony, "root")
        if root_element is not None:
            root_step = _find_child(root_element, "root-step")
            root_alter = _find_child(root_element, "root-alter")
            if root_step is None or not _validate_pitch_step(root_step.text):
                errors.append("A harmony root has an invalid root-step.")
            if root_alter is not None and not _validate_alter_text(root_alter.text):
                errors.append("A harmony root has a non-numeric root-alter.")
        bass_element = _find_child(harmony, "bass")
        if bass_element is not None:
            bass_step = _find_child(bass_element, "bass-step")
            bass_alter = _find_child(bass_element, "bass-alter")
            if bass_step is None or not _validate_pitch_step(bass_step.text):
                errors.append("A harmony bass has an invalid bass-step.")
            if bass_alter is not None and not _validate_alter_text(bass_alter.text):
                errors.append("A harmony bass has a non-numeric bass-alter.")

    return {
        "xml_valid": True,
        "harmony_elements_checked": harmony_checked,
        "key_signature_elements_checked": key_signature_checked,
        "note_pitch_elements_checked": pitch_checked,
        "errors": errors,
    }


def _musicxml_version_ok(root) -> bool:
    try:
        return float(root.attrib.get("version", "4.0")) <= 4.0
    except ValueError:
        return False


def _validate_and_repair_musicxml(
    output_path: Path,
    *,
    metadata_updated: int,
    measure_report: dict | None = None,
    duplicate_report: dict | None = None,
) -> dict:
    validation = {
        "xml_valid": False,
        "musicxml_4_0": False,
        "harmony_elements_checked": 0,
        "metadata_updated": metadata_updated,
        "measure_validation": measure_report or {
            "total_measures_checked": 0,
            "incomplete_measures_found": 0,
            "measures_repaired": 0,
            "measures_skipped_as_intentional": 0,
            "errors": [],
        },
        "duplicate_measure_validation": duplicate_report or {
            "duplicate_measures_found": 0,
            "duplicate_measures_removed": 0,
            "duplicates": [],
            "errors": [],
        },
        "musescore_compatibility_check": "failed",
        "repair_used": False,
        "errors": [],
    }

    if output_path.suffix.lower() == ".mxl":
        validation["errors"].append("Compressed .mxl validation is limited to the direct XML transposition pass.")
        return validation

    try:
        root = ET.parse(output_path).getroot()
        validation.update(_validate_musicxml_tree(root))
        validation["musicxml_4_0"] = _musicxml_version_ok(root)
    except ET.ParseError as exc:
        validation["errors"].append(f"XML parse error: {exc}")
        return validation

    validation["errors"].extend(validation["measure_validation"].get("errors") or [])
    validation["errors"].extend(validation["duplicate_measure_validation"].get("errors") or [])

    if validation["errors"]:
        return validation

    try:
        converter, _interval, _key = _require_music21()
        with tempfile.TemporaryDirectory(prefix="repair-") as tmpdir:
            repair_path = Path(tmpdir) / output_path.name
            score = converter.parse(str(output_path))
            score.write("musicxml", fp=str(repair_path))
            if repair_path.is_file() and repair_path.stat().st_size > 0:
                repair_tree = ET.parse(repair_path)
                repair_root = repair_tree.getroot()
                repair_duplicates = _detect_and_repair_duplicate_measures(repair_root)
                if repair_duplicates["duplicate_measures_found"]:
                    validation["duplicate_measure_validation"]["duplicate_measures_found"] += repair_duplicates[
                        "duplicate_measures_found"
                    ]
                    validation["duplicate_measure_validation"]["duplicate_measures_removed"] += repair_duplicates[
                        "duplicate_measures_removed"
                    ]
                    validation["duplicate_measure_validation"]["duplicates"].extend(repair_duplicates["duplicates"])
                    validation["duplicate_measure_validation"]["errors"].extend(repair_duplicates["errors"])
                post_export_measure_report = _repair_measure_durations(repair_root)
                validation["measure_validation"]["empty_measures_found"] += post_export_measure_report.get(
                    "empty_measures_found", 0
                )
                validation["measure_validation"]["empty_staff_measures_repaired"] += post_export_measure_report.get(
                    "empty_staff_measures_repaired", 0
                )
                validation["measure_validation"]["staff_duration_validation"] = post_export_measure_report.get(
                    "staff_duration_validation", []
                )
                validation["measure_validation"]["voice_duration_validation"] = post_export_measure_report.get(
                    "voice_duration_validation", []
                )
                repair_tree.write(repair_path, encoding="utf-8", xml_declaration=True)
                if validation["duplicate_measure_validation"]["errors"]:
                    validation["errors"].extend(validation["duplicate_measure_validation"]["errors"])
                    return validation
                shutil.copyfile(repair_path, output_path)
                validation["repair_used"] = True
                validation["musescore_compatibility_check"] = "passed"
    except Exception as exc:
        validation["errors"].append(f"MuseScore compatibility repair failed: {exc}")

    return validation


def _transpose_musicxml_directly(
    input_path: Path,
    output_path: Path,
    semitones: int | float,
    target_key,
    source_key_name: str = "unknown",
) -> Path:
    global LAST_TRANSPOSITION_REPORT

    if input_path.suffix.lower() == ".mxl":
        return _transpose_mxl_directly(input_path, output_path, semitones, target_key, source_key_name=source_key_name)

    ET.register_namespace("", "http://www.musicxml.org/ns/musicxml")
    tree = ET.parse(input_path)
    root = tree.getroot()
    semitone_delta = int(round(semitones))
    prefer_flats = int(target_key.sharps) < 0
    note_count = 0
    key_signature_count = 0
    if _local_name(root.tag) in {"score-partwise", "score-timewise"}:
        root.attrib["version"] = "4.0"

    for pitch in _iter_elements(root, "pitch"):
        step_element = _find_child(pitch, "step")
        octave_element = _find_child(pitch, "octave")
        if step_element is None or octave_element is None:
            continue

        alter_element = _find_child(pitch, "alter")
        step = (step_element.text or "").strip().upper()
        if step not in PITCH_CLASSES:
            continue

        try:
            alter = int(float((alter_element.text if alter_element is not None else "0") or "0"))
            octave = int((octave_element.text or "").strip())
        except ValueError:
            continue

        new_step, new_alter, new_octave = _spell_pitch(
            _pitch_to_midi(step, alter, octave) + semitone_delta,
            prefer_flats,
        )
        step_element.text = new_step
        octave_element.text = str(new_octave)

        if new_alter:
            _set_text(pitch, "alter", str(new_alter))
        elif alter_element is not None:
            pitch.remove(alter_element)
        note_count += 1

    for key_element in _iter_elements(root, "key"):
        fifths_element = _find_child(key_element, "fifths")
        if fifths_element is not None:
            fifths_element.text = str(int(target_key.sharps))
            key_signature_count += 1

    harmony_count = _transpose_harmonies(root, semitone_delta, prefer_flats)
    key_label_count, chord_text_count, metadata_count = _update_visible_text(root, semitone_delta, target_key, prefer_flats)
    duplicate_report = _detect_and_repair_duplicate_measures(root)
    measure_report = _repair_measure_durations(root)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    validation_report = _validate_and_repair_musicxml(
        output_path,
        metadata_updated=metadata_count,
        measure_report=measure_report,
        duplicate_report=duplicate_report,
    )
    LAST_TRANSPOSITION_REPORT = {
        "source_key": source_key_name,
        "target_key": _target_key_name(target_key),
        "interval": semitone_delta,
        "note_transposition_count": note_count,
        "key_signature_update_count": key_signature_count,
        "harmony_chord_update_count": harmony_count + chord_text_count,
        "visible_key_label_update_count": key_label_count,
        "metadata_update_count": metadata_count,
        "output_validation": validation_report,
    }
    return output_path


def _get_mxl_rootfile(zip_file: zipfile.ZipFile) -> str:
    try:
        container_xml = zip_file.read("META-INF/container.xml")
    except KeyError as exc:
        raise TranspositionError("Compressed MusicXML file is missing META-INF/container.xml.") from exc

    container_root = ET.fromstring(container_xml)
    for rootfile in _iter_elements(container_root, "rootfile"):
        full_path = rootfile.attrib.get("full-path")
        if full_path:
            return full_path

    raise TranspositionError("Compressed MusicXML file does not identify a root MusicXML document.")


def _transpose_mxl_directly(
    input_path: Path,
    output_path: Path,
    semitones: int | float,
    target_key,
    source_key_name: str = "unknown",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(input_path, "r") as source_zip:
        rootfile_path = _get_mxl_rootfile(source_zip)
        root_xml = source_zip.read(rootfile_path)
        temp_xml_path = output_path.with_suffix(".fallback.musicxml")
        temp_xml_path.write_bytes(root_xml)
        _transpose_musicxml_directly(temp_xml_path, temp_xml_path, semitones, target_key, source_key_name=source_key_name)
        transposed_xml = temp_xml_path.read_bytes()
        temp_xml_path.unlink(missing_ok=True)

        if output_path.suffix.lower() in {".musicxml", ".xml"}:
            output_path.write_bytes(transposed_xml)
            return output_path

        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as output_zip:
            for info in source_zip.infolist():
                if info.filename == rootfile_path:
                    output_zip.writestr(info, transposed_xml)
                else:
                    output_zip.writestr(info, source_zip.read(info.filename))

    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transpose local sheet music files to a target key.")
    parser.add_argument("--input", required=True, help="Path to the source .musicxml, .xml, or .pdf file.")
    parser.add_argument("--output", required=True, help="Path for the transposed output file.")
    parser.add_argument("--target-key", required=True, help="Target key, such as 'D major' or 'E minor'.")
    parser.add_argument(
        "--output-format",
        choices=["musicxml", "pdf"],
        default="musicxml",
        help="Output format. PDF export is a placeholder until an export engine is installed.",
    )
    parser.add_argument("--audiveris-path", default="", help="Path to the Audiveris executable for PDF import.")
    parser.add_argument("--musescore-path", default="", help="Path to the MuseScore executable for PDF export.")
    parser.add_argument("--temp-dir", default="", help="App-owned temp directory for intermediate files.")
    parser.add_argument("--detect-key-only", action="store_true", help="Detect and print the original key only.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.detect_key_only:
            print(detect_key_name(args.input))
            return 0

        from python.pipeline import run_pipeline

        output_path = run_pipeline(
            args.input,
            args.output,
            args.target_key,
            args.output_format,
            audiveris_path=args.audiveris_path,
            musescore_path=args.musescore_path,
            temp_dir=args.temp_dir,
            progress=emit_stage,
        )
    except TranspositionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
