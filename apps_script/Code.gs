/**
 * Oslo Apartment Hunt — voting backend.
 *
 * Deploy this as an Apps Script Web App attached to a Google Sheet.
 * The Sheet stores one row per (finn_id, voter), upserted on each POST.
 *
 * Endpoints:
 *   GET  → returns { votes: [ {finn_id, voter, vote, note, updated_at}, ... ] }
 *   POST → body is a JSON-encoded { finn_id, voter, vote?, note? }.
 *          `vote` is "up" / "down" / "" (empty clears).
 *          If `vote` or `note` is omitted, the existing value is preserved.
 *
 * One-time setup:
 *   1. Run `setupSheet()` from the editor (creates the 'votes' sheet
 *      with headers if it doesn't exist).
 *   2. Deploy → New deployment → Web app, "Execute as: Me",
 *      "Who has access: Anyone". Copy the Web app URL.
 *
 * Browsers normally trigger a CORS preflight for application/json POSTs;
 * Apps Script Web Apps don't handle preflight cleanly. The client sends
 * `Content-Type: text/plain` with a JSON-stringified body to avoid that.
 */

const SHEET_NAME = "votes";
const HEADERS = ["finn_id", "voter", "vote", "note", "updated_at"];
const VALID_VOTERS = ["arnaud", "celine"];
const VALID_VOTES = ["up", "down", ""];


function doGet(e) {
  const sheet = _getOrCreateSheet();
  const data = sheet.getDataRange().getValues();
  const votes = [];
  if (data.length > 1) {
    const headers = data[0];
    for (let i = 1; i < data.length; i++) {
      const row = data[i];
      if (!row[0]) continue;  // skip blank rows
      const obj = {};
      headers.forEach(function(h, j) { obj[h] = row[j]; });
      votes.push(obj);
    }
  }
  return _json({ votes: votes });
}


function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return _json({ ok: false, error: "invalid JSON body" });
  }
  const finnId = String(body.finn_id || "").trim();
  const voter = String(body.voter || "").toLowerCase().trim();
  if (!finnId) return _json({ ok: false, error: "missing finn_id" });
  if (VALID_VOTERS.indexOf(voter) < 0) return _json({ ok: false, error: "invalid voter" });

  // `vote` / `note` are optional — when omitted, keep the existing value.
  const incomingVote = body.hasOwnProperty("vote") ? String(body.vote || "") : null;
  const incomingNote = body.hasOwnProperty("note") ? String(body.note || "") : null;

  if (incomingVote !== null && VALID_VOTES.indexOf(incomingVote) < 0) {
    return _json({ ok: false, error: "invalid vote value" });
  }

  const sheet = _getOrCreateSheet();
  const data = sheet.getDataRange().getValues();
  const now = new Date().toISOString();

  // Find existing row by (finn_id, voter)
  let rowIndex = -1;
  for (let i = 1; i < data.length; i++) {
    if (String(data[i][0]) === finnId && String(data[i][1]) === voter) {
      rowIndex = i + 1;  // 1-indexed sheet row
      break;
    }
  }

  let currentVote = "", currentNote = "";
  if (rowIndex > 0) {
    currentVote = String(data[rowIndex - 1][2] || "");
    currentNote = String(data[rowIndex - 1][3] || "");
  }
  const newVote = incomingVote === null ? currentVote : incomingVote;
  const newNote = incomingNote === null ? currentNote : incomingNote;

  if (rowIndex > 0) {
    sheet.getRange(rowIndex, 1, 1, HEADERS.length)
         .setValues([[finnId, voter, newVote, newNote, now]]);
  } else {
    sheet.appendRow([finnId, voter, newVote, newNote, now]);
  }

  return _json({ ok: true, finn_id: finnId, voter: voter, vote: newVote, note: newNote, updated_at: now });
}


/** One-time helper: ensures the 'votes' sheet exists with the expected headers. */
function setupSheet() {
  const sheet = _getOrCreateSheet();
  Logger.log("Sheet '" + SHEET_NAME + "' ready with headers: " + HEADERS.join(", "));
}


function _getOrCreateSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
  }
  // Ensure headers exist on row 1.
  const firstRow = sheet.getRange(1, 1, 1, HEADERS.length).getValues()[0];
  const headersOk = HEADERS.every(function(h, i) { return firstRow[i] === h; });
  if (!headersOk) {
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}


function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
