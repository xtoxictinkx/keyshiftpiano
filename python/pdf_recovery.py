from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
from statistics import median
import unicodedata
import xml.etree.ElementTree as ET


RECOVERED_CHORD_LYRIC_NAME = "new-key-scores-chord"
RECOVERED_CHORD_LYRIC_NAMES = {RECOVERED_CHORD_LYRIC_NAME, "key-shift-chord"}
CHORD_PATTERN = re.compile(
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
PUA_TRANSLATION = {
    ord("\uf020"): " ",
    ord("\uf023"): "#",
    ord("\uf062"): "b",
    ord("\uf03d"): "=",
    **{0xF030 + value: str(value) for value in range(10)},
}
PERFORMANCE_DIRECTION_PATTERN = re.compile(
    r"^(?:"
    r"mel(?:ody)?\.?|solo|soli|tutti|all|unis(?:on)?\.?|sim(?:ile)?\.?|"
    r"a\s+tempo|tempo\s+primo|rit(?:ardando)?\.?|rall(?:entando)?\.?|"
    r"accel(?:erando)?\.?|ad\s+lib(?:itum)?\.?|rubato|legato|staccato|"
    r"cantabile|dolce|espressivo|marcato|sostenuto|pizz(?:icato)?\.?|arco"
    r")$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PdfChord:
    text: str
    page_index: int
    system_index: int
    x0: float
    top: float


@dataclass(frozen=True)
class SystemRegion:
    top: float
    bottom: float
    left: float
    right: float


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _qualified(parent, name: str) -> str:
    if parent.tag.startswith("{"):
        namespace = parent.tag.split("}", 1)[0] + "}"
        return f"{namespace}{name}"
    return name


def _children(element, name: str):
    return [child for child in list(element) if _local_name(child.tag) == name]


def _child(element, name: str):
    return next((child for child in list(element) if _local_name(child.tag) == name), None)


def _iter(element, name: str):
    return (candidate for candidate in element.iter() if _local_name(candidate.tag) == name)


def _canonicalize_pdf_chord(text: str) -> str | None:
    value = (text or "").translate(PUA_TRANSLATION)
    value = value.replace("♯", "#").replace("♭", "b").replace("−", "-")
    value = re.sub(r"\s+", "", value.strip())
    if value.upper() in {"N.C", "N.C."}:
        return "N.C."

    value = re.sub(r"(?i)^([A-G](?:#|b)?)MA(?=\d)", r"\1maj", value)
    value = re.sub(r"(?i)^([A-G](?:#|b)?)MIN", r"\1m", value)
    match = CHORD_PATTERN.fullmatch(value)
    if not match:
        return None

    root, suffix, bass = match.groups()
    suffix = re.sub(r"(?i)^MAJ", "maj", suffix or "")
    suffix = re.sub(r"(?i)^MIN", "m", suffix)
    suffix = re.sub(r"(?i)^M(?=\d|$)", "m", suffix)
    suffix = re.sub(r"(?i)SUS", "sus", suffix)
    suffix = re.sub(r"(?i)ADD", "add", suffix)
    return f"{root}{suffix}{f'/{bass}' if bass else ''}"


def _word_signature(word: dict) -> tuple[str, float] | None:
    chars = word.get("chars") or []
    if not chars:
        return None
    first = next((char for char in chars if (char.get("text") or "").strip()), chars[0])
    return str(first.get("fontname") or ""), round(float(first.get("size") or 0), 1)


def _object_bbox(obj: dict) -> tuple[float, float, float, float]:
    x0 = float(obj.get("x0") or 0)
    x1 = float(obj.get("x1") or x0)
    top = float(obj.get("top") or 0)
    bottom = float(obj.get("bottom") or top)
    return min(x0, x1), min(top, bottom), max(x0, x1), max(top, bottom)


def _near_white(value) -> bool:
    if isinstance(value, (int, float)):
        return float(value) >= 0.94
    if not isinstance(value, (tuple, list)) or not value:
        return False
    if len(value) == 4:
        cyan, magenta, yellow, black = (float(component) for component in value)
        red = 1.0 - min(1.0, cyan + black)
        green = 1.0 - min(1.0, magenta + black)
        blue = 1.0 - min(1.0, yellow + black)
        return min(red, green, blue) >= 0.94
    return min(float(component) for component in value[:3]) >= 0.94


def _opaque_white_masks(page) -> list[dict]:
    cached = getattr(page, "_new_key_scores_white_masks", None)
    if cached is not None:
        return cached
    masks = [
        rect
        for rect in (getattr(page, "rects", []) or [])
        if rect.get("fill")
        and _near_white(rect.get("non_stroking_color"))
        and float(rect.get("width") or 0) >= 24
        and float(rect.get("height") or 0) >= 12
    ]
    setattr(page, "_new_key_scores_white_masks", masks)
    return masks


def _bbox_is_inside_mask(obj: dict, mask: dict) -> bool:
    x0, top, x1, bottom = _object_bbox(obj)
    mask_x0, mask_top, mask_x1, mask_bottom = _object_bbox(mask)
    center_x = (x0 + x1) / 2
    center_y = (top + bottom) / 2
    return (
        mask_x0 - 0.5 <= center_x <= mask_x1 + 0.5
        and mask_top - 0.5 <= center_y <= mask_bottom + 0.5
    )


def _pdf_color_to_rgb(value) -> tuple[int, int, int] | None:
    if isinstance(value, (int, float)):
        channel = round(max(0.0, min(1.0, float(value))) * 255)
        return channel, channel, channel
    if not isinstance(value, (tuple, list)) or not value:
        return None
    if len(value) == 4:
        cyan, magenta, yellow, black = (float(component) for component in value)
        return (
            round((1.0 - min(1.0, cyan + black)) * 255),
            round((1.0 - min(1.0, magenta + black)) * 255),
            round((1.0 - min(1.0, yellow + black)) * 255),
        )
    if len(value) >= 3:
        return tuple(
            round(max(0.0, min(1.0, float(component))) * 255)
            for component in value[:3]
        )
    return None


def _rendered_pdf_page(page):
    cached = getattr(page, "_new_key_scores_rendered_page", None)
    if cached is not None:
        return cached
    rendered = page.to_image(resolution=144, antialias=True).original.convert("RGB")
    setattr(page, "_new_key_scores_rendered_page", rendered)
    return rendered


def _object_has_visible_ink(page, obj: dict, *, color_key: str) -> bool:
    try:
        image = _rendered_pdf_page(page)
    except Exception:
        # Visibility filtering is a safety improvement. If rendering is unavailable,
        # retain the object instead of silently discarding legitimate score text.
        return True

    x0, top, x1, bottom = _object_bbox(obj)
    scale_x = image.width / max(1.0, float(page.width))
    scale_y = image.height / max(1.0, float(page.height))
    left = max(0, int(x0 * scale_x) - 2)
    upper = max(0, int(top * scale_y) - 2)
    right = min(image.width, max(left + 1, int(x1 * scale_x) + 3))
    lower = min(image.height, max(upper + 1, int(bottom * scale_y) + 3))
    crop = image.crop((left, upper, right, lower))

    color = obj.get(color_key)
    if color is None and obj.get("chars"):
        color = next(
            (
                character.get(color_key)
                for character in obj["chars"]
                if character.get(color_key) is not None
            ),
            None,
        )
    expected = _pdf_color_to_rgb(color) or (0, 0, 0)
    pixels = list(crop.getdata())
    if max(expected) <= 64:
        matching = sum(
            1
            for red, green, blue in pixels
            if max(red, green, blue) <= 170 and max(red, green, blue) - min(red, green, blue) <= 70
        )
    else:
        matching = sum(
            1
            for red, green, blue in pixels
            if (red - expected[0]) ** 2 + (green - expected[1]) ** 2 + (blue - expected[2]) ** 2
            <= 95**2
        )
    return matching >= max(2, round(len(pixels) * 0.004))


def _filter_visible_pdf_objects(
    page,
    objects: list[dict],
    *,
    color_key: str,
) -> list[dict]:
    masks = _opaque_white_masks(page)
    if not masks:
        return objects
    return [
        obj
        for obj in objects
        if not any(_bbox_is_inside_mask(obj, mask) for mask in masks)
        or _object_has_visible_ink(page, obj, color_key=color_key)
    ]


def _is_strong_chord(text: str) -> bool:
    return text == "N.C." or re.fullmatch(r"[A-G](?:#|b)?", text) is None


def _cluster_values(values: list[float], tolerance: float) -> list[float]:
    if not values:
        return []
    clusters = [[value] for value in sorted(values)]
    merged: list[list[float]] = []
    for cluster in clusters:
        if merged and cluster[0] - merged[-1][-1] <= tolerance:
            merged[-1].extend(cluster)
        else:
            merged.append(cluster)
    return [sum(cluster) / len(cluster) for cluster in merged]


def _score_page_systems(root) -> list[list[list[str]]]:
    part = next(_iter(root, "part"), None)
    if part is None:
        return []

    pages: list[list[list[str]]] = [[[]]]
    for index, measure in enumerate(_children(part, "measure")):
        print_element = _child(measure, "print")
        new_page = (
            index > 0
            and print_element is not None
            and print_element.attrib.get("new-page") == "yes"
        )
        new_system = (
            index > 0
            and print_element is not None
            and print_element.attrib.get("new-system") == "yes"
        )
        if new_page:
            pages.append([[]])
        elif new_system:
            pages[-1].append([])
        pages[-1][-1].append(measure.attrib.get("number", str(index + 1)))
    return pages


def _vertical_objects(page) -> list[dict]:
    cached = getattr(page, "_new_key_scores_visible_vertical_objects", None)
    if cached is not None:
        return cached
    objects = list(getattr(page, "lines", []) or []) + list(getattr(page, "rects", []) or [])
    visible = _filter_visible_pdf_objects(page, objects, color_key="stroking_color")
    setattr(page, "_new_key_scores_visible_vertical_objects", visible)
    return visible


def _detect_system_regions(page, expected_count: int) -> list[SystemRegion]:
    if expected_count <= 0:
        return []

    minimum_height = max(80.0, float(page.height) * 0.11)
    candidates = []
    for obj in _vertical_objects(page):
        width = float(obj.get("width") or 0)
        height = float(obj.get("height") or 0)
        x0 = float(obj.get("x0") or 0)
        if width <= 3 and height >= minimum_height and x0 <= float(page.width) * 0.22:
            candidates.append(
                (
                    float(obj.get("top") or 0),
                    float(obj.get("bottom") or 0),
                    x0,
                )
            )

    deduplicated = []
    for candidate in sorted(candidates):
        if deduplicated and abs(candidate[0] - deduplicated[-1][0]) <= 4:
            if candidate[1] - candidate[0] > deduplicated[-1][1] - deduplicated[-1][0]:
                deduplicated[-1] = candidate
        else:
            deduplicated.append(candidate)

    if len(deduplicated) >= expected_count:
        if len(deduplicated) > expected_count:
            deduplicated = sorted(
                sorted(deduplicated, key=lambda item: item[1] - item[0], reverse=True)[:expected_count]
            )
        regions = []
        for top, bottom, left in deduplicated:
            region_barlines = []
            for obj in _vertical_objects(page):
                width = float(obj.get("width") or 0)
                height = float(obj.get("height") or 0)
                obj_top = float(obj.get("top") or 0)
                obj_bottom = float(obj.get("bottom") or 0)
                overlap = min(bottom, obj_bottom) - max(top, obj_top)
                if width <= 3 and height >= max(40.0, (bottom - top) * 0.24) and overlap > 20:
                    region_barlines.append(float(obj.get("x0") or 0))
            right = max(region_barlines, default=float(page.width) - left)
            regions.append(SystemRegion(top, bottom, left, right))
        return regions

    usable_top = 65.0
    usable_bottom = float(page.height) - 45.0
    height = (usable_bottom - usable_top) / expected_count
    return [
        SystemRegion(
            usable_top + index * height,
            usable_top + (index + 1) * height,
            40.0,
            float(page.width) - 40.0,
        )
        for index in range(expected_count)
    ]


def _word_font_size(word: dict) -> float:
    return max(
        (float(character.get("size") or 0) for character in (word.get("chars") or [])),
        default=0.0,
    )


def _chord_word_candidates(words: list[dict]) -> tuple[list[dict], int]:
    suffix_pattern = re.compile(
        r"(?i)^(?:m|maj|min|dim|aug|sus\d*|add\d+|omit\d+|no\d+|"
        r"\d+(?:\([^()\s]+\))*|\([^()\s]+\))$"
    )
    candidates = []
    suffixes_merged = 0
    ordered_words = sorted(words, key=lambda word: (float(word.get("x0") or 0), float(word.get("top") or 0)))
    for word in ordered_words:
        raw_text = word.get("text") or ""
        chord = _canonicalize_pdf_chord(raw_text)
        if chord is None:
            continue

        merged_text = raw_text
        merged_x1 = float(word.get("x1") or word.get("x0") or 0)
        base_center_y = (
            float(word.get("top") or 0) + float(word.get("bottom") or word.get("top") or 0)
        ) / 2
        fragments = []
        for fragment in ordered_words:
            if fragment is word:
                continue
            fragment_text = re.sub(r"\s+", "", fragment.get("text") or "")
            if not suffix_pattern.fullmatch(fragment_text):
                continue
            fragment_x0 = float(fragment.get("x0") or 0)
            if not merged_x1 - 0.75 <= fragment_x0 <= merged_x1 + 4.5:
                continue
            fragment_center_y = (
                float(fragment.get("top") or 0)
                + float(fragment.get("bottom") or fragment.get("top") or 0)
            ) / 2
            if abs(fragment_center_y - base_center_y) > max(5.0, _word_font_size(word) * 0.55):
                continue
            fragments.append(fragment)

        for fragment in sorted(fragments, key=lambda item: float(item.get("x0") or 0)):
            fragment_text = re.sub(r"\s+", "", fragment.get("text") or "")
            trial_text = f"{merged_text}{fragment_text}"
            trial_chord = _canonicalize_pdf_chord(trial_text)
            if trial_chord is None:
                continue
            merged_text = trial_text
            chord = trial_chord
            merged_x1 = max(merged_x1, float(fragment.get("x1") or fragment.get("x0") or 0))
            suffixes_merged += 1

        candidate = dict(word)
        candidate["text"] = chord
        candidate["x1"] = merged_x1
        candidates.append(candidate)
    return candidates, suffixes_merged


def _merge_stacked_chord_candidates(candidates: list[dict]) -> tuple[list[dict], int]:
    ordered = sorted(candidates, key=lambda candidate: (candidate["top"], candidate["x0"]))
    consumed = set()
    merged = []
    merged_count = 0
    for index, candidate in enumerate(ordered):
        if index in consumed:
            continue
        text = candidate["text"]
        if "/" not in text:
            x0 = float(candidate["x0"])
            x1 = float(candidate.get("x1", x0))
            center = (x0 + x1) / 2
            best = None
            for bass_index, bass in enumerate(ordered):
                if bass_index == index or bass_index in consumed:
                    continue
                if not re.fullmatch(r"[A-G](?:#|b)?", bass["text"]):
                    continue
                vertical_gap = float(bass["top"]) - float(candidate["top"])
                if not 4.0 <= vertical_gap <= 15.0:
                    continue
                bass_x0 = float(bass["x0"])
                bass_x1 = float(bass.get("x1", bass_x0))
                bass_center = (bass_x0 + bass_x1) / 2
                center_gap = abs(bass_center - center)
                if center_gap > max(5.0, (x1 - x0) * 0.25):
                    continue
                score = (center_gap, vertical_gap)
                if best is None or score < best[0]:
                    best = (score, bass_index, bass)
            if best is not None:
                _score, bass_index, bass = best
                joined = _canonicalize_pdf_chord(f"{text}/{bass['text']}")
                if joined is not None:
                    candidate = dict(candidate)
                    candidate["text"] = joined
                    candidate["x1"] = max(
                        float(candidate.get("x1", candidate["x0"])),
                        float(bass.get("x1", bass["x0"])),
                    )
                    consumed.add(bass_index)
                    merged_count += 1
        merged.append(candidate)
    return sorted(merged, key=lambda candidate: (candidate["x0"], candidate["top"])), merged_count


def _extract_candidate_words(pdf, page_systems) -> tuple[list[list[list[dict]]], dict, dict]:
    page_words: list[list[list[dict]]] = []
    all_candidates: list[dict] = []
    metadata = {}
    stats = {
        "covered_chord_words_ignored": 0,
        "chord_suffix_fragments_merged": 0,
        "stacked_chords_merged": 0,
    }
    page_titles = []
    prominent_directions = []
    section_captions = []
    section_labels = []
    colored_pitch_cues = []
    printed_measure_starts = []
    prominent_dynamics = []
    performance_directions = []

    for page_index, page in enumerate(pdf.pages):
        expected_systems = len(page_systems[page_index]) if page_index < len(page_systems) else 0
        regions = _detect_system_regions(page, expected_systems)
        systems = [[] for _region in regions]
        raw_words = page.extract_words(
            x_tolerance=2,
            y_tolerance=6,
            keep_blank_chars=False,
            use_text_flow=False,
            return_chars=True,
        )
        words = _filter_visible_pdf_objects(
            page,
            raw_words,
            color_key="non_stroking_color",
        )
        raw_chord_count = sum(
            1
            for word in raw_words
            if not _word_color_hex(word)
            and _canonicalize_pdf_chord(word.get("text") or "") is not None
        )
        visible_chord_count = sum(
            1
            for word in words
            if not _word_color_hex(word)
            and _canonicalize_pdf_chord(word.get("text") or "") is not None
        )
        stats["covered_chord_words_ignored"] += max(0, raw_chord_count - visible_chord_count)
        chord_words, suffixes_merged = _chord_word_candidates(
            [word for word in words if not _word_color_hex(word)]
        )
        stats["chord_suffix_fragments_merged"] += suffixes_merged
        for word in chord_words:
            chord = word["text"]
            top = float(word.get("top") or 0)
            system_index = next(
                (
                    index
                    for index, region in enumerate(regions)
                    if region.top <= top <= region.bottom
                ),
                None,
            )
            if system_index is None:
                system_index = next(
                    (
                        index
                        for index, region in enumerate(regions)
                        if region.top - 5 <= top <= region.bottom + 5
                    ),
                    None,
                )
            if system_index is None:
                continue
            candidate = {
                "text": chord,
                "page_index": page_index,
                "system_index": system_index,
                "x0": float(word.get("x0") or 0),
                "x1": float(word.get("x1") or word.get("x0") or 0),
                "top": top,
                "signature": _word_signature(word),
                "region": regions[system_index],
            }
            systems[system_index].append(candidate)
            all_candidates.append(candidate)
        page_words.append(systems)

        page_metadata = _extract_pdf_metadata(page)
        page_titles.append(page_metadata.get("title") or "")
        prominent_directions.extend(
            {
                **direction,
                "page_index": page_index,
            }
            for direction in page_metadata.get("prominent_directions", [])
        )
        score_systems = page_systems[page_index] if page_index < len(page_systems) else []
        for caption in _extract_pdf_section_captions(page, words):
            mapped = _map_caption_to_measure(page, regions, score_systems, caption)
            if mapped:
                section_captions.append(
                    {
                        **caption,
                        **mapped,
                        "page_index": page_index,
                    }
                )
        for label in _extract_pdf_section_labels(page, words):
            mapped = _map_caption_to_measure(page, regions, score_systems, label)
            if mapped:
                section_labels.append(
                    {
                        **label,
                        **mapped,
                        "page_index": page_index,
                    }
                )
        colored_pitch_cues.extend(
            {
                **cue,
                "page_index": page_index,
            }
            for cue in _extract_colored_pitch_cues(page, words, regions, score_systems)
        )
        printed_measure_starts.extend(
            _extract_printed_measure_starts(page, words, regions, score_systems)
        )
        prominent_dynamics.extend(
            {
                **dynamic,
                "page_index": page_index,
            }
            for dynamic in _extract_prominent_pdf_dynamics(page, regions, score_systems)
        )
        performance_directions.extend(
            {
                **direction,
                "page_index": page_index,
            }
            for direction in _extract_pdf_performance_directions(
                page,
                words,
                regions,
                score_systems,
            )
        )
        if page_index == 0:
            metadata = page_metadata

    strong_signatures = Counter(
        candidate["signature"]
        for candidate in all_candidates
        if candidate["signature"] is not None and _is_strong_chord(candidate["text"])
    )
    all_signatures = Counter(
        candidate["signature"] for candidate in all_candidates if candidate["signature"] is not None
    )
    accepted_signatures = {
        signature for signature, count in strong_signatures.items() if count >= 2
    }
    if not accepted_signatures and all_signatures:
        signature, count = all_signatures.most_common(1)[0]
        if count >= 3:
            accepted_signatures.add(signature)

    filtered_pages: list[list[list[dict]]] = []
    for systems in page_words:
        filtered_systems = []
        for candidates in systems:
            filtered = [
                candidate
                for candidate in candidates
                if candidate["signature"] in accepted_signatures
            ]
            filtered, stacked_count = _merge_stacked_chord_candidates(filtered)
            stats["stacked_chords_merged"] += stacked_count
            filtered_systems.append(
                sorted(filtered, key=lambda candidate: (candidate["x0"], candidate["top"]))
            )
        filtered_pages.append(filtered_systems)
    metadata["page_titles"] = page_titles
    metadata["prominent_directions"] = prominent_directions
    metadata["section_captions"] = section_captions
    metadata["section_labels"] = section_labels
    metadata["colored_pitch_cues"] = colored_pitch_cues
    metadata["measure_number_resets"] = _infer_measure_number_resets(printed_measure_starts)
    metadata["prominent_dynamics"] = prominent_dynamics
    metadata["performance_directions"] = performance_directions
    return filtered_pages, metadata, stats


def _decode_pdf_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(PUA_TRANSLATION)).strip()


def _group_words_into_lines(words: list[dict], tolerance: float = 3.5) -> list[str]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda value: (float(value.get("top") or 0), float(value.get("x0") or 0))):
        top = float(word.get("top") or 0)
        if rows and abs(top - float(rows[-1][0].get("top") or 0)) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [
        _decode_pdf_text(" ".join(word.get("text") or "" for word in sorted(row, key=lambda value: float(value.get("x0") or 0))))
        for row in rows
    ]


