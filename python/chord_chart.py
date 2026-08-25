from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from python.transposer import (
    TranspositionError,
    _canonicalize_chord_text,
    _nearest_transposition_delta,
    _parse_simple_key,
    _pitch_name_to_class,
    _transpose_chord_text,
)


KEY_PATTERN = re.compile(
    r"(?i)\bkey\s*:?\s*([A-G](?:#|b|♯|♭)?)(?:\s*(major|minor|m))?\b"
)
SECTION_WORDS = {
    "bridge",
    "chorus",
    "ending",
    "intro",
    "interlude",
    "outro",
    "pre-chorus",
    "refrain",
    "tag",
    "turnaround",
    "verse",
}
LEADING_WRAPPERS = "|:([{\"'"
TRAILING_WRAPPERS = "|:;,.)]}\"'"


@dataclass(frozen=True)
class ChordOccurrence:
    page_index: int
    x0: float
    x1: float
    top: float
    bottom: float
    text: str
    chord: str
    font_size: float


@dataclass(frozen=True)
class ChordChartInspection:
    kind: str
    original_key: str | None
    key_source: str | None
    chord_count: int
    text_layer: bool
    staff_notation_detected: bool
    occurrences: tuple[ChordOccurrence, ...] = ()

    @property
    def is_chord_chart(self) -> bool:
        return self.kind == "chord-chart-pdf"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "original_key": self.original_key,
            "key_source": self.key_source,
            "chord_count": self.chord_count,
            "text_layer": self.text_layer,
            "staff_notation_detected": self.staff_notation_detected,
        }


def _unwrap_chord_token(text: str) -> tuple[str, str, str] | None:
    value = (text or "").strip()
    if not value:
        return None

    leading = ""
    trailing = ""
    while value and value[0] in LEADING_WRAPPERS:
        leading += value[0]
        value = value[1:]
    while value and value[-1] in TRAILING_WRAPPERS:
        trailing = value[-1] + trailing
        value = value[:-1]

    chord = _canonicalize_chord_text(value)
    if chord is None:
        return None
    return leading, chord, trailing


def _transpose_wrapped_chord(text: str, semitones: int, prefer_flats: bool) -> str | None:
    parsed = _unwrap_chord_token(text)
    if parsed is None:
        return None
    leading, chord, trailing = parsed
    shifted = _transpose_chord_text(chord, semitones, prefer_flats)
    if shifted is None:
        return None
    return f"{leading}{shifted}{trailing}"


def _group_words_into_lines(words: list[dict], tolerance: float = 3.5) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0)))):
        top = float(word.get("top", 0))
        matching_line = next(
            (
                line
                for line in reversed(lines[-4:])
                if abs(sum(float(item.get("top", 0)) for item in line) / len(line) - top) <= tolerance
            ),
            None,
        )
        if matching_line is None:
            matching_line = []
            lines.append(matching_line)
        matching_line.append(word)

    for line in lines:
        line.sort(key=lambda item: float(item.get("x0", 0)))
    return lines


def _has_staff_like_lines(page) -> bool:
    horizontal = []
    minimum_length = float(page.width) * 0.22
    line_objects = list(page.lines)
    line_objects.extend(
        rect
        for rect in page.rects
        if abs(float(rect.get("y1", 0)) - float(rect.get("y0", 0))) <= 1.8
    )
    for line in line_objects:
        x0 = float(line.get("x0", 0))
        x1 = float(line.get("x1", 0))
        y0 = float(line.get("y0", 0))
        y1 = float(line.get("y1", 0))
        if abs(y1 - y0) <= 1.2 and abs(x1 - x0) >= minimum_length:
            horizontal.append((min(x0, x1), max(x0, x1), (y0 + y1) / 2))

    for index, first in enumerate(horizontal):
        nearby = [first]
        for candidate in horizontal[index + 1 :]:
            overlap = min(first[1], candidate[1]) - max(first[0], candidate[0])
            if overlap < minimum_length * 0.7:
                continue
            if abs(candidate[2] - first[2]) <= 60:
                nearby.append(candidate)
        ys = sorted({round(item[2], 1) for item in nearby})
        for start in range(max(0, len(ys) - 4)):
            group = ys[start : start + 5]
            if len(group) < 5:
                continue
            gaps = [group[position + 1] - group[position] for position in range(4)]
            average = sum(gaps) / len(gaps)
            if 4 <= average <= 16 and max(abs(gap - average) for gap in gaps) <= 2.2:
                return True
    return False


