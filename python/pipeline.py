from __future__ import annotations

from pathlib import Path
import tempfile

from python.converters import convert_source_to_musicxml, expand_mxl_to_musicxml
from python.transposer import (
    TranspositionError,
    clean_imported_musicxml_layout,
    detect_key_name,
    get_last_transposition_report,
    transpose_to_key,
)

INPUT_SUFFIXES = {".musicxml", ".xml", ".mxl", ".pdf"}
OUTPUT_FORMATS = {"musicxml", "pdf"}


def validate_input_path(file_path: str | Path, *, must_exist: bool) -> Path:
    path = Path(file_path).expanduser()
    if path.suffix.lower() not in INPUT_SUFFIXES:
        raise TranspositionError("Input files must end in .musicxml, .xml, .mxl, or .pdf.")

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
    temp_dir: str | Path | None = None,
    clean_export_layout: bool = True,
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
        if source_path.suffix.lower() == ".pdf":
            report("Converting PDF to MusicXML")
        conversion = convert_source_to_musicxml(source_path, working_dir, audiveris_path=audiveris_path)
        musicxml_source = conversion.musicxml_path
        if source_path.suffix.lower() == ".pdf":
            musicxml_source = expand_mxl_to_musicxml(musicxml_source, working_dir)
        import_cleanup_report = {}

        if source_path.suffix.lower() == ".pdf":
            report(
                "Cleaning export layout"
                if clean_export_layout
                else "Recovering PDF words and chords"
            )
            import_cleanup_report = clean_imported_musicxml_layout(
                musicxml_source,
                source_pdf_path=source_path,
                rebuild_title_block=clean_export_layout,
                apply_layout_cleanup=clean_export_layout,
            )

        report("Detecting key")
        original_key = detect_key_name(musicxml_source)
        report("Detecting key", f"Original key: {original_key}")

        report("Transposing")
        if normalized_format == "pdf":
            raise TranspositionError(
                "The Python engine produces MusicXML. PDF export is handled by the desktop app through MuseScore."
            )

        result = transpose_to_key(musicxml_source, destination_path, target_key_name)
        _report_validation(progress, original_key, target_key_name, import_cleanup_report=import_cleanup_report)
        report("Complete", f"Saved {result}")
        return result


