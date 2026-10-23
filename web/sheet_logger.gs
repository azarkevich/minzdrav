// Google Apps Script: receives rows from kb_web.py (sheet_log.py) and appends them to this spreadsheet.
// Setup (once): Extensions → Apps Script → paste this file → set TOKEN → Deploy → New deployment →
// type "Web app", Execute as: Me, Who has access: Anyone → copy the /exec URL into KB_SHEET_URL on Render,
// TOKEN into KB_SHEET_TOKEN. After editing the script: Deploy → Manage deployments → edit → New version.
const TOKEN = "замените-на-длинный-секрет";

function doPost(e) {
  const d = JSON.parse(e.postData.contents);
  if (d.token !== TOKEN) return reply({ error: "bad token" });
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sh = ss.getSheetByName(d.sheet) || ss.insertSheet(d.sheet);
    // header = column titles; a new field from the server becomes a new column
    let header = sh.getLastRow() ? sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0] : [];
    const missing = Object.keys(d.row).filter(k => header.indexOf(k) < 0);
    if (missing.length) {
      header = header.concat(missing);
      sh.getRange(1, 1, 1, header.length).setValues([header]).setFontWeight("bold");
      sh.setFrozenRows(1);
    }
    sh.appendRow(header.map(k => (k in d.row ? d.row[k] : "")));
  } finally {
    lock.releaseLock();
  }
  return reply({ ok: true });
}

function reply(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
