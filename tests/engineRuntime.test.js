const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');

const { selectTransposerExecutable } = require('../src/main/engineRuntime');

test('development ignores a stale built transposer executable', () => {
  const resourcesPath = 'C:\\project\\resources';
  const expected = path.join(resourcesPath, 'python', 'transposer.exe');
  const fsModule = { existsSync: (candidate) => candidate === expected };

  assert.equal(
    selectTransposerExecutable({
      isPackaged: false,
      resourcesPath,
      fsModule
    }),
    null
  );
});

test('packaged app uses its bundled transposer executable', () => {
  const resourcesPath = 'C:\\app\\resources';
  const expected = path.join(resourcesPath, 'python', 'transposer.exe');
  const fsModule = { existsSync: (candidate) => candidate === expected };

  assert.equal(
    selectTransposerExecutable({
      isPackaged: true,
      resourcesPath,
      fsModule
    }),
    expected
  );
});
