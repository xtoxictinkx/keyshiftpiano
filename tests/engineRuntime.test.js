const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');

const { selectPackagedPythonEngine } = require('../src/main/engineRuntime');

test('development ignores a stale built transposer executable', () => {
  const resourcesPath = 'C:\\project\\resources';
  const expected = path.join(resourcesPath, 'python', 'transposer.exe');
  const fsModule = { existsSync: (candidate) => candidate === expected };

  assert.equal(
    selectPackagedPythonEngine({
      isPackaged: false,
      resourcesPath,
      fsModule
    }),
    null
  );
});

test('packaged app uses its signed Python runtime and bundled source', () => {
  const resourcesPath = 'C:\\app\\resources';
  const runtimeRoot = path.join(resourcesPath, 'python-runtime');
  const command = path.join(runtimeRoot, 'python.exe');
  const scriptPath = path.join(runtimeRoot, 'engine', 'python', 'transposer.py');
  const fsModule = { existsSync: (candidate) => candidate === command || candidate === scriptPath };

  assert.deepEqual(
    selectPackagedPythonEngine({
      isPackaged: true,
      resourcesPath,
      fsModule
    }),
    { command, scriptPath, pythonHome: runtimeRoot }
  );
});
