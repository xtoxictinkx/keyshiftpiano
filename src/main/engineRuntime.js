const fs = require('fs');
const path = require('path');

function selectPackagedPythonEngine({
  isPackaged,
  resourcesPath,
  fsModule = fs
}) {
  if (!isPackaged || !resourcesPath) {
    return null;
  }

  const runtimeRoot = path.join(resourcesPath, 'python-runtime');
  const command = path.join(runtimeRoot, 'python.exe');
  const scriptPath = path.join(runtimeRoot, 'engine', 'python', 'transposer.py');

  return fsModule.existsSync(command) && fsModule.existsSync(scriptPath)
    ? { command, scriptPath, pythonHome: runtimeRoot }
    : null;
}

module.exports = {
  selectPackagedPythonEngine
};