def _line_key(line: list[dict]) -> tuple[str, str] | None:
    text = " ".join(str(word.get("text", "")) for word in line)
    match = KEY_PATTERN.search(text)
    if match is None:
        return None
    tonic = match.group(1).replace("♯", "#").replace("♭", "b")
    mode_value = (match.group(2) or "major").lower()
    mode = "minor" if mode_value in {"minor", "m"} else "major"
    return tonic, mode


def _is_section_word(text: str) -> bool:
    normalized = re.sub(r"[^A-Za-z-]", "", text or "").lower()
    return normalized in SECTION_WORDS or normalized.rstrip("0123456789") in SECTION_WORDS


def _candidate_occurrence(page_index: int, word: dict) -> ChordOccurrence | None:
    parsed = _unwrap_chord_token(str(word.get("text", "")))
    if parsed is None:
        return None
    _leading, chord, _trailing = parsed
    height = max(6.0, float(word.get("bottom", 0)) - float(word.get("top", 0)))
    size = float(word.get("size") or height * 0.82)
    return ChordOccurrence(
        page_index=page_index,
        x0=float(word.get("x0", 0)),
        x1=float(word.get("x1", 0)),
        top=float(word.get("top", 0)),
        bottom=float(word.get("bottom", 0)),
        text=str(word.get("text", "")),
        chord=chord,
        font_size=max(6.0, min(size, 36.0)),
    )


def _quality(chord: str) -> str:
    suffix = re.sub(r"^[A-G](?:#|b)?", "", chord).split("/", 1)[0].lower()
    if suffix.startswith(("dim", "°", "ø")):
        return "dim"
    if suffix.startswith(("m", "min")) and not suffix.startswith("maj"):
        return "minor"
    return "major"


def _infer_key(occurrences: list[ChordOccurrence]) -> str | None:
    if not occurrences:
        return None

    chords = [occurrence.chord for occurrence in occurrences if occurrence.chord != "N.C."]
    if not chords:
        return None
    roots = [re.match(r"^([A-G](?:#|b)?)", chord).group(1) for chord in chords]
    root_classes = [_pitch_name_to_class(root) for root in roots]
    qualities = [_quality(chord) for chord in chords]

    best: tuple[float, int, str] | None = None
    for tonic_name in ("C", "G", "D", "A", "E", "B", "F#", "C#", "F", "Bb", "Eb", "Ab", "Db", "Gb", "Cb"):
        tonic_class = _pitch_name_to_class(tonic_name)
        for mode in ("major", "minor"):
            if mode == "major":
                expected = {0: "major", 2: "minor", 4: "minor", 5: "major", 7: "major", 9: "minor", 11: "dim"}
            else:
                expected = {0: "minor", 2: "dim", 3: "major", 5: "minor", 7: "major", 8: "major", 10: "major", 11: "dim"}
            score = 0.0
            for root_class, quality in zip(root_classes, qualities):
                degree = (root_class - tonic_class) % 12
                if degree in expected:
                    score += 2.0
                    if quality == expected[degree] or (mode == "minor" and degree == 7 and quality == "minor"):
                        score += 1.0
                else:
                    score -= 1.0
            if root_classes[0] == tonic_class:
                score += 2.5
            if root_classes[-1] == tonic_class:
                score += 3.0
            score += sum(1.0 for root_class in root_classes if root_class == tonic_class)
            preference = 1 if mode == "major" else 0
            candidate = (score, preference, f"{tonic_name} {mode}")
            if best is None or candidate > best:
                best = candidate
    return best[2] if best else None


