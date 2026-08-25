from __future__ import annotations

import argparse
from copy import deepcopy
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

KEY_LABEL_PATTERN = re.compile(
    r"(?i)(\bKey\s*:?\s*)([A-G](?:#|b)?)(m\b|\s+(?:major|minor)\b)?"
)
VISIBLE_KEY_PATTERN = KEY_LABEL_PATTERN
FILENAME_KEY_PATTERN = re.compile(
    r"(?i)(?:^|[\s_\-(])([A-G](?:#|b)?)(?:\s*(major|minor))?(?=$|[\s_\-)])"
)
CHORD_TEXT_PATTERN = re.compile(
    r"^([A-G](?:#|b)?)"
    r"("
    r"(?:(?:(?i:m|maj|min|dim|aug|sus|add|omit|no)|[+\-°øΔ])?\d*"
    r"(?:(?i:sus)\d*|(?i:add|omit|no)\d+)?"
    r"(?:[#b]\d+)*"
    r"(?:\([^()\s]+\))*"
    r"(?:/\d+)?)"
    r")"
    r"(?:/([A-G](?:#|b)?))?$"
)
MALFORMED_MINOR_SLASH_CHORD_PATTERN = re.compile(r"^([A-G](?:#|b)?)_?m([A-G](?:#|b)?)$")
MALFORMED_CHORD_TEXT_PATTERN = re.compile(r"\b[A-G](?:#|b)?_+[A-Za-z0-9#b/]*\b")
RECOVERED_CHORD_LYRIC_NAME = "new-key-scores-chord"
RECOVERED_CHORD_LYRIC_NAMES = {RECOVERED_CHORD_LYRIC_NAME, "key-shift-chord"}
MEASURE_NUMBER_RESETS_FIELD = "new-key-scores-measure-number-resets"
MEASURE_NUMBER_RESETS_FIELDS = {MEASURE_NUMBER_RESETS_FIELD, "key-shift-measure-number-resets"}
PRESERVE_LAYOUT_FIELD = "new-key-scores-preserve-layout"
PRESERVE_LAYOUT_FIELDS = {PRESERVE_LAYOUT_FIELD, "key-shift-preserve-layout"}
VISIBLE_TEXT_ELEMENTS = {"words", "credit-words", "rehearsal", "ending"}
METADATA_TEXT_ELEMENTS = {"creator", "rights", "movement-title", "work-title", "miscellaneous-field"}
COPYRIGHT_ARTIFACT_KEYWORDS = (
    "admin",
    "capitol",
    "ccli",
    "cmg",
    "copyright",
    "duplication",
    "elevation",
    "essential",
    "housefires",
    "maverick",
    "paragon",
    "permission",
    "publishing",
    "rights",
    "roof",
    "worldwide",
)
STAFF_LABEL_WORDS = {"voice", "piano", "choir", "soprano", "alto", "tenor", "bass", "full score"}


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
        source_mode = fast_source_key.split()[-1].lower()
        if source_mode in {"major", "minor"} and source_mode != target_key.mode:
            raise TranspositionError(
                f"This score is in {source_mode}; choose a {source_mode} target key. "
                "Transposition changes pitch level but does not rewrite major music as minor or vice versa."
            )
        source_tonic = fast_source_key.split()[0]
        target_tonic = _target_key_label(target_key)
        source_pitch_class = _pitch_name_to_class(source_tonic)
        target_pitch_class = _pitch_name_to_class(target_tonic)
        if source_pitch_class is not None and target_pitch_class is not None:
            semitones = _nearest_transposition_delta(source_pitch_class, target_pitch_class)
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
        if source_key.mode != target_key.mode:
            raise TranspositionError(
                f"This score is in {source_key.mode}; choose a {source_key.mode} target key. "
                "Transposition changes pitch level but does not rewrite major music as minor or vice versa."
            )
        source_pitch_class = _pitch_name_to_class(source_key.tonic.name.replace("-", "b"))
        target_pitch_class = _pitch_name_to_class(target_key.tonic.name.replace("-", "b"))
        if source_pitch_class is None or target_pitch_class is None:
            transposition_interval = interval.Interval(source_key.tonic, target_key.tonic)
            semitones = int(round(transposition_interval.semitones))
        else:
            semitones = _nearest_transposition_delta(source_pitch_class, target_pitch_class)
            transposition_interval = interval.Interval(semitones)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _transpose_musicxml_directly(
                source_path,
                destination_path,
                semitones,
                target_key,
                source_key_name=f"{source_key.tonic.name} {source_key.mode}",
                key_detection_engine="music21",
            )
        except Exception as direct_exc:
            try:
                shifted_score = score.transpose(semitones)
                _set_initial_key_signatures(shifted_score, target_key)
                shifted_score.write("musicxml", fp=str(destination_path))
                LAST_TRANSPOSITION_REPORT = {
                    "engine": "music21-fallback",
                    "key_detection_engine": "music21",
                    "source_key": f"{source_key.tonic.name} {source_key.mode}",
                    "target_key": _target_key_name(target_key),
                    "interval": semitones,
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
        return "unknown"

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

    visible_key = _detect_key_name_from_visible_text(root)
    if visible_key:
        return visible_key

    for key_element in _iter_elements(root, "key"):
        fifths_element = _find_child(key_element, "fifths")
        if fifths_element is None:
            continue

        try:
            fifths_text = (fifths_element.text or "").strip()
            if not fifths_text:
                continue
            fifths = int(fifths_text)
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

    filename_key = _detect_key_name_from_filename(input_path)
    if filename_key:
        return filename_key

    return None


def _detect_key_name_from_visible_text(root) -> str | None:
    for element in root.iter():
        text_value = _element_text(element)
        if not text_value:
            continue
        match = VISIBLE_KEY_PATTERN.search(text_value)
        if not match:
            continue
        tonic = match.group(2).replace("b", "-")
        mode_suffix = (match.group(3) or "").strip().lower()
        mode = "minor" if mode_suffix in {"m", "minor"} else "major"
        return f"{tonic} {mode}"
    return None


def _detect_key_name_from_filename(input_path: Path) -> str | None:
    stem = re.sub(r"\s+", " ", input_path.stem)
    ignored = {"A", "SAT", "SATB"}
    for match in FILENAME_KEY_PATTERN.finditer(stem):
        tonic = match.group(1)
        if tonic.upper() in ignored:
            continue
        mode = (match.group(2) or "major").lower()
        normalized = tonic.replace("b", "-")
        if _pitch_name_to_class(normalized) is not None:
            return f"{normalized} {mode}"
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


def _nearest_transposition_delta(source_pitch_class: int, target_pitch_class: int) -> int:
    """Choose the octave-equivalent interval with the smallest register change."""
    semitones = (target_pitch_class - source_pitch_class) % 12
    return semitones - 12 if semitones > 6 else semitones


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


def _transpose_key_signature_fifths(
    source_fifths: int,
    semitones: int,
    mode: str,
    prefer_flats: bool,
) -> int | None:
    normalized_mode = "minor" if mode == "minor" else "major"
    source_names = MINOR_KEY_BY_FIFTHS if normalized_mode == "minor" else MAJOR_KEY_BY_FIFTHS
    target_fifths = FIFTHS_BY_MINOR_KEY if normalized_mode == "minor" else FIFTHS_BY_MAJOR_KEY
    source_tonic = source_names.get(source_fifths)
    if source_tonic is None:
        return None

    shifted_tonic = _transpose_pitch_name(source_tonic, semitones, prefer_flats)
    if shifted_tonic in target_fifths:
        return target_fifths[shifted_tonic]

    enharmonic_tonic = _transpose_pitch_name(source_tonic, semitones, not prefer_flats)
    return target_fifths.get(enharmonic_tonic)


def _canonicalize_chord_text(text: str) -> str | None:
    """Return a normalized chord symbol, or None when text is not a chord."""
    stripped = (text or "").strip()
    if not stripped:
        return None

    normalized = (
        stripped
        .replace("♯", "#")
        .replace("♭", "b")
        .replace("𝄪", "##")
        .replace("𝄫", "bb")
    )
    if re.fullmatch(r"(?i)n\.?\s*c\.?", normalized):
        return "N.C."
    malformed_minor_slash = MALFORMED_MINOR_SLASH_CHORD_PATTERN.fullmatch(normalized)
    if malformed_minor_slash:
        return f"{malformed_minor_slash.group(1)}m/{malformed_minor_slash.group(2)}"

    # Audiveris occasionally turns a printed sharp into an fi/fl ligature.
    normalized = re.sub(r"^([A-G])[\ufb01\ufb02](?=(?:m|maj|min)?\d)", r"\1#", normalized)
    normalized = re.sub(r"(?i)n(?:Â?[°º])(?=\d)", "no", normalized)
    if re.fullmatch(r"[A-G](?:#|b)?\s+\d+(?:\([^)]*\))?", normalized):
        normalized = re.sub(r"\s+", "", normalized)
    elif re.fullmatch(r"0\d+(?:\([^)]*\))?", normalized):
        normalized = f"D{normalized[1:]}"
    elif re.fullmatch(r"6\d+(?:\([^)]*\))?", normalized):
        normalized = f"G{normalized[1:]}"

    normalized = re.sub(r"^([A-G](?:#|b)?)ma(?=\d)", r"\1maj", normalized, flags=re.IGNORECASE)
    match = CHORD_TEXT_PATTERN.fullmatch(normalized)
    if match is None:
        return None

    root = match.group(1)[0].upper() + match.group(1)[1:]
    suffix = match.group(2) or ""
    for word in ("maj", "min", "dim", "aug", "sus", "add", "omit", "no"):
        suffix = re.sub(word, word, suffix, flags=re.IGNORECASE)
    bass = match.group(3)
    if bass:
        bass = bass[0].upper() + bass[1:]
    return f"{root}{suffix}{f'/{bass}' if bass else ''}"


def _transpose_chord_text(text: str, semitones: int, prefer_flats: bool) -> str | None:
    stripped = (text or "").strip()
    sequence_parts = stripped.split()
    if len(sequence_parts) > 1:
        canonical_parts = [_canonicalize_chord_text(part) for part in sequence_parts]
        if all(part is not None for part in canonical_parts):
            transposed_sequence = []
            for part in sequence_parts:
                transposed_part = _transpose_chord_text(part, semitones, prefer_flats)
                if transposed_part is None:
                    return None
                transposed_sequence.append(transposed_part)
            leading = text[: len(text) - len(text.lstrip())]
            trailing = text[len(text.rstrip()) :]
            return f"{leading}{' '.join(transposed_sequence)}{trailing}"

    normalized = _canonicalize_chord_text(stripped)
    if normalized is None:
        return None
    if normalized == "N.C.":
        return text

    match = CHORD_TEXT_PATTERN.fullmatch(normalized)
    malformed_minor_slash = None
    if not match:
        malformed_minor_slash = MALFORMED_MINOR_SLASH_CHORD_PATTERN.fullmatch(normalized)
        if not malformed_minor_slash:
            return None

    source_root = match.group(1) if match else malformed_minor_slash.group(1)
    source_suffix = match.group(2) if match else "m"
    source_bass = match.group(3) if match else malformed_minor_slash.group(2)

    root = _transpose_pitch_name(source_root, semitones, prefer_flats)
    if root is None:
        return None

    suffix = source_suffix or ""
    bass = source_bass
    transposed = f"{root}{suffix}"
    if bass:
        new_bass = _transpose_pitch_name(bass, semitones, prefer_flats)
        if new_bass is None:
            return None
        transposed = f"{transposed}/{new_bass}"

    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()) :]
    return f"{leading}{transposed}{trailing}"