def _clean_pdf_lyric_text(value: str) -> str:
    return "".join(
        character
        for character in (value or "")
        if unicodedata.category(character) != "Co"
    ).strip()


def _set_pdf_token_syllabics(tokens: list[dict]) -> None:
    group_counts = Counter(token["group"] for token in tokens)
    group_positions = Counter()
    for token in tokens:
        group = token["group"]
        group_positions[group] += 1
        position = group_positions[group]
        count = group_counts[group]
        token["syllabic"] = (
            "single"
            if count == 1
            else "begin"
            if position == 1
            else "end"
            if position == count
            else "middle"
        )


def _tokenize_pdf_lyric_words(words: list[dict]) -> list[dict]:
    """Split a positioned PDF lyric line into MusicXML syllable tokens."""
    tokens: list[dict] = []
    pending_connector = False
    next_group = -1

    for word in sorted(words, key=lambda item: float(item.get("x0") or 0)):
        cleaned_word = _clean_pdf_lyric_text(word.get("text") or "")
        if re.fullmatch(r"\d+\.", cleaned_word):
            continue
        if cleaned_word and not cleaned_word.strip("-–—"):
            pending_connector = bool(tokens)
            continue

        characters = [
            character
            for character in (word.get("chars") or [])
            if unicodedata.category(character.get("text") or "") != "Co"
        ]
        segments: list[list[dict] | None] = []
        current: list[dict] = []
        for character in characters:
            if (character.get("text") or "") in {"-", "–", "—"}:
                if current:
                    segments.append(current)
                    current = []
                segments.append(None)
            else:
                current.append(character)
        if current:
            segments.append(current)

        real_segments = [segment for segment in segments if segment]
        for segment_index, segment in enumerate(real_segments):
            text_value = "".join(character.get("text") or "" for character in segment).strip()
            if not text_value:
                continue
            if (pending_connector or segment_index > 0) and tokens:
                group = tokens[-1]["group"]
            else:
                next_group += 1
                group = next_group
            tokens.append(
                {
                    "text": text_value,
                    "x0": float(segment[0].get("x0") or word.get("x0") or 0),
                    "group": group,
                }
            )
            pending_connector = segment_index < len(real_segments) - 1
        if real_segments and not any(segment is None for segment in segments):
            pending_connector = False

    _set_pdf_token_syllabics(tokens)
    return tokens


def _normalized_lyric_text(value: str) -> str:
    value = (value or "").replace("’", "'").replace("‘", "'")
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _lyric_text(lyric) -> str:
    return " ".join(
        (element.text or "").strip()
        for element in _iter(lyric, "text")
        if (element.text or "").strip()
    ).strip()


def _is_preserved_lyric_direction(lyric) -> bool:
    lowered = _lyric_text(lyric).lower().strip()
    return (
        lowered.startswith("(")
        or lowered in {"only)", "basses)", "tenors)", "parts)", "mel.)"}
    )


