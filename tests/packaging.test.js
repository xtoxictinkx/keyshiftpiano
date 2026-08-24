const assert = require('node:assert/strict');
const test = require('node:test');

const packageJson = require('../package.json');

test('Windows installer is fixed to a non-elevated per-user installation', () => {
  const nsis = packageJson.build.nsis;

  assert.equal(nsis.oneClick, true);
  assert.equal(nsis.perMachine, false);
  assert.equal(nsis.allowElevation, false);
  assert.equal(nsis.allowToChangeInstallationDirectory, false);
  assert.equal(nsis.packElevateHelper, false);
  assert.equal(nsis.include, 'scripts/installer.nsh');
  assert.equal(nsis.createDesktopShortcut, 'always');
  assert.equal(nsis.createStartMenuShortcut, true);
  assert.equal(nsis.shortcutName, 'Key Shift Piano');
});