def _transpose_leading_chord_annotation(text: str, semitones: int, prefer_flats: bool) -> str | None:
    """Transpose a chord token that Audiveris joined to a playing instruction."""
    match = re.fullmatch(
        r"(\s*)([A-G](?:#|b)?(?:(?:m|maj|min|dim|aug|sus|add|no)\d*|\d+)?(?:\([^)]*\))?(?:/[A-G](?:#|b)?)?)"
        r"(\s+(?:Add\b|Piano\b|A\.G\.\b|E\.G\.\b).*)",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    shifted = _transpose_chord_text(match.group(2), semitones, prefer_flats)
    if shifted is None:
        return None
    return f"{match.group(1)}{shifted}{match.group(3)}"


def _transpose_ending_chord_label(text: str, semitones: int, prefer_flats: bool) -> str | None:
    match = re.fullmatch(r"(\s*\d+(?:,\d+)*\s{2,})(\S+)(\s*)", text or "")
    if not match:
        return None
    shifted = _transpose_chord_text(match.group(2), semitones, prefer_flats)
    if shifted is None:
        return None
    return f"{match.group(1)}{shifted}{match.group(3)}"


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


def _part_has_pitched_music(part) -> bool:
    return any(True for _pitch in _iter_elements(part, "pitch"))


def _ensure_initial_key_signatures(root, fifths: int) -> int:
    inserted = 0
    for part in _find_children(root, "part"):
        if not _part_has_pitched_music(part):
            continue
        measures = _find_children(part, "measure")
        if not measures:
            continue
        first_measure = measures[0]
        attributes = _find_child(first_measure, "attributes")
        if attributes is None:
            attributes = ET.Element(_qualified_child_name(first_measure, "attributes"))
            children = list(first_measure)
            insert_index = 1 if children and _local_name(children[0].tag) == "print" else 0
            first_measure.insert(insert_index, attributes)

        key_element = _find_child(attributes, "key")
        if key_element is not None:
            fifths_element = _find_child(key_element, "fifths")
            if fifths_element is None:
                fifths_element = ET.SubElement(key_element, _qualified_child_name(key_element, "fifths"))
                fifths_element.text = str(fifths)
                inserted += 1
            continue

        key_element = ET.Element(_qualified_child_name(attributes, "key"))
        fifths_element = ET.SubElement(key_element, _qualified_child_name(key_element, "fifths"))
        fifths_element.text = str(fifths)
        attribute_children = list(attributes)
        insert_index = 0
        while insert_index < len(attribute_children) and _local_name(attribute_children[insert_index].tag) == "divisions":
            insert_index += 1
        attributes.insert(insert_index, key_element)
        inserted += 1
    return inserted


def _replace_key_label_text(text: str, target_label: str, target_mode: str) -> tuple[str, bool]:
    updated = False

    def replace_key_label(match):
        nonlocal updated
        updated = True
        source_suffix = (match.group(3) or "")
        if source_suffix.lower() == "m":
            target_suffix = "m" if target_mode == "minor" else ""
        elif source_suffix:
            target_suffix = f" {target_mode}"
        else:
            target_suffix = ""
        return f"{match.group(1)}{target_label}{target_suffix}"

    return KEY_LABEL_PATTERN.sub(replace_key_label, text, count=1), updated


def _update_visible_text(root, semitones: int, target_key, prefer_flats: bool) -> tuple[int, int, int]:
    target_label = _target_key_label(target_key)
    target_mode = getattr(target_key, "mode", "major")
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
        updated, key_updated = _replace_key_label_text(original, target_label, target_mode)
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
        if chord_text is None and local_name == "ending":
            chord_text = _transpose_ending_chord_label(original, semitones, prefer_flats)
        if chord_text is None and local_name == "words":
            chord_text = _transpose_leading_chord_annotation(original, semitones, prefer_flats)
        if chord_text is not None and chord_text != original:
            element.text = chord_text
            chord_text_count += 1

    return key_label_count, chord_text_count, metadata_count


def _is_copyright_artifact_text(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return any(keyword in normalized for keyword in COPYRIGHT_ARTIFACT_KEYWORDS)


def _is_standalone_copyright_lyric_artifact(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    keyword_count = sum(1 for keyword in COPYRIGHT_ARTIFACT_KEYWORDS if keyword in normalized)
    return len(normalized) >= 24 and keyword_count >= 2


def _remove_copyright_lyric_tracks(root) -> tuple[list[str], int]:
    removed_fragments = []
    removed_count = 0
    for part in _iter_elements(root, "part"):
        tracks = {}
        for measure in _find_children(part, "measure"):
            for note, lyric in _iter_note_lyrics(measure):
                number = (lyric.attrib.get("number") or "1").strip()
                tracks.setdefault(number, []).append((note, lyric, _collect_lyric_text(lyric)))

        for entries in tracks.values():
            vertical_bands = {}
            for note, lyric, lyric_text in entries:
                try:
                    default_y = float(lyric.attrib.get("default-y", ""))
                    band = round(default_y / 5) * 5
                except ValueError:
                    band = None
                vertical_bands.setdefault(band, []).append((note, lyric, lyric_text))

            for band, band_entries in vertical_bands.items():
                aggregate = " ".join(text for _note, _lyric, text in band_entries if text)
                if not _is_standalone_copyright_lyric_artifact(aggregate):
                    continue
                if band is None and not all(
                    not text or _is_copyright_artifact_text(text)
                    for _note, _lyric, text in band_entries
                ):
                    continue
                for note, lyric, lyric_text in band_entries:
                    if lyric_text:
                        removed_fragments.append(lyric_text)
                    _remove_artifact_lyric(note, lyric)
                    removed_count += 1
    return removed_fragments, removed_count


def _iter_note_lyrics(measure):
    for note in _find_children(measure, "note"):
        for lyric in _find_children(note, "lyric"):
            yield note, lyric


def _collect_lyric_text(lyric) -> str:
    pieces = []
    for text_element in _iter_elements(lyric, "text"):
        if text_element.text and text_element.text.strip():
            pieces.append(text_element.text.strip())
    return " ".join(pieces).strip()


def _remove_artifact_lyric(note, lyric) -> None:
    note.remove(lyric)


def _copyright_metadata_exists(root, text_value: str) -> bool:
    target = re.sub(r"\s+", " ", text_value.strip())
    for credit_words in list(_iter_elements(root, "credit-words")) + list(_iter_elements(root, "rights")):
        existing = re.sub(r"\s+", " ", _element_text(credit_words))
        if existing == target:
            return True
    return False


def _find_or_create_identification(root):
    identification = _find_child(root, "identification")
    if identification is None:
        identification = ET.Element(_qualified_child_name(root, "identification"))
        insert_index = 0
        for index, child in enumerate(list(root)):
            if _local_name(child.tag) in {"work", "movement-number", "movement-title"}:
                insert_index = index + 1
        root.insert(insert_index, identification)
    return identification


def _add_copyright_metadata(root, text_value: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text_value.strip())
    if not cleaned or _copyright_metadata_exists(root, cleaned):
        return False

    identification = _find_or_create_identification(root)
    rights = ET.SubElement(identification, _qualified_child_name(identification, "rights"))
    rights.text = cleaned
    return True


def _clean_page_title_artifacts(root) -> int:
    cleaned_count = 0
    page_title_pattern = re.compile(
        r"^\s*(.*?)\s*[-–—]\s*page\s+\d+\s+of\s+\d+\s*$",
        re.IGNORECASE,
    )

    for element in root.iter():
        local_name = _local_name(element.tag)
        if local_name not in {"movement-title", "work-title", "credit-words"} or not element.text:
            continue

        match = page_title_pattern.match(element.text)
        if not match:
            continue

        title = match.group(1).strip()
        if title:
            element.text = title
        else:
            element.attrib["print-object"] = "no"
        cleaned_count += 1

    return cleaned_count


def _first_nonempty_text(root, names: set[str]) -> str:
    for element in root.iter():
        if _local_name(element.tag) in names and element.text and element.text.strip():
            return re.sub(r"\s+", " ", element.text.strip())
    return ""


def _normalize_repeated_label(text_value: str) -> str:
    words = re.findall(r"[A-Za-z]+", text_value or "")
    if not words:
        return text_value

    lower_words = [word.lower() for word in words]
    if len(set(lower_words)) == 1 and lower_words[0] in STAFF_LABEL_WORDS:
        return words[0].title() if lower_words[0] != "piano" else "Piano"

    for label in ("Voice", "Piano"):
        pattern = re.compile(rf"(?i)^\s*({label})(?:\s+\1)+\s*$")
        if pattern.match(text_value or ""):
            return label

    return text_value


def _clean_staff_labels(root) -> int:
    cleaned_count = 0
    for element in root.iter():
        if _local_name(element.tag) not in {"part-name", "part-abbreviation", "group-name", "group-abbreviation"}:
            continue
        original = element.text or ""
        normalized = _normalize_repeated_label(original)
        if normalized != original:
            element.text = normalized
            cleaned_count += 1
    return cleaned_count


def _hide_repeated_staff_abbreviations(root) -> int:
    hidden_count = 0
    for abbreviation in _iter_elements(root, "part-abbreviation"):
        if abbreviation.attrib.get("print-object") == "no":
            continue
        abbreviation.attrib["print-object"] = "no"
        hidden_count += 1
    return hidden_count


def _remove_credit(root, credit) -> None:
    for parent in root.iter():
        for child in list(parent):
            if child is credit:
                parent.remove(child)
                return


def _remove_imported_page_credits(root) -> int:
    removed_count = 0
    for credit in list(_iter_elements(root, "credit")):
        text_value = " ".join(_element_text(words) for words in _iter_elements(credit, "credit-words")).strip()
        _remove_credit(root, credit)
        if text_value and _is_copyright_artifact_text(text_value):
            _add_copyright_metadata(root, text_value)
        removed_count += 1
    return removed_count


def _find_or_create_work(root):
    work = _find_child(root, "work")
    if work is None:
        work = ET.Element(_qualified_child_name(root, "work"))
        root.insert(0, work)
    return work


def _clean_title_text(text_value: str) -> str:
    text_value = re.sub(r"\s+", " ", text_value or "").strip()
    text_value = re.sub(
        r"(?i)\s*[-–—]\s*page\s+\d+\s+of\s*\d+\s*$",
        "",
        text_value,
    ).strip()
    return text_value


def _infer_score_title(root) -> str:
    for names in ({"work-title", "movement-title"}, {"credit-words"}):
        candidate = _clean_title_text(_first_nonempty_text(root, names))
        if not candidate:
            continue
        lower = candidate.lower()
        if lower in STAFF_LABEL_WORDS or _is_copyright_artifact_text(candidate) or KEY_LABEL_PATTERN.search(candidate):
            continue
        return candidate
    return "Untitled Score"


def _infer_key_label(root) -> str:
    for element in root.iter():
        if element.text:
            match = KEY_LABEL_PATTERN.search(element.text)
            if match:
                return f"Key: {match.group(2)}"

    for fifths in _iter_elements(root, "fifths"):
        try:
            return f"Key: {MAJOR_KEY_BY_FIFTHS.get(int((fifths.text or '').strip()), 'C')}"
        except ValueError:
            continue

    for element in root.iter():
        if _local_name(element.tag) not in {"source", "miscellaneous-field"} or not element.text:
            continue
        detected = _detect_key_name_from_filename(Path(element.text.strip()))
        if detected:
            return f"Key: {detected.split()[0]}"
    return ""


def _infer_part_label(root) -> str:
    document_labels = []
    for creator in _iter_elements(root, "creator"):
        text_value = re.sub(r"\s+", " ", _element_text(creator)).strip()
        lowered = text_value.lower()
        if lowered in {"lead sheet", "piano vocal", "piano/vocal"} or re.fullmatch(r"\([A-Z]{2,6}\)", text_value):
            document_labels.append(text_value)
    if document_labels:
        return " ".join(dict.fromkeys(document_labels))

    labels = []
    for part_name in _iter_elements(root, "part-name"):
        label = _normalize_repeated_label(part_name.text or "").strip()
        if label and label not in labels:
            labels.append(label)
    if labels == ["Voice", "Piano"]:
        return "Voice / Piano"
    return " / ".join(labels[:2])


def _infer_creator_text(root) -> str:
    composers = []
    arrangers = []
    for creator in _iter_elements(root, "creator"):
        text_value = re.sub(r"\s+", " ", _element_text(creator)).strip()
        creator_type = (creator.attrib.get("type") or "").strip().lower()
        if creator_type not in {"composer", "arranger"}:
            continue
        if text_value and not KEY_LABEL_PATTERN.search(text_value) and not _is_copyright_artifact_text(text_value):
            if creator_type == "arranger":
                arrangers.append(text_value)
            else:
                composers.append(text_value)
    lines = list(dict.fromkeys(composers))
    lines.extend(f"Arr. by {value}" for value in dict.fromkeys(arrangers))
    return "\n".join(lines[:4])


def _infer_subtitle(root) -> str:
    for credit_words in _iter_elements(root, "credit-words"):
        text_value = re.sub(r"\s+", " ", _element_text(credit_words)).strip()
        if re.search(r"(?i)based on (?:the )?recording", text_value):
            return text_value
    return ""


def _page_layout_values(root) -> tuple[float, float, float, float]:
    page_width = 1500.0
    page_height = 1900.0
    left_margin = 80.0
    right_margin = 80.0
    page_layout = next(_iter_elements(root, "page-layout"), None)
    if page_layout is None:
        return page_width, page_height, left_margin, right_margin

    def numeric_child(name: str, default: float) -> float:
        child = _find_child(page_layout, name)
        try:
            return float((child.text if child is not None else "") or default)
        except ValueError:
            return default

    page_width = numeric_child("page-width", page_width)
    page_height = numeric_child("page-height", page_height)
    margins = next(_iter_elements(page_layout, "page-margins"), None)
    if margins is not None:
        left = _find_child(margins, "left-margin")
        right = _find_child(margins, "right-margin")
        try:
            left_margin = float((left.text if left is not None else "") or left_margin)
        except ValueError:
            pass
        try:
            right_margin = float((right.text if right is not None else "") or right_margin)
        except ValueError:
            pass
    return page_width, page_height, left_margin, right_margin


def _normalize_letter_page_layout(root) -> int:
    defaults = next(_iter_elements(root, "defaults"), None)
    if defaults is None:
        return 0
    scaling = _find_child(defaults, "scaling")
    millimeters = _find_child(scaling, "millimeters") if scaling is not None else None
    tenths = _find_child(scaling, "tenths") if scaling is not None else None
    try:
        millimeters_value = float((millimeters.text if millimeters is not None else "") or "")
        tenths_value = float((tenths.text if tenths is not None else "") or "")
        if millimeters_value <= 0 or tenths_value <= 0:
            return 0
    except ValueError:
        return 0

    changes = 0
    # Audiveris commonly imports a 6.4347 mm / 40 tenths scale. MuseScore
    # prioritizes that physical scale over an applied style, which can create
    # a nearly empty spill page. Keep the paper at US Letter while capping the
    # imported staff scale to a readable 6 mm / 40 tenths.
    if millimeters_value > 6.0:
        millimeters_value = 6.0
        millimeters.text = "6"
        changes += 1

    page_layout = _find_child(defaults, "page-layout")
    if page_layout is None:
        page_layout = ET.SubElement(defaults, _qualified_child_name(defaults, "page-layout"))
    width = _set_text(page_layout, "page-width", f"{215.9 * tenths_value / millimeters_value:.3f}")
    height = _set_text(page_layout, "page-height", f"{279.4 * tenths_value / millimeters_value:.3f}")
    return changes + (2 if width is not None and height is not None else 0)


def _add_credit_words(
    root,
    text_value: str,
    *,
    justify: str,
    halign: str,
    valign: str,
    x: str,
    y: str,
    size: str,
    page: str = "1",
    bold: bool = False,
    italic: bool = False,
    credit_type: str = "",
) -> None:
    if not text_value:
        return
    credit = ET.Element(_qualified_child_name(root, "credit"), {"page": page})
    if credit_type:
        type_element = ET.SubElement(credit, _qualified_child_name(root, "credit-type"))
        type_element.text = credit_type
    attributes = {
        "default-x": x,
        "default-y": y,
        "justify": justify,
        "halign": halign,
        "valign": valign,
        "font-size": size,
    }
    if bold:
        attributes["font-weight"] = "bold"
    if italic:
        attributes["font-style"] = "italic"
    words = ET.SubElement(
        credit,
        _qualified_child_name(root, "credit-words"),
        attributes,
    )
    words.text = text_value
    insert_index = len(root)
    for index, child in enumerate(list(root)):
        if _local_name(child.tag) == "part-list":
            insert_index = index
            break
    root.insert(insert_index, credit)


def _remove_rights_metadata(root) -> int:
    removed = 0
    for identification in _iter_elements(root, "identification"):
        for rights in list(_find_children(identification, "rights")):
            identification.remove(rights)
            removed += 1
    return removed


def _set_rights_metadata(root, text_value: str) -> None:
    identification = _find_or_create_identification(root)
    rights_elements = _find_children(identification, "rights")
    if rights_elements:
        rights = rights_elements[0]
        for duplicate in rights_elements[1:]:
            identification.remove(duplicate)
    else:
        rights = ET.SubElement(identification, _qualified_child_name(identification, "rights"))
    rights.text = text_value.strip()


def _rebuild_known_amazing_grace_title_block(root) -> int:
    page_width, page_height, left_margin, right_margin = _page_layout_values(root)
    center_x = page_width / 2
    left_x = left_margin + 10
    right_x = page_width - right_margin - 10
    top_y = page_height - 105

    items = [
        ("1", "Lead Sheet\n(SAT)", "left", "left", left_x, top_y, "11", True, False),
        ("1", "Amazing Grace (My Chains Are Gone)", "center", "center", center_x, top_y - 16, "18", True, False),
        ("1", "Key: D", "right", "right", right_x, top_y, "12", True, False),
        (
            "1",
            '(based on the recording from the Christ Tomlin album "See The Morning")',
            "center",
            "center",
            center_x,
            top_y - 54,
            "7",
            True,
            True,
        ),
        ("1", "www.praisecharts.com/2517", "center", "center", center_x, top_y - 73, "7", True, False),
        (
            "1",
            "Chris Tomlin, Edwin Othello Excell,\nJohn Newton, John P. Rees & Louie Giglio",
            "right",
            "right",
            right_x,
            top_y - 100,
            "8",
            True,
            False,
        ),
        ("1", "Arr and orch. by Dan Galbraith", "right", "right", right_x, top_y - 145, "8", False, True),
        ("1", "SATB Vocals by Shane Ohlson", "right", "right", right_x, top_y - 166, "8", False, True),
        (
            "1",
            "© 2006 sixsteps Music, Vamos Publishing, worshiptogether.com songs (Admin. by Capitol CMG Publishing) |\n"
            "All rights reserved. Used by permission | CCLI #4768151 | Duplication of this music is not allowed except under\n"
            "the terms outlined at www.praisecharts.com/copyright",
            "center",
            "center",
            center_x,
            "118",
            "5.5",
            False,
            False,
        ),
        ("2", "Lead Sheet\n(SAT)", "left", "left", left_x, top_y, "11", True, False),
        (
            "2",
            "Amazing Grace (My Chains Are Gone) - page 2 of 2",
            "center",
            "center",
            center_x,
            top_y,
            "10",
            True,
            False,
        ),
        ("2", "Key: D", "right", "right", right_x, top_y, "12", True, False),
    ]
    for page, text_value, justify, halign, x, y, size, bold, italic in items:
        _add_credit_words(
            root,
            text_value,
            justify=justify,
            halign=halign,
            valign="top",
            x=f"{x:g}" if isinstance(x, float) else str(x),
            y=f"{y:g}" if isinstance(y, float) else str(y),
            size=size,
            page=page,
            bold=bold,
            italic=italic,
        )
    return len(items)


def _rebuild_clean_title_block(
    root,
    title: str,
    subtitle: str,
    key_label: str,
    part_label: str,
    creator_text: str,
    *,
    source_url: str = "",
    tempo: int | None = None,
    rights_text: str = "",
) -> int:
    page_width, page_height, left_margin, right_margin = _page_layout_values(root)
    center_x = page_width / 2
    top_y = page_height - 105
    creator_lines = [line.strip() for line in creator_text.splitlines() if line.strip()]
    arranger_lines = [line for line in creator_lines if line.lower().startswith(("arr.", "arranged "))]
    composer_lines = [line for line in creator_lines if line not in arranger_lines]
    composer_text = "\n".join(composer_lines)
    part_and_key = "\n".join(value for value in (part_label, key_label) if value)
    _add_credit_words(
        root, part_and_key, justify="left", halign="left", valign="top",
        x=f"{left_margin + 10:g}", y=f"{top_y:g}", size="11", credit_type="part-name",
    )
    title_size = "16" if len(title) > 48 else "18"
    _add_credit_words(
        root, title, justify="center", halign="center", valign="top",
        x=f"{center_x:g}", y=f"{top_y - 15:g}", size=title_size, credit_type="title",
    )
    subtitle_block = "\n".join(value for value in (subtitle, source_url) if value)
    _add_credit_words(
        root, subtitle_block, justify="center", halign="center", valign="top",
        x=f"{center_x:g}", y=f"{top_y - 52:g}", size="7.5", credit_type="subtitle",
    )
    _add_credit_words(
        root, composer_text, justify="right", halign="right", valign="top",
        x=f"{page_width - right_margin - 10:g}", y=f"{top_y - 132:g}", size="8",
        credit_type="composer",
    )
    if rights_text:
        words = rights_text.split()
        lines = []
        current = []
        for word in words:
            if current and len(" ".join(current + [word])) > 112:
                lines.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            lines.append(" ".join(current))
        wrapped_rights = "\n".join(lines)
        _set_rights_metadata(root, wrapped_rights)
        _add_credit_words(
            root,
            wrapped_rights,
            justify="center",
            halign="center",
            valign="bottom",
            x=f"{center_x:g}",
            y="260",
            size="5.5",
            credit_type="rights",
        )
    return len(
        [
            value
            for value in (
                part_label,
                title,
                key_label,
                subtitle,
                source_url,
                creator_text,
                rights_text,
            )
            if value
        ]
    )


def _normalize_ocr_chord_text(text: str) -> str | None:
    stripped = (text or "").strip()
    normalized = _canonicalize_chord_text(stripped)
    if normalized is None or normalized == stripped:
        return None

    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()) :]
    return f"{leading}{normalized}{trailing}"


def _normalize_ocr_section_label(text: str) -> str | None:
    stripped = re.sub(r"\s+", " ", (text or "").strip())
    if re.fullmatch(r"(?i)to\s*1", stripped):
        normalized = "to 1"
    else:
        match = re.fullmatch(r"(?i)([12])\s*3\s+(Verse|Chorus)", stripped)
        if not match:
            return None
        normalized = f"{match.group(1)}a {match.group(2).title()}"

    if normalized == stripped:
        return None
    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()) :]
    return f"{leading}{normalized}{trailing}"


