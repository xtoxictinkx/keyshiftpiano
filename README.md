# Key Shift Piano

Key Shift Piano is a local desktop app for transposing piano sheet music. It uses Electron for the desktop shell and Python with `music21` for the MusicXML transposition engine.

Everything runs on the user's computer. The app does not use accounts, cloud uploads, databases, web hosting, subscriptions, OCR, image scanning, or MIDI playback.

## Features

- Open `.musicxml` and `.xml` MusicXML files
- Open `.pdf` files through Audiveris OMR PDF-to-MusicXML conversion
- Choose a target key from a simple dropdown
- Save a new transposed MusicXML file
- Optionally export transposed files as PDF through MuseScore
- Clear file validation and error messages
- Local Python transposition engine
- Windows installer configuration through Electron Builder

## Current Pipeline

Supported:

```text
MusicXML/XML input -> transpose with music21 -> save MusicXML/XML
PDF input -> Audiveris conversion -> transpose with music21 -> save MusicXML/XML
MusicXML/XML input -> transpose with music21 -> MuseScore export -> save PDF
PDF input -> Audiveris conversion -> transpose with music21 -> MuseScore export -> save PDF
```

PDF support requires local tool paths in **Settings**:

- PDF import requires the Audiveris OMR engine.
- PDF export requires MuseScore.

If either path is missing, the app shows a clear message and does not change the original file.

## Prerequisites

- Node.js 20 or newer
- Python 3.10 or newer
- npm
- Audiveris OMR engine for PDF import
- MuseScore for PDF export, optional

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

If you want to **export the transposed result as PDF**, install MuseScore Studio:

- MuseScore Studio: https://musescore.org/en/download

After installing either tool, open **Settings** in Key Shift Piano and click **Find Tools Automatically**. If the app cannot find a tool, use **Browse** beside that tool's executable path.

## Run The App

```powershell
npm start
```

## Use The App

1. Click **Upload**.
2. Select a `.musicxml`, `.xml`, or `.pdf` file.
3. Pick the target key.
4. Choose the output format.
5. Click **Shift Key**.
6. Choose where to save the new file.

For PDF import or PDF export, open **Settings** and click **Find Tools Automatically**. The app searches common Windows install locations and Start Menu shortcuts, saves any detected paths, and calls Audiveris or MuseScore automatically during processing. If a tool is not found, use **Browse** beside its executable field and click **Save Settings**.

Temporary conversion files are written only inside the app temp folder. The app never overwrites the original PDF.

## Run Tests

With the virtual environment active:

```powershell
python -m unittest discover -s tests
```

The tests cover the Python transposition function using generated MusicXML when `music21` is installed, MusicXML routing without PDF tools, and mocked Audiveris and MuseScore command calls.

## Build Preparation

The project is configured for Electron Builder and can be packaged later as a Windows installer:

```powershell
npm run dist:win
```

Installer output will be written to `dist/`.

For a fully bundled Windows app, build the Python engine as an executable before packaging:

```powershell
pip install pyinstaller
npm run build:engine
npm run dist:win
```

The Electron app automatically uses `python/dist/transposer.exe` during development or the bundled copy when packaged.

## Project Structure

```text
Key Shift Piano/
  src/
    main/          Electron main process and local Python bridge
    renderer/      App UI
  python/          music21 transposition engine
  tests/           Python transposition tests
```
