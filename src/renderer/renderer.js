const fileInput = document.querySelector('#file-path');
const chooseFileButton = document.querySelector('#choose-file');
const shiftButton = document.querySelector('#shift-key');
const targetKeySelect = document.querySelector('#target-key');
const outputFormatSelect = document.querySelector('#output-format');
const pdfNote = document.querySelector('#pdf-note');
const statusBox = document.querySelector('#status');
const audiverisPathInput = document.querySelector('#audiveris-path');
const musescorePathInput = document.querySelector('#musescore-path');
const chooseAudiverisButton = document.querySelector('#choose-audiveris');
const chooseMuseScoreButton = document.querySelector('#choose-musescore');
const saveSettingsButton = document.querySelector('#save-settings');

let selectedFile = '';
let toolSettings = {
  audiverisPath: '',
  musescorePath: ''
};

function setStatus(message, type = 'neutral') {
  statusBox.textContent = message;
  statusBox.className = `status ${type === 'neutral' ? '' : type}`.trim();
}

function getFriendlyErrorMessage(error, fallback) {
  const message = error?.message || fallback;
  const remotePrefix = "Error invoking remote method 'transpose-file': Error: ";

  if (message.startsWith(remotePrefix)) {
    return message.slice(remotePrefix.length);
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
  saveSettingsButton.disabled = isBusy;
  shiftButton.textContent = isBusy ? 'Shifting Key...' : 'Shift Key';
}

function updateModeText() {
  const pdfInvolved = isPdfPath(selectedFile) || outputFormatSelect.value === 'pdf';
  pdfNote.hidden = !pdfInvolved;
}

function getMissingToolMessage() {
  if (isPdfPath(selectedFile) && !toolSettings.audiverisPath) {
    return 'PDF import requires the Audiveris OMR engine. Please configure it in Settings.';
  }

  if (outputFormatSelect.value === 'pdf' && !toolSettings.musescorePath) {
    return 'PDF export needs MuseScore installed locally. For now, choose MusicXML output instead.';
  }

  return '';
}

chooseFileButton.addEventListener('click', async () => {
  try {
    setStatus('');
    const filePath = await window.keyShiftPiano.selectFile();
    if (!filePath) {
      return;
    }

    if (!isValidInputPath(filePath)) {
      selectedFile = '';
      fileInput.value = '';
      setStatus('Please choose a .musicxml, .xml, or .pdf file.', 'error');
      return;
    }

    selectedFile = filePath;
    fileInput.value = filePath;
    updateModeText();

    const missingToolMessage = getMissingToolMessage();
    if (missingToolMessage) {
      setStatus(missingToolMessage, 'error');
      return;
    }

    setStatus(isPdfPath(filePath)
      ? 'PDF selected. Audiveris will convert it before transposition.'
      : 'Ready to shift this MusicXML file.');
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
    setStatus('Preparing the transposed file...');
    const result = await window.keyShiftPiano.transposeFile({
      inputPath: selectedFile,
      targetKey: targetKeySelect.value,
      outputFormat: outputFormatSelect.value
    });

    if (result?.canceled) {
      setStatus('Save canceled. No new file was created.');
      return;
    }

    setStatus(`Saved transposed ${outputFormatSelect.value === 'pdf' ? 'PDF' : 'MusicXML'} to ${result.outputPath}`, 'success');
  } catch (error) {
    setStatus(getFriendlyErrorMessage(error, 'The file could not be processed.'), 'error');
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
      audiverisPath: audiverisPathInput.value.trim(),
      musescorePath: musescorePathInput.value.trim()
    });

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

chooseAudiverisButton.addEventListener('click', () => chooseToolPath('audiveris', audiverisPathInput));
chooseMuseScoreButton.addEventListener('click', () => chooseToolPath('musescore', musescorePathInput));
saveSettingsButton.addEventListener('click', () => saveSettings(true));

updateModeText();
loadSettings();
setStatus('Choose an input file to begin.');