def inspect_chord_chart_pdf(input_path: str | Path) -> ChordChartInspection:
    source_path = Path(input_path).expanduser()
    if source_path.suffix.lower() != ".pdf" or not source_path.is_file():
        raise TranspositionError(f"Chord-chart inspection requires an existing PDF: {source_path}")

    occurrences: list[ChordOccurrence] = []
    printed_key: tuple[str, str] | None = None
    text_layer = False
    staff_notation_detected = False

    try:
        with pdfplumber.open(source_path) as pdf:
            for page_index, page in enumerate(pdf.pages):
                staff_notation_detected = staff_notation_detected or _has_staff_like_lines(page)
                words = page.extract_words(
                    use_text_flow=True,
                    keep_blank_chars=False,
                    extra_attrs=["fontname", "size"],
                )
                if words:
                    text_layer = True
                for line in _group_words_into_lines(words):
                    key_value = _line_key(line)
                    if key_value and printed_key is None:
                        printed_key = key_value
                    candidates = [
                        candidate
                        for word in line
                        if (candidate := _candidate_occurrence(page_index, word)) is not None
                    ]
                    meaningful = [word for word in line if re.search(r"[A-Za-z0-9]", str(word.get("text", "")))]
                    has_section_label = any(_is_section_word(str(word.get("text", ""))) for word in line)
                    chord_row = (
                        len(candidates) >= 2 and len(candidates) / max(1, len(meaningful)) >= 0.45
                    ) or (
                        len(candidates) == 1 and (len(meaningful) == 1 or has_section_label or key_value is not None)
                    )
                    if chord_row:
                        occurrences.extend(candidates)
    except Exception:
        return ChordChartInspection(
            kind="score-pdf",
            original_key=None,
            key_source=None,
            chord_count=0,
            text_layer=False,
            staff_notation_detected=False,
        )

    unique_occurrences = list({
        (item.page_index, item.x0, item.top, item.text): item for item in occurrences
    }.values())
    is_chart = text_layer and not staff_notation_detected and len(unique_occurrences) >= 2
    original_key = None
    key_source = None
    if printed_key:
        original_key = f"{printed_key[0]} {printed_key[1]}"
        key_source = "printed key label"
    elif is_chart:
        original_key = _infer_key(unique_occurrences)
        key_source = "chord progression inference" if original_key else None

    return ChordChartInspection(
        kind="chord-chart-pdf" if is_chart else "score-pdf",
        original_key=original_key,
        key_source=key_source,
        chord_count=len(unique_occurrences),
        text_layer=text_layer,
        staff_notation_detected=staff_notation_detected,
        occurrences=tuple(unique_occurrences) if is_chart else (),
    )


def _overlay_for_page(width: float, height: float, replacements: list[tuple[ChordOccurrence, str]]) -> BytesIO:
    packet = BytesIO()
    pdf = canvas.Canvas(packet, pagesize=(width, height))
    for occurrence, shifted in replacements:
        box_height = max(occurrence.bottom - occurrence.top, occurrence.font_size * 1.05)
        x = occurrence.x0 - 1.0
        y = height - occurrence.bottom - 1.0
        box_width = max(occurrence.x1 - occurrence.x0 + 4.0, occurrence.font_size * len(shifted) * 0.62 + 3.0)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(x, y, box_width, box_height + 3.0, stroke=0, fill=1)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.setFont("Helvetica-Bold", occurrence.font_size)
        pdf.drawString(occurrence.x0, height - occurrence.bottom + max(0.5, occurrence.font_size * 0.08), shifted)
    pdf.save()
    packet.seek(0)
    return packet


def transpose_chord_chart_pdf(
    input_path: str | Path,
    output_path: str | Path,
    target_key_name: str,
    *,
    inspection: ChordChartInspection | None = None,
) -> dict:
    source_path = Path(input_path).expanduser()
    destination_path = Path(output_path).expanduser()
    chart = inspection or inspect_chord_chart_pdf(source_path)
    if not chart.is_chord_chart:
        raise TranspositionError("This PDF was not recognized as a text-based chord chart.")
    if not chart.original_key:
        raise TranspositionError("The chord chart key could not be determined safely.")

    source_key = _parse_simple_key(chart.original_key)
    target_key = _parse_simple_key(target_key_name)
    source_class = _pitch_name_to_class(source_key.tonic.name)
    target_class = _pitch_name_to_class(target_key.tonic.name)
    semitones = _nearest_transposition_delta(source_class, target_class)
    prefer_flats = target_key.sharps is not None and target_key.sharps < 0

    replacements_by_page: dict[int, list[tuple[ChordOccurrence, str]]] = {}
    for occurrence in chart.occurrences:
        shifted = _transpose_wrapped_chord(occurrence.text, semitones, prefer_flats)
        if shifted is not None and shifted != occurrence.text:
            replacements_by_page.setdefault(occurrence.page_index, []).append((occurrence, shifted))

    reader = PdfReader(str(source_path))
    writer = PdfWriter(clone_from=reader)
    for page_index, page in enumerate(writer.pages):
        replacements = replacements_by_page.get(page_index, [])
        if replacements:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            overlay = PdfReader(_overlay_for_page(width, height, replacements)).pages[0]
            page.merge_page(overlay)

    metadata = {
        str(key): str(value)
        for key, value in (reader.metadata or {}).items()
        if key and value is not None
    }
    metadata["/Producer"] = "New Key Scores chord-chart PDF writer"
    writer.add_metadata(metadata)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with destination_path.open("wb") as output_stream:
        writer.write(output_stream)

    return {
        "engine": "new-key-scores-chord-chart",
        "source_key": chart.original_key,
        "source_key_method": chart.key_source,
        "target_key": target_key_name,
        "interval": semitones,
        "chords_found": chart.chord_count,
        "chords_transposed": sum(len(items) for items in replacements_by_page.values()),
    }
