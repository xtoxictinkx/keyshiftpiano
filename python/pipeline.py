from __future__ import annotations

from pathlib import Path
import tempfile

from python.pdf_conversion import convert_pdf_to_musicxml
from python.pdf_export import export_musicxml_to_pdf
from python.transposer import TranspositionError, transpose_to_key, validate_musicxml_path

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
) -> Path:
    source_path = validate_input_path(input_path, must_exist=True)
    normalized_format = normalize_output_format(output_format)
    destination_path = validate_output_path(output_path, normalized_format)

    temp_root = _get_temp_root(temp_dir)

    with tempfile.TemporaryDirectory(prefix="pipeline-", dir=temp_root) as tmpdir:
        working_dir = Path(tmpdir)
        musicxml_source = source_path

        if source_path.suffix.lower() == ".pdf":
            musicxml_source = convert_pdf_to_musicxml(source_path, working_dir, audiveris_path)
            validate_musicxml_path(musicxml_source, must_exist=True)

        if normalized_format == "musicxml":
            return transpose_to_key(musicxml_source, destination_path, target_key_name)

        transposed_musicxml = working_dir / "transposed.musicxml"
        transpose_to_key(musicxml_source, transposed_musicxml, target_key_name)
        return export_musicxml_to_pdf(transposed_musicxml, destination_path, musescore_path)


def _get_temp_root(temp_dir: str | Path | None) -> Path:
    root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.gettempdir()) / "Key Shift Piano"
    root.mkdir(parents=True, exist_ok=True)
    return root