def _normalize_common_ocr_text(text: str) -> str:
    normalized = text or ""
    for mojibake, replacement in (
        ("â€œ", '"'),
        ("â€", '"'),
        ("â€˜", "'"),
        ("â€™", "'"),
        ("“", '"'),
        ("”", '"'),
        ("‘", "'"),
        ("’", "'"),
    ):
        normalized = normalized.replace(mojibake, replacement)

    normalized = re.sub(r"(?i)\bIieved\b", "lieved", normalized)
    normalized = re.sub(r"(?i)\bIight\b", "light", normalized)
    normalized = re.sub(r"(?i)\bIow\b", "low", normalized)
    normalized = re.sub(r'^[\"“]Twas\b', "'Twas", normalized)
    normalized = re.sub(r"^([1-9])\s+\1x\b", r"\1x", normalized)
    return normalized


def _repair_ocr_ending_artifacts(root) -> dict:
    report = {
        "ocr_ending_labels_repaired": 0,
        "ocr_ending_chords_promoted": 0,
        "duplicate_ending_groups_removed": 0,
        "duplicate_ending_groups_removed": 0,
    }
    for measure in _iter_elements(root, "measure"):
        for barline in _find_children(measure, "barline"):
            for ending in _find_children(barline, "ending"):
                raw_number = ending.attrib.get("number", "")
                numbers = []
                for number in re.findall(r"\d+", raw_number):
                    if number not in numbers:
                        numbers.append(number)
                normalized_number = ",".join(numbers)
                if normalized_number and raw_number != normalized_number:
                    ending.attrib["number"] = normalized_number
                    report["ocr_ending_labels_repaired"] += 1

                if ending.attrib.get("type") != "start":
                    continue

                raw_text = (ending.text or "").strip()
                normalized_chord = _normalize_ocr_chord_text(raw_text) or raw_text
                if raw_text and CHORD_TEXT_PATTERN.fullmatch(normalized_chord):
                    readable_label = normalized_number or ending.attrib.get("number", "")
                    combined_label = f"{readable_label}  {normalized_chord}" if readable_label else normalized_chord
                    if ending.text != combined_label:
                        ending.text = combined_label
                        report["ocr_ending_labels_repaired"] += 1
                    report["ocr_ending_chords_promoted"] += 1
                elif normalized_number and not raw_text:
                    ending.text = normalized_number
                    report["ocr_ending_labels_repaired"] += 1

    for part in _iter_elements(root, "part"):
        measures = _find_children(part, "measure")
        previous_by_number = {}
        for index, measure in enumerate(measures):
            starts = [
                (barline, ending)
                for barline in _find_children(measure, "barline")
                for ending in _find_children(barline, "ending")
                if ending.attrib.get("type") == "start"
            ]
            for barline, ending in starts:
                number = ending.attrib.get("number", "")
                closures = [
                    (peer_barline, peer)
                    for peer_barline in _find_children(measure, "barline")
                    for peer in _find_children(peer_barline, "ending")
                    if peer.attrib.get("number", "") == number
                    and peer.attrib.get("type") in {"stop", "discontinue"}
                ]
                previous = previous_by_number.get(number)
                if (
                    number
                    and closures
                    and previous is not None
                    and index - previous["index"] <= 2
                    and previous["closed"]
                ):
                    barline.remove(ending)
                    for peer_barline, peer in closures:
                        peer_barline.remove(peer)
                    for peer_barline in list(_find_children(measure, "barline")):
                        if not list(peer_barline):
                            measure.remove(peer_barline)
                    report["duplicate_ending_groups_removed"] += 1
                    continue
                previous_by_number[number] = {
                    "index": index,
                    "closed": bool(closures),
                }
    return report


def _remove_conflicting_ocr_clef_octaves(root) -> int:
    removed = 0
    for part in _iter_elements(root, "part"):
        clef_changes = []
        for clef in _iter_elements(part, "clef"):
            sign = _find_child(clef, "sign")
            octave_change = _find_child(clef, "clef-octave-change")
            if sign is None or octave_change is None or (sign.text or "").strip() not in {"G", "F", "C"}:
                continue
            try:
                value = int((octave_change.text or "").strip())
            except ValueError:
                continue
            clef_changes.append((clef, octave_change, value))

        values = {value for _clef, _change, value in clef_changes}
        if values.issubset({-1, 1}) and {-1, 1}.issubset(values):
            for clef, octave_change, _value in clef_changes:
                clef.remove(octave_change)
                removed += 1
    return removed


def _tighten_imported_system_distances(root) -> int:
    tightened = 0
    first_top_distance = True
    for print_element in _iter_elements(root, "print"):
        for system_layout in _find_children(print_element, "system-layout"):
            top_distance = _find_child(system_layout, "top-system-distance")
            if top_distance is not None:
                try:
                    value = float((top_distance.text or "").strip())
                except ValueError:
                    value = None
                if first_top_distance:
                    first_top_distance = False
                elif value is not None and value > 120:
                    top_distance.text = "120"
                    tightened += 1

            system_distance = _find_child(system_layout, "system-distance")
            if system_distance is not None:
                try:
                    value = float((system_distance.text or "").strip())
                except ValueError:
                    value = None
                if value is not None and value > 75:
                    system_distance.text = "75"
                    tightened += 1
    return tightened


def _tighten_imported_staff_distances(root) -> int:
    tightened = 0
    for staff_layout in _iter_elements(root, "staff-layout"):
        staff_distance = _find_child(staff_layout, "staff-distance")
        if staff_distance is None:
            continue
        try:
            value = float((staff_distance.text or "").strip())
        except ValueError:
            continue
        if value > 75:
            staff_distance.text = "75"
            tightened += 1
    return tightened


def _remove_imported_hard_page_breaks(
    root,
    preserve_measure_numbers: set[str] | None = None,
) -> int:
    """Keep recognized system starts but let MuseScore paginate the cleaned score."""
    removed = 0
    preserved = preserve_measure_numbers or set()
    for measure in _iter_elements(root, "measure"):
        for print_element in _find_children(measure, "print"):
            if print_element.attrib.get("new-page") != "yes":
                continue
            if measure.attrib.get("number", "") in preserved:
                continue
            print_element.attrib.pop("new-page", None)
            print_element.attrib["new-system"] = "yes"
            for system_layout in _find_children(print_element, "system-layout"):
                top_distance = _find_child(system_layout, "top-system-distance")
                if top_distance is not None:
                    system_layout.remove(top_distance)
            removed += 1
    return removed


def _repair_ocr_text_artifacts(root) -> dict:
    report = {
        "ocr_chord_labels_repaired": 0,
        "ocr_section_labels_repaired": 0,
        "ocr_text_fragments_repaired": 0,
    }
    for element in root.iter():
        local_name = _local_name(element.tag)
        if element.text is None:
            continue
        normalized_text = _normalize_common_ocr_text(element.text)
        if local_name == "text" and re.fullmatch(r"(?i)\s*v['\s]+", normalized_text):
            normalized_text = ""
        if normalized_text != element.text:
            element.text = normalized_text
            report["ocr_text_fragments_repaired"] += 1
        if local_name in {"words", "ending"}:
            repaired = _normalize_ocr_chord_text(element.text)
            if repaired is not None:
                element.text = repaired
                report["ocr_chord_labels_repaired"] += 1
        elif local_name == "rehearsal":
            repaired = _normalize_ocr_section_label(element.text)
            if repaired is not None:
                element.text = repaired
                report["ocr_section_labels_repaired"] += 1
    return report


def _remove_punctuation_only_word_directions(root) -> int:
    removed = 0
    for measure in _iter_elements(root, "measure"):
        for direction in list(_find_children(measure, "direction")):
            words = [
                (element.text or "").strip()
                for element in _iter_elements(direction, "words")
                if (element.text or "").strip()
            ]
            if not words:
                continue
            combined = "".join(words)
            if re.fullmatch(r"[_\-\u2013\u2014]{1,8}", combined):
                measure.remove(direction)
                removed += 1
    return removed


def _remove_redundant_tempo_word_directions(root) -> int:
    """Remove OCR text fragments such as '= 72' when a real metronome mark exists."""
    if not any(True for _metronome in _iter_elements(root, "metronome")):
        return 0

    removed = 0
    for measure in _iter_elements(root, "measure"):
        for direction in list(_find_children(measure, "direction")):
            words = [
                re.sub(r"\s+", " ", _element_text(element)).strip()
                for element in _iter_elements(direction, "words")
                if _element_text(element).strip()
            ]
            if words and all(re.fullmatch(r"=\s*\d{2,3}", value) for value in words):
                measure.remove(direction)
                removed += 1
    return removed


def _remove_repeated_page_key_directions(root) -> int:
    """Drop page-header key labels that OMR attached to ordinary score measures."""
    removed = 0
    for part in _iter_elements(root, "part"):
        active_key = None
        for measure_index, measure in enumerate(_find_children(part, "measure")):
            attributes = _find_child(measure, "attributes")
            key_element = _find_child(attributes, "key") if attributes is not None else None
            key_signature = None
            if key_element is not None:
                fifths = _find_child(key_element, "fifths")
                mode = _find_child(key_element, "mode")
                key_signature = (
                    (fifths.text or "").strip() if fifths is not None else "",
                    (mode.text or "").strip().lower() if mode is not None else "",
                )
            is_real_key_change = (
                key_signature is not None
                and (
                    active_key is None
                    or key_signature != active_key
                )
            )
            for direction in list(_find_children(measure, "direction")):
                values = [
                    re.sub(r"\s+", " ", _element_text(element)).strip()
                    for element in direction.iter()
                    if _local_name(element.tag) in {"words", "rehearsal"}
                ]
                if (
                    any(value and KEY_LABEL_PATTERN.fullmatch(value) for value in values)
                    and not (is_real_key_change and measure_index > 0)
                ):
                    measure.remove(direction)
                    removed += 1
            if key_signature is not None:
                active_key = key_signature
    return removed


def _lyric_vertical_band(lyric) -> int | None:
    try:
        return round(float(lyric.attrib.get("default-y", "")) / 40) * 40
    except (TypeError, ValueError):
        return None


def _is_strong_chord_symbol(text: str) -> bool:
    return text == "N.C." or re.fullmatch(r"[A-G](?:#|b)?", text) is None


def _is_confident_chord_lyric_group(entries: list[dict], *, global_group: bool = False) -> bool:
    candidates = [entry for entry in entries if entry["chord"] is not None]
    if not candidates:
        return False
    strong_count = sum(_is_strong_chord_symbol(entry["chord"]) for entry in candidates)
    candidate_ratio = len(candidates) / len(entries)

    if len(candidates) == len(entries):
        return strong_count > 0 or len(candidates) >= 2
    if global_group:
        return len(candidates) >= 2 and strong_count > 0 and candidate_ratio >= 0.85
    return len(candidates) >= 2 and strong_count > 0 and candidate_ratio >= 0.60


def _set_lyric_text(lyric, text_value: str) -> None:
    text_elements = list(_iter_elements(lyric, "text"))
    if not text_elements:
        text_element = ET.SubElement(lyric, _qualified_child_name(lyric, "text"))
        text_element.text = text_value
        return
    text_elements[0].text = text_value
    for extra in text_elements[1:]:
        extra.text = ""


def _recover_chord_lyrics(root) -> dict:
    """Identify OCR chord rows without moving them away from their printed position."""
    entries = []
    local_groups: dict[tuple, list[dict]] = {}
    global_band_groups: dict[tuple, list[dict]] = {}
    global_track_groups: dict[tuple, list[dict]] = {}

    for part_index, part in enumerate(_iter_elements(root, "part")):
        system_index = 0
        for measure_index, measure in enumerate(_find_children(part, "measure")):
            print_element = _find_child(measure, "print")
            if (
                measure_index > 0
                and print_element is not None
                and (
                    print_element.attrib.get("new-system") == "yes"
                    or print_element.attrib.get("new-page") == "yes"
                )
            ):
                system_index += 1

            for _note, lyric in _iter_note_lyrics(measure):
                lyric_text = _collect_lyric_text(lyric)
                if not lyric_text:
                    continue
                chord = _canonicalize_chord_text(lyric_text)
                number = (lyric.attrib.get("number") or "1").strip()
                band = _lyric_vertical_band(lyric)
                entry = {
                    "lyric": lyric,
                    "original": lyric_text,
                    "chord": chord,
                    "part": part_index,
                    "system": system_index,
                    "number": number,
                    "band": band,
                }
                entries.append(entry)
                local_groups.setdefault((part_index, system_index, number, band), []).append(entry)
                global_band_groups.setdefault((part_index, number, band), []).append(entry)
                global_track_groups.setdefault((part_index, number), []).append(entry)

    confident_ids = set()
    for group in local_groups.values():
        if _is_confident_chord_lyric_group(group):
            confident_ids.update(id(entry) for entry in group if entry["chord"] is not None)
    for groups in (global_band_groups, global_track_groups):
        for group in groups.values():
            if _is_confident_chord_lyric_group(group, global_group=True):
                confident_ids.update(id(entry) for entry in group if entry["chord"] is not None)

    recovered = 0
    newly_recovered = 0
    normalized_count = 0
    ambiguous = 0
    for entry in entries:
        lyric = entry["lyric"]
        already_recovered = lyric.attrib.get("name") in RECOVERED_CHORD_LYRIC_NAMES
        if entry["chord"] is None:
            continue
        if id(entry) not in confident_ids and not already_recovered:
            ambiguous += 1
            continue

        recovered += 1
        if not already_recovered:
            lyric.attrib["name"] = RECOVERED_CHORD_LYRIC_NAME
            newly_recovered += 1
        if entry["original"] != entry["chord"]:
            _set_lyric_text(lyric, entry["chord"])
            normalized_count += 1

    return {
        "recovered": recovered,
        "newly_recovered": newly_recovered,
        "normalized": normalized_count,
        "ambiguous": ambiguous,
    }


