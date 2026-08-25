const fileInput = document.querySelector('#file-path');
const chooseFileButton = document.querySelector('#choose-file');
const shiftButton = document.querySelector('#shift-key');
const targetKeySelect = document.querySelector('#target-key');
const outputFormatSelect = document.querySelector('#output-format');
const pdfNote = document.querySelector('#pdf-note');
const statusBox = document.querySelector('#status');
const engineReport = document.querySelector('#engine-report');
const engineReportList = document.querySelector('#engine-report-list');
const progressWrap = document.querySelector('#progress-wrap');
const progressTrack = document.querySelector('.progress-track');
const progressFill = document.querySelector('#progress-fill');
const audiverisPathInput = document.querySelector('#audiveris-path');
const chooseAudiverisButton = document.querySelector('#choose-audiveris');
const testAudiverisButton = document.querySelector('#test-audiveris');
const musescorePathInput = document.querySelector('#musescore-path');
const chooseMuseScoreButton = document.querySelector('#choose-musescore');
const testMuseScoreButton = document.querySelector('#test-musescore');
const cleanExportLayoutInput = document.querySelector('#clean-export-layout');
const findToolsButton = document.querySelector('#find-tools');
const saveSettingsButton = document.querySelector('#save-settings');

let selectedFile = '';
let selectedFileType = '';
let originalKey = '';
let toolSettings = {
  audiverisPath: '',
  musescorePath: '',
  cleanExportLayout: true
};

const STAGE_PROGRESS = {
  'Loading file': 8,
  'Converting PDF to MusicXML': 28,
  'Recovering PDF words and chords': 36,
  'Cleaning export layout': 36,
  'Detecting key': 45,
  Transposing: 68,
  'Validation report': 78,
  'Validate Output': 82,
  'Engine report': 86,
  'Exporting output': 90,
  Complete: 100
};

function setStatus(message, type = 'neutral') {
  statusBox.textContent = message;
  statusBox.className = `status ${type === 'neutral' ? '' : type}`.trim();
}

function setProgress(value, isActive = false) {
  const normalizedValue = Math.max(0, Math.min(100, value));
  progressWrap.hidden = normalizedValue === 0 && !isActive;
  progressTrack.setAttribute('aria-valuenow', String(normalizedValue));
  progressFill.style.width = `${normalizedValue}%`;
  progressFill.classList.toggle('active', isActive && normalizedValue < 100);
}

function resetProgress() {
  setProgress(0, false);
}

function resetEngineReport() {
  engineReport.hidden = true;
  engineReportList.replaceChildren();
}

function showEngineReport(detail) {
  const entries = String(detail || '')
    .split(';')
    .map((entry) => entry.trim())
    .filter(Boolean);

  engineReportList.replaceChildren();
  for (const entry of entries) {
    const item = document.createElement('li');
    item.textContent = entry;
    engineReportList.appendChild(item);
  }
  engineReport.hidden = entries.length === 0;
  engineReport.open = entries.length > 0;
}

function describeSelectedFile() {
  if (!selectedFile) {
    return '';
  }

  const keyText = originalKey
    ? `Original key: ${originalKey}.`
    : isPdfPath(selectedFile)
      ? 'Original key: will be detected after PDF conversion.'
      : 'Original key: could not be detected yet.';

  return `Detected file type: ${selectedFileType || 'Supported file'}. ${keyText}`;
}

function getFriendlyErrorMessage(error, fallback) {
  const message = error?.message || fallback;
  const remotePrefix = /^Error invoking remote method '[^']+': Error: /;

  if (remotePrefix.test(message)) {
    return message.replace(remotePrefix, '');
  }

  return message;
}

function isValidInputPath(filePath) {
  return /\.(musicxml|xml|mxl|pdf)$/i.test(filePath || '');
}

function isPdfPath(filePath) {
  return /\.pdf$/i.test(filePath || '');
}

function isPdfOutput() {
  return outputFormatSelect.value === 'pdf';
}

function setBusy(isBusy) {
  chooseFileButton.disabled = isBusy;
  shiftButton.disabled = isBusy;
  chooseAudiverisButton.disabled = isBusy;
  testAudiverisButton.disabled = isBusy;
  findToolsButton.disabled = isBusy;
  saveSettingsButton.disabled = isBusy;
  chooseMuseScoreButton.disabled = isBusy;
  testMuseScoreButton.disabled = isBusy;
  shiftButton.textContent = isBusy ? 'Shifting Key...' : 'Shift Key';
}

