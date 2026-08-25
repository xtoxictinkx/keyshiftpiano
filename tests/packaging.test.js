const assert = require('node:assert/strict');
const test = require('node:test');

const packageJson = require('../package.json');

test('application metadata uses the New Key Scores identity', () => {
  assert.equal(packageJson.name, 'new-key-scores');
  assert.equal(packageJson.version, '0.3.0-alpha.2');
  assert.equal(packageJson.author, 'New Key Scores');
  assert.equal(packageJson.build.appId, 'com.newkeyscores.app');
  assert.equal(packageJson.build.productName, 'New Key Scores');
  assert.deepEqual(packageJson.build.extraResources, [
    { from: 'python/runtime', to: 'python-runtime' }
  ]);
});

test('personal Windows alpha is an unsigned portable ZIP', () => {
  assert.equal(packageJson.build.win.signExecutable, false);
  assert.equal(packageJson.build.win.signAndEditExecutable, undefined);
  assert.equal(packageJson.build.win.target, 'zip');
  assert.equal(packageJson.build.nsis, undefined);
});
