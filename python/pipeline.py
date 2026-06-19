from __future__ import annotations

from pathlib import Path
import tempfile

from python.pdf_conversion import convert_pdf_to_musicxml
from python.pdf_export import export_musicxml_to_pdf
from python.transposer import (
    TranspositionError,
    detect_key_name,
    get_last_transposition_report,
    transpose_to_key,
    transpose_to_key_direct,
    validate_musicxml_path,
)

INPUT_SUFFIXES = {".musicxml", ".xml", ".pdf"}
OUTPUT_FORMATS = {"musicxml", "pdf"}


def validate_input_path(file_path: str | Path, *, must_exist: bool) -> Path:
    path = Path(file_path).expanduser()
    if path.suffix.lower() not in INPUT_SUFFIXES:
        raise TranspositionError("Input files must end in .musicxml, .xml, or .pdf.")

    if must_exist and not path.is_file():
        raise TranspositionError(f"Input file was not found: {path}")

    return path


def validate_output_path(file_path: str | Path, output_format: str) -> Path:
    path = Path(file_path).expanduser()
    normalized_format = normalize_output_format(output_format)
    expected_suffixes = {".musicxml", ".xml"} if normalized_format == "musicxml" else {".pdf"}

    if path.suffix.lower() not in expected_suffixes:
        expected = ".musicxml or .xml" if normalized_format == "musicxml" else ".pdf"
        raise TranspositionError(f"Output file must end in {expected}.")

    return path


def normalize_output_format(output_format: str) -> str:
    normalized = (output_format or "").strip().lower()
    if normalized not in OUTPUT_FORMATS:
        raise TranspositionError("Output format must be MusicXML or PDF.")

    return normalized


def run_pipeline(
    input_path: str | Path,
    output_path: str | Path,
    target_key_name: str,
    output_format: str = "musicxml",
    *,
    audiveris_path: str | Path | None = None,
    musescore_path: str | Path | None = None,
    temp_dir: str | Path | None = None,
    progress=None,
) -> Path:
    report = progress or (lambda _name, _detail="": None)
    report("Loading file")
    source_path = validate_input_path(input_path, must_exist=True)
    normalized_format = normalize_output_format(output_format)
    destination_path = validate_output_path(output_path, normalized_format)

    temp_root = _get_temp_root(temp_dir)

    with tempfile.TemporaryDirectory(prefix="pipeline-", dir=temp_root) as tmpdir:
        working_dir = Path(tmpdir)
        musicxml_source = source_path

        if source_path.suffix.lower() == ".pdf":
            report("Converting PDF to MusicXML")
            musicxml_source = convert_pdf_to_musicxml(source_path, working_dir, audiveris_path)
            validate_musicxml_path(musicxml_source, must_exist=True)

        report("Detecting key")
        original_key = detect_key_name(musicxml_source)
        report("Detecting key", f"Original key: {original_key}")

        report("Transposing")
        if normalized_format == "musicxml":
            result = transpose_to_key(musicxml_source, destination_path, target_key_name)
            _report_validation(progress, original_key, target_key_name)
            _run_musescore_validation(result, musescore_path, working_dir, progress)
            report("Complete", f"Saved {result}")
            return result

        transposed_musicxml = destination_path.with_suffix(".musicxml")
        transpose_to_key(musicxml_source, transposed_musicxml, target_key_name)
        if not _looks_like_musicxml(transposed_musicxml):
            transpose_to_key_direct(musicxml_source, transposed_musicxml, target_key_name)
        if not _looks_like_musicxml(transposed_musicxml):
            raise TranspositionError("PDF export could not continue because the transposed MusicXML file was not valid.")
        _report_validation(progress, original_key, target_key_name)
        report("Exporting output")
        try:
            result = export_musicxml_to_pdf(transposed_musicxml, destination_path, musescore_path)
            report("Complete", f"Saved {result}")
            return result
        except TranspositionError as exc:
            report(
                "Complete",
                f"Transposed MusicXML saved to {transposed_musicxml}. PDF export did not complete: {exc}",
            )
            return transposed_musicxml


def _get_temp_root(temp_dir: str | Path | None) -> Path:
    root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.gettempdir()) / "Key Shift Piano"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _looks_like_musicxml(file_path: Path) -> bool:
    try:
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return False

        with file_path.open("rb") as handle:
            prefix = handle.read(256).lstrip()

        return prefix.startswith(b"<?xml") or prefix.startswith(b"<score-")
    except OSError:
        return False


def _report_validation(progress, original_key: str, target_key_name: str) -> None:
    if progress is None:
        return

    transposition_report = get_last_transposition_report() or {}
    detail = (
        f"source key: {transposition_report.get('source_key') or original_key}; "
        f"target key: {transposition_report.get('target_key') or target_key_name}; "
        f"interval: {transposition_report.get('interval', 'unknown')}; "
        f"notes: {transposition_report.get('note_transposition_count', 'unknown')}; "
        f"key signatures: {transposition_report.get('key_signature_update_count', 'unknown')}; "
        f"chords/harmony: {transposition_report.get('harmony_chord_update_count', 'unknown')}; "
        f"visible key labels: {transposition_report.get('visible_key_label_update_count', 'unknown')}"
    )
    progress("Validation report", detail)
    output_validation = transposition_report.get("output_validation") or {}
    if output_validation:
        errors = output_validation.get("errors") or []
        compatibility = output_validation.get("musescore_compatibility_check", "failed")
        measure_validation = output_validation.get("measure_validation") or {}
        validation_detail = (
            f"XML valid: {'Yes' if output_validation.get('xml_valid') else 'No'}; "
            f"Harmony elements checked: {output_validation.get('harmony_elements_checked', 0)}; "
            f"Metadata updated: {output_validation.get('metadata_updated', 0)}; "
            f"Measures checked: {measure_validation.get('total_measures_checked', 0)}; "
            f"Incomplete measures found: {measure_validation.get('incomplete_measures_found', 0)}; "
            f"Measures repaired: {measure_validation.get('measures_repaired', 0)}; "
            f"Skipped as intentional: {measure_validation.get('measures_skipped_as_intentional', 0)}; "
            f"MuseScore compatibility check: {compatibility}"
        )
        if errors:
            validation_detail = f"{validation_detail}; issues: {' | '.join(errors[:3])}"
        progress("Validate Output", validation_detail)


def _run_musescore_validation(
    musicxml_path: Path,
    musescore_path: str | Path | None,
    working_dir: Path,
    progress,
) -> None:
    if progress is None or not musescore_path:
        return

    validation_pdf = working_dir / "musescore-validation.pdf"
    try:
        export_musicxml_to_pdf(musicxml_path, validation_pdf, musescore_path)
        progress("Validate Output", "MuseScore silent validation/export test: passed")
    except TranspositionError as exc:
        progress("Validate Output", f"MuseScore silent validation/export test: failed; {exc}")