function updateProgressForStage(stage) {
  const value = STAGE_PROGRESS[stage?.name] || 12;
  setProgress(value, stage?.name !== 'Complete');
}

function updateModeText() {
  const pdfInvolved = isPdfPath(selectedFile);
  const pdfOutput = isPdfOutput();
  pdfNote.hidden = !pdfInvolved && !pdfOutput;
  if (pdfInvolved && pdfOutput) {
    pdfNote.textContent = 'PDF import uses Audiveris. PDF saving uses MuseScore Studio in the background.';
  } else if (pdfInvolved) {
    pdfNote.textContent = 'PDF import uses the Audiveris OMR engine. Configure it in Settings before importing PDFs.';
  } else if (pdfOutput) {
    pdfNote.textContent = 'PDF saving uses MuseScore Studio in the background.';
  }
}

function formatStageStatus(stage) {
  const detail = stage?.detail ? ` ${stage.detail}` : '';
  switch (stage?.name) {
    case 'Loading file':
      return 'Loading file...';
    case 'Converting PDF to MusicXML':
      return 'Converting PDF to MusicXML...';
    case 'Cleaning export layout':
      return 'Cleaning up the imported layout...';
    case 'Detecting key':
      return stage.detail ? `Detecting key... ${stage.detail}` : 'Detecting key...';
    case 'Transposing':
      return 'Transposing...';
    case 'Validation report':
      return stage.detail ? `Validation report: ${stage.detail}` : 'Validation report complete.';
    case 'Validate Output':
      return stage.detail ? `Validate Output: ${stage.detail}` : 'Output validation complete.';
    case 'Engine report':
      return stage.detail ? `Engines used: ${stage.detail}` : 'Engine report complete.';
    case 'Exporting output':
      return 'Exporting output...';
    case 'Complete':
      return `Complete.${detail}`;
    default:
      return stage?.name || 'Processing...';
  }
}

function getMissingToolMessage() {
  if (isPdfPath(selectedFile) && !toolSettings.audiverisPath) {
    return 'PDF import requires the Audiveris OMR engine. Please configure it in Settings.';
  }

  return '';
}

chooseFileButton.addEventListener('click', async () => {
  try {
    setStatus('');
    resetEngineReport();
    const fileInfo = await window.newKeyScores.selectFile();
    if (!fileInfo) {
      return;
    }

    const filePath = fileInfo.filePath || fileInfo;
    if (!isValidInputPath(filePath)) {
      selectedFile = '';
      selectedFileType = '';
      originalKey = '';
      fileInput.value = '';
      setStatus('Please choose a .musicxml, .xml, .mxl, or .pdf file.', 'error');
      return;
    }

    selectedFile = filePath;
    selectedFileType = fileInfo.fileType || '';
    originalKey = fileInfo.originalKey || '';
    fileInput.value = filePath;
    updateModeText();

    const fileDescription = describeSelectedFile();
    const missingToolMessage = getMissingToolMessage();
    if (missingToolMessage) {
      setStatus(`${fileDescription} ${missingToolMessage}`, 'error');
      return;
    }

    setStatus(isPdfPath(filePath)
      ? `${fileDescription} Audiveris will convert it before transposition.`
      : `${fileDescription} Ready to shift this MusicXML file.`);
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'The file could not be selected.'), 'error');
  }
});

shiftButton.addEventListener('click', async () => {
  if (!selectedFile || !isValidInputPath(selectedFile)) {
    setStatus('Choose a .musicxml, .xml, .mxl, or .pdf file first.', 'error');
    return;
  }

  const missingToolMessage = getMissingToolMessage();
  if (missingToolMessage) {
    setStatus(missingToolMessage, 'error');
    return;
  }

  try {
    setBusy(true);
    resetEngineReport();
    setProgress(5, true);
    setStatus('Loading file...');
    const result = await window.newKeyScores.transposeFile({
      inputPath: selectedFile,
      targetKey: targetKeySelect.value,
      outputFormat: outputFormatSelect.value
    });

    if (result?.canceled) {
      setStatus('Save canceled. No new file was created.');
      return;
    }

    const outputName = result.outputFormat === 'pdf' ? 'PDF' : 'MusicXML';
    setStatus(`Saved transposed ${outputName} to ${result.outputPath}`, 'success');
    showEngineReport(result.engineReport);
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'The file could not be processed.'), 'error');
    resetProgress();
  } finally {
    setBusy(false);
  }
});

