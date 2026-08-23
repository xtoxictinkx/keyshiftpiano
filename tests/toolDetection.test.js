const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');

const {
  classifyExecutable,
  detectToolPaths,
  getKnownExecutableCandidates
} = require('../src/main/toolDetection');

function createMockFs(existingFiles, directoryEntries = {}) {
  const files = new Set(existingFiles);

  return {
    statSync(filePath) {
      if (!files.has(filePath)) {
        throw new Error(`Missing: ${filePath}`);
      }

      return { isFile: () => true };
    },
    readdirSync(dirPath) {
      return directoryEntries[dirPath] || [];
    }
  };
}

function dirent(name, type) {
  return {
    name,
    isFile: () => type === 'file',
    isDirectory: () => type === 'dir'
  };
}

test('known Windows install paths are included', () => {
  const candidates = getKnownExecutableCandidates({
    ProgramFiles: 'C:\\Program Files'
  });

  assert.ok(candidates.includes('C:\\Program Files\\Audiveris\\Audiveris.exe'));
  assert.ok(candidates.includes('C:\\Program Files\\MuseScore 4\\bin\\MuseScore4.exe'));
});

test('detectToolPaths finds known Audiveris path', async () => {
  const audiverisPath = 'C:\\Program Files\\Audiveris\\Audiveris.exe';
  const fsModule = createMockFs([audiverisPath]);

  const result = await detectToolPaths({
    fsModule,
    env: { ProgramFiles: 'C:\\Program Files' },
    programRoots: ['C:\\Program Files'],
    startMenuRoots: []
  });

  assert.equal(result.audiverisPath, audiverisPath);
});

test('detectToolPaths finds known MuseScore path', async () => {
  const musescorePath = 'C:\\Program Files\\MuseScore 4\\bin\\MuseScore4.exe';
  const fsModule = createMockFs([musescorePath]);

  const result = await detectToolPaths({
    fsModule,
    env: { ProgramFiles: 'C:\\Program Files' },
    programRoots: ['C:\\Program Files'],
    startMenuRoots: []
  });

  assert.equal(result.musescorePath, musescorePath);
});

test('detectToolPaths resolves Start Menu shortcut targets', async () => {
  const startRoot = 'C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs';
  const shortcutPath = path.join(startRoot, 'Audiveris.lnk');
  const audiverisPath = 'C:\\Program Files\\Audiveris\\Audiveris.exe';
  const fsModule = createMockFs(
    [audiverisPath],
    {
      [startRoot]: [dirent('Audiveris.lnk', 'file')]
    }
  );

  const result = await detectToolPaths({
    fsModule,
    env: { ProgramFiles: 'C:\\Program Files' },
    programRoots: [],
    startMenuRoots: [startRoot],
    resolveShortcutTarget: async (shortcut) => shortcut === shortcutPath ? audiverisPath : ''
  });

  assert.equal(result.audiverisPath, audiverisPath);
});

test('classifyExecutable recognizes supported executable names', () => {
  assert.equal(classifyExecutable('C:\\Program Files\\Audiveris\\Audiveris.exe'), 'audiverisPath');
  assert.equal(classifyExecutable('C:\\Program Files\\MuseScore 4\\bin\\MuseScore4.exe'), 'musescorePath');
  assert.equal(classifyExecutable('C:\\Program Files\\MuseScore Studio\\bin\\MuseScoreStudio.exe'), 'musescorePath');
  assert.equal(classifyExecutable('C:\\Other\\Installer.exe'), '');
});
