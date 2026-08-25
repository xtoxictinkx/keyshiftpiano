const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { detectToolPaths } = require('./toolDetection');
const { selectPackagedPythonEngine } = require('./engineRuntime');
const { appendEngineReport } = require('./engineReport');

const MUSICXML_EXTENSIONS = new Set(['.musicxml', '.xml', '.mxl']);
const MUSICXML_OUTPUT_EXTENSIONS = new Set(['.musicxml', '.xml']);
const INPUT_EXTENSIONS = new Set([...MUSICXML_EXTENSIONS, '.pdf']);
const SETTINGS_FILE = 'settings.json';

function getSettingsPath() {
  return path.join(app.getPath('userData'), SETTINGS_FILE);
}

function normalizeExecutablePath(filePath) {
  if (typeof filePath !== 'string') {
    return '';
  }

  return filePath.trim().replace(/^["']|["']$/g, '');
}

function readSettings() {
  try {
    const settingsPath = getSettingsPath();
    if (!fs.existsSync(settingsPath)) {
      return { audiverisPath: '', musescorePath: '', cleanExportLayout: true };
    }

    const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf-8'));
    return {
      audiverisPath: normalizeExecutablePath(settings.audiverisPath),
      musescorePath: normalizeExecutablePath(settings.musescorePath),
      cleanExportLayout: settings.cleanExportLayout !== false
    };
  } catch {
    return { audiverisPath: '', musescorePath: '', cleanExportLayout: true };
  }
}

function saveSettings(settings) {
  const cleanSettings = {
    audiverisPath: normalizeExecutablePath(settings?.audiverisPath),
    musescorePath: normalizeExecutablePath(settings?.musescorePath),
    cleanExportLayout: settings?.cleanExportLayout !== false
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
    title: 'New Key Scores',
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
    : MUSICXML_OUTPUT_EXTENSIONS.has(getExtension(filePath));
}

function isExistingFile(filePath) {
  try {
    return Boolean(normalizeExecutablePath(filePath)) && fs.statSync(normalizeExecutablePath(filePath)).isFile();
  } catch {
    return false;
  }
}

function validateToolSettingsForJob(inputPath, outputFormat, settings, inputKind = '') {
  const isChordChart = inputKind === 'chord-chart-pdf';
  const needsAudiveris = getExtension(inputPath) === '.pdf' && !isChordChart;
  const needsMuseScore = outputFormat === 'pdf' && !isChordChart;

  if (needsAudiveris) {
    if (!settings.audiverisPath) {
      throw new Error('PDF import requires the Audiveris OMR engine. Please configure it in Settings.');
    }

    if (!isExistingFile(settings.audiverisPath)) {
      throw new Error(`The saved Audiveris path is invalid: ${settings.audiverisPath}. Please choose the Audiveris executable in Settings.`);
    }
  }

  if (needsMuseScore) {
    if (!settings.musescorePath) {
      throw new Error('PDF saving requires MuseScore Studio. Please configure it in Settings.');
    }

    if (!isExistingFile(settings.musescorePath)) {
      throw new Error(`The saved MuseScore path is invalid: ${settings.musescorePath}. Please choose the MuseScore executable in Settings.`);
    }
  }
}

async function autoFillMissingToolSettings(settings, inputPath, outputFormat, inputKind = '') {
  const isChordChart = inputKind === 'chord-chart-pdf';
  const needsAudiveris = getExtension(inputPath) === '.pdf' && !isChordChart;
  const needsMuseScore = outputFormat === 'pdf' && !isChordChart;
  const hasAudiveris = settings.audiverisPath && isExistingFile(settings.audiverisPath);
  const hasMuseScore = settings.musescorePath && isExistingFile(settings.musescorePath);

  if ((!needsAudiveris || hasAudiveris) && (!needsMuseScore || hasMuseScore)) {
    return settings;
  }

  const detected = await detectToolPaths({
    fsModule: fs,
    env: process.env,
    resolveShortcutTarget
  });

  return saveSettings({
    audiverisPath: hasAudiveris ? settings.audiverisPath : detected.audiverisPath || settings.audiverisPath,
    musescorePath: hasMuseScore ? settings.musescorePath : detected.musescorePath || settings.musescorePath,
    cleanExportLayout: settings.cleanExportLayout
  });
}

function getToolDisplayName(toolName) {
  return toolName === 'musescore' ? 'MuseScore' : 'Audiveris';
}

function getPythonCommand() {
  const configuredPython = process.env.NEW_KEY_SCORES_PYTHON || process.env.KEY_SHIFT_PYTHON;
  if (configuredPython) {
    return { command: configuredPython, argsPrefix: [] };
  }

  const venvPython = path.join(app.getAppPath(), '.venv', 'Scripts', 'python.exe');
  if (fs.existsSync(venvPython)) {
    return { command: venvPython, argsPrefix: [] };
  }

  return process.platform === 'win32'
    ? { command: 'py', argsPrefix: ['-3'] }
    : { command: 'python3', argsPrefix: [] };
}

function getPackagedPythonEngine() {
  return selectPackagedPythonEngine({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    fsModule: fs
  });
}

function getTransposerPath() {
  return path.join(app.getAppPath(), 'python', 'transposer.py');
}

function getEngineInvocation(extraArgs) {
  const packagedEngine = getPackagedPythonEngine();
  if (app.isPackaged && !packagedEngine) {
    throw new Error(
      'The bundled transposition engine is missing. Reinstall New Key Scores or rebuild the installer.'
    );
  }

  const python = packagedEngine || getPythonCommand();
  const scriptPath = packagedEngine?.scriptPath || getTransposerPath();
  const args = [
    ...(python.argsPrefix || []),
    scriptPath,
    ...extraArgs
  ];

  return {
    command: python.command,
    args,
    env: packagedEngine ? { PYTHONHOME: packagedEngine.pythonHome } : {}
  };
}

function getAppTempPath() {
  const tempPath = path.join(app.getPath('temp'), 'New Key Scores');
  fs.mkdirSync(tempPath, { recursive: true });
  return tempPath;
}

function getTempMusicXmlPath(outputPath) {
  const safeName = path.basename(outputPath, path.extname(outputPath)).replace(/[^\w.-]+/g, '-');
  return path.join(getAppTempPath(), `${safeName}-${Date.now()}.musicxml`);
}

function getMuseScoreStylePath() {
  const stylePath = path.join(getAppTempPath(), 'clean-export-layout.mss');
  const sourceStylePath = path.join(app.getAppPath(), 'src', 'main', 'clean-export-layout.mss');
  fs.copyFileSync(sourceStylePath, stylePath);
  return stylePath;
}

async function waitForStableFile(filePath, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  let previousSize = -1;
  let stableChecks = 0;

  while (Date.now() < deadline) {
    try {
      const { size } = fs.statSync(filePath);
      if (size > 0 && size === previousSize) {
        stableChecks += 1;
        if (stableChecks >= 2) {
          return;
        }
      } else {
        stableChecks = 0;
      }
      previousSize = size;
    } catch {
      stableChecks = 0;
      previousSize = -1;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }

  throw new Error(`MuseScore did not finish writing: ${filePath}`);
}

async function exportMusicXmlToPdfWithMuseScore(musicXmlPath, outputPdfPath, musescorePath, cleanExportLayout = true) {
  return new Promise((resolve, reject) => {
    const cleanMuseScorePath = normalizeExecutablePath(musescorePath);
    const args = cleanExportLayout
      ? [musicXmlPath, '-S', getMuseScoreStylePath(), '-o', outputPdfPath]
      : [musicXmlPath, '-o', outputPdfPath];
    const child = spawn(cleanMuseScorePath, args, {
      windowsHide: true,
      env: { ...process.env, QT_LOGGING_RULES: '*.debug=false' }
    });

    let stdout = '';
    let stderr = '';
    let settled = false;

    const timeout = setTimeout(() => {
      if (settled) {
        return;
      }

      settled = true;
      child.kill();
      reject(new Error(`PDF saving timed out using MuseScore at: ${cleanMuseScorePath}.`));
    }, 120000);

    child.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    child.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    child.on('error', (error) => {
      if (settled) {
        return;
      }

      settled = true;
      clearTimeout(timeout);
      reject(new Error(`PDF saving requires MuseScore Studio. Tried: ${cleanMuseScorePath}. ${error.message}`));
    });

    child.on('close', async (code) => {
      if (settled) {
        return;
      }

      clearTimeout(timeout);
      if (code === 0 && fs.existsSync(outputPdfPath)) {
        try {
          await waitForStableFile(outputPdfPath);
          settled = true;
          resolve(outputPdfPath);
          return;
        } catch (error) {
          settled = true;
          reject(error);
          return;
        }
      }

      settled = true;
      const detail = (stderr || stdout || `MuseScore exited with code ${code}.`).trim();
      reject(new Error(`PDF saving failed using MuseScore at: ${cleanMuseScorePath}. ${detail}`));
    });
  });
}

function parseStageLine(line) {
  try {
    const event = JSON.parse(line);
    return event?.type === 'stage' && event.name ? event : null;
  } catch {
    return null;
  }
}

function getFileTypeLabel(filePath) {
  const extension = getExtension(filePath);
  if (extension === '.pdf') {
    return 'PDF';
  }

  if (MUSICXML_EXTENSIONS.has(extension)) {
    return extension === '.mxl' ? 'Compressed MusicXML' : extension === '.xml' ? 'XML MusicXML' : 'MusicXML';
  }

  return 'Unknown';
}

function detectOriginalKey(inputPath) {
  if (!isValidMusicXmlFile(inputPath)) {
    return Promise.resolve(null);
  }

  return new Promise((resolve) => {
    const invocation = getEngineInvocation([
      '--input',
      inputPath,
      '--output',
      inputPath,
      '--target-key',
      'C major',
      '--detect-key-only'
    ]);

    const child = spawn(invocation.command, invocation.args, {
      windowsHide: true,
      env: { ...process.env, ...invocation.env, PYTHONIOENCODING: 'utf-8' }
    });

    let stdout = '';

    child.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    child.on('error', () => resolve(null));
    child.on('close', (code) => {
      resolve(code === 0 ? stdout.trim() || null : null);
    });
  });
}

function inspectInputFile(inputPath) {
  if (getExtension(inputPath) !== '.pdf') {
    return Promise.resolve({ kind: 'musicxml', original_key: null });
  }

  return new Promise((resolve, reject) => {
    const invocation = getEngineInvocation([
      '--input',
      inputPath,
      '--output',
      inputPath,
      '--target-key',
      'C major',
      '--inspect-input'
    ]);
    const child = spawn(invocation.command, invocation.args, {
      windowsHide: true,
      env: { ...process.env, ...invocation.env, PYTHONIOENCODING: 'utf-8' }
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (data) => {
      stdout += data.toString();
    });
    child.stderr.on('data', (data) => {
      stderr += data.toString();
    });
    child.on('error', (error) => reject(new Error(`The PDF could not be inspected. ${error.message}`)));
    child.on('close', (code) => {
      if (code !== 0) {
        reject(new Error((stderr || 'The PDF could not be inspected.').trim()));
        return;
      }
      try {
        resolve(JSON.parse(stdout.trim()));
      } catch {
        reject(new Error('The PDF inspection result could not be read.'));
      }
    });
  });
}

function runTransposer(inputPath, outputPath, targetKey, outputFormat, settings, sender) {
  return new Promise((resolve, reject) => {
    const invocation = getEngineInvocation([
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
      '--temp-dir',
      getAppTempPath(),
      '--clean-export-layout',
      settings.cleanExportLayout === false ? 'false' : 'true'
    ]);

    const child = spawn(invocation.command, invocation.args, {
      windowsHide: true,
      env: { ...process.env, ...invocation.env, PYTHONIOENCODING: 'utf-8' }
    });

    let stdout = '';
    let stderr = '';
    let stdoutBuffer = '';
    let engineReport = '';

    child.stdout.on('data', (data) => {
      stdoutBuffer += data.toString();
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.trim()) {
          continue;
        }

        const stage = parseStageLine(line);
        if (stage) {
          if (stage.name === 'Engine report') {
            engineReport = stage.detail || '';
          }
          sender?.send('processing-stage', stage);
        } else {
          stdout += `${line}\n`;
        }
      }
    });

    child.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    child.on('error', (error) => {
      reject(new Error(`Could not start Python. ${error.message}`));
    });

    child.on('close', (code) => {
      if (stdoutBuffer.trim()) {
        const stage = parseStageLine(stdoutBuffer.trim());
        if (stage) {
          if (stage.name === 'Engine report') {
            engineReport = stage.detail || '';
          }
          sender?.send('processing-stage', stage);
        } else {
          stdout += `${stdoutBuffer.trim()}\n`;
        }
      }

      if (code === 0) {
        resolve({ outputPath: stdout.trim(), engineReport });
      } else {
        reject(new Error((stderr || stdout || 'The transposition failed.').trim()));
      }
    });
  });
}

ipcMain.handle('get-settings', async () => readSettings());

ipcMain.handle('save-settings', async (_event, payload) => saveSettings(payload));

function resolveShortcutTarget(shortcutPath) {
  return new Promise((resolve) => {
    const script = [
      '$shortcutPath = $args[0]',
      '$shell = New-Object -ComObject WScript.Shell',
      '$shortcut = $shell.CreateShortcut($shortcutPath)',
      'Write-Output $shortcut.TargetPath'
    ].join('; ');

    const child = spawn('powershell.exe', ['-NoProfile', '-Command', script, shortcutPath], {
      windowsHide: true
    });

    let stdout = '';

    child.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    child.on('error', () => resolve(''));
    child.on('close', () => resolve(normalizeExecutablePath(stdout)));
  });
}

function formatToolDetectionSummary(detected) {
  const missing = [];
  if (!detected.audiverisPath) {
    missing.push('Audiveris: install Audiveris, then use Browse or Find Tools Automatically again.');
  }
  if (!detected.musescorePath) {
    missing.push('MuseScore: install MuseScore Studio, then use Browse or Find Tools Automatically again.');
  }

  return [
    `Audiveris found: ${detected.audiverisPath || 'Not found'}`,
    `MuseScore found: ${detected.musescorePath || 'Not found'}`,
    missing.length ? `Missing tools: ${missing.join(' ')}` : 'Missing tools: none'
  ].join('\n');
}

ipcMain.handle('find-tools-automatically', async () => {
  const existingSettings = readSettings();
  const detected = await detectToolPaths({
    fsModule: fs,
    env: process.env,
    resolveShortcutTarget
  });

  const savedSettings = saveSettings({
    audiverisPath: detected.audiverisPath || existingSettings.audiverisPath,
    musescorePath: detected.musescorePath || existingSettings.musescorePath,
    cleanExportLayout: existingSettings.cleanExportLayout
  });

  return {
    ...detected,
    savedSettings,
    summary: formatToolDetectionSummary(detected)
  };
});

function testExecutablePath(toolName, filePath) {
  const cleanPath = normalizeExecutablePath(filePath);
  const displayName = getToolDisplayName(toolName);

  if (!cleanPath) {
    return Promise.resolve({
      ok: false,
      message: `✗ ${displayName} failed\nPath tested: ${cleanPath || '(none)'}\nError: ${displayName} path is missing.`
    });
  }

  if (!isExistingFile(cleanPath)) {
    return Promise.resolve({
      ok: false,
      message: `✗ ${displayName} failed\nPath tested: ${cleanPath}\nError: File does not exist.`
    });
  }

  return new Promise((resolve, reject) => {
    const child = spawn(cleanPath, ['--help'], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe']
    });

    let settled = false;
    let stdout = '';
    let stderr = '';

    child.stdout.on('data', (data) => {
      stdout += data.toString();
    });

    child.stderr.on('data', (data) => {
      stderr += data.toString();
    });

    const timeout = setTimeout(() => {
      if (settled) {
        return;
      }

      settled = true;
      child.kill();
      resolve({
        ok: true,
        message: `✓ ${displayName} detected and launchable\nPath tested: ${cleanPath}`
      });
    }, 4000);

    child.on('error', (error) => {
      if (settled) {
        return;
      }

      settled = true;
      clearTimeout(timeout);
      resolve({
        ok: false,
        message: `✗ ${displayName} failed\nPath tested: ${cleanPath}\nError: ${error.message}`
      });
    });

    child.on('close', (code) => {
      if (settled) {
        return;
      }

      settled = true;
      clearTimeout(timeout);
      const errorText = (stderr || stdout).trim();
      const errorDetail = errorText ? `\nCommand output: ${errorText}` : '';
      resolve({
        ok: true,
        message: `✓ ${displayName} detected and launchable\nPath tested: ${cleanPath}${code ? `\nValidation command exited with code ${code}.` : ''}${errorDetail}`
      });
    });
  });
}

ipcMain.handle('test-tool-path', async (_event, payload) => {
  const toolName = payload?.tool === 'musescore' ? 'musescore' : 'audiveris';
  const settings = saveSettings({
    ...readSettings(),
    [toolName === 'musescore' ? 'musescorePath' : 'audiverisPath']: payload?.path
  });
  return testExecutablePath(toolName, toolName === 'musescore' ? settings.musescorePath : settings.audiverisPath);
});

ipcMain.handle('choose-tool-path', async (_event, payload) => {
  const toolName = payload?.tool === 'musescore' ? 'musescore' : 'audiveris';
  const displayName = getToolDisplayName(toolName);
  const options = {
    title: `Choose ${displayName} Executable`,
    properties: ['openFile']
  };

  if (process.platform === 'win32') {
    options.filters = [{ name: 'Executable Files', extensions: ['exe'] }];
  }

  const result = await dialog.showOpenDialog(options);

  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }

  return normalizeExecutablePath(result.filePaths[0]);
});

ipcMain.handle('select-input-file', async () => {
  const result = await dialog.showOpenDialog({
    title: 'Choose Score or Chord Chart',
    properties: ['openFile'],
    filters: [
      { name: 'Supported Files', extensions: ['musicxml', 'xml', 'mxl', 'pdf'] },
      { name: 'MusicXML Files', extensions: ['musicxml', 'xml', 'mxl'] },
      { name: 'Sheet Music or Chord Chart PDFs', extensions: ['pdf'] }
    ]
  });

  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }

  const filePath = result.filePaths[0];
  if (!isValidInputFile(filePath)) {
    throw new Error('Please choose a .musicxml, .xml, .mxl, or .pdf file.');
  }

  const inspection = getExtension(filePath) === '.pdf'
    ? await inspectInputFile(filePath)
    : { kind: 'musicxml', original_key: await detectOriginalKey(filePath) };
  return {
    filePath,
    fileType: inspection.kind === 'chord-chart-pdf' ? 'PDF chord chart' : getFileTypeLabel(filePath),
    inputKind: inspection.kind,
    originalKey: inspection.original_key || null
  };
});

