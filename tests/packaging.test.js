const assert = require('node:assert/strict');
const test = require('node:test');

const packageJson = require('../package.json');

test('application metadata uses the New Key Scores identity', () => {
  assert.equal(packageJson.name, 'new-key-scores');
  assert.equal(packageJson.author, 'New Key Scores');
  assert.equal(packageJson.build.appId, 'com.newkeyscores.app');
  assert.equal(packageJson.build.productName, 'New Key Scores');
});

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
  assert.equal(nsis.shortcutName, 'New Key Scores');
});
