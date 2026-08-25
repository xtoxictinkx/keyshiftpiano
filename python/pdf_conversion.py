from __future__ import annotations

from pathlib import Path
import subprocess

from python.transposer import TranspositionError


MUSICXML_SUFFIXES = {".musicxml", ".xml", ".mxl"}
MISSING_AUDIVERIS_MESSAGE = "PDF import requires the Audiveris OMR engine. Please configure it in Settings."
STAFF_NOTATION_REQUIRED_MESSAGE = (
    "Audiveris could not find readable five-line music staffs in this PDF. "
    "If this is a chord-and-lyrics chart, it does not appear to contain a selectable text layer. "
    "New Key Scores currently reads digital/text-based chord-chart PDFs, but image-only chord-chart scans "
    "still need local OCR support. Please use a text-based PDF, MusicXML, or a PDF with printed staff notation."
)


def _audiveris_failure_message(executable: Path, details: str) -> str:
    normalized = details.lower()
    failed_during_scale_detection = (
        "flagged as invalid" in normalized
        and ("| scale" in normalized or "created scores: []" in normalized)
    )
    if failed_during_scale_detection:
        return STAFF_NOTATION_REQUIRED_MESSAGE

    suffix = f" {details}" if details else ""
    return f"PDF conversion failed using Audiveris at: {executable}.{suffix}"


def convert_pdf_to_musicxml(input_path: str | Path, working_dir: str | Path, audiveris_path: str | Path | None) -> Path:
    if not audiveris_path:
        raise TranspositionError(MISSING_AUDIVERIS_MESSAGE)

    attempted_path = str(audiveris_path).strip().strip("\"'")
    executable = Path(attempted_path).expanduser()
    if not executable.is_file():
        raise TranspositionError(f"{MISSING_AUDIVERIS_MESSAGE} Attempted path: {attempted_path}")

    source_path = Path(input_path)
    output_dir = Path(working_dir) / "audiveris-output"
    output_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(executable),
        "-batch",
        "-export",
        "-output",
        str(output_dir),
        str(source_path),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise TranspositionError(f"PDF conversion failed. Audiveris could not be started at: {executable}") from exc

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise TranspositionError(_audiveris_failure_message(executable, details))

    converted_files = [
        path for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in MUSICXML_SUFFIXES
    ]
    if not converted_files:
        raise TranspositionError("PDF conversion failed. Audiveris did not create a MusicXML file.")

    return sorted(converted_files, key=lambda path: path.stat().st_mtime, reverse=True)[0]
