const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const projectRoot = path.resolve(__dirname, '..');
const runtimeRoot = path.join(projectRoot, 'python', 'runtime');
const candidates = [
  process.env.NEW_KEY_SCORES_PYTHON || process.env.KEY_SHIFT_PYTHON
    ? process.env.NEW_KEY_SCORES_PYTHON || process.env.KEY_SHIFT_PYTHON
    : null,
  process.platform === 'win32'
    ? path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
    : path.join(projectRoot, '.venv', 'bin', 'python'),
  process.platform === 'win32' ? 'python' : 'python3'
].filter(Boolean);

function inspectPython(command) {
  if (path.isAbsolute(command) && !fs.existsSync(command)) {
    return null;
  }
  const code = [
    'import json, site, sys',
    'import music21, pdfplumber, pypdf, reportlab',
    'packages = next(path for path in site.getsitepackages() if path.lower().endswith("site-packages"))',
    'print(json.dumps({"base": sys.base_prefix, "site": packages}))'
  ].join('; ');
  const result = spawnSync(command, ['-c', code], {
    cwd: projectRoot,
    encoding: 'utf8',
    windowsHide: true
  });
  if (result.status !== 0) {
    return null;
  }
  try {
    return { command, ...JSON.parse(result.stdout.trim()) };
  } catch {
    return null;
  }
}

function assertGeneratedPath(targetPath) {
  const relative = path.relative(projectRoot, targetPath);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error(`Refusing to replace unexpected runtime path: ${targetPath}`);
  }
}

function copyDirectory(source, destination, filter = () => true) {
  fs.cpSync(source, destination, {
    recursive: true,
    force: true,
    filter: (candidate) => {
      const name = path.basename(candidate).toLowerCase();
      if (name === '__pycache__' || name.endsWith('.pyc') || name.endsWith('.pyo')) {
        return false;
      }
      return filter(candidate);
    }
  });
}

const python = candidates.map(inspectPython).find(Boolean);
if (!python) {
  console.error(
    'A Python environment containing music21, pdfplumber, pypdf, and reportlab is required. ' +
    'Install requirements.txt or set NEW_KEY_SCORES_PYTHON.'
  );
  process.exit(1);
}

assertGeneratedPath(runtimeRoot);
fs.rmSync(runtimeRoot, { recursive: true, force: true });
fs.mkdirSync(runtimeRoot, { recursive: true });

const baseFiles = [
  'python.exe',
  'python3.dll',
  'vcruntime140.dll',
  'vcruntime140_1.dll',
  'LICENSE.txt'
];
const versionedDll = fs.readdirSync(python.base).find((name) => /^python\d+\.dll$/i.test(name));
if (versionedDll) {
  baseFiles.push(versionedDll);
}
for (const name of new Set(baseFiles)) {
  const source = path.join(python.base, name);
  if (fs.existsSync(source) && fs.statSync(source).isFile()) {
    fs.copyFileSync(source, path.join(runtimeRoot, name));
  }
}

copyDirectory(path.join(python.base, 'DLLs'), path.join(runtimeRoot, 'DLLs'));
copyDirectory(
  path.join(python.base, 'Lib'),
  path.join(runtimeRoot, 'Lib'),
  (candidate) => {
    const relative = path.relative(path.join(python.base, 'Lib'), candidate).toLowerCase();
    return !(
      relative === 'site-packages' ||
      relative.startsWith(`site-packages${path.sep}`) ||
      relative === 'test' ||
      relative.startsWith(`test${path.sep}`) ||
      relative === 'idlelib' ||
      relative.startsWith(`idlelib${path.sep}`) ||
      relative === 'ensurepip' ||
      relative.startsWith(`ensurepip${path.sep}`)
    );
  }
);
copyDirectory(
  python.site,
  path.join(runtimeRoot, 'Lib', 'site-packages'),
  (candidate) => {
    const relative = path.relative(python.site, candidate).toLowerCase();
    const firstPart = relative.split(path.sep)[0];
    return ![
      '_pyinstaller_hooks_contrib',
      'pip',
      'pyinstaller',
      'setuptools',
      'wheel'
    ].some((name) => firstPart === name || firstPart.startsWith(`${name}-`));
  }
);

const enginePackage = path.join(runtimeRoot, 'engine', 'python');
fs.mkdirSync(enginePackage, { recursive: true });
for (const source of fs.readdirSync(path.join(projectRoot, 'python'))) {
  if (!source.endsWith('.py')) {
    continue;
  }
  fs.copyFileSync(path.join(projectRoot, 'python', source), path.join(enginePackage, source));
}

const runtimePython = path.join(runtimeRoot, 'python.exe');
const transposer = path.join(enginePackage, 'transposer.py');
const verificationCode = [
  'import sys',
  `sys.path.insert(0, ${JSON.stringify(path.join(runtimeRoot, 'engine'))})`,
  'import python.chord_chart, python.pipeline, python.transposer',
  'print("portable engine ready")'
].join('; ');
const verification = spawnSync(runtimePython, ['-c', verificationCode], {
  cwd: projectRoot,
  encoding: 'utf8',
  windowsHide: true,
  env: { ...process.env, PYTHONHOME: runtimeRoot }
});
if (verification.status !== 0) {
  console.error(verification.stderr || verification.stdout || 'Portable engine verification failed.');
  process.exit(verification.status || 1);
}

if (!fs.existsSync(transposer)) {
  console.error('Portable engine source is missing transposer.py.');
  process.exit(1);
}

console.log(`Portable signed-Python engine staged at ${runtimeRoot}.`);
