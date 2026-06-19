from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.modules["python.transposer"] = sys.modules[__name__]

VALID_SUFFIXES = {".musicxml", ".xml", ".mxl"}


class TranspositionError(Exception):
    """Raised when a MusicXML file cannot be transposed."""


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
    converter, interval, key = _require_music21()

    source_path = validate_musicxml_path(input_path, must_exist=True)
    destination_path = validate_musicxml_path(output_path, must_exist=False)

    try:
        parts = target_key_name.strip().split()
        tonic = parts[0]
        mode = parts[1] if len(parts) > 1 else "major"
        target_key = key.Key(tonic, mode)
    except Exception as exc:
        raise TranspositionError(f"Unsupported target key: {target_key_name}") from exc

    try:
        score = converter.parse(str(source_path))
        source_key = score.analyze("key")
        transposition_interval = interval.Interval(source_key.tonic, target_key.tonic)
        shifted_score = score.transpose(transposition_interval)
        _set_initial_key_signatures(shifted_score, target_key)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shifted_score.write("musicxml", fp=str(destination_path))
    except TranspositionError:
        raise
    except Exception as exc:
        raise TranspositionError(f"Could not transpose this MusicXML file. {exc}") from exc

    return destination_path


def _set_initial_key_signatures(score, target_key):
    from music21 import key

    for part in score.parts:
        first_measure = part.measure(1)
        if first_measure is None:
            continue

        key_signatures = first_measure.getElementsByClass("KeySignature")
        for existing in list(key_signatures):
            first_measure.remove(existing)

        first_measure.insert(0, key.KeySignature(target_key.sharps))


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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        from python.pipeline import run_pipeline

        output_path = run_pipeline(
            args.input,
            args.output,
            args.target_key,
            args.output_format,
            audiveris_path=args.audiveris_path,
            musescore_path=args.musescore_path,
            temp_dir=args.temp_dir,
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
