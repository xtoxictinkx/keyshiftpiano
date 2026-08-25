function appendEngineReport(report, detail) {
  return [report, detail]
    .map((value) => String(value || '').trim().replace(/;+$/, ''))
    .filter(Boolean)
    .join('; ');
}

module.exports = { appendEngineReport };
