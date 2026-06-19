const fileInput = document.querySelector('#file-path');
const chooseFileButton = document.querySelector('#choose-file');
const shiftButton = document.querySelector('#shift-key');
const targetKeySelect = document.querySelector('#target-key');
const outputFormatSelect = document.querySelector('#output-format');
const pdfNote = document.querySelector('#pdf-note');
const statusBox = document.querySelector('#status');
const progressWrap = document.querySelector('#progress-wrap');
const progressTrack = document.querySelector('.progress-track');
const progressFill = document.querySelector('#progress-fill');
const audiverisPathInput = document.querySelector('#audiveris-path');
const musescorePathInput = document.querySelector('#musescore-path');
const chooseAudiverisButton = document.querySelector('#choose-audiveris');
const chooseMuseScoreButton = document.querySelector('#choose-musescore');
const testAudiverisButton = document.querySelector('#test-audiveris');
const testMuseScoreButton = document.querySelector('#test-musescore');
const findToolsButton = document.querySelector('#find-tools');
const saveSettingsButton = document.querySelector('#save-settings');

let selectedFile = '';
let selectedFileType = '';
let originalKey = '';
let toolSettings = {
  audiverisPath: '',
  musescorePath: ''
};

const STAGE_PROGRESS = {
  'Loading file': 8,
  'Converting PDF to MusicXML': 28,
  'Detecting key': 45,
  Transposing: 68,
  'Validation report': 78,
  'Validate Output': 82,
  'Exporting output': 86,
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
  return /\.(musicxml|xml|pdf)$/i.test(filePath || '');
}

function isPdfPath(filePath) {
  return /\.pdf$/i.test(filePath || '');
}

function setBusy(isBusy) {
  chooseFileButton.disabled = isBusy;
  shiftButton.disabled = isBusy;
  chooseAudiverisButton.disabled = isBusy;
  chooseMuseScoreButton.disabled = isBusy;
  testAudiverisButton.disabled = isBusy;
  testMuseScoreButton.disabled = isBusy;
  findToolsButton.disabled = isBusy;
  saveSettingsButton.disabled = isBusy;
  shiftButton.textContent = isBusy ? 'Shifting Key...' : 'Shift Key';
}

function updateProgressForStage(stage) {
  const value = STAGE_PROGRESS[stage?.name] || 12;
  setProgress(value, stage?.name !== 'Complete');
}

function updateModeText() {
  const pdfInvolved = isPdfPath(selectedFile) || outputFormatSelect.value === 'pdf';
  pdfNote.hidden = !pdfInvolved;
}

function formatStageStatus(stage) {
  const detail = stage?.detail ? ` ${stage.detail}` : '';
  switch (stage?.name) {
    case 'Loading file':
      return 'Loading file...';
    case 'Converting PDF to MusicXML':
      return 'Converting PDF to MusicXML...';
    case 'Detecting key':
      return stage.detail ? `Detecting key... ${stage.detail}` : 'Detecting key...';
    case 'Transposing':
      return 'Transposing...';
    case 'Validation report':
      return stage.detail ? `Validation report: ${stage.detail}` : 'Validation report complete.';
    case 'Validate Output':
      return stage.detail ? `Validate Output: ${stage.detail}` : 'Output validation complete.';
    case 'Exporting output':
      return 'Exporting output with MuseScore... This can take a minute for larger scores.';
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

  if (outputFormatSelect.value === 'pdf' && !toolSettings.musescorePath) {
    return 'PDF export requires MuseScore.';
  }

  return '';
}

chooseFileButton.addEventListener('click', async () => {
  try {
    setStatus('');
    const fileInfo = await window.keyShiftPiano.selectFile();
    if (!fileInfo) {
      return;
    }

    const filePath = fileInfo.filePath || fileInfo;
    if (!isValidInputPath(filePath)) {
      selectedFile = '';
      selectedFileType = '';
      originalKey = '';
      fileInput.value = '';
      setStatus('Please choose a .musicxml, .xml, or .pdf file.', 'error');
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
    setStatus('Choose a .musicxml, .xml, or .pdf file first.', 'error');
    return;
  }

  const missingToolMessage = getMissingToolMessage();
  if (missingToolMessage) {
    setStatus(missingToolMessage, 'error');
    return;
  }

  try {
    setBusy(true);
    setProgress(5, true);
    setStatus('Loading file...');
    const result = await window.keyShiftPiano.transposeFile({
      inputPath: selectedFile,
      targetKey: targetKeySelect.value,
      outputFormat: outputFormatSelect.value
    });

    if (result?.canceled) {
      setStatus('Save canceled. No new file was created.');
      return;
    }

    const requestedPdf = outputFormatSelect.value === 'pdf';
    const receivedPdf = /\.pdf$/i.test(result.outputPath || '');
    if (requestedPdf && !receivedPdf) {
      setStatus(
        `Transposed MusicXML saved to ${result.outputPath}\nPDF export did not complete. You can open this MusicXML in MuseScore and export PDF manually.`,
        'success'
      );
      return;
    }

    setStatus(`Saved transposed ${requestedPdf ? 'PDF' : 'MusicXML'} to ${result.outputPath}`, 'success');
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
    const settings = await window.keyShiftPiano.getSettings();
    toolSettings = {
      audiverisPath: settings?.audiverisPath || '',
      musescorePath: settings?.musescorePath || ''
    };
    audiverisPathInput.value = toolSettings.audiverisPath;
    musescorePathInput.value = toolSettings.musescorePath;
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'Settings could not be loaded.'), 'error');
  }
}

async function saveSettings(showConfirmation = true) {
  try {
    toolSettings = await window.keyShiftPiano.saveSettings({
      audiverisPath: audiverisPathInput.value,
      musescorePath: musescorePathInput.value
    });
    audiverisPathInput.value = toolSettings.audiverisPath;
    musescorePathInput.value = toolSettings.musescorePath;

    if (showConfirmation) {
      setStatus('PDF tool settings saved.', 'success');
    }
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'Settings could not be saved.'), 'error');
  }
}

async function chooseToolPath(tool, input) {
  try {
    const filePath = await window.keyShiftPiano.chooseToolPath({ tool });
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
    const message = await window.keyShiftPiano.testToolPath({
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
    const result = await window.keyShiftPiano.findToolsAutomatically();
    toolSettings = result.savedSettings || toolSettings;
    audiverisPathInput.value = toolSettings.audiverisPath || '';
    musescorePathInput.value = toolSettings.musescorePath || '';
    const foundBoth = Boolean(result.audiverisPath && result.musescorePath);
    setStatus(result.summary || 'Tool search complete.', foundBoth ? 'success' : 'error');
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'Automatic tool detection failed.'), 'error');
  } finally {
    setBusy(false);
  }
}

chooseAudiverisButton.addEventListener('click', () => chooseToolPath('audiveris', audiverisPathInput));
chooseMuseScoreButton.addEventListener('click', () => chooseToolPath('musescore', musescorePathInput));
testAudiverisButton.addEventListener('click', () => testToolPath('audiveris', audiverisPathInput));
testMuseScoreButton.addEventListener('click', () => testToolPath('musescore', musescorePathInput));
findToolsButton.addEventListener('click', findToolsAutomatically);
saveSettingsButton.addEventListener('click', () => saveSettings(true));

window.keyShiftPiano.onProcessingStage((stage) => {
  updateProgressForStage(stage);
  setStatus(formatStageStatus(stage), stage?.name === 'Complete' ? 'success' : 'neutral');
});

updateModeText();
loadSettings();
resetProgress();
setStatus('Choose an input file to begin.');