def _infer_pdf_lyric_signature(pdf, page_systems) -> tuple[tuple[str, float] | None, list]:
    page_data = []
    signature_counts = Counter()
    for page_index, page in enumerate(pdf.pages):
        expected_systems = len(page_systems[page_index]) if page_index < len(page_systems) else 0
        regions = _detect_system_regions(page, expected_systems)
        raw_words = page.extract_words(
            x_tolerance=1,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=False,
            return_chars=True,
        )
        words = _filter_visible_pdf_objects(
            page,
            raw_words,
            color_key="non_stroking_color",
        )
        page_data.append((regions, words))
        for word in words:
            signature = _word_signature(word)
            if signature is None or not 6.5 <= signature[1] <= 13.5:
                continue
            top = float(word.get("top") or 0)
            if not any(region.top - 4 <= top <= region.bottom + 8 for region in regions):
                continue
            cleaned = _clean_pdf_lyric_text(word.get("text") or "")
            if not re.search(r"[A-Za-z]{2}", cleaned):
                continue
            if _canonicalize_pdf_chord(cleaned) is not None:
                continue
            signature_counts[signature] += 1

    if not signature_counts:
        return None, page_data
    signature, count = signature_counts.most_common(1)[0]
    minimum_count = max(4, sum(len(page) for page in page_systems) // 2)
    return (signature if count >= minimum_count else None), page_data


def _system_lyric_lines(words: list[dict], signature, region: SystemRegion) -> list[list[dict]]:
    candidates = [
        word
        for word in words
        if _word_signature(word) == signature
        and region.top - 4 <= float(word.get("top") or 0) <= region.bottom + 8
    ]
    clusters: list[list[dict]] = []
    for word in sorted(candidates, key=lambda item: (float(item.get("top") or 0), float(item.get("x0") or 0))):
        top = float(word.get("top") or 0)
        cluster = next(
            (
                existing
                for existing in clusters
                if abs(top - sum(float(item.get("top") or 0) for item in existing) / len(existing)) <= 3.5
            ),
            None,
        )
        if cluster is None:
            cluster = []
            clusters.append(cluster)
        cluster.append(word)
    return [
        sorted(cluster, key=lambda item: float(item.get("x0") or 0))
        for cluster in clusters
        if _tokenize_pdf_lyric_words(cluster)
    ]


def _merge_excess_lyric_tokens(tokens: list[dict], wanted_count: int) -> int:
    merged = 0
    while len(tokens) > wanted_count and len(tokens) > 1:
        merge_index = min(
            range(len(tokens) - 1),
            key=lambda index: max(0.0, tokens[index + 1]["x0"] - tokens[index]["x0"]),
        )
        left = tokens[merge_index]
        right = tokens[merge_index + 1]
        separator = "" if left["group"] == right["group"] else " "
        left["text"] = f"{left['text']}{separator}{right['text']}"
        del tokens[merge_index + 1]
        merged += 1
    _set_pdf_token_syllabics(tokens)
    return merged


def _assign_lyric_tokens_to_notes(tokens: list[dict], notes: list[dict]) -> list[int]:
    token_count = len(tokens)
    note_count = len(notes)
    if not token_count or token_count > note_count:
        return []

    infinity = float("inf")
    costs = [[infinity] * (note_count + 1) for _ in range(token_count + 1)]
    previous = [[None] * (note_count + 1) for _ in range(token_count + 1)]
    for note_index in range(note_count + 1):
        costs[0][note_index] = note_index * 2.0
        if note_index:
            previous[0][note_index] = (0, note_index - 1, False)

    for token_index in range(1, token_count + 1):
        for note_index in range(1, note_count + 1):
            skipped_cost = costs[token_index][note_index - 1] + 2.0
            if skipped_cost < costs[token_index][note_index]:
                costs[token_index][note_index] = skipped_cost
                previous[token_index][note_index] = (token_index, note_index - 1, False)

            token = tokens[token_index - 1]
            note = notes[note_index - 1]
            old_text = _normalized_lyric_text(note["old_text"])
            new_text = _normalized_lyric_text(token["text"])
            anchor_bonus = 0.0
            if old_text and old_text == new_text:
                anchor_bonus = -120.0
            elif old_text and new_text and (old_text in new_text or new_text in old_text):
                anchor_bonus = -30.0
            distance_cost = ((note["x0"] - token["x0"]) / 8.0) ** 2
            assigned_cost = costs[token_index - 1][note_index - 1] + distance_cost + anchor_bonus
            if assigned_cost < costs[token_index][note_index]:
                costs[token_index][note_index] = assigned_cost
                previous[token_index][note_index] = (token_index - 1, note_index - 1, True)

    note_index = min(
        range(token_count, note_count + 1),
        key=lambda index: costs[token_count][index] + (note_count - index) * 2.0,
    )
    token_index = token_count
    assignments = []
    while token_index:
        event = previous[token_index][note_index]
        if event is None:
            return []
        if event[2]:
            assignments.append(note_index - 1)
        token_index, note_index = event[0], event[1]
    return list(reversed(assignments))


def _clean_pdf_page_title(value: str) -> str:
    return re.sub(
        r"(?i)\s*[-–—]\s*page\s+\d+\s+of\s+\d+\s*$",
        "",
        re.sub(r"\s+", " ", value or "").strip(),
    ).strip()


def _line_word_groups(words: list[dict], tolerance: float = 4.0) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for word in sorted(words, key=lambda value: (float(value.get("top") or 0), float(value.get("x0") or 0))):
        top = float(word.get("top") or 0)
        if groups and abs(top - median(float(item.get("top") or 0) for item in groups[-1])) <= tolerance:
            groups[-1].append(word)
        else:
            groups.append([word])
    split_groups = []
    for group in groups:
        current = []
        for word in sorted(group, key=lambda value: float(value.get("x0") or 0)):
            if current:
                previous = current[-1]
                gap = float(word.get("x0") or 0) - float(previous.get("x1") or previous.get("x0") or 0)
                split_threshold = max(
                    24.0,
                    2.5 * max(_word_font_size(previous), _word_font_size(word)),
                )
                if gap > split_threshold:
                    split_groups.append(current)
                    current = []
            current.append(word)
        if current:
            split_groups.append(current)
    return split_groups


def _extract_pdf_page_title(page, words: list[dict]) -> str:
    candidates = []
    header_words = [
        word
        for word in words
        if float(word.get("top") or 0) < min(47.0, float(page.height) * 0.07)
    ]
    for group in _line_word_groups(header_words):
        text = _clean_pdf_page_title(
            _decode_pdf_text(" ".join(word.get("text") or "" for word in group))
        )
        if not text or not re.search(r"[A-Za-z]{2}", text):
            continue
        if re.search(
            r"(?i)(?:\bkey\s*:|\bbased on\b|\bwww\.|\bccli\b|"
            r"^(?:piano|voice|vocal|choir|satb|lead sheet)\b|^\(?satb\)?$)",
            text,
        ):
            continue
        sizes = [_word_font_size(word) for word in group]
        left = min(float(word.get("x0") or 0) for word in group)
        right = max(float(word.get("x1") or word.get("x0") or 0) for word in group)
        center = (left + right) / 2
        center_distance = abs(center - float(page.width) / 2)
        candidates.append((max(sizes, default=0.0), -center_distance, -float(group[0].get("top") or 0), text))
    if not candidates:
        return ""
    return max(candidates)[-1]


def _extract_prominent_pdf_directions(page, words: list[dict]) -> list[dict]:
    candidates = []
    text_words = [
        word
        for word in words
        if re.search(r"[A-Za-z0-9]", _clean_pdf_lyric_text(word.get("text") or ""))
    ]
    for group in _line_word_groups(text_words, tolerance=6.0):
        text = _decode_pdf_text(" ".join(word.get("text") or "" for word in group))
        if not re.search(r"[A-Za-z]", text):
            continue
        size = max((_word_font_size(word) for word in group), default=0.0)
        top = min(float(word.get("top") or 0) for word in group)
        if size < 22 or top < 70 or top > float(page.height) * 0.82:
            continue
        candidates.append(
            {
                "text": text,
                "top": top,
                "x0": min(float(word.get("x0") or 0) for word in group),
                "size": size,
            }
        )

    directions = []
    index = 0
    while index < len(candidates):
        current = dict(candidates[index])
        if index + 1 < len(candidates):
            following = candidates[index + 1]
            if (
                0 < following["top"] - current["top"] <= max(current["size"], following["size"]) * 1.8
                and abs(following["x0"] - current["x0"]) <= 45
            ):
                current["text"] = f"{current['text']} {following['text']}"
                index += 1
        if re.search(r"(?i)\bto\b.+\bmeasure\s+\d+\b", current["text"]):
            directions.append(current)
        index += 1
    return directions


def _extract_pdf_section_captions(page, words: list[dict]) -> list[dict]:
    captions = []
    section_pattern = re.compile(
        r"^\s*(?P<label>(?:\d+\s+)?"
        r"(?:Verse|Chorus|Bridge|Pre[- ]?Chorus|Refrain|Intro|Interlude|Tag|Outro))"
        r"\s+(?P<caption>.+?)\s*$",
        re.IGNORECASE,
    )
    for group in _line_word_groups(words, tolerance=5.5):
        text = _decode_pdf_text(" ".join(word.get("text") or "" for word in group))
        match = section_pattern.match(text)
        if not match:
            continue
        caption = match.group("caption").strip()
        if len(re.sub(r"[^A-Za-z]", "", caption)) < 4:
            continue
        captions.append(
            {
                "label": re.sub(r"\s+", " ", match.group("label")).strip(),
                "text": caption,
                "top": min(float(word.get("top") or 0) for word in group),
                "x0": min(float(word.get("x0") or 0) for word in group),
                "size": max((_word_font_size(word) for word in group), default=0.0),
            }
        )
    return captions


def _extract_pdf_section_labels(page, words: list[dict]) -> list[dict]:
    labels = []
    section_pattern = re.compile(
        r"^\s*(?P<label>(?:\d+\s+)?"
        r"(?:Verse|Chorus|Bridge|Pre[- ]?Chorus|Refrain|Intro|Interlude|Tag|Outro))\b",
        re.IGNORECASE,
    )
    for group in _line_word_groups(words, tolerance=5.5):
        text = _decode_pdf_text(" ".join(word.get("text") or "" for word in group))
        match = section_pattern.match(text)
        if not match:
            continue
        labels.append(
            {
                "label": re.sub(r"\s+", " ", match.group("label")).strip(),
                "top": min(float(word.get("top") or 0) for word in group),
                "x0": min(float(word.get("x0") or 0) for word in group),
            }
        )
    return labels


def _extract_pdf_performance_directions(
    page,
    words: list[dict],
    regions: list[SystemRegion],
    score_systems: list[list[str]],
) -> list[dict]:
    """Find short italic playing/singing instructions embedded in the PDF text layer."""
    directions = []
    seen = set()

    def append_candidate(candidate_words: list[dict]) -> None:
        if not candidate_words:
            return
        text = _decode_pdf_text(
            " ".join(word.get("text") or "" for word in candidate_words)
        )
        if not PERFORMANCE_DIRECTION_PATTERN.fullmatch(text):
            return
        font_names = {
            str(character.get("fontname") or "").lower()
            for word in candidate_words
            for character in word.get("chars", [])
        }
        if not any("italic" in name or "oblique" in name for name in font_names):
            return
        candidate = {
            "text": text,
            "top": min(float(word.get("top") or 0) for word in candidate_words),
            "x0": min(float(word.get("x0") or 0) for word in candidate_words),
        }
        mapped = _map_caption_to_measure(page, regions, score_systems, candidate)
        if not mapped:
            return
        system_index = int(mapped.get("system_index", -1))
        if system_index < 0 or system_index >= len(regions) or system_index >= len(score_systems):
            return
        region = regions[system_index]
        candidate["relative_y"] = max(
            0.0,
            min(
                0.999,
                (candidate["top"] - region.top) / max(1.0, region.bottom - region.top),
            ),
        )
        candidate["system_measures"] = list(score_systems[system_index])
        candidate.update(mapped)
        signature = (
            candidate["text"].lower(),
            candidate["measure_number"],
            round(candidate["relative_y"], 2),
        )
        if signature not in seen:
            seen.add(signature)
            directions.append(candidate)

    for word in words:
        append_candidate([word])
    for group in _line_word_groups(words, tolerance=3.5):
        if len(group) > 1:
            append_candidate(group)
    return directions


def _extract_pdf_metadata(page) -> dict:
    raw_words = page.extract_words(
        x_tolerance=2,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
        return_chars=True,
    )
    words = _filter_visible_pdf_objects(
        page,
        raw_words,
        color_key="non_stroking_color",
    )
    header_groups = _line_word_groups(
        [
            word
            for word in words
            if float(word.get("top") or 0) < min(130.0, float(page.height) * 0.18)
        ],
        tolerance=3.5,
    )
    header_lines = [
        _decode_pdf_text(" ".join(word.get("text") or "" for word in group))
        for group in header_groups
    ]
    footer_lines = _group_words_into_lines(
        [word for word in words if float(word.get("top") or 0) > float(page.height) * 0.88]
    )
    part_lines = _group_words_into_lines(
        [
            word
            for word in words
            if float(word.get("top") or 0) < float(page.height) * 0.13
            and float(word.get("x0") or 0) < float(page.width) * 0.25
        ]
    )

    subtitle = next(
        (
            line
            for line in header_lines
            if re.search(r"(?i)\b(?:based on|recording|album|ep)\b", line)
        ),
        "",
    )
    source_url = next(
        (line for line in header_lines if re.search(r"(?i)(?:https?://|www\.)", line)),
        "",
    )
    rights = " ".join(
        line
        for line in footer_lines
        if re.search(r"(?i)(?:©|copyright|rights reserved|permission|ccli|publishing)", line)
    )

    raw_text = _decode_pdf_text(page.extract_text() or "")
    tempo_match = re.search(r"=\s*((?:\d\s*){2,3})", raw_text)
    return {
        "title": _extract_pdf_page_title(page, words),
        "subtitle": subtitle,
        "source_url": source_url,
        "rights": rights,
        "tempo": int(re.sub(r"\s+", "", tempo_match.group(1))) if tempo_match else None,
        "prominent_directions": _extract_prominent_pdf_directions(page, words),
        "part_label": "\n".join(
            line
            for line in part_lines
            if re.search(r"(?i)(?:piano|choir|satb|lead sheet|vocal)", line)
        ),
    }


def _measure_boundaries(page, region: SystemRegion, expected_count: int) -> list[float]:
    if expected_count <= 0:
        return []
    minimum_height = max(40.0, (region.bottom - region.top) * 0.24)
    x_values = [region.left, region.right]
    for obj in _vertical_objects(page):
        width = float(obj.get("width") or 0)
        height = float(obj.get("height") or 0)
        top = float(obj.get("top") or 0)
        bottom = float(obj.get("bottom") or 0)
        overlap = min(region.bottom, bottom) - max(region.top, top)
        if width <= 3 and height >= minimum_height and overlap > 20:
            x = float(obj.get("x0") or 0)
            if region.left - 5 <= x <= region.right + 5:
                x_values.append(x)
    boundaries = _cluster_values(x_values, 5.0)
    wanted = expected_count + 1
    if len(boundaries) == wanted:
        return boundaries

    left = min(boundaries, default=region.left)
    right = max(boundaries, default=region.right)
    if right <= left:
        right = left + 1
    if len(boundaries) > wanted:
        selected = [left]
        available = [value for value in boundaries[1:-1]]
        for index in range(1, wanted - 1):
            target = left + (right - left) * index / expected_count
            if available:
                chosen = min(available, key=lambda value: abs(value - target))
                available.remove(chosen)
                selected.append(chosen)
        selected.append(right)
        return sorted(selected)
    return [left + (right - left) * index / expected_count for index in range(wanted)]


def _map_caption_to_measure(
    page,
    regions: list[SystemRegion],
    score_systems: list[list[str]],
    caption: dict,
) -> dict:
    if not regions or not score_systems:
        return {}
    available = min(len(regions), len(score_systems))
    caption_top = float(caption.get("top") or 0)
    nearby = [
        index
        for index in range(available)
        if regions[index].top - 60 <= caption_top <= regions[index].bottom
    ]
    candidates = nearby or list(range(available))
    system_index = min(
        candidates,
        key=lambda index: abs(caption_top - regions[index].top),
    )
    measure_numbers = score_systems[system_index]
    if not measure_numbers:
        return {}
    boundaries = _measure_boundaries(page, regions[system_index], len(measure_numbers))
    if len(boundaries) != len(measure_numbers) + 1:
        return {"system_index": system_index, "measure_number": measure_numbers[0]}
    measure_index = max(
        0,
        min(
            len(measure_numbers) - 1,
            bisect_right(boundaries, float(caption.get("x0") or 0)) - 1,
        ),
    )
    return {
        "system_index": system_index,
        "measure_number": measure_numbers[measure_index],
    }


def _word_color_hex(word: dict) -> str | None:
    colors = [
        character.get("non_stroking_color")
        for character in (word.get("chars") or [])
        if character.get("non_stroking_color") is not None
    ]
    color = next((value for value in colors if isinstance(value, (tuple, list))), None)
    if color is None or len(color) < 3:
        return None
    try:
        red, green, blue = (float(color[index]) for index in range(3))
    except (TypeError, ValueError):
        return None
    if max(red, green, blue) - min(red, green, blue) < 0.08:
        return None
    return "#{:02x}{:02x}{:02x}".format(
        *(round(max(0.0, min(1.0, component)) * 255) for component in (red, green, blue))
    )


def _extract_colored_pitch_cues(
    page,
    words: list[dict],
    regions: list[SystemRegion],
    score_systems: list[list[str]],
) -> list[dict]:
    cues = []
    for group in _line_word_groups(words, tolerance=3.5):
        tokens = [_decode_pdf_text(word.get("text") or "") for word in group]
        if not 2 <= len(tokens) <= 8 or not all(re.fullmatch(r"[A-G](?:#|b)?", token) for token in tokens):
            continue
        color = next((_word_color_hex(word) for word in group if _word_color_hex(word)), None)
        if not color:
            continue
        cue = {
            "text": " ".join(tokens),
            "top": min(float(word.get("top") or 0) for word in group),
            "x0": min(float(word.get("x0") or 0) for word in group),
            "color": color,
        }
        mapped = _map_caption_to_measure(page, regions, score_systems, cue)
        if mapped:
            cues.append({**cue, **mapped})
    return cues


def _extract_printed_measure_starts(
    page,
    words: list[dict],
    regions: list[SystemRegion],
    score_systems: list[list[str]],
) -> list[dict]:
    starts = []
    for region, measure_numbers in zip(regions, score_systems):
        if not measure_numbers:
            continue
        candidates = [
            word
            for word in words
            if re.fullmatch(r"\d{1,4}", _decode_pdf_text(word.get("text") or ""))
            and float(word.get("x0") or 0) <= region.left + 10
            and region.bottom - 6 <= float(word.get("top") or 0) <= region.bottom + 22
            and _word_font_size(word) <= 11
        ]
        if not candidates:
            continue
        printed = min(
            candidates,
            key=lambda word: (
                abs(float(word.get("top") or 0) - region.bottom),
                float(word.get("x0") or 0),
            ),
        )
        starts.append(
            {
                "recognized_measure": str(measure_numbers[0]),
                "printed_measure": _decode_pdf_text(printed.get("text") or ""),
            }
        )
    return starts


def _infer_measure_number_resets(starts: list[dict]) -> list[dict]:
    observations = []
    for start in starts:
        try:
            recognized = int(start["recognized_measure"])
            printed = int(start["printed_measure"])
        except (KeyError, TypeError, ValueError):
            continue
        observations.append((recognized, printed, printed - recognized))

    resets = []
    index = 0
    while index < len(observations):
        recognized, printed, offset = observations[index]
        end = index + 1
        while end < len(observations) and observations[end][2] == offset:
            end += 1
        if offset and end - index >= 2:
            resets.append(
                {
                    "boundary_measure": str(recognized),
                    "printed_measure": str(printed),
                    "offset": offset,
                }
            )
        index = end
    return resets


def _extract_prominent_pdf_dynamics(
    page,
    regions: list[SystemRegion],
    score_systems: list[list[str]],
) -> list[dict]:
    # Maestro's legacy Windows mapping uses U+0192 for the engraved "ff" glyph.
    dynamics = []

    def append_dynamic(dynamic: str, x0: float, top: float) -> None:
        system_index = next(
            (
                index
                for index, region in enumerate(regions)
                if region.top <= top <= region.bottom
            ),
            None,
        )
        if system_index is None or system_index >= len(score_systems) or not score_systems[system_index]:
            return
        region = regions[system_index]
        boundaries = _measure_boundaries(page, region, len(score_systems[system_index]))
        measure_index = max(
            0,
            min(
                len(score_systems[system_index]) - 1,
                bisect_right(boundaries, x0) - 1,
            ),
        )
        candidate = {
            "dynamic": dynamic,
            "measure_number": score_systems[system_index][measure_index],
            "system_measures": list(score_systems[system_index]),
            "relative_y": max(
                0.0,
                min(0.999, (top - region.top) / max(1.0, region.bottom - region.top)),
            ),
        }
        if not any(
            existing["dynamic"] == candidate["dynamic"]
            and existing["measure_number"] == candidate["measure_number"]
            and abs(existing["relative_y"] - candidate["relative_y"]) <= 0.04
            for existing in dynamics
        ):
            dynamics.append(candidate)

    chars = _filter_visible_pdf_objects(
        page,
        list(getattr(page, "chars", []) or []),
        color_key="non_stroking_color",
    )
    for character in chars:
        if "maestro" not in str(character.get("fontname") or "").lower():
            continue
        if (character.get("text") or "") != "\u0192":
            continue
        top = float(character.get("top") or 0)
        append_dynamic(
            "ff",
            float(character.get("x0") or 0),
            top,
        )

    raw_words = page.extract_words(
        x_tolerance=2,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
        return_chars=True,
    )
    words = _filter_visible_pdf_objects(
        page,
        raw_words,
        color_key="non_stroking_color",
    )
    for word in words:
        value = re.sub(r"\s+", "", _decode_pdf_text(word.get("text") or "")).lower()
        if not re.fullmatch(r"(?:ppp|pp|p|mp|mf|f|ff|fff|sf|sfp|sfz|fz|fp)", value):
            continue
        font_names = {
            str(character.get("fontname") or "").lower()
            for character in word.get("chars", [])
        }
        if not any("italic" in font_name for font_name in font_names):
            continue
        append_dynamic(
            value,
            float(word.get("x0") or 0),
            float(word.get("top") or 0),
        )
    return dynamics


def _existing_chord_symbols(root) -> list[str]:
    symbols = []
    for lyric in _iter(root, "lyric"):
        if lyric.attrib.get("name") not in RECOVERED_CHORD_LYRIC_NAMES:
            continue
        text = "".join((element.text or "") for element in _iter(lyric, "text"))
        canonical = _canonicalize_pdf_chord(text)
        if canonical:
            symbols.append(canonical)
    for harmony in _iter(root, "harmony"):
        symbol = _harmony_symbol(harmony)
        if symbol:
            symbols.append(symbol)
    return symbols


def _harmony_symbol(harmony) -> str | None:
    kind = _child(harmony, "kind")
    kind_text = (kind.attrib.get("text") if kind is not None else "") or ""
    if kind is not None and (kind.text or "").strip() == "none":
        return "N.C."
    root = _child(harmony, "root")
    if root is None:
        return None
    step = _child(root, "root-step")
    if step is None or not (step.text or "").strip():
        return None
    accidental = ""
    alter = _child(root, "root-alter")
    if alter is not None:
        try:
            value = int(float((alter.text or "0").strip()))
            accidental = "#" * max(0, value) or "b" * max(0, -value)
        except ValueError:
            pass
    bass_text = ""
    bass = _child(harmony, "bass")
    if bass is not None:
        bass_step = _child(bass, "bass-step")
        if bass_step is not None:
            bass_text = f"/{(bass_step.text or '').strip()}"
    return _canonicalize_pdf_chord(f"{(step.text or '').strip()}{accidental}{kind_text}{bass_text}")


def _select_chord_part(root):
    best = None
    best_score = (-1, -1, -1)
    for part in _iter(root, "part"):
        recovered = sum(
            1
            for lyric in _iter(part, "lyric")
            if lyric.attrib.get("name") in RECOVERED_CHORD_LYRIC_NAMES
        )
        sung = sum(
            1
            for lyric in _iter(part, "lyric")
            if lyric.attrib.get("name") not in RECOVERED_CHORD_LYRIC_NAMES
        )
        pitched = sum(1 for _pitch in _iter(part, "pitch"))
        score = (recovered, sung, pitched)
        if score > best_score:
            best = part
            best_score = score
    return best


def _score_part_names(root) -> dict[str, str]:
    names = {}
    for score_part in _iter(root, "score-part"):
        part_name = _child(score_part, "part-name")
        names[score_part.attrib.get("id", "")] = (
            (part_name.text or "").strip() if part_name is not None else ""
        )
    return names


def _select_chord_part_for_measures(root, measure_numbers: list[str]):
    parts = _children(root, "part")
    if not parts:
        return None
    names = _score_part_names(root)
    activity = []
    wanted = set(measure_numbers)
    for part_index, part in enumerate(parts):
        measures = [
            measure
            for measure in _children(part, "measure")
            if measure.attrib.get("number", "") in wanted
        ]
        pitched = sum(1 for measure in measures for _pitch in _iter(measure, "pitch"))
        sung = sum(
            1
            for measure in measures
            for lyric in _iter(measure, "lyric")
            if lyric.attrib.get("name") not in RECOVERED_CHORD_LYRIC_NAMES and _lyric_text(lyric)
        )
        existing_chords = sum(
            1
            for measure in measures
            for _symbol in list(_iter(measure, "harmony"))
        )
        name = names.get(part.attrib.get("id", ""), "")
        vocal = bool(re.search(r"(?i)(?:voice|vocal|choir|soprano|alto|tenor|bass)", name))
        activity.append(
            {
                "part": part,
                "part_index": part_index,
                "pitched": pitched,
                "sung": sung,
                "existing_chords": existing_chords,
                "vocal": vocal,
            }
        )

    active = [entry for entry in activity if entry["pitched"] or entry["sung"]]
    if not active:
        return _select_chord_part(root)
    vocal_active = [entry for entry in active if entry["vocal"]]
    if len(active) > 1 and vocal_active:
        return max(
            vocal_active,
            key=lambda entry: (
                bool(entry["sung"]),
                entry["existing_chords"],
                -entry["part_index"],
            ),
        )["part"]
    return max(
        active,
        key=lambda entry: (
            entry["existing_chords"],
            bool(entry["sung"]),
            entry["pitched"],
            -entry["part_index"],
        ),
    )["part"]


def _direction_words_value(direction) -> str:
    return " ".join(
        re.sub(r"\s+", " ", (words.text or "")).strip()
        for words in _iter(direction, "words")
        if (words.text or "").strip()
    ).strip()


def _is_chord_only_direction(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "")
    if not compact:
        return False
    if _canonicalize_pdf_chord(compact) is not None:
        return True
    return bool(
        re.fullmatch(r"[A-Ga-g#b_m0-9()+/.\-]{1,16}", compact)
        and re.search(r"[A-Ga-g]", compact)
    )


def _remove_existing_score_chords(root) -> int:
    removed = 0
    for note in _iter(root, "note"):
        for lyric in list(_children(note, "lyric")):
            if lyric.attrib.get("name") in RECOVERED_CHORD_LYRIC_NAMES:
                note.remove(lyric)
                removed += 1
    for measure in _iter(root, "measure"):
        for harmony in list(_children(measure, "harmony")):
            measure.remove(harmony)
            removed += 1
        for direction in list(_children(measure, "direction")):
            if _is_chord_only_direction(_direction_words_value(direction)):
                measure.remove(direction)
                removed += 1
    return removed


def _measure_durations(part) -> dict[int, tuple[int, int]]:
    divisions = 1
    beats = 4
    beat_type = 4
    durations = {}
    for measure in _children(part, "measure"):
        attributes = _child(measure, "attributes")
        if attributes is not None:
            divisions_element = _child(attributes, "divisions")
            if divisions_element is not None:
                try:
                    divisions = max(1, int(float((divisions_element.text or "1").strip())))
                except ValueError:
                    pass
            time = _child(attributes, "time")
            if time is not None:
                beats_element = _child(time, "beats")
                beat_type_element = _child(time, "beat-type")
                try:
                    beats = int((beats_element.text or "").strip()) if beats_element is not None else beats
                    beat_type = (
                        int((beat_type_element.text or "").strip())
                        if beat_type_element is not None
                        else beat_type
                    )
                except ValueError:
                    pass
        duration = max(1, round(divisions * beats * 4 / beat_type))
        durations[id(measure)] = (duration, divisions)
    return durations


def _staff_line_groups(page, region: SystemRegion) -> list[list[float]]:
    objects = list(getattr(page, "lines", []) or []) + list(getattr(page, "rects", []) or [])
    visible = _filter_visible_pdf_objects(page, objects, color_key="stroking_color")
    candidates = []
    minimum_width = max(90.0, (region.right - region.left) * 0.45)
    for obj in visible:
        width = float(obj.get("width") or 0)
        height = float(obj.get("height") or 0)
        top = float(obj.get("top") or 0)
        x0 = float(obj.get("x0") or 0)
        x1 = float(obj.get("x1") or x0 + width)
        if (
            width >= minimum_width
            and height <= 2.0
            and region.top - 3 <= top <= region.bottom + 3
            and x1 >= region.left + (region.right - region.left) * 0.55
        ):
            candidates.append(top)
    y_values = _cluster_values(candidates, 0.8)
    groups = []
    index = 0
    while index + 4 < len(y_values):
        group = y_values[index : index + 5]
        gaps = [group[offset + 1] - group[offset] for offset in range(4)]
        average = sum(gaps) / len(gaps)
        if 2.0 <= average <= 9.0 and max(abs(gap - average) for gap in gaps) <= 0.9:
            groups.append(group)
            index += 5
        else:
            index += 1
    return groups


def _measure_key_fifths(part) -> dict[int, int]:
    current = 0
    result = {}
    for measure in _children(part, "measure"):
        attributes = _child(measure, "attributes")
        key = _child(attributes, "key") if attributes is not None else None
        fifths = _child(key, "fifths") if key is not None else None
        if fifths is not None:
            try:
                current = int((fifths.text or "0").strip())
            except ValueError:
                pass
        result[id(measure)] = current
    return result


def _measure_clefs(part) -> dict[int, dict[int, tuple[str, int, int]]]:
    current: dict[int, tuple[str, int, int]] = {}
    result = {}
    for measure in _children(part, "measure"):
        attributes = _child(measure, "attributes")
        if attributes is not None:
            for clef in _children(attributes, "clef"):
                try:
                    number = int(clef.attrib.get("number", "1"))
                except ValueError:
                    number = 1
                sign_element = _child(clef, "sign")
                line_element = _child(clef, "line")
                octave_element = _child(clef, "clef-octave-change")
                sign = (sign_element.text or "G").strip() if sign_element is not None else "G"
                default_line = {"G": 2, "F": 4, "C": 3}.get(sign, 2)
                try:
                    line = int((line_element.text or str(default_line)).strip()) if line_element is not None else default_line
                except ValueError:
                    line = default_line
                try:
                    octave_change = int((octave_element.text or "0").strip()) if octave_element is not None else 0
                except ValueError:
                    octave_change = 0
                current[number] = (sign, line, octave_change)
        result[id(measure)] = dict(current)
    return result


def _clef_bottom_line_diatonic(clef: tuple[str, int, int] | None, staff_number: int) -> int:
    sign, line, octave_change = clef or (("F", 4, 0) if staff_number == 2 else ("G", 2, 0))
    reference = {
        "G": 4 * 7 + 4,  # G4
        "F": 3 * 7 + 3,  # F3
        "C": 4 * 7,      # C4
    }.get(sign, 4 * 7 + 4)
    return reference - 2 * (line - 1) + 7 * octave_change


def _key_alter_for_step(step: str, fifths: int) -> int:
    if fifths > 0 and step in "FCGDAEB"[: min(7, fifths)]:
        return 1
    if fifths < 0 and step in "BEADGCF"[: min(7, -fifths)]:
        return -1
    return 0


def _measure_has_pitched_notes(measure) -> bool:
    return any(_child(note, "pitch") is not None for note in _children(measure, "note"))


def _append_whole_chord_note(
    measure,
    *,
    step: str,
    octave: int,
    alter: int,
    duration: int,
    staff_number: int,
    voice_number: int,
    chord: bool,
    fermata: str = "",
) -> None:
    note = ET.Element(_qualified(measure, "note"))
    if chord:
        ET.SubElement(note, _qualified(note, "chord"))
    pitch = ET.SubElement(note, _qualified(note, "pitch"))
    step_element = ET.SubElement(pitch, _qualified(pitch, "step"))
    step_element.text = step
    if alter:
        alter_element = ET.SubElement(pitch, _qualified(pitch, "alter"))
        alter_element.text = str(alter)
    octave_element = ET.SubElement(pitch, _qualified(pitch, "octave"))
    octave_element.text = str(octave)
    duration_element = ET.SubElement(note, _qualified(note, "duration"))
    duration_element.text = str(duration)
    voice_element = ET.SubElement(note, _qualified(note, "voice"))
    voice_element.text = str(voice_number)
    type_element = ET.SubElement(note, _qualified(note, "type"))
    type_element.text = "whole"
    staff_element = ET.SubElement(note, _qualified(note, "staff"))
    staff_element.text = str(staff_number)
    if fermata:
        notations = ET.SubElement(note, _qualified(note, "notations"))
        fermata_element = ET.SubElement(
            notations,
            _qualified(notations, "fermata"),
            {"type": fermata},
        )
        fermata_element.text = "normal"

    insert_index = next(
        (
            index
            for index, child in enumerate(list(measure))
            if _local_name(child.tag) == "barline"
        ),
        len(list(measure)),
    )
    measure.insert(insert_index, note)


def _replace_empty_measure_with_whole_chords(
    measure,
    recovered_by_staff: dict[int, list[dict]],
    *,
    duration: int,
    staff_count: int,
) -> int:
    if _measure_has_pitched_notes(measure) or not recovered_by_staff:
        return 0

    normalized: dict[int, list[dict]] = {}
    for staff_number, entries in recovered_by_staff.items():
        x_clusters = _cluster_values([float(entry["x0"]) for entry in entries], 4.0)
        if len(x_clusters) != 1:
            return 0
        unique = {}
        for entry in entries:
            unique[(entry["step"], entry["octave"], entry["alter"])] = entry
        if not 1 <= len(unique) <= 8:
            return 0
        normalized[staff_number] = sorted(
            unique.values(),
            key=lambda entry: entry["diatonic"],
        )

    for child in list(measure):
        if _local_name(child.tag) in {"note", "backup", "forward"}:
            measure.remove(child)

    added = 0
    staff_numbers = sorted(normalized)
    for staff_index, staff_number in enumerate(staff_numbers):
        if staff_index:
            backup = ET.Element(_qualified(measure, "backup"))
            backup_duration = ET.SubElement(backup, _qualified(backup, "duration"))
            backup_duration.text = str(duration)
            insert_index = next(
                (
                    index
                    for index, child in enumerate(list(measure))
                    if _local_name(child.tag) == "barline"
                ),
                len(list(measure)),
            )
            measure.insert(insert_index, backup)
        entries = normalized[staff_number]
        fermata_type = "inverted" if staff_count > 1 and staff_number == staff_count else "upright"
        for note_index, entry in enumerate(entries):
            place_fermata = bool(entry.get("fermata")) and (
                note_index == (0 if fermata_type == "inverted" else len(entries) - 1)
            )
            _append_whole_chord_note(
                measure,
                step=entry["step"],
                octave=entry["octave"],
                alter=entry["alter"],
                duration=duration,
                staff_number=staff_number,
                voice_number=staff_number,
                chord=note_index > 0,
                fermata=fermata_type if place_fermata else "",
            )
            added += 1
    return added


def _restore_sparse_pdf_whole_note_measures(root, pdf, score_pages) -> dict:
    report = {
        "sparse_whole_note_measures_found": 0,
        "sparse_whole_note_measures_restored": 0,
        "sparse_whole_notes_restored": 0,
    }
    parts = _children(root, "part")
    if not parts:
        return report

    layout = []
    measures_by_part = {}
    durations_by_part = {}
    fifths_by_part = {}
    clefs_by_part = {}
    for part in parts:
        part_id = part.attrib.get("id", "")
        staff_count = _part_staff_span(part)
        for staff_number in range(1, staff_count + 1):
            layout.append((part, part_id, staff_number, staff_count))
        measures_by_part[part_id] = {
            measure.attrib.get("number", ""): measure
            for measure in _children(part, "measure")
        }
        durations_by_part[part_id] = _measure_durations(part)
        fifths_by_part[part_id] = _measure_key_fifths(part)
        clefs_by_part[part_id] = _measure_clefs(part)

    recovered: dict[tuple[str, str], dict[int, list[dict]]] = {}
    page_count = min(len(pdf.pages), len(score_pages))
    for page_index in range(page_count):
        page = pdf.pages[page_index]
        score_systems = score_pages[page_index]
        regions = _detect_system_regions(page, len(score_systems))
        if len(regions) != len(score_systems):
            continue
        chars = _filter_visible_pdf_objects(
            page,
            list(getattr(page, "chars", []) or []),
            color_key="non_stroking_color",
        )
        whole_notes = [
            character
            for character in chars
            if (
                (
                    "jazz" in str(character.get("fontname") or "").lower()
                    and (character.get("text") or "") == "w"
                )
                or (character.get("text") or "") in {"\U0001d15d", "\U0001d15e"}
            )
        ]
        fermatas = [
            character
            for character in chars
            if "maestro" in str(character.get("fontname") or "").lower()
            and (character.get("text") or "") == "U"
        ]
        for system_index, (region, measure_numbers) in enumerate(zip(regions, score_systems)):
            if not measure_numbers:
                continue
            staff_groups = _staff_line_groups(page, region)
            if len(staff_groups) != len(layout):
                continue
            boundaries = _measure_boundaries(page, region, len(measure_numbers))
            if len(boundaries) != len(measure_numbers) + 1:
                continue
            for character in whole_notes:
                x0 = float(character.get("x0") or 0)
                top = float(character.get("top") or 0)
                # Music-font glyph boxes can extend well beyond the staff-line
                # rectangle, especially for whole notes below a bass staff.
                if not (
                    region.left - 3 <= x0 <= region.right + 3
                    and region.top - 30 <= top <= region.bottom + 30
                ):
                    continue
                staff_index = min(
                    range(len(staff_groups)),
                    key=lambda index: abs(top - median(staff_groups[index])),
                )
                lines = staff_groups[staff_index]
                spacing = median(
                    lines[offset + 1] - lines[offset]
                    for offset in range(4)
                )
                anchor_y = top - 0.4 * spacing
                if abs(anchor_y - median(lines)) > spacing * 5.2:
                    continue
                measure_index = max(
                    0,
                    min(
                        len(measure_numbers) - 1,
                        bisect_right(boundaries, x0) - 1,
                    ),
                )
                measure_number = measure_numbers[measure_index]
                part, part_id, staff_number, staff_count = layout[staff_index]
                measure = measures_by_part.get(part_id, {}).get(measure_number)
                if measure is None:
                    continue
                clef = clefs_by_part[part_id].get(id(measure), {}).get(staff_number)
                bottom_diatonic = _clef_bottom_line_diatonic(clef, staff_number)
                diatonic = bottom_diatonic + round((lines[-1] - anchor_y) / (spacing / 2))
                step_index = diatonic % 7
                octave = diatonic // 7
                step = "CDEFGAB"[step_index]
                fifths = fifths_by_part[part_id].get(id(measure), 0)
                nearby_fermata = any(
                    abs(float(symbol.get("x0") or 0) - x0) <= 15
                    and abs(float(symbol.get("top") or 0) - (lines[0] - 3)) <= 12
                    for symbol in fermatas
                )
                recovered.setdefault((part_id, measure_number), {}).setdefault(
                    staff_number,
                    [],
                ).append(
                    {
                        "x0": x0,
                        "step": step,
                        "octave": octave,
                        "alter": _key_alter_for_step(step, fifths),
                        "diatonic": diatonic,
                        "fermata": nearby_fermata,
                        "staff_count": staff_count,
                    }
                )

    for (part_id, measure_number), recovered_by_staff in recovered.items():
        measure = measures_by_part.get(part_id, {}).get(measure_number)
        if measure is None or _measure_has_pitched_notes(measure):
            continue
        report["sparse_whole_note_measures_found"] += 1
        duration, _divisions = durations_by_part[part_id].get(id(measure), (4, 1))
        staff_count = max(
            (entry["staff_count"] for entries in recovered_by_staff.values() for entry in entries),
            default=1,
        )
        added = _replace_empty_measure_with_whole_chords(
            measure,
            recovered_by_staff,
            duration=duration,
            staff_count=staff_count,
        )
        if added:
            report["sparse_whole_note_measures_restored"] += 1
            report["sparse_whole_notes_restored"] += added
    return report


def _pitch_components(pitch_name: str) -> tuple[str, int]:
    step = pitch_name[0].upper()
    accidental = pitch_name[1:]
    return step, accidental.count("#") - accidental.count("b")


def _kind_value(suffix: str) -> str:
    normalized = suffix.lower()
    return {
        "": "major",
        "m": "minor",
        "7": "dominant",
        "m7": "minor-seventh",
        "maj7": "major-seventh",
        "maj9": "major-ninth",
        "sus": "suspended-fourth",
        "sus4": "suspended-fourth",
        "sus2": "suspended-second",
        "dim": "diminished",
        "aug": "augmented",
    }.get(normalized, "other")


def _make_harmony(measure, chord: str, offset: int):
    harmony = ET.Element(
        _qualified(measure, "harmony"),
        {"placement": "above", "print-frame": "no"},
    )
    if chord == "N.C.":
        kind = ET.SubElement(harmony, _qualified(measure, "kind"), {"text": "N.C."})
        kind.text = "none"
    else:
        match = CHORD_PATTERN.fullmatch(chord)
        if match is None:
            return None
        root_name, suffix, bass_name = match.groups()
        root = ET.SubElement(harmony, _qualified(measure, "root"))
        step, alter = _pitch_components(root_name)
        root_step = ET.SubElement(root, _qualified(measure, "root-step"))
        root_step.text = step
        if alter:
            root_alter = ET.SubElement(root, _qualified(measure, "root-alter"))
            root_alter.text = str(alter)
        kind_attributes = {"text": suffix} if suffix else {}
        kind = ET.SubElement(harmony, _qualified(measure, "kind"), kind_attributes)
        kind.text = _kind_value(suffix)
        if bass_name:
            bass = ET.SubElement(harmony, _qualified(measure, "bass"))
            bass_step_text, bass_alter_value = _pitch_components(bass_name)
            bass_step = ET.SubElement(bass, _qualified(measure, "bass-step"))
            bass_step.text = bass_step_text
            if bass_alter_value:
                bass_alter = ET.SubElement(bass, _qualified(measure, "bass-alter"))
                bass_alter.text = str(bass_alter_value)
    if offset > 0:
        offset_element = ET.SubElement(harmony, _qualified(measure, "offset"))
        offset_element.text = str(offset)
    return harmony


def _insert_harmony_at_measure_start(measure, harmony) -> None:
    children = list(measure)
    insert_index = 0
    while insert_index < len(children) and _local_name(children[insert_index].tag) in {
        "print",
        "attributes",
        "direction",
        "barline",
        "harmony",
    }:
        insert_index += 1
    measure.insert(insert_index, harmony)


def _add_words_direction(
    measure,
    text: str,
    *,
    font_size: str = "10",
    bold: bool = False,
    italic: bool = False,
    default_y: str = "30",
    color: str | None = None,
    enclosure: str | None = None,
) -> int:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return 0
    if any(
        re.sub(r"\s+", " ", _direction_words_value(direction)).strip().lower()
        == normalized.lower()
        for direction in _children(measure, "direction")
    ):
        return 0
    direction = ET.Element(_qualified(measure, "direction"), {"placement": "above"})
    direction_type = ET.SubElement(direction, _qualified(measure, "direction-type"))
    attributes = {
        "font-family": "sans-serif",
        "font-size": font_size,
        "default-y": default_y,
    }
    if bold:
        attributes["font-weight"] = "bold"
    if italic:
        attributes["font-style"] = "italic"
    if color:
        attributes["color"] = color
    if enclosure:
        attributes["enclosure"] = enclosure
    words = ET.SubElement(direction_type, _qualified(measure, "words"), attributes)
    words.text = normalized
    _insert_harmony_at_measure_start(measure, direction)
    return 1


def _restore_score_boundaries_and_prominent_directions(
    root,
    metadata: dict,
    score_pages,
    target_part_by_measure: dict[str, str],
) -> dict:
    report = {
        "mixed_score_boundaries_found": 0,
        "mixed_score_titles_restored": 0,
        "prominent_directions_restored": 0,
        "section_captions_found": 0,
        "section_captions_restored": 0,
        "section_caption_artifacts_removed": 0,
        "section_boundary_measures": [],
    }
    parts = {
        part.attrib.get("id", ""): part
        for part in _children(root, "part")
    }
    measures_by_part = {
        part_id: {
            measure.attrib.get("number", ""): measure
            for measure in _children(part, "measure")
        }
        for part_id, part in parts.items()
    }

    page_titles = metadata.get("page_titles") or []
    primary_title = next((title for title in page_titles if title), "")
    previous_title = primary_title
    for page_index, title in enumerate(page_titles):
        if not title:
            continue
        if previous_title and title.lower() == previous_title.lower():
            continue
        if page_index >= len(score_pages) or not score_pages[page_index]:
            previous_title = title
            continue
        first_system = score_pages[page_index][0]
        if not first_system:
            previous_title = title
            continue
        measure_number = first_system[0]
        target_id = target_part_by_measure.get(measure_number, "")
        measure = measures_by_part.get(target_id, {}).get(measure_number)
        if measure is not None:
            report["mixed_score_boundaries_found"] += 1
            report["mixed_score_titles_restored"] += _add_words_direction(
                measure,
                title,
                font_size="14",
                bold=True,
                default_y="48",
            )
            report["section_boundary_measures"].append(measure_number)
        previous_title = title

    for direction in metadata.get("prominent_directions") or []:
        page_index = int(direction.get("page_index", -1))
        if page_index < 0 or page_index >= len(score_pages) or not score_pages[page_index]:
            continue
        last_system = score_pages[page_index][-1]
        if not last_system:
            continue
        measure_number = last_system[-1]
        target_id = target_part_by_measure.get(measure_number, "")
        measure = measures_by_part.get(target_id, {}).get(measure_number)
        if measure is None:
            continue
        report["prominent_directions_restored"] += _add_words_direction(
            measure,
            direction.get("text") or "",
            font_size="11",
            bold=True,
            italic=True,
            default_y="34",
        )
    return report


def _caption_signature(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", value or "").lower())


def _section_label_measure_numbers(root, label: str) -> list[str]:
    target = re.sub(r"\s+", " ", label or "").strip().lower()
    if not target:
        return []
    matches = []
    for part in _children(root, "part"):
        for measure in _children(part, "measure"):
            for direction in _children(measure, "direction"):
                values = [
                    re.sub(r"\s+", " ", (element.text or "")).strip().lower()
                    for element in direction.iter()
                    if _local_name(element.tag) in {"words", "rehearsal"}
                    and (element.text or "").strip()
                ]
                if target in values:
                    number = measure.attrib.get("number", "")
                    if number and number not in matches:
                        matches.append(number)
    return matches


def _restore_pdf_section_captions(
    root,
    metadata: dict,
    target_part_by_measure: dict[str, str],
) -> dict:
    report = {
        "section_captions_found": 0,
        "section_captions_restored": 0,
        "section_caption_artifacts_removed": 0,
    }
    captions = metadata.get("section_captions") or []
    report["section_captions_found"] = len(captions)
    if not captions:
        return report

    parts = {
        part.attrib.get("id", ""): part
        for part in _children(root, "part")
    }
    measures_by_part = {
        part_id: {
            measure.attrib.get("number", ""): measure
            for measure in _children(part, "measure")
        }
        for part_id, part in parts.items()
    }

    target_signatures = {
        signature
        for caption in captions
        if len(signature := _caption_signature(caption.get("text") or "")) >= 5
    }
    for part_measures in measures_by_part.values():
        for peer_measure in part_measures.values():
            for direction in list(_children(peer_measure, "direction")):
                existing_signature = _caption_signature(_direction_words_value(direction))
                if len(existing_signature) < 5:
                    continue
                if not any(
                    SequenceMatcher(None, existing_signature, target_signature).ratio() >= 0.55
                    for target_signature in target_signatures
                ):
                    continue
                peer_measure.remove(direction)
                report["section_caption_artifacts_removed"] += 1

    for caption in captions:
        text = re.sub(r"\s+", " ", caption.get("text") or "").strip()
        measure_number = str(caption.get("measure_number") or "")
        label_matches = _section_label_measure_numbers(root, caption.get("label") or "")
        if label_matches:
            try:
                approximate = int(measure_number)
                measure_number = min(
                    label_matches,
                    key=lambda number: abs(int(number) - approximate),
                )
            except ValueError:
                measure_number = label_matches[0]
        target_id = target_part_by_measure.get(measure_number, "")
        measure = measures_by_part.get(target_id, {}).get(measure_number)
        if not text or measure is None:
            continue
        report["section_captions_restored"] += _add_words_direction(
            measure,
            text,
            font_size="9",
            italic=True,
            default_y="30",
        )
    return report


def _restore_pdf_section_labels(
    root,
    metadata: dict,
    target_part_by_measure: dict[str, str],
) -> dict:
    labels = metadata.get("section_labels") or []
    report = {"section_labels_found": len(labels), "section_labels_restored": 0}
    parts = {part.attrib.get("id", ""): part for part in _children(root, "part")}
    measures_by_part = {
        part_id: {
            measure.attrib.get("number", ""): measure
            for measure in _children(part, "measure")
        }
        for part_id, part in parts.items()
    }
    for label in labels:
        text = re.sub(r"\s+", " ", label.get("label") or "").strip()
        if not text or _section_label_measure_numbers(root, text):
            continue
        measure_number = str(label.get("measure_number") or "")
        target_id = target_part_by_measure.get(measure_number, "")
        measure = measures_by_part.get(target_id, {}).get(measure_number)
        if measure is None:
            continue
        report["section_labels_restored"] += _add_words_direction(
            measure,
            text,
            font_size="10",
            bold=True,
            default_y="42",
            enclosure="rectangle",
        )
    return report


def _part_staff_span(part) -> int:
    maximum = 1
    for staves in _iter(part, "staves"):
        try:
            maximum = max(maximum, int((staves.text or "1").strip()))
        except ValueError:
            pass
    for staff in _iter(part, "staff"):
        try:
            maximum = max(maximum, int((staff.text or "1").strip()))
        except ValueError:
            pass
    return maximum


def _dynamic_directions(measure) -> list[tuple[object, object]]:
    found = []
    for direction in _children(measure, "direction"):
        if any(True for _dynamic in _iter(direction, "dynamics")):
            found.append((measure, direction))
    return found


def _set_direction_dynamic(direction, dynamic_name: str) -> None:
    for dynamics in _iter(direction, "dynamics"):
        for child in list(dynamics):
            dynamics.remove(child)
        ET.SubElement(dynamics, _qualified(dynamics, dynamic_name))
        return


def _add_dynamic_direction(measure, dynamic_name: str) -> object:
    direction = ET.Element(_qualified(measure, "direction"), {"placement": "below"})
    direction_type = ET.SubElement(direction, _qualified(measure, "direction-type"))
    dynamics = ET.SubElement(direction_type, _qualified(measure, "dynamics"))
    ET.SubElement(dynamics, _qualified(measure, dynamic_name))
    _insert_harmony_at_measure_start(measure, direction)
    return direction


def _restore_colored_pitch_cues(
    root,
    metadata: dict,
    target_part_by_measure: dict[str, str],
) -> dict:
    cues = metadata.get("colored_pitch_cues") or []
    report = {"colored_pitch_cues_found": len(cues), "colored_pitch_cues_restored": 0}
    parts = {part.attrib.get("id", ""): part for part in _children(root, "part")}
    measures_by_part = {
        part_id: {
            measure.attrib.get("number", ""): measure
            for measure in _children(part, "measure")
        }
        for part_id, part in parts.items()
    }
    pitch_classes = {
        "C": 0,
        "C#": 1,
        "Db": 1,
        "D": 2,
        "D#": 3,
        "Eb": 3,
        "E": 4,
        "F": 5,
        "F#": 6,
        "Gb": 6,
        "G": 7,
        "G#": 8,
        "Ab": 8,
        "A": 9,
        "A#": 10,
        "Bb": 10,
        "B": 11,
    }
    for cue in cues:
        number = str(cue.get("measure_number") or "")
        target_id = target_part_by_measure.get(number, "")
        measure = measures_by_part.get(target_id, {}).get(number)
        if measure is None:
            continue
        cue_tokens = (cue.get("text") or "").split()
        harmonies = _children(measure, "harmony")
        harmony_tokens = [_harmony_symbol(harmony) for harmony in harmonies]
        if (
            len(harmonies) == len(cue_tokens)
            and all(token in pitch_classes for token in cue_tokens)
            and all(token in pitch_classes for token in harmony_tokens)
            and len(
                {
                    (pitch_classes[harmony] - pitch_classes[source]) % 12
                    for source, harmony in zip(cue_tokens, harmony_tokens)
                }
            )
            == 1
        ):
            for harmony in harmonies:
                measure.remove(harmony)
        report["colored_pitch_cues_restored"] += _add_words_direction(
            measure,
            cue.get("text") or "",
            font_size="10",
            bold=True,
            default_y="46",
            color=cue.get("color") or "#ff0000",
        )
    return report


def _restore_prominent_pdf_dynamics(root, metadata: dict) -> dict:
    candidates = metadata.get("prominent_dynamics") or []
    report = {
        "prominent_dynamics_found": len(candidates),
        "prominent_dynamics_restored": 0,
        "conflicting_dynamics_removed": 0,
    }
    if not candidates:
        return report

    parts = _children(root, "part")
    spans = [_part_staff_span(part) for part in parts]
    total_span = max(1, sum(spans))
    part_measures = {
        id(part): {
            measure.attrib.get("number", ""): measure
            for measure in _children(part, "measure")
        }
        for part in parts
    }
    grouped: dict[tuple[int, tuple[str, ...]], list[dict]] = {}
    for candidate in candidates:
        slot = min(total_span - 1, max(0, int(float(candidate.get("relative_y") or 0) * total_span)))
        cumulative = 0
        part_index = len(parts) - 1
        for index, span in enumerate(spans):
            cumulative += span
            if slot < cumulative:
                part_index = index
                break
        system_measures = tuple(str(number) for number in candidate.get("system_measures") or [])
        grouped.setdefault((part_index, system_measures), []).append(candidate)

    for (part_index, system_numbers), source_dynamics in grouped.items():
        if not system_numbers or part_index >= len(parts):
            continue
        part = parts[part_index]
        measures = part_measures[id(part)]
        existing = [
            event
            for number in system_numbers
            if number in measures
            for event in _dynamic_directions(measures[number])
        ]
        used = set()
        for candidate in source_dynamics:
            target_number = str(candidate.get("measure_number") or "")
            target_measure = measures.get(target_number)
            if target_measure is None:
                continue
            target_index = system_numbers.index(target_number) if target_number in system_numbers else 0
            available = [event for event in existing if id(event[1]) not in used]
            if available:
                event = min(
                    available,
                    key=lambda value: abs(
                        system_numbers.index(value[0].attrib.get("number", ""))
                        - target_index
                    ),
                )
                old_measure, direction = event
                _set_direction_dynamic(direction, candidate.get("dynamic") or "ff")
                if old_measure is not target_measure:
                    old_measure.remove(direction)
                    _insert_harmony_at_measure_start(target_measure, direction)
                used.add(id(direction))
            else:
                direction = _add_dynamic_direction(
                    target_measure,
                    candidate.get("dynamic") or "ff",
                )
                used.add(id(direction))
            report["prominent_dynamics_restored"] += 1

        for measure, direction in existing:
            if id(direction) in used:
                continue
            measure.remove(direction)
            report["conflicting_dynamics_removed"] += 1
    return report


def _restore_pdf_performance_directions(root, metadata: dict) -> dict:
    candidates = metadata.get("performance_directions") or []
    report = {
        "performance_directions_found": len(candidates),
        "performance_directions_restored": 0,
        "performance_directions_repositioned": 0,
        "duplicate_performance_directions_removed": 0,
    }
    if not candidates:
        return report

    parts = _children(root, "part")
    if not parts:
        return report
    spans = [_part_staff_span(part) for part in parts]
    total_span = max(1, sum(spans))
    part_measures = [
        {
            measure.attrib.get("number", ""): measure
            for measure in _children(part, "measure")
        }
        for part in parts
    ]

    for candidate in candidates:
        text = re.sub(r"\s+", " ", candidate.get("text") or "").strip()
        target_number = str(candidate.get("measure_number") or "")
        system_numbers = tuple(str(number) for number in candidate.get("system_measures") or [])
        if not text or not target_number or not system_numbers:
            continue

        slot = min(
            total_span - 1,
            max(0, int(float(candidate.get("relative_y") or 0) * total_span)),
        )
        cumulative = 0
        target_part_index = len(parts) - 1
        for part_index, span in enumerate(spans):
            cumulative += span
            if slot < cumulative:
                target_part_index = part_index
                break
        target_measure = part_measures[target_part_index].get(target_number)
        if target_measure is None:
            continue

        matches = []
        for part_index, measures in enumerate(part_measures):
            for number in system_numbers:
                measure = measures.get(number)
                if measure is None:
                    continue
                for direction in _children(measure, "direction"):
                    existing = re.sub(
                        r"\s+",
                        " ",
                        _direction_words_value(direction),
                    ).strip()
                    if existing.lower() == text.lower():
                        measure_index = (
                            system_numbers.index(number)
                            if number in system_numbers
                            else 0
                        )
                        target_index = (
                            system_numbers.index(target_number)
                            if target_number in system_numbers
                            else 0
                        )
                        matches.append(
                            (
                                abs(measure_index - target_index),
                                0 if part_index == target_part_index else 1,
                                measure,
                                direction,
                            )
                        )

        if matches:
            matches.sort(key=lambda value: (value[0], value[1]))
            _distance, _part_penalty, old_measure, direction = matches[0]
            for words in _iter(direction, "words"):
                words.attrib["font-style"] = "italic"
            if old_measure is not target_measure:
                old_measure.remove(direction)
                _insert_harmony_at_measure_start(target_measure, direction)
                report["performance_directions_repositioned"] += 1
            for _distance, _part_penalty, duplicate_measure, duplicate in matches[1:]:
                duplicate_measure.remove(duplicate)
                report["duplicate_performance_directions_removed"] += 1
            continue

        report["performance_directions_restored"] += _add_words_direction(
            target_measure,
            text,
            font_size="8",
            italic=True,
            default_y="18",
        )
    return report


def _add_tempo_if_missing(root, part, tempo: int | None) -> int:
    if not tempo or any(True for _metronome in _iter(root, "metronome")):
        return 0
    measures = _children(part, "measure")
    if not measures:
        return 0
    measure = measures[0]
    direction = ET.Element(_qualified(measure, "direction"), {"placement": "above"})
    direction_type = ET.SubElement(direction, _qualified(measure, "direction-type"))
    metronome = ET.SubElement(direction_type, _qualified(measure, "metronome"))
    beat_unit = ET.SubElement(metronome, _qualified(measure, "beat-unit"))
    beat_unit.text = "quarter"
    per_minute = ET.SubElement(metronome, _qualified(measure, "per-minute"))
    per_minute.text = str(tempo)
    sound = ET.SubElement(direction, _qualified(measure, "sound"))
    sound.attrib["tempo"] = str(tempo)
    _insert_harmony_at_measure_start(measure, direction)
    return 1


def _add_rights_if_missing(root, rights: str) -> int:
    rights = re.sub(r"\s+", " ", rights or "").strip()
    if not rights:
        return 0
    for existing in _iter(root, "rights"):
        if rights.lower() in re.sub(r"\s+", " ", existing.text or "").lower():
            return 0
    identification = _child(root, "identification")
    if identification is None:
        identification = ET.Element(_qualified(root, "identification"))
        insert_index = next(
            (index for index, child in enumerate(list(root)) if _local_name(child.tag) == "part-list"),
            len(root),
        )
        root.insert(insert_index, identification)
    element = ET.SubElement(identification, _qualified(root, "rights"))
    element.text = rights
    return 1


def _recover_pdf_lyrics(root, pdf, page_systems) -> dict:
    report = {
        "lyric_signature": "",
        "lyric_lines_found": 0,
        "lyric_systems_restored": 0,
        "lyric_syllables_restored": 0,
        "existing_lyrics_replaced": 0,
        "lyric_tokens_merged": 0,
        "lyric_systems_skipped": 0,
    }
    signature, page_data = _infer_pdf_lyric_signature(pdf, page_systems)
    if signature is None:
        return report
    report["lyric_signature"] = f"{signature[0]} / {signature[1]:g} pt"

    part_names = {}
    for score_part in _iter(root, "score-part"):
        part_name = _child(score_part, "part-name")
        part_names[score_part.attrib.get("id", "")] = (
            (part_name.text or "") if part_name is not None else ""
        )
    all_parts = _children(root, "part")
    vocal_parts = [
        part
        for part in all_parts
        if re.search(
            r"(?i)(?:voice|vocal|choir|soprano|alto|tenor|bass)",
            part_names.get(part.attrib.get("id", ""), ""),
        )
        or any(True for _lyric in _iter(part, "lyric"))
    ]
    if not vocal_parts:
        return report
    measures_by_part = {
        id(part): {
            measure.attrib.get("number", ""): measure
            for measure in _children(part, "measure")
        }
        for part in vocal_parts
    }

    last_part = None
    last_default_y: dict[int, float] = {}
    for page_index, score_systems in enumerate(page_systems):
        if page_index >= len(page_data):
            break
        regions, words = page_data[page_index]
        if len(regions) != len(score_systems):
            report["lyric_systems_skipped"] += len(score_systems)
            continue
        page = pdf.pages[page_index]
        for system_index, measure_numbers in enumerate(score_systems):
            lines = _system_lyric_lines(words, signature, regions[system_index])
            substantial_lines = [
                line
                for line in lines
                if len(_tokenize_pdf_lyric_words(line)) >= 2
            ]
            if not substantial_lines:
                continue
            report["lyric_lines_found"] += len(substantial_lines)
            substantial_lines.sort(
                key=lambda line: len(_tokenize_pdf_lyric_words(line)),
                reverse=True,
            )
            if (
                len(substantial_lines) > 1
                and len(_tokenize_pdf_lyric_words(substantial_lines[1]))
                >= max(
                    3,
                    len(_tokenize_pdf_lyric_words(substantial_lines[0])) // 2,
                )
            ):
                report["lyric_systems_skipped"] += 1
                continue
            tokens = _tokenize_pdf_lyric_words(substantial_lines[0])

            lyric_counts = {}
            for part in vocal_parts:
                count = 0
                for measure_number in measure_numbers:
                    measure = measures_by_part[id(part)].get(measure_number)
                    if measure is None:
                        continue
                    for lyric in _iter(measure, "lyric"):
                        if lyric.attrib.get("name") in RECOVERED_CHORD_LYRIC_NAMES:
                            continue
                        if _lyric_text(lyric) and not _is_preserved_lyric_direction(lyric):
                            count += 1
                lyric_counts[id(part)] = count
            best_count = max(lyric_counts.values(), default=0)
            target_part = None
            if best_count:
                best_part_id = max(lyric_counts, key=lyric_counts.get)
                target_part = next(part for part in vocal_parts if id(part) == best_part_id)
            elif last_part is not None:
                target_part = last_part
            else:
                active_counts = {}
                for part in vocal_parts:
                    active_counts[id(part)] = sum(
                        1
                        for measure_number in measure_numbers
                        for pitch in _iter(
                            measures_by_part[id(part)].get(measure_number, ET.Element("measure")),
                            "pitch",
                        )
                    )
                best_active_count = max(active_counts.values(), default=0)
                if best_active_count:
                    best_part_id = max(active_counts, key=active_counts.get)
                    target_part = next(part for part in vocal_parts if id(part) == best_part_id)
            if target_part is None:
                report["lyric_systems_skipped"] += 1
                continue
            last_part = target_part

            voice_counts = Counter()
            default_y_values = []
            for measure_number in measure_numbers:
                measure = measures_by_part[id(target_part)].get(measure_number)
                if measure is None:
                    continue
                for note in _children(measure, "note"):
                    eligible = [
                        lyric
                        for lyric in _children(note, "lyric")
                        if lyric.attrib.get("name") not in RECOVERED_CHORD_LYRIC_NAMES
                        and not _is_preserved_lyric_direction(lyric)
                    ]
                    if not eligible:
                        continue
                    voice = _child(note, "voice")
                    voice_counts[
                        (voice.text or "1") if voice is not None else "1"
                    ] += len(eligible)
                    for lyric in eligible:
                        try:
                            default_y_values.append(float(lyric.attrib.get("default-y", "")))
                        except ValueError:
                            pass
            target_voice = voice_counts.most_common(1)[0][0] if voice_counts else "1"
            default_y = (
                median(default_y_values)
                if default_y_values
                else last_default_y.get(id(target_part), -95.0)
            )
            last_default_y[id(target_part)] = default_y

            boundaries = _measure_boundaries(
                page,
                regions[system_index],
                len(measure_numbers),
            )
            if len(boundaries) != len(measure_numbers) + 1:
                report["lyric_systems_skipped"] += 1
                continue

            note_candidates = []
            for measure_index, measure_number in enumerate(measure_numbers):
                measure = measures_by_part[id(target_part)].get(measure_number)
                if measure is None:
                    continue
                left, right = boundaries[measure_index : measure_index + 2]
                try:
                    measure_width = max(1.0, float(measure.attrib.get("width", "1")))
                except ValueError:
                    measure_width = 1.0
                pitched_notes = []
                for note in _children(measure, "note"):
                    voice = _child(note, "voice")
                    voice_value = (voice.text or "1") if voice is not None else "1"
                    if (
                        _child(note, "pitch") is not None
                        and _child(note, "chord") is None
                        and _child(note, "grace") is None
                        and voice_value == target_voice
                    ):
                        pitched_notes.append(note)
                for note_index, note in enumerate(pitched_notes):
                    try:
                        default_x = float(note.attrib.get("default-x", ""))
                    except ValueError:
                        default_x = (
                            measure_width
                            * (note_index + 1)
                            / (len(pitched_notes) + 1)
                        )
                    old_text = next(
                        (
                            _lyric_text(lyric)
                            for lyric in _children(note, "lyric")
                            if lyric.attrib.get("name") not in RECOVERED_CHORD_LYRIC_NAMES
                            and not _is_preserved_lyric_direction(lyric)
                            and _lyric_text(lyric)
                        ),
                        "",
                    )
                    note_candidates.append(
                        {
                            "note": note,
                            "x0": left + default_x / measure_width * (right - left),
                            "old_text": old_text,
                        }
                    )

            if not note_candidates:
                report["lyric_systems_skipped"] += 1
                continue
            original_token_count = len(tokens)
            report["lyric_tokens_merged"] += _merge_excess_lyric_tokens(
                tokens,
                len(note_candidates),
            )
            assignments = _assign_lyric_tokens_to_notes(tokens, note_candidates)
            if len(assignments) != len(tokens):
                report["lyric_systems_skipped"] += 1
                continue

            for part in vocal_parts:
                for measure_number in measure_numbers:
                    measure = measures_by_part[id(part)].get(measure_number)
                    if measure is None:
                        continue
                    for note in _children(measure, "note"):
                        for lyric in list(_children(note, "lyric")):
                            if (
                                lyric.attrib.get("name") in RECOVERED_CHORD_LYRIC_NAMES
                                or not _is_preserved_lyric_direction(lyric)
                            ):
                                note.remove(lyric)
                                report["existing_lyrics_replaced"] += 1

            for token, assignment in zip(tokens, assignments):
                note = note_candidates[assignment]["note"]
                lyric = ET.SubElement(
                    note,
                    _qualified(note, "lyric"),
                    {
                        "number": "1",
                        "placement": "below",
                        "default-y": f"{default_y:g}",
                    },
                )
                syllabic = ET.SubElement(lyric, _qualified(note, "syllabic"))
                syllabic.text = token["syllabic"]
                text_element = ET.SubElement(lyric, _qualified(note, "text"))
                text_element.text = token["text"]
            report["lyric_systems_restored"] += 1
            report["lyric_syllables_restored"] += original_token_count
    return report


def recover_pdf_text_layer(root, pdf_path: str | Path) -> dict:
    report = {
        "available": False,
        "pdf_pages_checked": 0,
        "score_systems_checked": 0,
        "chord_symbols_found": 0,
        "chord_symbols_restored": 0,
        "existing_chord_symbols_replaced": 0,
        "covered_chord_words_ignored": 0,
        "chord_suffix_fragments_merged": 0,
        "stacked_chords_merged": 0,
        "confidence": 0.0,
        "target_part_id": "",
        "target_part_by_measure": {},
        "tempo_recovered": 0,
        "rights_recovered": 0,
        "lyric_signature": "",
        "lyric_lines_found": 0,
        "lyric_systems_restored": 0,
        "lyric_syllables_restored": 0,
        "existing_lyrics_replaced": 0,
        "lyric_tokens_merged": 0,
        "lyric_systems_skipped": 0,
        "mixed_score_boundaries_found": 0,
        "mixed_score_titles_restored": 0,
        "prominent_directions_restored": 0,
        "section_labels_found": 0,
        "section_labels_restored": 0,
        "colored_pitch_cues_found": 0,
        "colored_pitch_cues_restored": 0,
        "prominent_dynamics_found": 0,
        "prominent_dynamics_restored": 0,
        "conflicting_dynamics_removed": 0,
        "performance_directions_found": 0,
        "performance_directions_restored": 0,
        "performance_directions_repositioned": 0,
        "duplicate_performance_directions_removed": 0,
        "measure_number_resets": [],
        "section_boundary_measures": [],
        "metadata": {},
        "warnings": [],
        "errors": [],
    }
    try:
        import pdfplumber
    except ImportError:
        report["errors"].append(
            "The optional pdfplumber dependency is not installed; embedded PDF text recovery was skipped."
        )
        return report

    score_pages = _score_page_systems(root)
    if not score_pages:
        return report
    report["score_systems_checked"] = sum(len(page) for page in score_pages)

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            report["available"] = any(bool(page.chars) for page in pdf.pages)
            report["pdf_pages_checked"] = len(pdf.pages)
            chord_pages, metadata, extraction_stats = _extract_candidate_words(pdf, score_pages)
            report["metadata"] = metadata
            report["measure_number_resets"] = metadata.get("measure_number_resets") or []
            report.update(extraction_stats)

            if not report["available"]:
                return report
            if len(chord_pages) != len(score_pages):
                report["errors"].append("PDF page count did not match the recognized score page map.")
                return report

            existing = _existing_chord_symbols(root)
            target_part_by_measure = {}
            selected_part_ids = []
            for score_systems in score_pages:
                for measure_numbers in score_systems:
                    target_part = _select_chord_part_for_measures(root, measure_numbers)
                    if target_part is None:
                        continue
                    target_id = target_part.attrib.get("id", "")
                    if target_id and target_id not in selected_part_ids:
                        selected_part_ids.append(target_id)
                    for measure_number in measure_numbers:
                        target_part_by_measure[measure_number] = target_id
            report["target_part_by_measure"] = target_part_by_measure
            report["target_part_id"] = selected_part_ids[0] if selected_part_ids else ""

            report.update(_recover_pdf_lyrics(root, pdf, score_pages))
            found = sum(len(system) for page in chord_pages for system in page)
            report["chord_symbols_found"] = found
            found_counts = Counter(
                candidate["text"] for page in chord_pages for system in page for candidate in system
            )
            existing_counts = Counter(existing)
            matched = sum(min(count, found_counts[symbol]) for symbol, count in existing_counts.items())
            confidence = matched / len(existing) if existing else 1.0
            report["confidence"] = round(confidence, 4)
            chord_recovery_allowed = bool(found and target_part_by_measure)
            if existing and confidence < 0.8:
                found_to_existing_ratio = found / max(1, len(existing))
                pdf_track_is_plausible = (
                    found >= max(3, report["score_systems_checked"])
                    and 0.5 <= found_to_existing_ratio <= 2.0
                )
                if pdf_track_is_plausible:
                    report["warnings"].append(
                        "Embedded PDF chord text replaced a conflicting recognized chord track."
                    )
                else:
                    report["errors"].append(
                        "Embedded PDF chord text did not agree closely enough with the recognized chord track."
                    )
                    chord_recovery_allowed = False

            added = 0
            pending: dict[int, list[tuple[int, float, str, object]]] = {}
            if chord_recovery_allowed:
                parts = {
                    part.attrib.get("id", ""): part
                    for part in _children(root, "part")
                }
                part_measures = {
                    part_id: {
                        measure.attrib.get("number", ""): measure
                        for measure in _children(part, "measure")
                    }
                    for part_id, part in parts.items()
                }
                durations = {
                    part_id: _measure_durations(part)
                    for part_id, part in parts.items()
                }
                for page_index, (score_systems, chord_systems) in enumerate(zip(score_pages, chord_pages)):
                    page = pdf.pages[page_index]
                    regions = _detect_system_regions(page, len(score_systems))
                    if len(regions) != len(score_systems):
                        report["errors"].append(f"Could not map systems on PDF page {page_index + 1}.")
                        chord_recovery_allowed = False
                        break
                    for system_index, (measure_numbers, candidates) in enumerate(zip(score_systems, chord_systems)):
                        boundaries = _measure_boundaries(page, regions[system_index], len(measure_numbers))
                        if len(boundaries) != len(measure_numbers) + 1:
                            continue
                        for candidate in candidates:
                            measure_index = max(
                                0,
                                min(
                                    len(measure_numbers) - 1,
                                    bisect_right(boundaries, candidate["x0"]) - 1,
                                ),
                            )
                            measure_number = measure_numbers[measure_index]
                            target_id = target_part_by_measure.get(measure_number, "")
                            measure = part_measures.get(target_id, {}).get(measure_number)
                            if measure is None:
                                continue
                            left = boundaries[measure_index]
                            right = boundaries[measure_index + 1]
                            fraction = max(
                                0.0,
                                min(
                                    0.999,
                                    (candidate["x0"] - left) / max(1.0, right - left),
                                ),
                            )
                            duration, divisions = durations.get(target_id, {}).get(id(measure), (4, 1))
                            quantum = max(1, divisions // 2)
                            offset = int(round((fraction * duration) / quantum) * quantum)
                            offset = max(0, min(duration - 1, offset))
                            pending.setdefault(id(measure), []).append(
                                (offset, candidate["x0"], candidate["text"], measure)
                            )

            if chord_recovery_allowed and pending:
                report["existing_chord_symbols_replaced"] = _remove_existing_score_chords(root)
                for events in pending.values():
                    for offset, _x, chord, measure in sorted(events, key=lambda event: (event[0], event[1])):
                        harmony = _make_harmony(measure, chord, offset)
                        if harmony is not None:
                            _insert_harmony_at_measure_start(measure, harmony)
                            added += 1
                report["chord_symbols_restored"] = added

            report.update(
                _restore_sparse_pdf_whole_note_measures(
                    root,
                    pdf,
                    score_pages,
                )
            )
            boundary_report = _restore_score_boundaries_and_prominent_directions(
                root,
                metadata,
                score_pages,
                target_part_by_measure,
            )
            report.update(boundary_report)
            report.update(
                _restore_pdf_section_labels(
                    root,
                    metadata,
                    target_part_by_measure,
                )
            )
            caption_report = _restore_pdf_section_captions(
                root,
                metadata,
                target_part_by_measure,
            )
            report.update(caption_report)
            report.update(
                _restore_colored_pitch_cues(
                    root,
                    metadata,
                    target_part_by_measure,
                )
            )
            report.update(_restore_prominent_pdf_dynamics(root, metadata))
            report.update(_restore_pdf_performance_directions(root, metadata))

            tempo_part = next(
                (
                    part
                    for part in _children(root, "part")
                    if part.attrib.get("id") == report["target_part_id"]
                ),
                None,
            )
            if tempo_part is not None:
                report["tempo_recovered"] = _add_tempo_if_missing(
                    root, tempo_part, metadata.get("tempo")
                )
            report["rights_recovered"] = _add_rights_if_missing(
                root, metadata.get("rights") or ""
            )
    except Exception as exc:
        report["errors"].append(f"Embedded PDF text recovery failed: {exc}")
    return report


__all__ = [
    "PdfChord",
    "SystemRegion",
    "recover_pdf_text_layer",
]
