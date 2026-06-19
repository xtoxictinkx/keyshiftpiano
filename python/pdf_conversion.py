from __future__ import annotations

from pathlib import Path
import subprocess

from python.transposer import TranspositionError


MUSICXML_SUFFIXES = {".musicxml", ".xml", ".mxl"}
MISSING_AUDIVERIS_MESSAGE = "PDF import requires the Audiveris OMR engine. Please configure it in Settings."


def convert_pdf_to_musicxml(input_path: str | Path, working_dir: str | Path, audiveris_path: str | Path | None) -> Path:
    if not audiveris_path:
        raise TranspositionError(MISSING_AUDIVERIS_MESSAGE)

    executable = Path(audiveris_path).expanduser()
    if not executable.is_file():
        raise TranspositionError(MISSING_AUDIVERIS_MESSAGE)

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
        raise TranspositionError("PDF conversion failed. Audiveris could not be started.") from exc

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        suffix = f" {details}" if details else ""
        raise TranspositionError(f"PDF conversion failed. Audiveris could not convert this file.{suffix}")

    converted_files = [
        path for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in MUSICXML_SUFFIXES
    ]
    if not converted_files:
        raise TranspositionError("PDF conversion failed. Audiveris did not create a MusicXML file.")

    return sorted(converted_files, key=lambda path: path.stat().st_mtime, reverse=True)[0]
