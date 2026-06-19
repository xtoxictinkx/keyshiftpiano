from __future__ import annotations

from pathlib import Path
import subprocess
import time

from python.transposer import TranspositionError

EXPORT_TIMEOUT_SECONDS = 90
OUTPUT_STABLE_SECONDS = 2


def export_musicxml_to_pdf(input_path: str | Path, output_path: str | Path, musescore_path: str | Path | None) -> Path:
    if not musescore_path:
        raise TranspositionError("PDF export requires MuseScore.")

    attempted_path = str(musescore_path).strip().strip("\"'")
    executable = Path(attempted_path).expanduser()
    if not executable.is_file():
        raise TranspositionError(f"PDF export requires MuseScore. Attempted path: {attempted_path}")

    source_path = Path(input_path)
    destination_path = Path(output_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    if not _looks_like_plain_musicxml(source_path):
        raise TranspositionError(
            f"PDF export could not continue because the transposed MusicXML file is empty or invalid: {source_path}"
        )

    commands = [
        [str(executable), "-n", "-o", str(destination_path), str(source_path)],
        [str(executable), "-o", str(destination_path), str(source_path)],
        [str(executable), str(source_path), "-o", str(destination_path)],
    ]

    last_result = None
    try:
        for command in commands:
            result = _run_musescore_export(command, destination_path)
            last_result = result
            if destination_path.is_file():
                return destination_path
            if result.returncode == 0:
                break
            if "-n" in command and _is_unknown_option_error(result, "-n"):
                continue
            if _is_input_order_error(result):
                continue
            break
    except OSError as exc:
        raise TranspositionError(f"PDF export failed. MuseScore could not be started at: {executable}") from exc

    if last_result and last_result.returncode != 0:
        details = (last_result.stderr or last_result.stdout or "").strip()
        suffix = f" {details}" if details else ""
        raise TranspositionError(f"PDF export failed using MuseScore at: {executable}.{suffix}")

    if not destination_path.is_file():
        raise TranspositionError("PDF export failed. MuseScore did not create a PDF file.")

    return destination_path


def _run_musescore_export(command: list[str], destination_path: Path) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started_at = time.monotonic()
    stable_since = None
    last_size = -1

    while True:
        returncode = process.poll()
        if destination_path.is_file():
            current_size = destination_path.stat().st_size
            if current_size == last_size and current_size > 0:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= OUTPUT_STABLE_SECONDS:
                    _stop_process(process)
                    stdout, stderr = process.communicate(timeout=5)
                    return subprocess.CompletedProcess(command, 0, stdout, stderr)
            else:
                stable_since = None
                last_size = current_size

        if returncode is not None:
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        if time.monotonic() - started_at > EXPORT_TIMEOUT_SECONDS:
            _stop_process(process)
            stdout, stderr = process.communicate(timeout=5)
            return subprocess.CompletedProcess(
                command,
                124,
                stdout,
                stderr
                or (
                    f"PDF export timed out after {EXPORT_TIMEOUT_SECONDS} seconds. "
                    "MuseScore may be waiting for input or may not support this command-line export form."
                ),
            )

        time.sleep(0.5)


def _is_unknown_option_error(result: subprocess.CompletedProcess, option: str) -> bool:
    output = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    normalized_option = option.lower().lstrip("-")
    return "unknown option" in output and (
        option.lower() in output
        or f"'{normalized_option}'" in output
        or f'"{normalized_option}"' in output
    )


def _is_input_order_error(result: subprocess.CompletedProcess) -> bool:
    output = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return "no such file" in output or "cannot open" in output or "failed to open" in output


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _looks_like_plain_musicxml(file_path: Path) -> bool:
    try:
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return False

        with file_path.open("rb") as handle:
            prefix = handle.read(256).lstrip()

        return prefix.startswith(b"<?xml") or prefix.startswith(b"<score-")
    except OSError:
        return False