ipcMain.handle('transpose-file', async (event, payload) => {
  const inputPath = payload?.inputPath;
  const targetKey = payload?.targetKey;
  const outputFormat = payload?.outputFormat === 'pdf' ? 'pdf' : 'musicxml';

  if (!isValidInputFile(inputPath)) {
    throw new Error('Please choose a valid .musicxml, .xml, .mxl, or .pdf file.');
  }

  if (!targetKey || typeof targetKey !== 'string') {
    throw new Error('Please choose a target key.');
  }

  const inspection = getExtension(inputPath) === '.pdf'
    ? await inspectInputFile(inputPath)
    : { kind: 'musicxml' };
  const inputKind = inspection.kind || 'score-pdf';
  if (inputKind === 'chord-chart-pdf' && outputFormat !== 'pdf') {
    throw new Error('Chord charts currently save as PDF so their lyrics and layout remain intact.');
  }
  const settings = await autoFillMissingToolSettings(readSettings(), inputPath, outputFormat, inputKind);
  validateToolSettingsForJob(inputPath, outputFormat, settings, inputKind);

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

  if (outputFormat === 'pdf') {
    if (inputKind === 'chord-chart-pdf') {
      const transposition = await runTransposer(
        inputPath,
        saveResult.filePath,
        targetKey,
        'pdf',
        settings,
        event.sender
      );
      return {
        canceled: false,
        outputPath: transposition.outputPath || saveResult.filePath,
        requestedOutputPath: saveResult.filePath,
        outputFormat,
        engineReport: transposition.engineReport
      };
    }
    const intermediateMusicXml = getTempMusicXmlPath(saveResult.filePath);
    try {
      const transposition = await runTransposer(
        inputPath,
        intermediateMusicXml,
        targetKey,
        'musicxml',
        settings,
        event.sender
      );
      event.sender?.send('processing-stage', { type: 'stage', name: 'Exporting output' });
      const pdfPath = await exportMusicXmlToPdfWithMuseScore(
        intermediateMusicXml,
        saveResult.filePath,
        settings.musescorePath,
        settings.cleanExportLayout !== false
      );
      event.sender?.send('processing-stage', { type: 'stage', name: 'Complete', detail: `Saved ${pdfPath}` });
      return {
        canceled: false,
        outputPath: pdfPath,
        requestedOutputPath: saveResult.filePath,
        outputFormat,
        engineReport: appendEngineReport(transposition.engineReport, 'PDF writer: MuseScore Studio')
      };
    } finally {
      fs.rmSync(intermediateMusicXml, { force: true });
    }
  }

  const transposition = await runTransposer(
    inputPath,
    saveResult.filePath,
    targetKey,
    outputFormat,
    settings,
    event.sender
  );
  return {
    canceled: false,
    outputPath: transposition.outputPath || saveResult.filePath,
    requestedOutputPath: saveResult.filePath,
    outputFormat,
    engineReport: transposition.engineReport
  };
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
