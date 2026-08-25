const assert = require('node:assert/strict');
const test = require('node:test');

const { appendEngineReport } = require('../src/main/engineReport');

test('appends the desktop PDF writer to the Python engine report', () => {
  assert.equal(
    appendEngineReport(
      'Input reader: New Key Scores MusicXML reader; MusicXML output: New Key Scores direct MusicXML transposer/writer',
      'PDF writer: MuseScore Studio'
    ),
    'Input reader: New Key Scores MusicXML reader; MusicXML output: New Key Scores direct MusicXML transposer/writer; PDF writer: MuseScore Studio'
  );
});

test('handles a missing Python engine report without extra punctuation', () => {
  assert.equal(appendEngineReport('', 'PDF writer: MuseScore Studio'), 'PDF writer: MuseScore Studio');
});