def _get_temp_root(temp_dir: str | Path | None) -> Path:
    root = Path(temp_dir).expanduser() if temp_dir else Path(tempfile.gettempdir()) / "New Key Scores"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _report_validation(
    progress,
    original_key: str,
    target_key_name: str,
    *,
    import_cleanup_report: dict | None = None,
) -> None:
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
        f"recovered chord lyrics: {transposition_report.get('recovered_chord_lyric_count', 0)}; "
        f"ambiguous chord-like lyrics unchanged: {transposition_report.get('ambiguous_chord_lyric_count', 0)}; "
        f"visible key labels: {transposition_report.get('visible_key_label_update_count', 'unknown')}"
    )
    progress("Validation report", detail)
    output_validation = transposition_report.get("output_validation") or {}
    if output_validation:
        errors = output_validation.get("errors") or []
        compatibility = output_validation.get("musicxml_compatibility_check", "failed")
        measure_validation = output_validation.get("measure_validation") or {}
        duplicate_validation = output_validation.get("duplicate_measure_validation") or {}
        leading_alignment = output_validation.get("leading_part_alignment") or {}
        pickup_alignment = output_validation.get("pickup_marker_alignment") or {}
        rendering_artifacts = output_validation.get("rendering_artifact_repair") or {}
        staff_duration_issues = measure_validation.get("staff_duration_validation") or []
        voice_duration_issues = measure_validation.get("voice_duration_validation") or []
        validation_detail = (
            f"XML valid: {'Yes' if output_validation.get('xml_valid') else 'No'}; "
            f"Harmony elements checked: {output_validation.get('harmony_elements_checked', 0)}; "
            f"Metadata updated: {output_validation.get('metadata_updated', 0)}; "
            f"Measures checked: {measure_validation.get('total_measures_checked', 0)}; "
            f"Incomplete measures found: {measure_validation.get('incomplete_measures_found', 0)}; "
            f"Measures repaired: {measure_validation.get('measures_repaired', 0)}; "
            f"Skipped as intentional: {measure_validation.get('measures_skipped_as_intentional', 0)}; "
            f"Empty measures found: {measure_validation.get('empty_measures_found', 0)}; "
            f"Empty staff measures repaired: {measure_validation.get('empty_staff_measures_repaired', 0)}; "
            f"Time signatures inferred: {measure_validation.get('time_signatures_inferred', 0)}; "
            f"Staff duration issues remaining: {len(staff_duration_issues)}; "
            f"Voice duration issues remaining: {len(voice_duration_issues)}; "
            f"Duplicate measures found: {duplicate_validation.get('duplicate_measures_found', 0)}; "
            f"Duplicate measures removed: {duplicate_validation.get('duplicate_measures_removed', 0)}; "
            f"Duplicate parts found: {(output_validation.get('duplicate_part_validation') or {}).get('duplicate_parts_found', 0)}; "
            f"Duplicate parts removed: {(output_validation.get('duplicate_part_validation') or {}).get('duplicate_parts_removed', 0)}; "
            f"Intro rest measures added: {leading_alignment.get('leading_rest_measures_added', 0)}; "
            f"Pickup verse markers moved: {pickup_alignment.get('pickup_rehearsal_markers_moved', 0)}; "
            f"Pickup verse numbers added: {pickup_alignment.get('pickup_verse_numbers_added', 0)}; "
            f"Pickup leading rests added: {pickup_alignment.get('pickup_leading_rests_added', 0)}; "
            f"Page title artifacts cleaned: {rendering_artifacts.get('page_title_artifacts_cleaned', 0)}; "
            f"Copyright lyric artifacts hidden: {rendering_artifacts.get('copyright_lyric_artifacts_hidden', 0)}; "
            f"Copyright metadata added: {rendering_artifacts.get('copyright_metadata_added', 0)}; "
            f"Unmatched slurs removed: {rendering_artifacts.get('unmatched_slurs_removed', 0)}; "
            f"Malformed chord strings remaining: {len(rendering_artifacts.get('malformed_chord_text_remaining') or [])}; "
            f"MusicXML compatibility check: {compatibility}"
        )
        if import_cleanup_report:
            validation_detail = (
                f"{validation_detail}; "
                f"Import metadata normalized: {import_cleanup_report.get('metadata_normalized', 0)}; "
                f"Duplicate first-page credits removed: {import_cleanup_report.get('duplicate_first_page_credits_removed', 0)}; "
                f"Staff labels cleaned: {import_cleanup_report.get('staff_labels_cleaned', 0)}; "
                f"Repeated staff labels hidden: {import_cleanup_report.get('repeated_staff_labels_hidden', 0)}; "
                f"Clean title block items rebuilt: {import_cleanup_report.get('title_block_items_rebuilt', 0)}; "
                f"OCR chord labels repaired: {import_cleanup_report.get('ocr_chord_labels_repaired', 0)}; "
                f"OCR chord lyrics recovered: {import_cleanup_report.get('ocr_chord_lyrics_recovered', 0)}; "
                f"Ambiguous chord-like lyrics left unchanged: {import_cleanup_report.get('ocr_chord_lyrics_ambiguous', 0)}; "
                f"Duplicate rehearsal marks removed: {import_cleanup_report.get('duplicate_rehearsal_marks_removed', 0)}; "
                f"Rehearsal marks moved to top staff: {import_cleanup_report.get('rehearsal_marks_moved_to_top', 0)}; "
                f"Rehearsal marks normalized for PDF: {import_cleanup_report.get('rehearsal_marks_converted_to_top_text', 0)}; "
                f"Section labels moved to song staff: {import_cleanup_report.get('section_directions_moved_to_song_staff', 0)}; "
                f"Duplicate section labels removed: {import_cleanup_report.get('duplicate_section_directions_removed', 0)}; "
                f"OCR section labels repaired: {import_cleanup_report.get('ocr_section_labels_repaired', 0)}; "
                f"OCR text fragments repaired: {import_cleanup_report.get('ocr_text_fragments_repaired', 0)}; "
                f"Punctuation-only directions removed: {import_cleanup_report.get('punctuation_only_directions_removed', 0)}; "
                f"Ending labels repaired: {import_cleanup_report.get('ocr_ending_labels_repaired', 0)}; "
                f"Ending chords restored: {import_cleanup_report.get('ocr_ending_chords_promoted', 0)}; "
                f"False octave clefs removed: {import_cleanup_report.get('ocr_clef_octave_changes_removed', 0)}; "
                f"System spacing tightened: {import_cleanup_report.get('system_distances_tightened', 0)}; "
                f"Staff spacing tightened: {import_cleanup_report.get('staff_distances_tightened', 0)}; "
                f"Hard page breaks removed: {import_cleanup_report.get('hard_page_breaks_removed', 0)}; "
                f"Known score repairs applied: {import_cleanup_report.get('known_score_repairs_applied', 0)}"
            )
            pdf_recovery = import_cleanup_report.get("pdf_text_recovery") or {}
            if pdf_recovery:
                validation_detail = (
                    f"{validation_detail}; "
                    f"PDF text layer available: {pdf_recovery.get('available', False)}; "
                    f"PDF chord symbols found: {pdf_recovery.get('chord_symbols_found', 0)}; "
                    f"PDF chord symbols restored: {pdf_recovery.get('chord_symbols_restored', 0)}; "
                    f"Covered chord words ignored: {pdf_recovery.get('covered_chord_words_ignored', 0)}; "
                    f"Stacked slash chords restored: {pdf_recovery.get('stacked_chords_merged', 0)}; "
                    f"PDF chord confidence: {pdf_recovery.get('confidence', 0)}; "
                    f"PDF lyric systems restored: {pdf_recovery.get('lyric_systems_restored', 0)}; "
                    f"PDF lyric syllables restored: {pdf_recovery.get('lyric_syllables_restored', 0)}; "
                    f"PDF lyric systems skipped: {pdf_recovery.get('lyric_systems_skipped', 0)}; "
                    f"Mixed-score boundaries restored: {pdf_recovery.get('mixed_score_boundaries_found', 0)}; "
                    f"Prominent PDF directions restored: {pdf_recovery.get('prominent_directions_restored', 0)}; "
                    f"Section captions restored: {pdf_recovery.get('section_captions_restored', 0)}; "
                    f"Corrupted section captions replaced: {pdf_recovery.get('section_caption_artifacts_removed', 0)}; "
                    f"Tempo recovered: {pdf_recovery.get('tempo_recovered', 0)}; "
                    f"Rights recovered: {pdf_recovery.get('rights_recovered', 0)}"
                )
        manual_review = measure_validation.get("manual_review_measures") or []
        if manual_review:
            validation_detail = (
                f"{validation_detail}; "
                f"Measures {', '.join(manual_review)} still need manual review."
            )
        if errors:
            validation_detail = f"{validation_detail}; issues: {' | '.join(errors[:3])}"
        progress("Validate Output", validation_detail)