outputFormatSelect.addEventListener('change', updateModeText);

async function loadSettings() {
  try {
    const settings = await window.newKeyScores.getSettings();
    toolSettings = {
      audiverisPath: settings?.audiverisPath || '',
      musescorePath: settings?.musescorePath || '',
      cleanExportLayout: settings?.cleanExportLayout !== false
    };
    audiverisPathInput.value = toolSettings.audiverisPath;
    musescorePathInput.value = toolSettings.musescorePath;
    cleanExportLayoutInput.checked = toolSettings.cleanExportLayout;
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'Settings could not be loaded.'), 'error');
  }
}

async function saveSettings(showConfirmation = true) {
  try {
    toolSettings = await window.newKeyScores.saveSettings({
      audiverisPath: audiverisPathInput.value,
      musescorePath: musescorePathInput.value,
      cleanExportLayout: cleanExportLayoutInput.checked
    });
    audiverisPathInput.value = toolSettings.audiverisPath;
    musescorePathInput.value = toolSettings.musescorePath;
    cleanExportLayoutInput.checked = toolSettings.cleanExportLayout;

    if (showConfirmation) {
      setStatus('Tool settings saved.', 'success');
    }
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'Settings could not be saved.'), 'error');
  }
}

async function chooseToolPath(tool, input) {
  try {
    const filePath = await window.newKeyScores.chooseToolPath({ tool });
    if (!filePath) {
      return;
    }

    input.value = filePath;
    await saveSettings(false);
    setStatus(`${tool === 'musescore' ? 'MuseScore' : 'Audiveris'} path saved.`, 'success');
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'Tool path could not be selected.'), 'error');
  }
}

async function testToolPath(tool, input) {
  try {
    setBusy(true);
    setStatus(`Testing ${tool === 'musescore' ? 'MuseScore' : 'Audiveris'} path...`);
    const message = await window.newKeyScores.testToolPath({
      tool,
      path: input.value
    });
    await loadSettings();
    setStatus(message.message || 'Tool validation finished.', message.ok ? 'success' : 'error');
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'Tool path could not be tested.'), 'error');
  } finally {
    setBusy(false);
  }
}

async function findToolsAutomatically() {
  try {
    setBusy(true);
    setStatus('Searching common install locations and Start Menu shortcuts...');
    const result = await window.newKeyScores.findToolsAutomatically();
    toolSettings = result.savedSettings || toolSettings;
    audiverisPathInput.value = toolSettings.audiverisPath || '';
    musescorePathInput.value = toolSettings.musescorePath || '';
    cleanExportLayoutInput.checked = toolSettings.cleanExportLayout !== false;
    const foundAudiveris = result.audiverisPath ? `Audiveris found: ${result.audiverisPath}` : 'Audiveris not found.';
    const foundMuseScore = result.musescorePath ? `MuseScore found: ${result.musescorePath}` : 'MuseScore not found.';
    const hasAnyMissing = !result.audiverisPath || !result.musescorePath;
    setStatus(`${foundAudiveris} ${foundMuseScore}`, hasAnyMissing ? 'error' : 'success');
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'Automatic tool detection failed.'), 'error');
  } finally {
    setBusy(false);
  }
}

chooseAudiverisButton.addEventListener('click', () => chooseToolPath('audiveris', audiverisPathInput));
testAudiverisButton.addEventListener('click', () => testToolPath('audiveris', audiverisPathInput));
chooseMuseScoreButton.addEventListener('click', () => chooseToolPath('musescore', musescorePathInput));
testMuseScoreButton.addEventListener('click', () => testToolPath('musescore', musescorePathInput));
findToolsButton.addEventListener('click', findToolsAutomatically);
saveSettingsButton.addEventListener('click', () => saveSettings(true));

window.newKeyScores.onProcessingStage((stage) => {
  updateProgressForStage(stage);
  if (stage?.name === 'Engine report') {
    showEngineReport(stage.detail);
  }
  setStatus(formatStageStatus(stage), stage?.name === 'Complete' ? 'success' : 'neutral');
});

updateModeText();
loadSettings();
resetProgress();
resetEngineReport();
setStatus('Choose an input file to begin.');
