const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const projectRoot = path.resolve(__dirname, '..');
const candidates = [
  process.env.NEW_KEY_SCORES_PYTHON || process.env.KEY_SHIFT_PYTHON
    ? {
      command: process.env.NEW_KEY_SCORES_PYTHON || process.env.KEY_SHIFT_PYTHON,
      prefix: []
    }
    : null,
  process.platform === 'win32'
    ? {
      command: path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
      prefix: []
    }
    : {
      command: path.join(projectRoot, '.venv', 'bin', 'python'),
      prefix: []
    },
  { command: process.platform === 'win32' ? 'python' : 'python3', prefix: [] },
  process.platform === 'win32'
    ? { command: 'py', prefix: ['-3'] }
    : null
].filter(Boolean);

function commandExists(candidate) {
  if (path.isAbsolute(candidate.command) && !fs.existsSync(candidate.command)) {
    return false;
  }
  const result = spawnSync(
    candidate.command,
    [...candidate.prefix, '-c', 'import PyInstaller'],
    {
      cwd: projectRoot,
      encoding: 'utf8',
      windowsHide: true
    }
  );
  return result.status === 0;
}

const python = candidates.find(commandExists);
if (!python) {
  console.error(
    'PyInstaller is not available. Install it with "python -m pip install pyinstaller" ' +
    'or set NEW_KEY_SCORES_PYTHON to a Python executable that includes PyInstaller.'
  );
  process.exit(1);
}

const buildRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'new-key-scores-engine-'));
let status = 1;

try {
  const result = spawnSync(
    python.command,
    [
      ...python.prefix,
      '-m',
      'PyInstaller',
      '--noconfirm',
      '--onefile',
      '--name',
      'transposer',
      '--distpath',
      'python/dist',
      '--workpath',
      path.join(buildRoot, 'work'),
      '--specpath',
      path.join(buildRoot, 'spec'),
      'python/transposer.py'
    ],
    {
      cwd: projectRoot,
      env: {
        ...process.env,
        MPLCONFIGDIR: path.join(buildRoot, 'matplotlib')
      },
      stdio: 'inherit',
      windowsHide: true
    }
  );
  status = result.status ?? 1;
} finally {
  fs.rmSync(buildRoot, { recursive: true, force: true });
}

process.exit(status);
