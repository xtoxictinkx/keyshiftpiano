const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const MUSICXML_EXTENSIONS = new Set(['.musicxml', '.xml']);
const INPUT_EXTENSIONS = new Set(['.musicxml', '.xml', '.pdf']);
const SETTINGS_FILE = 'settings.json';

function getSettingsPath() {
  return path.join(app.getPath('userData'), SETTINGS_FILE);
}

function readSettings() {
  try {
    const settingsPath = getSettingsPath();
    if (!fs.existsSync(settingsPath)) {
      return { audiverisPath: '', musescorePath: '' };
    }

    const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf-8'));
    return {
      audiverisPath: typeof settings.audiverisPath === 'string' ? settings.audiverisPath : '',
      musescorePath: typeof settings.musescorePath === 'string' ? settings.musescorePath : ''
    };
  } catch {
    return { audiverisPath: '', musescorePath: '' };
  }
}

function saveSettings(settings) {
  const cleanSettings = {
    audiverisPath: typeof settings?.audiverisPath === 'string' ? settings.audiverisPath : '',
    musescorePath: typeof settings?.musescorePath === 'string' ? settings.musescorePath : ''
  };

  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  fs.writeFileSync(getSettingsPath(), JSON.stringify(cleanSettings, null, 2), 'utf-8');
  return cleanSettings;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 920,
    height: 680,
    minWidth: 760,
    minHeight: 560,
    title: 'Key Shift Piano',
    backgroundColor: '#f7f3ec',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  win.loadFile(path.join(__dirname, '../renderer/index.html'));
}

function getExtension(filePath) {
  if (!filePath || typeof filePath !== 'string') {
    return '';
  }

  return path.extname(filePath).toLowerCase();
}

function isValidInputFile(filePath) {
  return INPUT_EXTENSIONS.has(getExtension(filePath));
}

function isValidMusicXmlFile(filePath) {
  return MUSICXML_EXTENSIONS.has(getExtension(filePath));
}

function isValidOutputFile(filePath, outputFormat) {
  return outputFormat === 'pdf'
    ? getExtension(filePath) === '.pdf'
    : isValidMusicXmlFile(filePath);
}

function validateToolSettingsForJob(inputPath, outputFormat, settings) {
  if (getExtension(inputPath) === '.pdf' && !settings.audiverisPath) {
    throw new Error('PDF import requires the Audiveris OMR engine. Please configure it in Settings.');
  }

  if (outputFormat === 'pdf' && !settings.musescorePath) {
    throw new Error('PDF export requires MuseScore.');
  }
}

function getPythonCommand() {
  if (process.env.KEY_SHIFT_PYTHON) {
    return { command: process.env.KEY_SHIFT_PYTHON, argsPrefix: [] };
  }

  const venvPython = path.join(app.getAppPath(), '.venv', 'Scripts', 'python.exe');
  if (fs.existsSync(venvPython)) {
    return { command: venvPython, argsPrefix: [] };
  }

  return process.platform === 'win32'
    ? { command: 'py', argsPrefix: ['-3'] }
    : { command: 'python3', argsPrefix: [] };
}

function getTransposerExecutable() {
  const packagedExecutable = process.resourcesPath
    ? path.join(process.resourcesPath, 'python', 'dist', 'transposer.exe')
    : null;

  if (packagedExecutable && fs.existsSync(packagedExecutable)) {
    return packagedExecutable;
  }

  const developmentExecutable = path.join(app.getAppPath(), 'python', 'dist', 'transposer.exe');
  if (fs.existsSync(developmentExecutable)) {
    return developmentExecutable;
  }

  return null;
}

function getTransposerPath() {
  return path.join(app.getAppPath(), 'python', 'transposer.py');
}

function getAppTempPath() {
  const tempPath = path.join(app.getPath('temp'), 'Key Shift Piano');
  fs.mkdirSync(tempPath, { recursive: true });
  return tempPath;
}

