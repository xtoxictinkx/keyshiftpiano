from __future__ import annotations

from pathlib import Path
import subprocess

from python.transposer import TranspositionError


def export_musicxml_to_pdf(input_path: str | Path, output_path: str | Path, musescore_path: str | Path | None) -> Path:
    if not musescore_path:
        raise TranspositionError("PDF export requires MuseScore.")

    executable = Path(musescore_path).expanduser()
    if not executable.is_file():
        raise TranspositionError("PDF export requires MuseScore.")

    source_path = Path(input_path)
    destination_path = Path(output_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(executable),
        "-o",
        str(destination_path),
        str(source_path),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise TranspositionError("PDF export failed. MuseScore could not be started.") from exc

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        suffix = f" {details}" if details else ""
        raise TranspositionError(f"PDF export failed. MuseScore could not export this file.{suffix}")

    if not destination_path.is_file():
        raise TranspositionError("PDF export failed. MuseScore did not create a PDF file.")

    return destination_path
