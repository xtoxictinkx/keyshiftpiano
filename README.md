# New Key Scores

New Key Scores is a privacy-first desktop app that puts sheet music and chord charts in the key musicians need. It uses an Electron desktop shell and a local Python engine to transpose MusicXML notation, key signatures, chord symbols, visible key labels, and digital PDF chord charts.

Everything runs on the user's computer. The app does not use accounts, cloud uploads, databases, web hosting, subscriptions, cloud OCR, image scanning services, or MIDI playback.

## Project Status

New Key Scores is currently a **personal alpha** maintained and tested by one person. It is not offered as a public download, and no claims are made about broad adoption or production readiness. The source is public so the implementation and development history can be reviewed while the application is completed.

The current alpha reliably saves transposed MusicXML/XML files in the tested workflows, directly reads and writes text-based PDF chord charts, and can save score PDF output through MuseScore Studio when it is installed locally. Results from optical music recognition still require careful review.

## Reader And Writer Architecture

New Key Scores owns its desktop interface, processing workflow, direct MusicXML transposition, chord-chart detection and transposition, validation, PDF-text recovery, and score-cleanup logic. It uses established open-source components for specialized file operations:

- Audiveris performs optical music recognition when importing printed PDF notation.
- MuseScore Studio renders transposed MusicXML as a printable PDF.
- `music21` is a fallback MusicXML parser and writer for scores the direct engine cannot safely interpret.
- `pdfplumber` reads embedded PDF text so lyrics, chords, tempo, and rights text can be recovered.
- `pypdf` and `reportlab` preserve a chord chart's original PDF pages while New Key Scores writes the shifted chord symbols.

Audiveris and MuseScore are installed separately by the user. Their executables are not bundled with New Key Scores and are not represented as New Key Scores code.

Personal Windows builds package the readable New Key Scores Python source with a locally staged, validly signed Python Software Foundation runtime. This avoids launching an unsigned PyInstaller child under Windows Smart App Control while code signing is deferred during the personal-alpha phase.

## Privacy

New Key Scores processes selected files locally. It does not create an account, upload sheet music, collect analytics, or send application data to a New Key Scores service. Audiveris and MuseScore are started locally only when their respective PDF features are requested.

## Releases And Code Signing

There is currently no supported public binary release. Personal alpha builds are unsigned portable ZIP packages intended only for the maintainer's development and testing computer. Smart App Control prevents a trustworthy unsigned NSIS installer from being built and exercised on this computer, so installation is deferred until code signing is appropriate. A public beta, formal code-signing policy, and code-signing application will be considered only after independent testing and a verifiable public release history exist.

## Features

- Open `.musicxml`, `.xml`, and compressed `.mxl` MusicXML files
- Open `.pdf` files through Audiveris OMR PDF-to-MusicXML conversion
- Read and transpose digital/text-based PDF chord charts while preserving their lyrics and page layout
- Choose a target key from a simple dropdown
- Save a new transposed MusicXML file
- Save PDF output through MuseScore Studio in the background
- Clean Audiveris layout artifacts before PDF export
- Clear file validation and error messages
- A completion report showing which reader, transposer, and writer engines were used
- Local Python transposition engine
- Portable Windows alpha packaging through Electron Builder

## Current Pipeline

Supported:

```text
MusicXML/XML/MXL input -> New Key Scores direct transposer (music21 fallback) -> save MusicXML/XML or PDF
Sheet-music PDF input -> Audiveris conversion -> transpose MusicXML -> save MusicXML/XML or PDF
Text-based chord-chart PDF -> New Key Scores chord reader/transposer/writer -> save PDF
```

The code has a small converter layer around this workflow:

- `PDF -> MusicXML` uses Audiveris when a PDF is selected, then reconciles the result with any embedded PDF text layer.
- `MusicXML -> MusicXML` passes through directly to the transposer.
- `MusicXML -> PDF` is exported through MuseScore Studio in the background after transposition.
- `Chord-chart PDF -> PDF` is handled directly by New Key Scores and does not require Audiveris or MuseScore.
- The default **Polish PDF page layout** setting normalizes Audiveris metadata, removes duplicated first-page title/credit text, cleans repeated staff labels, and applies a MuseScore export style for more readable spacing.
- When a source PDF contains embedded text, its lyric lines, chord symbols, tempo, title details, and copyright text are recovered from that source and mapped back to the recognized measures. This word-and-chord recovery always runs, even when optional page polishing is disabled.
- For image-only scans, chord rows that Audiveris stores as lyrics are recovered by staff, system, lyric row, and vertical position before every transposition. Ambiguous single-letter lyric text is left unchanged and reported for review.
- Transposition uses the nearest octave-equivalent interval, avoiding an unnecessary octave jump (for example, A major to E major moves down a fourth).

