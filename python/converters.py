from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

from python.pdf_conversion import convert_pdf_to_musicxml
from python.transposer import TranspositionError, validate_musicxml_path


MUSICXML_SUFFIXES = {".musicxml", ".xml", ".mxl"}


@dataclass(frozen=True)
class ConversionResult:
    source_path: Path
    musicxml_path: Path
    input_format: str
    engine: str


def convert_source_to_musicxml(
    input_path: str | Path,
    working_dir: str | Path,
    *,
    audiveris_path: str | Path | None = None,
) -> ConversionResult:
    source_path = Path(input_path).expanduser()
    suffix = source_path.suffix.lower()

    if suffix in MUSICXML_SUFFIXES:
        musicxml_path = validate_musicxml_path(source_path, must_exist=True)
        return ConversionResult(
            source_path=source_path,
            musicxml_path=musicxml_path,
            input_format="musicxml",
            engine="none",
        )

    if suffix == ".pdf":
        musicxml_path = convert_pdf_to_musicxml(source_path, working_dir, audiveris_path)
        validate_musicxml_path(musicxml_path, must_exist=True)
        return ConversionResult(
            source_path=source_path,
            musicxml_path=musicxml_path,
            input_format="pdf",
            engine="audiveris",
        )

    raise TranspositionError("Input files must end in .musicxml, .xml, .mxl, or .pdf.")


def expand_mxl_to_musicxml(musicxml_path: str | Path, working_dir: str | Path) -> Path:
    source_path = validate_musicxml_path(musicxml_path, must_exist=True)
    if source_path.suffix.lower() != ".mxl":
        return source_path

    try:
        with zipfile.ZipFile(source_path, "r") as archive:
            container_root = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile_path = ""
            for element in container_root.iter():
                if element.tag.rsplit("}", 1)[-1] == "rootfile" and element.attrib.get("full-path"):
                    rootfile_path = element.attrib["full-path"]
                    break
            if not rootfile_path:
                raise TranspositionError("Compressed MusicXML does not identify its score document.")
            score_xml = archive.read(rootfile_path)
    except (KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise TranspositionError("Audiveris created a compressed MusicXML file that could not be opened.") from exc

    destination = Path(working_dir) / "audiveris-expanded.musicxml"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(score_xml)
    return destination
