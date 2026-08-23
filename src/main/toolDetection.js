const path = require('path');

const AUDIVERIS_NAMES = ['Audiveris.exe'];
const MUSESCORE_NAMES = ['MuseScore4.exe', 'MuseScoreStudio.exe'];

function unique(items) {
  return [...new Set(items.filter(Boolean))];
}

function getProgramRoots(env = process.env) {
  return unique([
    env.ProgramFiles,
    env['ProgramFiles(x86)'],
    env.ProgramW6432
  ]);
}

function getStartMenuRoots(env = process.env) {
  return unique([
    env.ProgramData ? path.join(env.ProgramData, 'Microsoft', 'Windows', 'Start Menu', 'Programs') : '',
    env.APPDATA ? path.join(env.APPDATA, 'Microsoft', 'Windows', 'Start Menu', 'Programs') : ''
  ]);
}

function getKnownExecutableCandidates(env = process.env) {
  const roots = getProgramRoots(env);
  const candidates = [];

  for (const root of roots) {
    candidates.push(path.join(root, 'Audiveris', 'Audiveris.exe'));
    candidates.push(path.join(root, 'MuseScore 4', 'bin', 'MuseScore4.exe'));
    candidates.push(path.join(root, 'MuseScore Studio', 'bin', 'MuseScoreStudio.exe'));
  }

  return unique(candidates);
}

function isExistingFile(fsModule, filePath) {
  try {
    return Boolean(filePath) && fsModule.statSync(filePath).isFile();
  } catch {
    return false;
  }
}

function findFirstExisting(fsModule, candidates) {
  return candidates.find((candidate) => isExistingFile(fsModule, candidate)) || '';
}

function findExecutablesRecursively(fsModule, rootDirs, executableNames, options = {}) {
  const maxDepth = options.maxDepth ?? 5;
  const matches = [];
  const targetNames = new Set(executableNames.map((name) => name.toLowerCase()));

  function visit(dirPath, depth) {
    if (!dirPath || depth > maxDepth) {
      return;
    }

    let entries = [];
    try {
      entries = fsModule.readdirSync(dirPath, { withFileTypes: true });
    } catch {
      return;
    }

    for (const entry of entries) {
      const entryPath = path.join(dirPath, entry.name);
      if (entry.isFile() && targetNames.has(entry.name.toLowerCase())) {
        matches.push(entryPath);
      } else if (entry.isDirectory()) {
        visit(entryPath, depth + 1);
      }
    }
  }

  for (const rootDir of rootDirs) {
    visit(rootDir, 0);
  }

  return unique(matches);
}

function findShortcutFilesByWalking(fsModule, rootDirs, options = {}) {
  const maxDepth = options.maxDepth ?? 6;
  const matches = [];

  function visit(dirPath, depth) {
    if (!dirPath || depth > maxDepth) {
      return;
    }

    let entries = [];
    try {
      entries = fsModule.readdirSync(dirPath, { withFileTypes: true });
    } catch {
      return;
    }

    for (const entry of entries) {
      const entryPath = path.join(dirPath, entry.name);
      if (entry.isFile() && entry.name.toLowerCase().endsWith('.lnk')) {
        matches.push(entryPath);
      } else if (entry.isDirectory()) {
        visit(entryPath, depth + 1);
      }
    }
  }

  for (const rootDir of rootDirs) {
    visit(rootDir, 0);
  }

  return unique(matches);
}

function classifyExecutable(filePath) {
  const baseName = path.basename(filePath || '').toLowerCase();
  if (AUDIVERIS_NAMES.map((name) => name.toLowerCase()).includes(baseName)) {
    return 'audiverisPath';
  }

  if (MUSESCORE_NAMES.map((name) => name.toLowerCase()).includes(baseName)) {
    return 'musescorePath';
  }

  return '';
}

async function detectToolPaths(options) {
  const fsModule = options.fsModule;
  const env = options.env || process.env;
  const resolveShortcutTarget = options.resolveShortcutTarget || (async () => '');
  const programRoots = options.programRoots || getProgramRoots(env);
  const startMenuRoots = options.startMenuRoots || getStartMenuRoots(env);

  const result = {
    audiverisPath: '',
    musescorePath: '',
    checkedPaths: []
  };

  const knownCandidates = getKnownExecutableCandidates(env);
  result.checkedPaths.push(...knownCandidates);
  result.audiverisPath = findFirstExisting(
    fsModule,
    knownCandidates.filter((candidate) => classifyExecutable(candidate) === 'audiverisPath')
  );
  result.musescorePath = findFirstExisting(
    fsModule,
    knownCandidates.filter((candidate) => classifyExecutable(candidate) === 'musescorePath')
  );

  if (!result.audiverisPath || !result.musescorePath) {
    const recursiveCandidates = findExecutablesRecursively(
      fsModule,
      programRoots,
      [...AUDIVERIS_NAMES, ...MUSESCORE_NAMES]
    );
    result.checkedPaths.push(...recursiveCandidates);

    for (const candidate of recursiveCandidates) {
      const key = classifyExecutable(candidate);
      if (key && !result[key]) {
        result[key] = candidate;
      }
    }
  }

  if (!result.audiverisPath || !result.musescorePath) {
    const shortcuts = findShortcutFilesByWalking(fsModule, startMenuRoots);
    result.checkedPaths.push(...shortcuts);

    for (const shortcut of shortcuts) {
      const target = await resolveShortcutTarget(shortcut);
      result.checkedPaths.push(target);
      const key = classifyExecutable(target);
      if (key && !result[key] && isExistingFile(fsModule, target)) {
        result[key] = target;
      }
    }
  }

  result.checkedPaths = unique(result.checkedPaths);
  return result;
}

module.exports = {
  AUDIVERIS_NAMES,
  MUSESCORE_NAMES,
  classifyExecutable,
  detectToolPaths,
  findExecutablesRecursively,
  findFirstExisting,
  getKnownExecutableCandidates,
  getProgramRoots,
  getStartMenuRoots
};
