const fs = require('fs');
const path = require('path');

function selectTransposerExecutable({
  isPackaged,
  resourcesPath,
  fsModule = fs
}) {
  if (!isPackaged || !resourcesPath) {
    return null;
  }

  const executablePath = path.join(
    resourcesPath,
    'python',
    'transposer.exe'
  );

  return fsModule.existsSync(executablePath) ? executablePath : null;
}

module.exports = {
  selectTransposerExecutable
};