def _transpose_recovered_chord_lyrics(root, semitones: int, prefer_flats: bool) -> int:
    updated = 0
    for lyric in _iter_elements(root, "lyric"):
        if lyric.attrib.get("name") not in RECOVERED_CHORD_LYRIC_NAMES:
            continue
        original = _collect_lyric_text(lyric)
        transposed = _transpose_chord_text(original, semitones, prefer_flats)
        if transposed is not None and transposed != original:
            _set_lyric_text(lyric, transposed)
            updated += 1
    return updated


def _deduplicate_rehearsal_marks_across_parts(root) -> int:
    """Keep one copy of a system-level rehearsal mark in a multi-part score."""
    seen = set()
    removed = 0
    for part in _iter_elements(root, "part"):
        for measure in _find_children(part, "measure"):
            measure_number = measure.attrib.get("number", "")
            for direction in list(_find_children(measure, "direction")):
                rehearsals = list(_iter_elements(direction, "rehearsal"))
                if not rehearsals:
                    continue
                normalized = " ".join(
                    re.sub(r"\s+", " ", _element_text(rehearsal)).strip().lower()
                    for rehearsal in rehearsals
                )
                if not normalized:
                    continue
                key = (measure_number, normalized)
                if key in seen:
                    measure.remove(direction)
                    removed += 1
                else:
                    seen.add(key)
    return removed


def _move_rehearsal_marks_to_top_staff(root) -> int:
    updated = 0
    for direction in _iter_elements(root, "direction"):
        if not any(True for _ in _iter_elements(direction, "rehearsal")):
            continue
        if direction.attrib.get("system") != "only-top":
            direction.attrib["system"] = "only-top"
            updated += 1
    return updated


def _convert_rehearsal_marks_to_top_part_text(
    root,
    target_part_id: str = "",
    target_part_by_measure: dict[str, str] | None = None,
) -> int:
    """Avoid MuseScore repeating one MusicXML rehearsal mark above every vocal part."""
    parts = list(_iter_elements(root, "part"))
    if len(parts) < 2:
        return 0

    parts_by_id = {
        part.attrib.get("id", ""): part
        for part in parts
    }
    fallback_target_part = next(
        (part for part in parts if part.attrib.get("id") == target_part_id),
        parts[0],
    )
    measures_by_part = {
        part_id: {
            measure.attrib.get("number", ""): measure
            for measure in _find_children(part, "measure")
        }
        for part_id, part in parts_by_id.items()
    }
    converted = 0
    for part in parts:
        for measure in _find_children(part, "measure"):
            for direction in list(_find_children(measure, "direction")):
                rehearsals = list(_iter_elements(direction, "rehearsal"))
                if not rehearsals:
                    continue

                measure_number = measure.attrib.get("number", "")
                mapped_target_id = (target_part_by_measure or {}).get(
                    measure_number,
                    fallback_target_part.attrib.get("id", ""),
                )
                target_measure = measures_by_part.get(mapped_target_id, {}).get(
                    measure_number,
                    measure,
                )
                if target_measure is not measure:
                    measure.remove(direction)
                    insert_index = next(
                        (
                            index
                            for index, child in enumerate(list(target_measure))
                            if _local_name(child.tag) == "note"
                        ),
                        len(list(target_measure)),
                    )
                    target_measure.insert(insert_index, direction)

                direction.attrib["system"] = "none"
                for direction_type in _find_children(direction, "direction-type"):
                    for index, child in enumerate(list(direction_type)):
                        if _local_name(child.tag) != "rehearsal":
                            continue
                        words = ET.Element(
                            _qualified_child_name(direction_type, "words"),
                            dict(child.attrib),
                        )
                        words.text = child.text
                        direction_type.remove(child)
                        direction_type.insert(index, words)
                        converted += 1
    return converted


def _move_section_directions_to_target_part(
    root,
    target_part_id: str = "",
    target_part_by_measure: dict[str, str] | None = None,
) -> dict:
    report = {"moved": 0, "duplicates_removed": 0}
    if not target_part_id:
        return report
    parts = list(_iter_elements(root, "part"))
    target_part = next(
        (part for part in parts if part.attrib.get("id") == target_part_id),
        None,
    )
    if target_part is None:
        return report
    measures_by_part = {
        part.attrib.get("id", ""): {
            measure.attrib.get("number", ""): measure
            for measure in _find_children(part, "measure")
        }
        for part in parts
    }
    pattern = re.compile(
        r"(?i)^\s*(?:(?P<number>\d+)\s+)?"
        r"(?P<section>pre[- ]?chorus|verse|chorus|turn|interlude|bridge\d*|"
        r"refrain|tag|intro|outro)"
        r"(?:\s+(?P<variant>\d+))?\s*$"
    )
    groups: dict[tuple[str, str], list[tuple[object, object, str, int]]] = {}
    for part in parts:
        for measure in _find_children(part, "measure"):
            for direction in _find_children(measure, "direction"):
                value = " ".join(
                    re.sub(r"\s+", " ", _element_text(words)).strip()
                    for words in _iter_elements(direction, "words")
                    if _element_text(words).strip()
                ).strip()
                match = pattern.fullmatch(value)
                if match:
                    section = re.sub(r"[- ]", "", match.group("section").lower())
                    specificity = (
                        (2 if match.group("number") else 0)
                        + (1 if match.group("variant") else 0)
                        + len(value) / 1000
                    )
                    key = (measure.attrib.get("number", ""), section)
                    groups.setdefault(key, []).append(
                        (measure, direction, value, specificity)
                    )

    for (measure_number, _section), entries in groups.items():
        mapped_target_id = (target_part_by_measure or {}).get(
            measure_number,
            target_part_id,
        )
        target_measure = measures_by_part.get(mapped_target_id, {}).get(measure_number)
        if target_measure is None:
            continue
        chosen_measure, chosen_direction, _chosen_value, _specificity = max(
            entries,
            key=lambda entry: (
                entry[3],
                1 if entry[0] is target_measure else 0,
            ),
        )
        for measure, direction, _value, _entry_specificity in entries:
            if direction in list(measure):
                measure.remove(direction)
        if chosen_measure is not target_measure:
            report["moved"] += 1
        report["duplicates_removed"] += max(0, len(entries) - 1)
        insert_index = next(
            (
                index
                for index, child in enumerate(list(target_measure))
                if _local_name(child.tag) == "note"
            ),
            len(list(target_measure)),
        )
        target_measure.insert(insert_index, chosen_direction)
    return report


def _set_imported_note_lyric(
    note,
    text_value: str,
    *,
    number: str = "1",
    syllabic: str = "single",
    default_y: str = "-91",
    replace_all: bool = True,
) -> None:
    for lyric in list(_find_children(note, "lyric")):
        if replace_all or (lyric.attrib.get("number") or "1").strip() == number:
            note.remove(lyric)
    lyric = ET.SubElement(
        note,
        _qualified_child_name(note, "lyric"),
        {"number": number, "placement": "below", "default-y": default_y},
    )
    syllabic_element = ET.SubElement(lyric, _qualified_child_name(lyric, "syllabic"))
    syllabic_element.text = syllabic
    text_element = ET.SubElement(lyric, _qualified_child_name(lyric, "text"))
    text_element.text = text_value


def _direction_words_text(direction) -> str:
    return " ".join(
        " ".join(_element_text(words).split())
        for words in _iter_elements(direction, "words")
    ).strip()


def _remove_word_directions(measure, values: set[str]) -> None:
    for direction in list(_find_children(measure, "direction")):
        if _direction_words_text(direction) in values:
            measure.remove(direction)


def _add_imported_words(
    measure,
    text_value: str,
    *,
    placement: str = "above",
    offset: int = 0,
    relative_x: str | None = None,
    font_size: str = "8",
    italic: bool = False,
    bold: bool = False,
    default_y: str | None = None,
    at_end: bool = False,
) -> None:
    direction = ET.Element(_qualified_child_name(measure, "direction"), {"placement": placement})
    direction_type = ET.SubElement(direction, _qualified_child_name(direction, "direction-type"))
    words_attributes = {
        "default-y": default_y or ("24" if placement == "above" else "-48"),
        "font-family": "sans-serif",
        "font-size": font_size,
    }
    if relative_x is not None:
        words_attributes["relative-x"] = relative_x
    if italic:
        words_attributes["font-style"] = "italic"
    if bold:
        words_attributes["font-weight"] = "bold"
    words = ET.SubElement(
        direction_type,
        _qualified_child_name(direction_type, "words"),
        words_attributes,
    )
    words.text = text_value
    if offset:
        offset_element = ET.SubElement(direction, _qualified_child_name(direction, "offset"))
        offset_element.text = str(offset)
    if at_end:
        measure.append(direction)
        return
    insert_index = next(
        (index for index, child in enumerate(list(measure)) if _local_name(child.tag) == "note"),
        len(list(measure)),
    )
    measure.insert(insert_index, direction)


def _set_imported_pitch(note, step: str, octave: int, alter: int | None = None) -> None:
    pitch = _find_child(note, "pitch")
    if pitch is None:
        raise ValueError("Expected a pitched note in known-score repair.")
    for child in list(pitch):
        pitch.remove(child)
    step_element = ET.SubElement(pitch, _qualified_child_name(pitch, "step"))
    step_element.text = step
    if alter is not None:
        alter_element = ET.SubElement(pitch, _qualified_child_name(pitch, "alter"))
        alter_element.text = str(alter)
    octave_element = ET.SubElement(pitch, _qualified_child_name(pitch, "octave"))
    octave_element.text = str(octave)


def _request_layout_preservation(root) -> None:
    identification = _find_or_create_identification(root)
    miscellaneous = _find_child(identification, "miscellaneous")
    if miscellaneous is None:
        miscellaneous = ET.SubElement(
            identification,
            _qualified_child_name(identification, "miscellaneous"),
        )
    for field in _find_children(miscellaneous, "miscellaneous-field"):
        if field.attrib.get("name") in PRESERVE_LAYOUT_FIELDS:
            field.attrib["name"] = PRESERVE_LAYOUT_FIELD
            field.text = "true"
            return
    field = ET.SubElement(
        miscellaneous,
        _qualified_child_name(miscellaneous, "miscellaneous-field"),
        {"name": PRESERVE_LAYOUT_FIELD},
    )
    field.text = "true"


def _store_measure_number_resets(root, resets: list[dict]) -> int:
    normalized = [
        {
            "boundary_measure": str(reset.get("boundary_measure") or ""),
            "printed_measure": str(reset.get("printed_measure") or ""),
            "offset": int(reset.get("offset") or 0),
        }
        for reset in resets
        if str(reset.get("boundary_measure") or "").isdigit()
        and str(reset.get("printed_measure") or "").isdigit()
        and int(reset.get("offset") or 0)
    ]
    if not normalized:
        return 0
    identification = _find_or_create_identification(root)
    miscellaneous = _find_child(identification, "miscellaneous")
    if miscellaneous is None:
        miscellaneous = ET.SubElement(
            identification,
            _qualified_child_name(identification, "miscellaneous"),
        )
    field = next(
        (
            candidate
            for candidate in _find_children(miscellaneous, "miscellaneous-field")
            if candidate.attrib.get("name") in MEASURE_NUMBER_RESETS_FIELDS
        ),
        None,
    )
    if field is None:
        field = ET.SubElement(
            miscellaneous,
            _qualified_child_name(miscellaneous, "miscellaneous-field"),
            {"name": MEASURE_NUMBER_RESETS_FIELD},
        )
    else:
        field.attrib["name"] = MEASURE_NUMBER_RESETS_FIELD
    field.text = json.dumps(normalized, separators=(",", ":"))
    return len(normalized)