Staff-notation PDF import requires a local tool path in **Settings**:

- PDF import requires the Audiveris OMR engine.

If the Audiveris path is missing, the app shows a clear message and does not change the original file.

Score PDF saving requires MuseScore Studio. New Key Scores calls MuseScore in the background; users do not need to open MuseScore manually. Chord-chart PDF saving uses the built-in writer instead.

## Prerequisites

- Node.js 20 or newer
- Python 3.10 or newer
- npm
- Audiveris OMR engine for staff-notation PDF import (optional)
- MuseScore Studio for score PDF saving (optional)

## Setup

Install the desktop app dependencies:

```powershell
npm install
```

Create and activate a Python virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the Python transposition dependency:

```powershell
pip install -r requirements.txt
```

## Optional PDF Tools

New Key Scores can transpose MusicXML/XML files and text-based PDF chord charts without any extra PDF tools.

If you want to **upload PDF sheet music**, install Audiveris:

- Audiveris OMR engine: https://github.com/Audiveris/audiveris/releases

After installing Audiveris, open **Settings** in New Key Scores and click **Find Tools Automatically**. If the app cannot find it, use **Browse** beside the Audiveris executable path.

If you want to **save PDF output**, install MuseScore Studio:

- MuseScore Studio: https://musescore.org/

The app can find common MuseScore installs automatically from Settings.

The **Polish PDF page layout** setting is enabled by default. It makes PDF imports cleaner and more usable, but it cannot perfectly recreate every layout decision from a published PDF because Audiveris recognition may not preserve the original page design exactly. Turning it off does not disable word or chord recovery.

Image-only PDF recognition is still an OMR process: a printed chord or note that cannot be read from an embedded text layer or recognized by Audiveris cannot be reconstructed reliably. Text-based PDFs receive the additional embedded-text recovery pass. New Key Scores transposes every recovered or recognized note and chord, reports ambiguous chord-like text, and preserves the original PDF.

Text-based chord-and-lyrics charts without staff notation use the built-in chord-chart reader and writer. Scanned/image-only chord charts still need a future local OCR path and are not treated as safely readable chord charts yet.

## Run The App

```powershell
npm start
```

## Use The App

1. Click **Upload**.
2. Select a `.musicxml`, `.xml`, `.mxl`, or `.pdf` file.
3. Pick the target key.
4. Click **Shift Key**.
5. Choose where to save the new MusicXML or PDF file.

For staff-notation PDF import or score PDF saving, open **Settings** and click **Find Tools Automatically**. The app searches common Windows install locations and Start Menu shortcuts, saves detected paths, and calls Audiveris/MuseScore automatically during processing. Text-based chord-chart PDFs do not require either tool. If a required score tool is not found, use **Browse** beside its executable field and click **Save Settings**.

Temporary conversion files are written only inside the app temp folder. The app never overwrites the original PDF.

## Run Tests

With the virtual environment active:

```powershell
python -m unittest discover -s tests
```

The tests cover direct and fallback MusicXML transposition, chord-chart PDF detection/writing, staff-PDF separation, routing without unnecessary external tools, mocked Audiveris conversion calls, and import cleanup for metadata/staff-label artifacts.

## Build Preparation

The project is configured for Electron Builder and produces a personal Windows portable ZIP:

```powershell
npm run dist:win
```

The ZIP is written to `dist/`. Extract it, then run `New Key Scores.exe` from the extracted folder.

The build stages the readable Python engine source, its local dependencies, and a validly signed Python Software Foundation runtime before creating the ZIP. The generated staging directory is excluded from Git and can be rebuilt from the checked-in source and `requirements.txt`.

Public Windows installers should be Authenticode-signed before release. Windows Smart App Control blocks the unsigned NSIS installer's build-time uninstaller step on the maintainer's computer, so the personal alpha deliberately uses the portable ZIP instead of weakening Windows security.

Development runs the current Python source directly. Portable builds contain the staged source under `resources/python-runtime/engine/python/` and launch it through the signed runtime at `resources/python-runtime/python.exe`.

## Project Structure

```text
New Key Scores/
  src/
    main/          Electron main process and local Python bridge
    renderer/      App UI
  python/          direct MusicXML and chord-chart PDF engines
  scripts/         packaging helpers
  tests/           Python transposition tests
```
