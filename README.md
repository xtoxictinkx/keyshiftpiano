# Key Shift Piano

Key Shift Piano is a local desktop app for transposing piano sheet music. It uses Electron for the desktop shell and Python with `music21` for the MusicXML transposition engine.

Everything runs on the user's computer. The app does not use accounts, cloud uploads, databases, web hosting, subscriptions, cloud OCR, image scanning services, or MIDI playback.

Current beta scope: Key Shift Piano reliably saves transposed MusicXML/XML files and can save PDF output through MuseScore Studio when it is installed locally.

## Features

- Open `.musicxml`, `.xml`, and compressed `.mxl` MusicXML files
- Open `.pdf` files through Audiveris OMR PDF-to-MusicXML conversion
- Choose a target key from a simple dropdown
- Save a new transposed MusicXML file
- Save PDF output through MuseScore Studio in the background
- Clean Audiveris layout artifacts before PDF export
- Clear file validation and error messages
- Local Python transposition engine
- Windows installer configuration through Electron Builder

## Current Pipeline

Supported:

```text
MusicXML/XML/MXL input -> transpose with music21 -> save MusicXML/XML or PDF
PDF input -> Audiveris conversion -> transpose with music21 -> save MusicXML/XML or PDF
```

The code has a small converter layer around this workflow:

- `PDF -> MusicXML` uses Audiveris when a PDF is selected, then reconciles the result with any embedded PDF text layer.
- `MusicXML -> MusicXML` passes through directly to the transposer.
- `MusicXML -> PDF` is exported through MuseScore Studio in the background after transposition.
- The default **Polish PDF page layout** setting normalizes Audiveris metadata, removes duplicated first-page title/credit text, cleans repeated staff labels, and applies a MuseScore export style for more readable spacing.
- When a source PDF contains embedded text, its lyric lines, chord symbols, tempo, title details, and copyright text are recovered from that source and mapped back to the recognized measures. This word-and-chord recovery always runs, even when optional page polishing is disabled.
- For image-only scans, chord rows that Audiveris stores as lyrics are recovered by staff, system, lyric row, and vertical position before every transposition. Ambiguous single-letter lyric text is left unchanged and reported for review.
- Transposition uses the nearest octave-equivalent interval, avoiding an unnecessary octave jump (for example, A major to E major moves down a fourth).

PDF import requires a local tool path in **Settings**:

- PDF import requires the Audiveris OMR engine.

If the Audiveris path is missing, the app shows a clear message and does not change the original file.

PDF saving requires MuseScore Studio. Key Shift Piano calls MuseScore in the background; users do not need to open MuseScore manually.

## Prerequisites

- Node.js 20 or newer
- Python 3.10 or newer
- npm
- Audiveris OMR engine for PDF import
- MuseScore Studio for PDF saving

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

Key Shift Piano can transpose MusicXML/XML files without any extra PDF tools.

If you want to **upload PDF sheet music**, install Audiveris:

- Audiveris OMR engine: https://github.com/Audiveris/audiveris/releases

After installing Audiveris, open **Settings** in Key Shift Piano and click **Find Tools Automatically**. If the app cannot find it, use **Browse** beside the Audiveris executable path.

If you want to **save PDF output**, install MuseScore Studio:

- MuseScore Studio: https://musescore.org/

The app can find common MuseScore installs automatically from Settings.

The **Polish PDF page layout** setting is enabled by default. It makes PDF imports cleaner and more usable, but it cannot perfectly recreate every layout decision from a published PDF because Audiveris recognition may not preserve the original page design exactly. Turning it off does not disable word or chord recovery.

Image-only PDF recognition is still an OMR process: a printed chord or note that Audiveris does not recognize at all cannot be reconstructed reliably from MusicXML alone. Text-based PDFs receive the additional embedded-text recovery pass. Key Shift Piano transposes every recovered or recognized note and chord, reports ambiguous chord-like text, and preserves the original PDF.

Audiveris requires visible five-line music staffs. Chord-and-lyrics charts without staff notation cannot be converted safely; use a MusicXML version of the song or a PDF that includes the printed notation.

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

For PDF import or PDF saving, open **Settings** and click **Find Tools Automatically**. The app searches common Windows install locations and Start Menu shortcuts, saves detected paths, and calls Audiveris/MuseScore automatically during processing. If a tool is not found, use **Browse** beside its executable field and click **Save Settings**.

Temporary conversion files are written only inside the app temp folder. The app never overwrites the original PDF.

## Run Tests

With the virtual environment active:

```powershell
python -m unittest discover -s tests
```

The tests cover the Python transposition function using generated MusicXML when `music21` is installed, MusicXML routing without PDF tools, mocked Audiveris conversion calls, and import cleanup for metadata/staff-label artifacts.

## Build Preparation

The project is configured for Electron Builder and can be packaged later as a Windows installer:

```powershell
npm run dist:win
```

Installer output will be written to `dist/`.

The Windows setup is a one-click per-user installer. It installs under the current
Windows account without administrator permission, creates Desktop and Start Menu
shortcuts, and does not write application files to `Program Files`.

For a fully bundled Windows app, install PyInstaller and package normally. The
packaging commands rebuild the Python engine automatically so a stale engine
cannot be shipped:

```powershell
pip install pyinstaller
npm run dist:win
```

Development runs the current Python source directly. Packaged releases contain one rebuilt engine at
`resources/python/transposer.exe`, preventing stale or duplicate Python copies from being shipped.

## Project Structure

```text
Key Shift Piano/
  src/
    main/          Electron main process and local Python bridge
    renderer/      App UI
  python/          music21 transposition engine
  scripts/         packaging helpers
  tests/           Python transposition tests
```