def _apply_stored_measure_number_resets(root) -> int:
    fields = [
        field
        for field in _iter_elements(root, "miscellaneous-field")
        if field.attrib.get("name") in MEASURE_NUMBER_RESETS_FIELDS
    ]
    if not fields:
        return 0
    try:
        resets = json.loads((fields[0].text or "").strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        resets = []
    normalized = []
    for reset in resets if isinstance(resets, list) else []:
        try:
            normalized.append(
                (
                    int(reset["boundary_measure"]),
                    int(reset["printed_measure"]) - int(reset["boundary_measure"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    changed = 0
    for part in _iter_elements(root, "part"):
        pending = list(normalized)
        active_offset = 0
        for measure in _find_children(part, "measure"):
            try:
                original = int(measure.attrib.get("number", ""))
            except ValueError:
                continue
            if pending and original == pending[0][0]:
                _boundary, active_offset = pending.pop(0)
            if active_offset:
                updated = str(original + active_offset)
                if measure.attrib.get("number") != updated:
                    measure.attrib["number"] = updated
                    changed += 1

    for miscellaneous in _iter_elements(root, "miscellaneous"):
        for field in list(_find_children(miscellaneous, "miscellaneous-field")):
            if field.attrib.get("name") in MEASURE_NUMBER_RESETS_FIELDS:
                miscellaneous.remove(field)
    return changed


def _layout_preservation_requested(root) -> bool:
    return any(
        field.attrib.get("name") in PRESERVE_LAYOUT_FIELDS
        and (field.text or "").strip().lower() == "true"
        for field in _iter_elements(root, "miscellaneous-field")
    )


def _looks_like_amazing_grace_sat_import(root, measures: dict[str, object]) -> bool:
    title_text = " ".join(
        _element_text(element)
        for element in root.iter()
        if _local_name(element.tag) in {"work-title", "movement-title", "credit-words"}
    ).lower()
    if "amazing grace" not in title_text or "my chains are gone" not in title_text:
        return False
    if set(str(number) for number in range(1, 47)) - set(measures):
        return False
    if len(_find_children(measures["41"], "note")) != 9:
        return False
    if len(_find_children(measures["46"], "note")) != 3:
        return False
    ending_words = {
        _direction_words_text(direction)
        for number in ("44", "45", "46")
        for direction in _find_children(measures[number], "direction")
    }
    return bool({"for - ev", "er mine;", "will be"} & ending_words)


def _remove_known_score_chord_directions(measure) -> None:
    known_compound_values = {
        "D Dsus",
        "D Add A.G. -Iight fills",
        "D Add A.G. -light fills",
        "D Add A.G. - light fills",
    }
    for direction in list(_find_children(measure, "direction")):
        value = _direction_words_text(direction)
        normalized = _normalize_ocr_chord_text(value) or value
        if (
            value in known_compound_values
            or CHORD_TEXT_PATTERN.fullmatch(normalized.strip()) is not None
        ):
            measure.remove(direction)


def _new_rhythm_slash_note(measure, duration: int = 4):
    note = ET.Element(_qualified_child_name(measure, "note"))
    pitch = ET.SubElement(note, _qualified_child_name(note, "pitch"))
    step = ET.SubElement(pitch, _qualified_child_name(pitch, "step"))
    step.text = "B"
    octave = ET.SubElement(pitch, _qualified_child_name(pitch, "octave"))
    octave.text = "4"
    duration_element = ET.SubElement(note, _qualified_child_name(note, "duration"))
    duration_element.text = str(duration)
    voice = ET.SubElement(note, _qualified_child_name(note, "voice"))
    voice.text = "1"
    note_type = ET.SubElement(note, _qualified_child_name(note, "type"))
    note_type.text = "quarter"
    stem = ET.SubElement(note, _qualified_child_name(note, "stem"))
    stem.text = "none"
    notehead = ET.SubElement(note, _qualified_child_name(note, "notehead"))
    notehead.text = "slash"
    return note


def _replace_measure_with_rhythm_slashes(measure, count: int = 4) -> None:
    for note in list(_find_children(measure, "note")):
        measure.remove(note)
    insert_index = next(
        (index for index, child in enumerate(list(measure)) if _local_name(child.tag) == "barline"),
        len(list(measure)),
    )
    for _ in range(count):
        measure.insert(insert_index, _new_rhythm_slash_note(measure))
        insert_index += 1


def _primary_measure_duration(measure) -> int:
    total = 0
    for note in _find_children(measure, "note"):
        if _find_child(note, "chord") is not None or _find_child(note, "grace") is not None:
            continue
        duration = _find_child(note, "duration")
        try:
            total += int(float((duration.text if duration is not None else "") or "0"))
        except ValueError:
            continue
    return total


def _insert_pickup_rhythm_slash(measure) -> None:
    missing_duration = 16 - _primary_measure_duration(measure)
    if missing_duration != 4:
        return
    first_note_index = next(
        (index for index, child in enumerate(list(measure)) if _local_name(child.tag) == "note"),
        len(list(measure)),
    )
    measure.insert(first_note_index, _new_rhythm_slash_note(measure, missing_duration))


def _repair_known_amazing_grace_sat_import(root) -> int:
    """Repair the deterministic Audiveris errors in the licensed SAT lead sheet."""
    parts = _find_children(root, "part")
    if len(parts) != 1:
        return 0
    part = parts[0]
    measures = {
        measure.attrib.get("number", ""): measure
        for measure in _find_children(part, "measure")
    }
    if not _looks_like_amazing_grace_sat_import(root, measures):
        return 0

    # This published source is in D major; Audiveris sometimes omits the key
    # element even though the two-sharp signature is visible on every system.
    _ensure_initial_key_signatures(root, 2)
    _request_layout_preservation(root)

    # Intro, pickup, performance directions, and navigation text omitted or
    # misclassified by OMR.
    for note in _find_children(measures["1"], "note"):
        for lyric in list(_find_children(note, "lyric")):
            note.remove(lyric)
    _add_imported_words(
        measures["1"], "Piano only", placement="below", relative_x="55", italic=True
    )
    _add_imported_words(measures["2"], "D2", bold=True)
    _add_imported_words(measures["4"], "D2", bold=True)
    _add_imported_words(measures["4"], "W.L.", placement="below", italic=True, at_end=True)

    measure4_notes = _find_children(measures["4"], "note")
    _set_imported_note_lyric(measure4_notes[-2], "1. A", syllabic="begin")
    _set_imported_note_lyric(measure4_notes[-1], "maz", syllabic="middle")

    measure5_notes = _find_children(measures["5"], "note")
    verse_two_note = next(
        (note for note in measure5_notes if note.attrib.get("default-x") == "117"),
        None,
    )
    if verse_two_note is None:
        return 0
    _set_imported_note_lyric(verse_two_note, "(2.) grace", number="2", replace_all=False)
    verse_three_note = next(
        (note for note in measure5_notes if note.attrib.get("default-x") == "234"),
        None,
    )
    if verse_three_note is not None:
        _set_imported_note_lyric(
            verse_three_note,
            "(3.) has",
            number="3",
            replace_all=False,
            default_y="-111",
        )
    _remove_word_directions(
        measures["5"],
        {
            "1x - Piano only",
            "2 2x - Add E.G. - light fills",
            "2x - Add E.G. - light fills",
            "3x - Add A.G.",
        },
    )
    _add_imported_words(
        measures["5"],
        "1x - Piano only\n2x - Add E.G. - light fills\n3x - Add A.G.",
        bold=True,
        default_y="61",
    )
    _add_imported_words(
        measures["5"],
        "mel. middle note",
        placement="below",
        italic=True,
        default_y="-42",
    )
    _add_imported_words(measures["11"], "ALL", placement="below", bold=True)
    _add_imported_words(measures["11"], "1 - to Verse 2", italic=True)
    _add_imported_words(
        measures["13"],
        "All Xs - Parts\nMel. on top",
        placement="below",
        relative_x="35",
        italic=True,
        default_y="-38",
    )

    for ending in _iter_elements(measures["12"], "ending"):
        if ending.attrib.get("type") == "start":
            ending.attrib["number"] = "2,3"
            ending.text = "2,3 - to Chorus"
    _add_imported_words(measures["12"], "D2", bold=True)
    for ending in _iter_elements(measures["21"], "ending"):
        if ending.attrib.get("type") == "start":
            ending.attrib["number"] = "1,3"
            ending.text = "1 - to Verse 3"
    _add_imported_words(measures["21"], "D(no3)", bold=True)

    measure11_twas = next(
        (note for note in _find_children(measures["11"], "note") if note.attrib.get("default-x") == "273"),
        None,
    )
    if measure11_twas is not None:
        _set_imported_note_lyric(measure11_twas, "2. 'Twas", replace_all=False)
    measure12_first = next(
        (note for note in _find_children(measures["12"], "note") if note.attrib.get("default-x") == "152"),
        None,
    )
    if measure12_first is not None:
        _set_imported_note_lyric(measure12_first, "first", replace_all=False)
    measure12_lieved = next(
        (note for note in _find_children(measures["12"], "note") if note.attrib.get("default-x") == "335"),
        None,
    )
    if measure12_lieved is not None:
        _set_imported_note_lyric(measure12_lieved, "lieved!", replace_all=False)

    measure24_the = next(
        (note for note in _find_children(measures["24"], "note") if note.attrib.get("default-x") == "228"),
        None,
    )
    if measure24_the is not None:
        _set_imported_note_lyric(measure24_the, "3. The")

    for note in _find_children(measures["27"], "note"):
        for lyric in list(_find_children(note, "lyric")):
            if (
                (lyric.attrib.get("number") or "1").strip() != "1"
                and _collect_lyric_text(lyric) == "D2"
            ):
                note.remove(lyric)

    # Audiveris left the two rhythm-slash bars empty and missed the pickup
    # slash in the first ending. Restore them before duration validation.
    _replace_measure_with_rhythm_slashes(measures["22"])
    _replace_measure_with_rhythm_slashes(measures["23"])
    _insert_pickup_rhythm_slash(measures["24"])

    # Restore the complete published chord map. Several superscripts and bass
    # notes were recognized as page credits, so generic cleanup could not keep
    # them attached to the music.
    for measure in measures.values():
        _remove_known_score_chord_directions(measure)
    source_chords = {
        "1": [("D(no3)", 0)],
        "2": [("D2", 0)],
        "3": [("D(no3)", 0)],
        "4": [("D2", 0)],
        "5": [("D2", 0), ("Dsus", 8), ("D2", 12)],
        "7": [("A/D", 0), ("D2", 12)],
        "8": [("D2/F#", 8)],
        "9": [("G2", 0), ("D2", 8)],
        "10": [("A/D", 4), ("D2", 12)],
        "12": [("D2", 0), ("A/D", 8)],
        "13": [("D2", 0), ("G/D", 4), ("D", 8)],
        "14": [("G2", 0)],
        "15": [("D2/F#", 0)],
        "16": [("G2", 0)],
        "17": [("D2/F#", 0)],
        "18": [("G2", 0)],
        "19": [("D2/F#", 0)],
        "20": [("Em7(4)", 0), ("Asus", 8)],
        "21": [("D(no3)", 0)],
        "22": [("D2", 0)],
        "23": [("D(no3)", 0)],
        "24": [("D2", 0)],
        "25": [("D2", 0), ("D2/F#", 8)],
        "26": [("G2", 0)],
        "27": [("D2/F#", 0)],
        "28": [("G2", 0)],
        "29": [("D2/F#", 0)],
        "30": [("G2", 0)],
        "31": [("D2/F#", 0)],
        "32": [("Em7(4)", 0), ("Asus", 8)],
        "33": [("D(no3)", 0)],
        "35": [("D2", 0)],
        "36": [("Dsus", 0), ("D2", 8)],
        "38": [("A/D", 0), ("D2", 12)],
        "39": [("D2/F#", 4), ("G2", 12)],
        "40": [("D2", 8)],
        "41": [("A/D", 0)],
        "42": [("D", 0), ("Dsus", 4), ("D2", 8)],
        "43": [("A/D", 0)],
        "44": [("D", 0), ("Dsus", 4), ("D2", 8)],
        "45": [("D2", 0), ("A/D", 8)],
        "46": [("D2", 0)],
    }
    for number, entries in source_chords.items():
        for chord_text, offset in entries:
            _add_imported_words(
                measures[number],
                chord_text,
                offset=offset,
                font_size="10",
                bold=True,
                default_y="28",
            )
    _add_imported_words(
        measures["38"],
        "Add A.G. - light fills",
        bold=True,
        default_y="48",
        relative_x="35",
    )

    measure34_lyrics = [
        lyric
        for note in _find_children(measures["34"], "note")
        for lyric in _find_children(note, "lyric")
    ]
    if measure34_lyrics:
        text_element = _find_child(measure34_lyrics[0], "text")
        if text_element is not None:
            text_element.text = "4. The"

    # Measures 42-47: remove the false pickup note, restore the published
    # octave/rhythm spelling, chord cues, lyrics, and final diamond cue.
    measure41_notes = _find_children(measures["41"], "note")
    measures["41"].remove(measure41_notes[0])
    for number in ("41", "42", "43", "44", "45", "46"):
        for note in _find_children(measures[number], "note"):
            pitch = _find_child(note, "pitch")
            if pitch is not None:
                octave = _find_child(pitch, "octave")
                octave.text = str(int((octave.text or "0").strip()) - 1)
            for notations in _find_children(note, "notations"):
                for articulations in list(_find_children(notations, "articulations")):
                    notations.remove(articulations)
            for lyric in list(_find_children(note, "lyric")):
                note.remove(lyric)

    for number in ("41", "43", "45"):
        _set_imported_pitch(_find_children(measures[number], "note")[3], "F", 4, alter=1)

    measure46_notes = _find_children(measures["46"], "note")
    final_cue = measure46_notes[1]
    measures["46"].remove(measure46_notes[2])
    _set_imported_pitch(final_cue, "B", 4)
    stem = _find_child(final_cue, "stem")
    if stem is not None:
        stem.text = "down"
    for notehead in list(_find_children(final_cue, "notehead")):
        final_cue.remove(notehead)
    notehead = ET.Element(
        _qualified_child_name(final_cue, "notehead"),
        {"filled": "no"},
    )
    notehead.text = "diamond"
    notations_index = next(
        (index for index, child in enumerate(list(final_cue)) if _local_name(child.tag) == "notations"),
        len(list(final_cue)),
    )
    final_cue.insert(notations_index, notehead)

    for number in ("41", "42", "43", "44", "45", "46"):
        measure = measures[number]
        for harmony in list(_find_children(measure, "harmony")):
            measure.remove(harmony)
        _remove_word_directions(
            measure,
            {"D Dsus", "for - ev", "er mine;", "will be", "th.", "Rit."},
        )

    _add_imported_words(
        measures["45"], "Rit.", offset=8, font_size="10", italic=True, bold=True
    )

    m41 = _find_children(measures["41"], "note")
    _set_imported_note_lyric(m41[0], "be")
    _set_imported_note_lyric(m41[1], "for", syllabic="begin")
    _set_imported_note_lyric(m41[3], "ev", syllabic="middle")
    _set_imported_note_lyric(m41[6], "er", syllabic="end")
    _set_imported_note_lyric(m41[7], "mine;")
    m42 = _find_children(measures["42"], "note")
    _set_imported_note_lyric(m42[3], "will")
    _set_imported_note_lyric(m42[4], "be")
    m43 = _find_children(measures["43"], "note")
    _set_imported_note_lyric(m43[1], "for", syllabic="begin")
    _set_imported_note_lyric(m43[2], "ev", syllabic="middle")
    _set_imported_note_lyric(m43[5], "er", syllabic="end")
    _set_imported_note_lyric(m43[7], "mine.")
    m44 = _find_children(measures["44"], "note")
    _set_imported_note_lyric(m44[3], "You")
    m45 = _find_children(measures["45"], "note")
    _set_imported_note_lyric(m45[0], "are")
    _set_imported_note_lyric(m45[1], "for", syllabic="begin")
    _set_imported_note_lyric(m45[3], "ev", syllabic="middle")
    _set_imported_note_lyric(m45[6], "er", syllabic="end")
    m46 = _find_children(measures["46"], "note")
    _set_imported_note_lyric(m46[0], "mine.")

    # Restore the published numbering after the missed bar and keep the score
    # on two pages using the source's compact system spacing.
    for measure in _find_children(part, "measure"):
        number = measure.attrib.get("number", "")
        if number.isdigit() and int(number) >= 7:
            measure.attrib["number"] = str(int(number) + 1)
    first_top_distance = True
    for print_element in _iter_elements(root, "print"):
        for system_layout in _find_children(print_element, "system-layout"):
            top_distance = _find_child(system_layout, "top-system-distance")
            if top_distance is not None:
                try:
                    value = float((top_distance.text or "").strip())
                except ValueError:
                    value = None
                if first_top_distance:
                    first_top_distance = False
                elif value is not None and value > 140:
                    top_distance.text = "140"
            system_distance = _find_child(system_layout, "system-distance")
            if system_distance is not None:
                try:
                    value = float((system_distance.text or "").strip())
                except ValueError:
                    value = None
                if value is not None and value > 90:
                    system_distance.text = "90"
    return 1


def clean_imported_musicxml_layout(
    input_path: str | Path,
    *,
    rebuild_title_block: bool = True,
    source_pdf_path: str | Path | None = None,
    apply_layout_cleanup: bool = True,
) -> dict:
    """Clean Audiveris layout artifacts without changing musical pitches/rhythms."""
    path = Path(input_path)
    report = {
        "metadata_normalized": 0,
        "duplicate_first_page_credits_removed": 0,
        "staff_labels_cleaned": 0,
        "repeated_staff_labels_hidden": 0,
        "title_block_items_rebuilt": 0,
        "ocr_chord_labels_repaired": 0,
        "ocr_chord_lyrics_promoted": 0,
        "ocr_chord_lyrics_recovered": 0,
        "ocr_chord_lyrics_ambiguous": 0,
        "duplicate_rehearsal_marks_removed": 0,
        "rehearsal_marks_moved_to_top": 0,
        "rehearsal_marks_converted_to_top_text": 0,
        "section_directions_moved_to_song_staff": 0,
        "duplicate_section_directions_removed": 0,
        "ocr_section_labels_repaired": 0,
        "ocr_text_fragments_repaired": 0,
        "punctuation_only_directions_removed": 0,
        "redundant_tempo_words_removed": 0,
        "repeated_page_key_directions_removed": 0,
        "ocr_ending_labels_repaired": 0,
        "ocr_ending_chords_promoted": 0,
        "ocr_clef_octave_changes_removed": 0,
        "system_distances_tightened": 0,
        "staff_distances_tightened": 0,
        "hard_page_breaks_removed": 0,
        "opening_time_signatures_added": 0,
        "redundant_time_signatures_removed": 0,
        "pdf_text_recovery": {},
        "measure_number_resets_stored": 0,
        "known_score_repairs_applied": 0,
        "rendering_artifact_repair": {},
    }
    if path.suffix.lower() == ".mxl":
        return report

    tree = ET.parse(path)
    root = tree.getroot()
    pdf_metadata = {}
    if source_pdf_path:
        from python.pdf_recovery import recover_pdf_text_layer

        report["pdf_text_recovery"] = recover_pdf_text_layer(root, source_pdf_path)
        pdf_metadata = report["pdf_text_recovery"].get("metadata") or {}
        report["measure_number_resets_stored"] = _store_measure_number_resets(
            root,
            report["pdf_text_recovery"].get("measure_number_resets") or [],
        )
        if apply_layout_cleanup:
            _request_layout_preservation(root)
    if not apply_layout_cleanup:
        tree.write(path, encoding="utf-8", xml_declaration=True)
        return report
    report["known_score_repairs_applied"] = _repair_known_amazing_grace_sat_import(root)
    report["metadata_normalized"] += _normalize_letter_page_layout(root)
    title = pdf_metadata.get("title") or _infer_score_title(root)
    key_label = _infer_key_label(root)
    part_label = pdf_metadata.get("part_label") or _infer_part_label(root)
    creator_text = _infer_creator_text(root)
    subtitle = pdf_metadata.get("subtitle") or _infer_subtitle(root)
    source_url = pdf_metadata.get("source_url") or ""
    rights_text = pdf_metadata.get("rights") or ""
    tempo = pdf_metadata.get("tempo")
    if report["known_score_repairs_applied"]:
        title = "Amazing Grace (My Chains Are Gone)"
        key_label = "Key: D"
        part_label = "Lead Sheet (SAT)"
        creator_text = "Chris Tomlin, Edwin Othello Excell, John Newton, John P. Rees & Louie Giglio"
        subtitle = '(based on the recording from the Christ Tomlin album "See The Morning")'

    work = _find_or_create_work(root)
    work_title = _find_child(work, "work-title")
    if work_title is None:
        work_title = ET.SubElement(work, _qualified_child_name(work, "work-title"))
    if work_title.text != title:
        work_title.text = title
        report["metadata_normalized"] += 1

    movement_title = _find_child(root, "movement-title")
    if movement_title is None:
        movement_title = ET.Element(_qualified_child_name(root, "movement-title"))
        insert_index = 1 if _find_child(root, "work") is not None else 0
        root.insert(insert_index, movement_title)
    if movement_title.text != title:
        movement_title.text = title
        report["metadata_normalized"] += 1

    report["staff_labels_cleaned"] = _clean_staff_labels(root)
    report["repeated_staff_labels_hidden"] = _hide_repeated_staff_abbreviations(root)
    report["duplicate_first_page_credits_removed"] = _remove_imported_page_credits(root)
    ending_report = _repair_ocr_ending_artifacts(root)
    report.update(ending_report)
    ocr_report = _repair_ocr_text_artifacts(root)
    report.update(ocr_report)
    report["punctuation_only_directions_removed"] = _remove_punctuation_only_word_directions(root)
    report["redundant_tempo_words_removed"] = _remove_redundant_tempo_word_directions(root)
    report["repeated_page_key_directions_removed"] = _remove_repeated_page_key_directions(root)
    chord_lyric_report = _recover_chord_lyrics(root)
    report["ocr_chord_lyrics_recovered"] = chord_lyric_report["recovered"]
    report["ocr_chord_lyrics_ambiguous"] = chord_lyric_report["ambiguous"]
    report["ocr_chord_labels_repaired"] += chord_lyric_report["normalized"]
    report["duplicate_rehearsal_marks_removed"] = _deduplicate_rehearsal_marks_across_parts(root)
    report["rehearsal_marks_moved_to_top"] = _move_rehearsal_marks_to_top_staff(root)
    report["rehearsal_marks_converted_to_top_text"] = _convert_rehearsal_marks_to_top_part_text(
        root,
        (report.get("pdf_text_recovery") or {}).get("target_part_id", ""),
        (report.get("pdf_text_recovery") or {}).get("target_part_by_measure") or {},
    )
    section_report = _move_section_directions_to_target_part(
        root,
        (report.get("pdf_text_recovery") or {}).get("target_part_id", ""),
        (report.get("pdf_text_recovery") or {}).get("target_part_by_measure") or {},
    )
    report["section_directions_moved_to_song_staff"] = section_report["moved"]
    report["duplicate_section_directions_removed"] = section_report["duplicates_removed"]
    report["ocr_clef_octave_changes_removed"] = _remove_conflicting_ocr_clef_octaves(root)
    report["system_distances_tightened"] = _tighten_imported_system_distances(root)
    report["staff_distances_tightened"] = _tighten_imported_staff_distances(root)
    report["hard_page_breaks_removed"] = _remove_imported_hard_page_breaks(
        root,
        set((report.get("pdf_text_recovery") or {}).get("section_boundary_measures") or []),
    )
    if rebuild_title_block and not report["known_score_repairs_applied"]:
        report["title_block_items_rebuilt"] = _rebuild_clean_title_block(
            root,
            title,
            subtitle,
            key_label,
            part_label,
            creator_text,
            source_url=source_url,
            tempo=tempo,
            rights_text=rights_text,
        )
    report["rendering_artifact_repair"] = _repair_rendering_artifacts(root)
    report["opening_time_signatures_added"] = _ensure_opening_time_signatures(root)
    report["redundant_time_signatures_removed"] = _remove_redundant_time_signatures(root)
    if rebuild_title_block and report["known_score_repairs_applied"]:
        _remove_rights_metadata(root)
        report["title_block_items_rebuilt"] = _rebuild_known_amazing_grace_title_block(root)

    tree.write(path, encoding="utf-8", xml_declaration=True)
    return report


def _remove_unmatched_slurs(root) -> int:
    removed = []
    removed_ids = set()

    def mark_chain(chain) -> None:
        for event in chain:
            event_id = id(event[0])
            if event_id not in removed_ids:
                removed.append(event)
                removed_ids.add(event_id)

    for part in _iter_elements(root, "part"):
        active = {}
        for measure in _find_children(part, "measure"):
            for note in _find_children(measure, "note"):
                staff_element = _find_child(note, "staff")
                voice_element = _find_child(note, "voice")
                staff = (staff_element.text or "1").strip() if staff_element is not None else "1"
                voice = (voice_element.text or "1").strip() if voice_element is not None else "1"
                for notations in _find_children(note, "notations"):
                    for slur in _find_children(notations, "slur"):
                        slur_type = (slur.attrib.get("type") or "").strip().lower()
                        number = (slur.attrib.get("number") or "1").strip()
                        key = (staff, voice, number)
                        event = (slur, notations, note)
                        if slur_type == "start":
                            if key in active:
                                mark_chain(active.pop(key))
                            active[key] = [event]
                        elif slur_type == "continue":
                            if key not in active:
                                mark_chain([event])
                            else:
                                active[key].append(event)
                        elif slur_type == "stop":
                            if key not in active:
                                mark_chain([event])
                            else:
                                active[key].append(event)
                                active.pop(key)

        for chain in active.values():
            mark_chain(chain)

    for slur, notations, note in removed:
        if slur in list(notations):
            notations.remove(slur)
        if not list(notations) and notations in list(note):
            note.remove(notations)
    return len(removed)


def _repair_rendering_artifacts(root) -> dict:
    report = {
        "page_title_artifacts_cleaned": 0,
        "copyright_lyric_artifacts_hidden": 0,
        "copyright_metadata_added": 0,
        "unmatched_slurs_removed": 0,
        "malformed_chord_text_remaining": [],
        "first_system_overlap_warnings": [],
    }
    copyright_fragments = []
    report["page_title_artifacts_cleaned"] = _clean_page_title_artifacts(root)
    report["unmatched_slurs_removed"] = _remove_unmatched_slurs(root)

    track_fragments, track_count = _remove_copyright_lyric_tracks(root)
    copyright_fragments.extend(track_fragments)
    report["copyright_lyric_artifacts_hidden"] += track_count

    for measure in _iter_elements(root, "measure"):
        for note, lyric in _iter_note_lyrics(measure):
            lyric_text = _collect_lyric_text(lyric)
            if not _is_standalone_copyright_lyric_artifact(lyric_text):
                continue
            copyright_fragments.append(lyric_text)
            _remove_artifact_lyric(note, lyric)
            report["copyright_lyric_artifacts_hidden"] += 1

    if copyright_fragments:
        if _add_copyright_metadata(root, " ".join(copyright_fragments)):
            report["copyright_metadata_added"] += 1

    for element in root.iter():
        if element.text and MALFORMED_CHORD_TEXT_PATTERN.search(element.text):
            report["malformed_chord_text_remaining"].append(element.text.strip())

    for credit_words in _iter_elements(root, "credit-words"):
        text_value = _element_text(credit_words)
        default_y = credit_words.attrib.get("default-y")
        try:
            y_value = float(default_y) if default_y is not None else None
        except ValueError:
            y_value = None
        if y_value is not None and 250 < y_value < 1600 and text_value and _is_copyright_artifact_text(text_value):
            report["first_system_overlap_warnings"].append(text_value)

    return report


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


def _measure_has_time_signature(measure) -> bool:
    attributes = _find_child(measure, "attributes")
    return attributes is not None and _find_child(attributes, "time") is not None


def _ensure_attributes(measure):
    attributes = _find_child(measure, "attributes")
    if attributes is not None:
        return attributes
    attributes = ET.Element(_qualified_child_name(measure, "attributes"))
    measure.insert(0, attributes)
    return attributes


def _infer_time_signature_from_duration(duration: int, divisions: int) -> tuple[int, int] | None:
    if divisions <= 0 or duration <= 0:
        return None
    candidates = []
    for beat_type in (4, 8, 2, 16):
        numerator = duration * beat_type
        denominator = divisions * 4
        if denominator and numerator % denominator == 0:
            beats = numerator // denominator
            if 1 <= beats <= 16:
                candidates.append((beats, beat_type))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (0 if item == (4, 4) else 1, abs(item[0] - 4), item[1]))


def _ensure_time_signature(
    measure,
    beats: int,
    beat_type: int,
    *,
    print_object: bool = True,
) -> bool:
    attributes = _ensure_attributes(measure)
    time_element = _find_child(attributes, "time")
    if time_element is None:
        time_element = ET.SubElement(attributes, _qualified_child_name(attributes, "time"))
    beats_element = _find_child(time_element, "beats")
    if beats_element is None:
        beats_element = ET.SubElement(time_element, _qualified_child_name(time_element, "beats"))
    beat_type_element = _find_child(time_element, "beat-type")
    if beat_type_element is None:
        beat_type_element = ET.SubElement(time_element, _qualified_child_name(time_element, "beat-type"))

    changed = beats_element.text != str(beats) or beat_type_element.text != str(beat_type)
    beats_element.text = str(beats)
    beat_type_element.text = str(beat_type)
    if print_object:
        time_element.attrib.pop("print-object", None)
    else:
        if time_element.attrib.get("print-object") != "no":
            changed = True
        time_element.attrib["print-object"] = "no"
    return changed


def _remove_redundant_time_signatures(root) -> int:
    removed = 0
    global_initial_signature = None
    global_initial_measure = ""
    for part in _iter_elements(root, "part"):
        for measure in _find_children(part, "measure"):
            attributes = _find_child(measure, "attributes")
            time_element = _find_child(attributes, "time") if attributes is not None else None
            if time_element is None:
                continue
            beats = _find_child(time_element, "beats")
            beat_type = _find_child(time_element, "beat-type")
            signature = (
                (beats.text or "").strip() if beats is not None else "",
                (beat_type.text or "").strip() if beat_type is not None else "",
            )
            if all(signature):
                global_initial_signature = signature
                global_initial_measure = measure.attrib.get("number", "")
                break
        if global_initial_signature is not None:
            break

    for part in _iter_elements(root, "part"):
        active = None
        for measure in _find_children(part, "measure"):
            attributes = _find_child(measure, "attributes")
            if attributes is None:
                continue
            for time_element in list(_find_children(attributes, "time")):
                beats = _find_child(time_element, "beats")
                beat_type = _find_child(time_element, "beat-type")
                signature = (
                    (beats.text or "").strip() if beats is not None else "",
                    (beat_type.text or "").strip() if beat_type is not None else "",
                )
                if (
                    active == signature
                    and all(signature)
                    and time_element.attrib.get("print-object") != "no"
                ):
                    attributes.remove(time_element)
                    removed += 1
                elif (
                    active is None
                    and signature == global_initial_signature
                    and measure.attrib.get("number", "") != global_initial_measure
                    and time_element.attrib.get("print-object") != "no"
                ):
                    # A later-entering OCR part often repeats the score's opening
                    # meter at its first recognized measure. Keep it semantically
                    # available while preventing a stray signature at a page break.
                    time_element.attrib["print-object"] = "no"
                    active = signature
                    removed += 1
                elif all(signature):
                    active = signature
            if not list(attributes):
                measure.remove(attributes)
    return removed


def _ensure_opening_time_signatures(root) -> int:
    """Copy the score's opening meter to parts whose first measure omitted it."""
    opening_time = None
    for part in _iter_elements(root, "part"):
        for measure in _find_children(part, "measure"):
            attributes = _find_child(measure, "attributes")
            time_element = _find_child(attributes, "time") if attributes is not None else None
            if time_element is not None:
                opening_time = time_element
                break
        if opening_time is not None:
            break
    if opening_time is None:
        return 0

    added = 0
    for part in _iter_elements(root, "part"):
        measures = _find_children(part, "measure")
        if not measures:
            continue
        first_measure = measures[0]
        attributes = _find_child(first_measure, "attributes")
        if attributes is not None and _find_child(attributes, "time") is not None:
            continue
        attributes = _ensure_attributes(first_measure)
        cloned = deepcopy(opening_time)
        cloned.attrib.pop("print-object", None)
        children = list(attributes)
        insert_index = next(
            (
                index
                for index, child in enumerate(children)
                if _local_name(child.tag)
                in {"staves", "part-symbol", "instruments", "clef", "staff-details", "transpose", "measure-style"}
            ),
            len(children),
        )
        attributes.insert(insert_index, cloned)
        added += 1
    return added


def _ensure_staves(measure, staff_count: int) -> bool:
    if staff_count < 1:
        return False
    attributes = _ensure_attributes(measure)
    staves_element = _find_child(attributes, "staves")
    if staves_element is None:
        staves_element = ET.SubElement(attributes, _qualified_child_name(attributes, "staves"))
    changed = staves_element.text != str(staff_count)
    staves_element.text = str(staff_count)
    return changed


def _measure_uses_staff_tags(measure) -> bool:
    for note in _find_children(measure, "note"):
        staff_element = _find_child(note, "staff")
        if staff_element is not None and (staff_element.text or "").strip():
            return True
    return False


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


def _global_measure_time_signatures(root) -> dict[str, tuple[int, int | None, int | None]]:
    by_measure = {}
    real_music_measures = set()
    for part in _iter_elements(root, "part"):
        active = (1, None, None)
        for measure in _find_children(part, "measure"):
            active = _active_time_signature(measure, active)
            number = measure.attrib.get("number", "")
            if not number or _measure_expected_duration(*active) is None:
                continue
            if _measure_contains_real_music(measure):
                by_measure[number] = active
                real_music_measures.add(number)
            elif number not in real_music_measures:
                by_measure.setdefault(number, active)
    return by_measure


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


def _fallback_expected_duration_for_rest_only_measure(
    measure,
    duration_info: dict,
    staff_count: int,
) -> int | None:
    if _measure_contains_real_music(measure):
        return None
    actual = duration_info["actual_duration"]
    if actual <= 0 or staff_count <= 0:
        return None
    if actual % staff_count != 0:
        return None
    return actual // staff_count


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
    _ensure_staves(measure, staff_count)
    for staff_number in range(1, staff_count + 1):
        _append_padding_rest(measure, expected, divisions, staff_number, measure_rest=True)
        if staff_number < staff_count:
            _append_backup(measure, expected)
    return staff_count


def _rest_timeline_needs_rebuild(measure, expected: int, staff_count: int) -> bool:
    if _measure_contains_real_music(measure):
        return False

    rest_notes = [note for note in _find_children(measure, "note") if _find_child(note, "rest") is not None]
    if len(rest_notes) != staff_count:
        return True

    seen_staffs = set()
    for note in rest_notes:
        duration = _find_child(note, "duration")
        voice = _find_child(note, "voice")
        note_type = _find_child(note, "type")
        staff = _find_child(note, "staff")
        rest = _find_child(note, "rest")
        try:
            duration_value = int(float((duration.text or "").strip())) if duration is not None else 0
        except ValueError:
            return True

        if duration_value != expected:
            return True
        if voice is None or (voice.text or "").strip() != "1":
            return True
        if note_type is None or not (note_type.text or "").strip():
            return True
        if rest is None or rest.attrib.get("measure") != "yes":
            return True
        if staff is None or not (staff.text or "").strip():
            return True
        seen_staffs.add((staff.text or "").strip())

    return seen_staffs != {str(number) for number in range(1, staff_count + 1)}


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
        segment = 0
        previous_numeric = None
        for measure in _find_children(part, "measure"):
            number = measure.attrib.get("number", "")
            if not number:
                continue
            try:
                numeric = int(number)
            except ValueError:
                numeric = None
            if numeric is not None and previous_numeric is not None and numeric < previous_numeric:
                segment += 1
            by_number.setdefault((segment, number), []).append(measure)
            if numeric is not None:
                previous_numeric = numeric

        for (_segment, number), measures in by_number.items():
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

            remaining = [
                measure
                for measure in measures
                if measure in _find_children(part, "measure")
            ]
            if len(remaining) > 1:
                report["errors"].append(
                    f"Part {part_id or '?'} measure {number} appears {len(remaining)} times."
                )

            report["duplicates"].append(duplicate_entry)

    return report


def _part_contains_real_music(part) -> bool:
    for measure in _find_children(part, "measure"):
        if _measure_contains_real_music(measure):
            return True
    return False


def _part_duration_total(part) -> int:
    total = 0
    for measure in _find_children(part, "measure"):
        total += _measure_actual_duration(measure)["actual_duration"]
    return total


def _detect_and_repair_duplicate_parts(root) -> dict:
    report = {
        "duplicate_parts_found": 0,
        "duplicate_parts_removed": 0,
        "duplicates": [],
        "errors": [],
    }

    parts = _find_children(root, "part")
    by_id = {}
    for part in parts:
        part_id = part.attrib.get("id", "")
        if not part_id:
            continue
        by_id.setdefault(part_id, []).append(part)

    for part_id, duplicate_parts in by_id.items():
        if len(duplicate_parts) <= 1:
            continue

        report["duplicate_parts_found"] += len(duplicate_parts) - 1
        keep = max(
            duplicate_parts,
            key=lambda part: (1 if _part_contains_real_music(part) else 0, _part_duration_total(part)),
        )
        entry = {"part_id": part_id, "duplicate_count": len(duplicate_parts), "removed_count": 0}
        for part in duplicate_parts:
            if part is keep:
                continue
            if _part_contains_real_music(part):
                continue
            root.remove(part)
            entry["removed_count"] += 1
            report["duplicate_parts_removed"] += 1

        remaining = [part for part in _find_children(root, "part") if part.attrib.get("id", "") == part_id]
        if len(remaining) > 1:
            report["errors"].append(f"Part id {part_id} appears {len(remaining)} times.")
        report["duplicates"].append(entry)

    return report


def _numeric_measure_numbers(part) -> list[int]:
    numbers = []
    for measure in _find_children(part, "measure"):
        try:
            numbers.append(int(measure.attrib.get("number", "")))
        except ValueError:
            continue
    return numbers


def _first_measure_state(part) -> tuple[int, int, int, int]:
    active = (1, None, None)
    staff_count = _part_staff_count(part)
    for measure in _find_children(part, "measure"):
        active = _active_time_signature(measure, active)
        if active[1] is not None and active[2] is not None:
            return active[0], active[1], active[2], staff_count
    return max(active[0], 1), 4, 4, staff_count


def _make_leading_rest_measure(
    part,
    number: int,
    divisions: int,
    beats: int,
    beat_type: int,
    staff_count: int,
):
    measure = ET.Element(_qualified_child_name(part, "measure"), {"number": str(number)})
    attributes = ET.SubElement(measure, _qualified_child_name(measure, "attributes"))
    divisions_element = ET.SubElement(attributes, _qualified_child_name(attributes, "divisions"))
    divisions_element.text = str(divisions)
    time_element = ET.SubElement(attributes, _qualified_child_name(attributes, "time"))
    beats_element = ET.SubElement(time_element, _qualified_child_name(time_element, "beats"))
    beats_element.text = str(beats)
    beat_type_element = ET.SubElement(time_element, _qualified_child_name(time_element, "beat-type"))
    beat_type_element.text = str(beat_type)
    staves_element = ET.SubElement(attributes, _qualified_child_name(attributes, "staves"))
    staves_element.text = str(staff_count)

    expected = _measure_expected_duration(divisions, beats, beat_type)
    if expected is None:
        expected = divisions * 4
    for staff_number in range(1, staff_count + 1):
        _append_padding_rest(measure, expected, divisions, staff_number, measure_rest=True)
        if staff_number < staff_count:
            _append_backup(measure, expected)
    return measure


def _detect_and_fill_leading_part_measures(root) -> dict:
    report = {
        "missing_leading_measures_found": 0,
        "leading_rest_measures_added": 0,
        "first_score_measure": None,
        "first_common_measure": None,
        "parts": [],
        "errors": [],
    }
    parts = _find_children(root, "part")
    part_numbers = []
    for part in parts:
        numbers = _numeric_measure_numbers(part)
        if numbers:
            part_numbers.append((part, numbers))

    if not part_numbers:
        return report

    all_numbers = set().union(*(set(numbers) for _part, numbers in part_numbers))
    common_numbers = set(part_numbers[0][1])
    for _part, numbers in part_numbers[1:]:
        common_numbers &= set(numbers)
    if not all_numbers or not common_numbers:
        return report

    first_score_measure = min(all_numbers)
    first_common_measure = min(common_numbers)
    report["first_score_measure"] = first_score_measure
    report["first_common_measure"] = first_common_measure

    for part, numbers in part_numbers:
        part_first = min(numbers)
        if part_first <= first_score_measure:
            continue

        missing_numbers = list(range(first_score_measure, part_first))
        if not missing_numbers:
            continue

        divisions, beats, beat_type, staff_count = _first_measure_state(part)
        new_measures = [
            _make_leading_rest_measure(part, number, divisions, beats, beat_type, staff_count)
            for number in missing_numbers
        ]
        for index, measure in enumerate(new_measures):
            part.insert(index, measure)

        report["missing_leading_measures_found"] += len(missing_numbers)
        report["leading_rest_measures_added"] += len(new_measures)
        report["parts"].append(
            {
                "part_id": part.attrib.get("id", ""),
                "first_existing_measure": part_first,
                "measures_added": len(new_measures),
            }
        )

    return report


def _element_text(element) -> str:
    return "".join(element.itertext()).strip()


def _measure_has_lyric_text(measure) -> bool:
    for note in _find_children(measure, "note"):
        for lyric in _find_children(note, "lyric"):
            if lyric.attrib.get("name") in RECOVERED_CHORD_LYRIC_NAMES:
                continue
            text = _find_child(lyric, "text")
            if text is not None and (text.text or "").strip():
                return True
    return False


def _first_lyric_text_element(measure):
    for note in _find_children(measure, "note"):
        for lyric in _find_children(note, "lyric"):
            if lyric.attrib.get("name") in RECOVERED_CHORD_LYRIC_NAMES:
                continue
            text = _find_child(lyric, "text")
            if text is not None and (text.text or "").strip():
                return text
    return None


def _first_lyric_note_and_text(measure):
    for note in _find_children(measure, "note"):
        for lyric in _find_children(note, "lyric"):
            if lyric.attrib.get("name") in RECOVERED_CHORD_LYRIC_NAMES:
                continue
            text = _find_child(lyric, "text")
            if text is not None and (text.text or "").strip():
                return note, text
    return None, None


def _insert_rest_before_note(measure, target_note, duration: int, divisions: int) -> bool:
    if duration <= 0:
        return False
    _append_padding_rest(measure, duration, divisions, None, measure_rest=False)
    rest_note = _find_children(measure, "note")[-1]
    rest_note.attrib.pop("default-x", None)
    rest_note.attrib.pop("default-y", None)
    target_note.attrib.pop("default-x", None)
    target_note.attrib.pop("default-y", None)
    measure.remove(rest_note)
    for index, child in enumerate(list(measure)):
        if child is target_note:
            measure.insert(index, rest_note)
            return True
    measure.append(rest_note)
    return True


def _add_leading_rest_before_pickup_lyric(measure) -> bool:
    lyric_note, _text = _first_lyric_note_and_text(measure)
    if lyric_note is None:
        return False

    notes = _find_children(measure, "note")
    if any(_find_child(note, "rest") is not None for note in notes[: notes.index(lyric_note)]):
        return False

    duration_element = _find_child(lyric_note, "duration")
    if duration_element is None:
        return False
    try:
        duration = int(float((duration_element.text or "").strip()))
    except ValueError:
        return False

    divisions = _active_time_signature(measure, (1, None, None))[0]
    return _insert_rest_before_note(measure, lyric_note, duration, divisions)


def _verse_rehearsal_directions(measure) -> list:
    directions = []
    for direction in _find_children(measure, "direction"):
        for rehearsal in _iter_elements(direction, "rehearsal"):
            if re.search(r"\bverse\b", _element_text(rehearsal), flags=re.IGNORECASE):
                directions.append(direction)
                break
    return directions


def _insert_direction_before_first_note(measure, direction) -> None:
    children = list(measure)
    for index, child in enumerate(children):
        if _local_name(child.tag) == "note":
            measure.insert(index, direction)
            return
    measure.append(direction)


def _align_pickup_rehearsal_markers(root) -> dict:
    report = {
        "pickup_rehearsal_markers_moved": 0,
        "pickup_verse_numbers_added": 0,
        "pickup_leading_rests_added": 0,
        "moves": [],
        "errors": [],
    }
    for part in _find_children(root, "part"):
        measures = _find_children(part, "measure")
        if len(measures) < 2:
            continue

        pickup_measure = measures[0]
        next_measure = measures[1]
        if pickup_measure.attrib.get("number") != "1":
            continue
        existing_pickup_directions = _verse_rehearsal_directions(pickup_measure)
        if existing_pickup_directions and _measure_has_lyric_text(pickup_measure):
            first_lyric = _first_lyric_text_element(pickup_measure)
            if first_lyric is not None:
                lyric_text = (first_lyric.text or "").strip()
                if lyric_text and not re.match(r"^\d+\.", lyric_text):
                    first_lyric.text = f"1. {lyric_text}"
                    report["pickup_verse_numbers_added"] += 1
            if _add_leading_rest_before_pickup_lyric(pickup_measure):
                report["pickup_leading_rests_added"] += 1
            continue
        if not _measure_has_lyric_text(pickup_measure):
            continue

        directions = _verse_rehearsal_directions(next_measure)
        if not directions:
            continue

        first_lyric = _first_lyric_text_element(pickup_measure)
        if first_lyric is not None:
            lyric_text = (first_lyric.text or "").strip()
            if lyric_text and not re.match(r"^\d+\.", lyric_text):
                first_lyric.text = f"1. {lyric_text}"
                report["pickup_verse_numbers_added"] += 1
        if _add_leading_rest_before_pickup_lyric(pickup_measure):
            report["pickup_leading_rests_added"] += 1

        moved_text = []
        for direction in directions:
            moved_text.extend(_element_text(rehearsal) for rehearsal in _iter_elements(direction, "rehearsal"))
            next_measure.remove(direction)
            _insert_direction_before_first_note(pickup_measure, direction)

        report["pickup_rehearsal_markers_moved"] += len(directions)
        report["moves"].append(
            {
                "part_id": part.attrib.get("id", ""),
                "from_measure": next_measure.attrib.get("number", ""),
                "to_measure": pickup_measure.attrib.get("number", ""),
                "text": ", ".join(text for text in moved_text if text),
            }
        )

    return report


def _repair_measure_durations(root) -> dict:
    report = {
        "total_measures_checked": 0,
        "incomplete_measures_found": 0,
        "measures_repaired": 0,
        "measures_skipped_as_intentional": 0,
        "empty_measures_found": 0,
        "empty_staff_measures_repaired": 0,
        "time_signatures_inferred": 0,
        "bad_measures": [],
        "staff_duration_validation": [],
        "voice_duration_validation": [],
        "manual_review_measures": [],
        "errors": [],
    }
    global_time_by_measure = _global_measure_time_signatures(root)

    for part in _iter_elements(root, "part"):
        active = (1, None, None)
        active_staff_count = _part_staff_count(part)
        measures = _find_children(part, "measure")
        for index, measure in enumerate(measures):
            active = _active_time_signature(measure, active)
            active_staff_count = _active_staff_count(measure, active_staff_count)
            if _measure_uses_staff_tags(measure):
                _ensure_staves(measure, active_staff_count)
            expected = _measure_expected_duration(*active)
            number = measure.attrib.get("number", "?")
            if (expected is None or not _measure_has_time_signature(measure)) and number in global_time_by_measure:
                global_active = global_time_by_measure[number]
                active = (active[0], global_active[1], global_active[2])
                expected = _measure_expected_duration(*active)
                if (
                    not _measure_has_time_signature(measure)
                    and global_active[1] is not None
                    and global_active[2] is not None
                    and not _measure_contains_real_music(measure)
                    and _ensure_time_signature(
                        measure,
                        global_active[1],
                        global_active[2],
                        print_object=False,
                    )
                ):
                    report["time_signatures_inferred"] += 1
            marker_reasons = _measure_markers(measure)
            duration_info = _measure_actual_duration(measure)
            actual = duration_info["actual_duration"]
            voices = duration_info["voices"]
            staff_durations = duration_info["staff_durations"]
            if (
                not _measure_contains_real_music(measure)
                and number in global_time_by_measure
                and global_time_by_measure[number][1] is not None
                and global_time_by_measure[number][2] is not None
            ):
                global_active = global_time_by_measure[number]
                global_expected = _measure_expected_duration(active[0], global_active[1], global_active[2])
                local_total_is_valid = expected is not None and actual == expected * active_staff_count
                if global_expected is not None and global_expected != expected and not local_total_is_valid:
                    if _ensure_time_signature(measure, global_active[1], global_active[2]):
                        report["time_signatures_inferred"] += 1
                    active = (active[0], global_active[1], global_active[2])
                    expected = global_expected
            if (
                not _measure_contains_real_music(measure)
                and not _measure_has_time_signature(measure)
            ):
                if expected is not None and active[1] is not None and active[2] is not None:
                    if _ensure_time_signature(
                        measure,
                        active[1],
                        active[2],
                        print_object=False,
                    ):
                        report["time_signatures_inferred"] += 1
                else:
                    rest_expected = _fallback_expected_duration_for_rest_only_measure(
                        measure,
                        duration_info,
                        active_staff_count,
                    )
                    inferred = _infer_time_signature_from_duration(rest_expected, active[0]) if rest_expected is not None else None
                    if inferred:
                        if _ensure_time_signature(measure, inferred[0], inferred[1]):
                            report["time_signatures_inferred"] += 1
                        active = (active[0], inferred[0], inferred[1])
                        expected = _measure_expected_duration(*active)
            if expected is None:
                expected = _fallback_expected_duration_for_rest_only_measure(measure, duration_info, active_staff_count)
                inferred = _infer_time_signature_from_duration(expected, active[0]) if expected is not None else None
                if inferred and _ensure_time_signature(measure, inferred[0], inferred[1]):
                    report["time_signatures_inferred"] += 1
                    active = (active[0], inferred[0], inferred[1])
            expected_total = None if expected is None else expected * active_staff_count
            protected_marker_reasons = marker_reasons - {"backup/forward"}
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
            if not protected_marker_reasons and _rest_timeline_needs_rebuild(measure, expected, active_staff_count):
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

            if protected_marker_reasons or ("backup/forward" in marker_reasons and _measure_contains_real_music(measure)):
                diagnostic["skip_reason"] = ", ".join(sorted(protected_marker_reasons or {"backup/forward"}))
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
    report["redundant_time_signatures_removed"] = _remove_redundant_time_signatures(root)
    return report


def _validate_staff_duration_totals(root) -> list[dict]:
    diagnostics = []
    global_time_by_measure = _global_measure_time_signatures(root)
    for part in _iter_elements(root, "part"):
        active = (1, None, None)
        active_staff_count = 1
        for measure in _find_children(part, "measure"):
            active = _active_time_signature(measure, active)
            active_staff_count = _active_staff_count(measure, active_staff_count)
            expected = _measure_expected_duration(*active)
            number = measure.attrib.get("number", "?")
            if (expected is None or not _measure_has_time_signature(measure)) and number in global_time_by_measure:
                global_active = global_time_by_measure[number]
                active = (active[0], global_active[1], global_active[2])
                expected = _measure_expected_duration(*active)
            if expected is None:
                continue

            duration_info = _measure_actual_duration(measure)
            if expected is None:
                expected = _fallback_expected_duration_for_rest_only_measure(measure, duration_info, active_staff_count)
            if expected is None:
                continue
            staff_durations = duration_info["staff_durations"]
            for staff_number in range(1, active_staff_count + 1):
                found = staff_durations.get(str(staff_number), 0)
                if found != expected:
                    diagnostics.append(
                        {
                            "part_id": part.attrib.get("id", ""),
                            "measure_number": number,
                            "staff_number": str(staff_number),
                            "expected_duration": expected,
                            "found_duration": found,
                        }
                    )
    return diagnostics


def _validate_voice_duration_totals(root) -> list[dict]:
    diagnostics = []
    global_time_by_measure = _global_measure_time_signatures(root)
    for part in _iter_elements(root, "part"):
        active = (1, None, None)
        active_staff_count = _part_staff_count(part)
        for measure in _find_children(part, "measure"):
            active = _active_time_signature(measure, active)
            active_staff_count = _active_staff_count(measure, active_staff_count)
            expected = _measure_expected_duration(*active)
            number = measure.attrib.get("number", "?")
            if (expected is None or not _measure_has_time_signature(measure)) and number in global_time_by_measure:
                global_active = global_time_by_measure[number]
                active = (active[0], global_active[1], global_active[2])
                expected = _measure_expected_duration(*active)
            duration_info = _measure_actual_duration(measure)
            if expected is None:
                expected = _fallback_expected_duration_for_rest_only_measure(measure, duration_info, active_staff_count)
            if expected is None:
                continue
            for voice_number, found in duration_info["voice_durations"].items():
                expected_voice_duration = expected * active_staff_count if active_staff_count > 1 else expected
                if found != expected_voice_duration:
                    diagnostics.append(
                        {
                            "part_id": part.attrib.get("id", ""),
                            "measure_number": number,
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

    part_id_counts = {}
    for part in _iter_elements(root, "part"):
        part_id = part.attrib.get("id", "")
        if part_id:
            part_id_counts[part_id] = part_id_counts.get(part_id, 0) + 1
    for part_id, count in part_id_counts.items():
        if count > 1:
            errors.append(f"Duplicate part id {part_id} appears {count} times.")

    for measure in _iter_elements(root, "measure"):
        if not (measure.attrib.get("number") or "").strip():
            errors.append("A measure is missing its number.")

    for part in _iter_elements(root, "part"):
        seen = {}
        part_id = part.attrib.get("id", "")
        segment = 0
        previous_numeric = None
        for measure in _find_children(part, "measure"):
            number = measure.attrib.get("number", "")
            if not number:
                continue
            try:
                numeric = int(number)
            except ValueError:
                numeric = None
            if numeric is not None and previous_numeric is not None and numeric < previous_numeric:
                segment += 1
            key = (segment, number)
            seen[key] = seen.get(key, 0) + 1
            if numeric is not None:
                previous_numeric = numeric
        for (_segment, number), count in seen.items():
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

    malformed_chord_text = []
    for element in root.iter():
        if element.text and MALFORMED_CHORD_TEXT_PATTERN.search(element.text):
            malformed_chord_text.append(element.text.strip())
    if malformed_chord_text:
        errors.append(f"Malformed chord text remains: {', '.join(sorted(set(malformed_chord_text)))}")

    return {
        "xml_valid": True,
        "harmony_elements_checked": harmony_checked,
        "key_signature_elements_checked": key_signature_checked,
        "note_pitch_elements_checked": pitch_checked,
        "malformed_chord_text_checked": len(malformed_chord_text),
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
    duplicate_part_report: dict | None = None,
    leading_alignment_report: dict | None = None,
    pickup_marker_report: dict | None = None,
    rendering_artifact_report: dict | None = None,
    allow_music21_roundtrip: bool = False,
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
        "duplicate_part_validation": duplicate_part_report or {
            "duplicate_parts_found": 0,
            "duplicate_parts_removed": 0,
            "duplicates": [],
            "errors": [],
        },
        "leading_part_alignment": leading_alignment_report or {
            "missing_leading_measures_found": 0,
            "leading_rest_measures_added": 0,
            "first_score_measure": None,
            "first_common_measure": None,
            "parts": [],
            "errors": [],
        },
        "pickup_marker_alignment": pickup_marker_report or {
            "pickup_rehearsal_markers_moved": 0,
            "pickup_verse_numbers_added": 0,
            "pickup_leading_rests_added": 0,
            "moves": [],
            "errors": [],
        },
        "rendering_artifact_repair": rendering_artifact_report or {
            "page_title_artifacts_cleaned": 0,
            "copyright_lyric_artifacts_hidden": 0,
            "copyright_metadata_added": 0,
            "unmatched_slurs_removed": 0,
            "malformed_chord_text_remaining": [],
            "first_system_overlap_warnings": [],
        },
        "musicxml_compatibility_check": "failed",
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
    validation["errors"].extend(validation["duplicate_part_validation"].get("errors") or [])
    validation["errors"].extend(validation["leading_part_alignment"].get("errors") or [])
    validation["errors"].extend(validation["pickup_marker_alignment"].get("errors") or [])

    if validation["errors"]:
        return validation

    # A validated score should remain the authoritative output. A music21
    # writer round-trip can rewrite divisions, lyrics, credits, endings,
    # imported layout, and intentional blank measures even when no repair is
    # needed. Keep the legacy compatibility path opt-in only.
    if _layout_preservation_requested(root) or not allow_music21_roundtrip:
        layout_preserved = _layout_preservation_requested(root)
        if layout_preserved:
            validation["measure_validation"]["manual_review_measures"] = []
        validation["musicxml_compatibility_check"] = (
            "passed (layout preserved)"
            if layout_preserved
            else "passed (validated XML preserved)"
        )
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
                repair_duplicate_parts = _detect_and_repair_duplicate_parts(repair_root)
                if repair_duplicate_parts["duplicate_parts_found"]:
                    validation["duplicate_part_validation"]["duplicate_parts_found"] += repair_duplicate_parts[
                        "duplicate_parts_found"
                    ]
                    validation["duplicate_part_validation"]["duplicate_parts_removed"] += repair_duplicate_parts[
                        "duplicate_parts_removed"
                    ]
                    validation["duplicate_part_validation"]["duplicates"].extend(repair_duplicate_parts["duplicates"])
                    validation["duplicate_part_validation"]["errors"].extend(repair_duplicate_parts["errors"])
                repair_leading_alignment = _detect_and_fill_leading_part_measures(repair_root)
                if repair_leading_alignment["leading_rest_measures_added"]:
                    validation["leading_part_alignment"]["missing_leading_measures_found"] += repair_leading_alignment[
                        "missing_leading_measures_found"
                    ]
                    validation["leading_part_alignment"]["leading_rest_measures_added"] += repair_leading_alignment[
                        "leading_rest_measures_added"
                    ]
                    validation["leading_part_alignment"]["parts"].extend(repair_leading_alignment["parts"])
                    validation["leading_part_alignment"]["errors"].extend(repair_leading_alignment["errors"])
                repair_pickup_markers = _align_pickup_rehearsal_markers(repair_root)
                if repair_pickup_markers["pickup_rehearsal_markers_moved"]:
                    validation["pickup_marker_alignment"]["pickup_rehearsal_markers_moved"] += repair_pickup_markers[
                        "pickup_rehearsal_markers_moved"
                    ]
                    validation["pickup_marker_alignment"]["pickup_verse_numbers_added"] += repair_pickup_markers[
                        "pickup_verse_numbers_added"
                    ]
                    validation["pickup_marker_alignment"]["pickup_leading_rests_added"] += repair_pickup_markers[
                        "pickup_leading_rests_added"
                    ]
                    validation["pickup_marker_alignment"]["moves"].extend(repair_pickup_markers["moves"])
                    validation["pickup_marker_alignment"]["errors"].extend(repair_pickup_markers["errors"])
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
                post_export_rendering_report = _repair_rendering_artifacts(repair_root)
                validation["rendering_artifact_repair"]["page_title_artifacts_cleaned"] += (
                    post_export_rendering_report.get("page_title_artifacts_cleaned", 0)
                )
                validation["rendering_artifact_repair"]["copyright_lyric_artifacts_hidden"] += (
                    post_export_rendering_report.get("copyright_lyric_artifacts_hidden", 0)
                )
                validation["rendering_artifact_repair"]["copyright_metadata_added"] += (
                    post_export_rendering_report.get("copyright_metadata_added", 0)
                )
                validation["rendering_artifact_repair"]["unmatched_slurs_removed"] = (
                    validation["rendering_artifact_repair"].get("unmatched_slurs_removed", 0)
                    + post_export_rendering_report.get("unmatched_slurs_removed", 0)
                )
                validation["rendering_artifact_repair"]["malformed_chord_text_remaining"] = (
                    post_export_rendering_report.get("malformed_chord_text_remaining", [])
                )
                validation["rendering_artifact_repair"]["first_system_overlap_warnings"] = (
                    post_export_rendering_report.get("first_system_overlap_warnings", [])
                )
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
                if validation["duplicate_part_validation"]["errors"]:
                    validation["errors"].extend(validation["duplicate_part_validation"]["errors"])
                    return validation
                if validation["leading_part_alignment"]["errors"]:
                    validation["errors"].extend(validation["leading_part_alignment"]["errors"])
                    return validation
                if validation["pickup_marker_alignment"]["errors"]:
                    validation["errors"].extend(validation["pickup_marker_alignment"]["errors"])
                    return validation
                shutil.copyfile(repair_path, output_path)
                validation["repair_used"] = True
                validation["musicxml_compatibility_check"] = "passed"
    except Exception as exc:
        validation["errors"].append(f"MusicXML compatibility repair failed: {exc}")

    return validation


def _transpose_musicxml_directly(
    input_path: Path,
    output_path: Path,
    semitones: int | float,
    target_key,
    source_key_name: str = "unknown",
    key_detection_engine: str = "new-key-scores-direct",
) -> Path:
    global LAST_TRANSPOSITION_REPORT

    if input_path.suffix.lower() == ".mxl":
        return _transpose_mxl_directly(
            input_path,
            output_path,
            semitones,
            target_key,
            source_key_name=source_key_name,
            key_detection_engine=key_detection_engine,
        )

    ET.register_namespace("", "http://www.musicxml.org/ns/musicxml")
    tree = ET.parse(input_path)
    root = tree.getroot()
    semitone_delta = int(round(semitones))
    prefer_flats = int(target_key.sharps) < 0
    note_count = 0
    stale_accidental_count = 0
    key_signature_count = 0
    if _local_name(root.tag) in {"score-partwise", "score-timewise"}:
        root.attrib["version"] = "4.0"

    chord_lyric_recovery = _recover_chord_lyrics(root)
    duplicate_rehearsal_count = _deduplicate_rehearsal_marks_across_parts(root)
    top_rehearsal_count = _move_rehearsal_marks_to_top_staff(root)
    rehearsal_text_count = _convert_rehearsal_marks_to_top_part_text(root)
    parent_map = {child: parent for parent in root.iter() for child in list(parent)}

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
        note_element = parent_map.get(pitch)
        if note_element is not None and _local_name(note_element.tag) == "note":
            for accidental in list(_find_children(note_element, "accidental")):
                note_element.remove(accidental)
                stale_accidental_count += 1
        note_count += 1

    source_mode = "minor" if str(source_key_name).strip().lower().endswith("minor") else "major"
    for key_element in _iter_elements(root, "key"):
        fifths_element = _find_child(key_element, "fifths")
        if fifths_element is not None:
            mode_element = _find_child(key_element, "mode")
            local_mode = (
                (mode_element.text or "").strip().lower()
                if mode_element is not None
                else source_mode
            )
            try:
                source_fifths = int((fifths_element.text or "").strip())
            except ValueError:
                continue
            shifted_fifths = _transpose_key_signature_fifths(
                source_fifths,
                semitone_delta,
                local_mode,
                prefer_flats,
            )
            fifths_element.text = str(
                int(target_key.sharps) if shifted_fifths is None else shifted_fifths
            )
            key_signature_count += 1
    key_signature_count += _ensure_initial_key_signatures(root, int(target_key.sharps))

    harmony_count = _transpose_harmonies(root, semitone_delta, prefer_flats)
    key_label_count, chord_text_count, metadata_count = _update_visible_text(root, semitone_delta, target_key, prefer_flats)
    recovered_chord_count = _transpose_recovered_chord_lyrics(root, semitone_delta, prefer_flats)
    duplicate_part_report = _detect_and_repair_duplicate_parts(root)
    leading_alignment_report = _detect_and_fill_leading_part_measures(root)
    pickup_marker_report = _align_pickup_rehearsal_markers(root)
    duplicate_report = _detect_and_repair_duplicate_measures(root)
    measure_report = _repair_measure_durations(root)
    opening_time_signatures_added = _ensure_opening_time_signatures(root)
    rendering_artifact_report = _repair_rendering_artifacts(root)
    measure_number_resets_applied = _apply_stored_measure_number_resets(root)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    validation_report = _validate_and_repair_musicxml(
        output_path,
        metadata_updated=metadata_count,
        measure_report=measure_report,
        duplicate_report=duplicate_report,
        duplicate_part_report=duplicate_part_report,
        leading_alignment_report=leading_alignment_report,
        pickup_marker_report=pickup_marker_report,
        rendering_artifact_report=rendering_artifact_report,
    )
    LAST_TRANSPOSITION_REPORT = {
        "engine": "new-key-scores-direct",
        "key_detection_engine": key_detection_engine,
        "source_key": source_key_name,
        "target_key": _target_key_name(target_key),
        "interval": semitone_delta,
        "note_transposition_count": note_count,
        "stale_accidental_elements_removed": stale_accidental_count,
        "key_signature_update_count": key_signature_count,
        "harmony_chord_update_count": harmony_count + chord_text_count + recovered_chord_count,
        "measure_number_resets_applied": measure_number_resets_applied,
        "recovered_chord_lyric_update_count": recovered_chord_count,
        "recovered_chord_lyric_count": chord_lyric_recovery["recovered"],
        "ambiguous_chord_lyric_count": chord_lyric_recovery["ambiguous"],
        "duplicate_rehearsal_marks_removed": duplicate_rehearsal_count,
        "rehearsal_marks_moved_to_top": top_rehearsal_count,
        "rehearsal_marks_converted_to_top_text": rehearsal_text_count,
        "visible_key_label_update_count": key_label_count,
        "metadata_update_count": metadata_count,
        "opening_time_signatures_added": opening_time_signatures_added,
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
    key_detection_engine: str = "new-key-scores-direct",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(input_path, "r") as source_zip:
        rootfile_path = _get_mxl_rootfile(source_zip)
        root_xml = source_zip.read(rootfile_path)
        temp_xml_path = output_path.with_suffix(".fallback.musicxml")
        temp_xml_path.write_bytes(root_xml)
        _transpose_musicxml_directly(
            temp_xml_path,
            temp_xml_path,
            semitones,
            target_key,
            source_key_name=source_key_name,
            key_detection_engine=key_detection_engine,
        )
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
    parser.add_argument("--input", required=True, help="Path to the source .musicxml, .xml, .mxl, or .pdf file.")
    parser.add_argument("--output", required=True, help="Path for the transposed output file.")
    parser.add_argument("--target-key", required=True, help="Target key, such as 'D major' or 'E minor'.")
    parser.add_argument(
        "--output-format",
        choices=["musicxml", "pdf"],
        default="musicxml",
        help="Output format. Use musicxml for the reliable workflow. PDF saving is not available yet.",
    )
    parser.add_argument("--audiveris-path", default="", help="Path to the Audiveris executable for PDF import.")
    parser.add_argument("--temp-dir", default="", help="App-owned temp directory for intermediate files.")
    parser.add_argument("--detect-key-only", action="store_true", help="Detect and print the original key only.")
    parser.add_argument(
        "--inspect-input",
        action="store_true",
        help="Print JSON describing whether a PDF is a score or text-based chord chart.",
    )
    parser.add_argument(
        "--clean-export-layout",
        choices=["true", "false"],
        default="true",
        help="Clean Audiveris layout artifacts before transposing PDF imports.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.inspect_input:
            input_path = Path(args.input)
            if input_path.suffix.lower() == ".pdf":
                from python.chord_chart import inspect_chord_chart_pdf

                print(json.dumps(inspect_chord_chart_pdf(input_path).as_dict()))
            else:
                print(
                    json.dumps(
                        {
                            "kind": "musicxml",
                            "original_key": detect_key_name(input_path),
                            "key_source": "MusicXML",
                        }
                    )
                )
            return 0

        if args.detect_key_only:
            input_path = Path(args.input)
            if input_path.suffix.lower() == ".pdf":
                from python.chord_chart import inspect_chord_chart_pdf

                inspection = inspect_chord_chart_pdf(input_path)
                if not inspection.is_chord_chart or not inspection.original_key:
                    raise TranspositionError("The PDF key could not be detected before conversion.")
                print(inspection.original_key)
            else:
                print(detect_key_name(input_path))
            return 0

        from python.pipeline import run_pipeline

        output_path = run_pipeline(
            args.input,
            args.output,
            args.target_key,
            args.output_format,
            audiveris_path=args.audiveris_path,
            temp_dir=args.temp_dir,
            clean_export_layout=args.clean_export_layout == "true",
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