function runTransposer(inputPath, outputPath, targetKey, outputFormat, settings) {
  return new Promise((resolve, reject) => {
    const executablePath = getTransposerExecutable();
    const python = executablePath ? null : getPythonCommand();
    const command = executablePath || python.command;
    const args = [
      ...(python?.argsPrefix || []),
      ...(executablePath ? [] : [getTransposerPath()]),
      '--input',
      inputPath,
      '--output',
      outputPath,
      '--target-key',
      targetKey,
      '--output-format',
      outputFormat,
      '--audiveris-path',
      settings.audiverisPath || '',
      '--musescore-path',
      settings.musescorePath || '',
      '--temp-dir',
      getAppTempPath()
    ];

    const child = spawn(command, args, {
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
    });

    let stdout = '';
    let stderr = '';

    child.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    child.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    child.on('error', (error) => {
      reject(new Error(`Could not start Python. ${error.message}`));
    });

    child.on('close', (code) => {
      if (code === 0) {
        resolve(stdout.trim());
      } else {
        reject(new Error((stderr || stdout || 'The transposition failed.').trim()));
      }
    });
  });
}

ipcMain.handle('get-settings', async () => readSettings());

ipcMain.handle('save-settings', async (_event, payload) => saveSettings(payload));

ipcMain.handle('choose-tool-path', async (_event, payload) => {
  const tool = payload?.tool === 'musescore' ? 'musescore' : 'audiveris';
  const title = tool === 'musescore' ? 'Choose MuseScore Executable' : 'Choose Audiveris Executable';
  const options = {
    title,
    properties: ['openFile']
  };

  if (process.platform === 'win32') {
    options.filters = [{ name: 'Executable Files', extensions: ['exe'] }];
  }

  const result = await dialog.showOpenDialog(options);

  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }

  return result.filePaths[0];
});

ipcMain.handle('select-input-file', async () => {
  const result = await dialog.showOpenDialog({
    title: 'Choose Sheet Music File',
    properties: ['openFile'],
    filters: [
      { name: 'Supported Files', extensions: ['musicxml', 'xml', 'pdf'] },
      { name: 'MusicXML Files', extensions: ['musicxml', 'xml'] },
      { name: 'PDF Files', extensions: ['pdf'] }
    ]
  });

  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }

  const filePath = result.filePaths[0];
  if (!isValidInputFile(filePath)) {
    throw new Error('Please choose a .musicxml, .xml, or .pdf file.');
  }

  return filePath;
});

ipcMain.handle('transpose-file', async (_event, payload) => {
  const inputPath = payload?.inputPath;
  const targetKey = payload?.targetKey;
  const outputFormat = payload?.outputFormat === 'pdf' ? 'pdf' : 'musicxml';

  if (!isValidInputFile(inputPath)) {
    throw new Error('Please choose a valid .musicxml, .xml, or .pdf file.');
  }

  if (!targetKey || typeof targetKey !== 'string') {
    throw new Error('Please choose a target key.');
  }

  const settings = readSettings();
  validateToolSettingsForJob(inputPath, outputFormat, settings);

  const parsed = path.parse(inputPath);
  const defaultExtension = outputFormat === 'pdf' ? 'pdf' : 'musicxml';
  const defaultName = `${parsed.name}-in-${targetKey.replace(/\s+/g, '-')}.${defaultExtension}`;
  const filters = outputFormat === 'pdf'
    ? [{ name: 'PDF File', extensions: ['pdf'] }]
    : [
      { name: 'MusicXML File', extensions: ['musicxml'] },
      { name: 'XML File', extensions: ['xml'] }
    ];

  const saveResult = await dialog.showSaveDialog({
    title: outputFormat === 'pdf' ? 'Save Transposed PDF' : 'Save Transposed MusicXML',
    defaultPath: path.join(parsed.dir, defaultName),
    filters
  });

  if (saveResult.canceled || !saveResult.filePath) {
    return { canceled: true };
  }

  if (!isValidOutputFile(saveResult.filePath, outputFormat)) {
    const expected = outputFormat === 'pdf' ? '.pdf' : '.musicxml or .xml';
    throw new Error(`Please save the transposed file as ${expected}.`);
  }

  await runTransposer(inputPath, saveResult.filePath, targetKey, outputFormat, settings);
  return { canceled: false, outputPath: saveResult.filePath };
});

app.whenReady().then(() => {
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
