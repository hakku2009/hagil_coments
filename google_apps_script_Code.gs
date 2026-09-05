// Google Apps Script
// Google Sheets를 영구 저장소로 사용하는 버전입니다.
const SHEET_NAME = '평가내역';
const HEADERS = ['ID','시간','이벤트','평가 종류','평가한 학생 학번','평가한 학생 이름','평가 대상 학번','평가 대상 이름','점수','코멘트','답문'];

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.getRange(1,1,1,HEADERS.length).setValues([HEADERS]);
  }
  migrateHeaders_(sheet);
  return sheet;
}

function migrateHeaders_(sheet) {
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1,1,1,HEADERS.length).setValues([HEADERS]);
    return;
  }
  const oldHeaders = sheet.getRange(1,1,1,Math.max(sheet.getLastColumn(),1)).getValues()[0].map(String);
  const same = HEADERS.length === oldHeaders.length && HEADERS.every((h,i) => h === oldHeaders[i]);
  if (same) return;

  const values = sheet.getLastRow() > 1 ? sheet.getRange(2,1,sheet.getLastRow()-1,oldHeaders.length).getValues() : [];
  const index = {}; oldHeaders.forEach((h,i) => index[h]=i);
  const converted = values.map(row => HEADERS.map(h => index[h] !== undefined ? row[index[h]] : ''));
  sheet.clearContents();
  sheet.getRange(1,1,1,HEADERS.length).setValues([HEADERS]);
  if (converted.length) sheet.getRange(2,1,converted.length,HEADERS.length).setValues(converted);
}

function doGet(e) {
  const action = e && e.parameter ? e.parameter.action : '';
  if (action === 'feedbacks') {
    const sheet = getSheet_();
    const lastRow = sheet.getLastRow();
    const result = [];
    if (lastRow > 1) {
      const values = sheet.getRange(2,1,lastRow-1,HEADERS.length).getValues();
      values.forEach(row => {
        if (!row[0] || !row[4] || !row[6]) return;
        result.push({
          id: Number(row[0]) || null,
          created_at: row[1] instanceof Date ? row[1].toISOString() : String(row[1] || ''),
          event: String(row[2] || ''),
          evaluation_type: String(row[3] || 'peer'),
          sender_number: String(row[4] || ''),
          sender_name: String(row[5] || ''),
          target_number: String(row[6] || ''),
          target_name: String(row[7] || ''),
          score: Number(row[8]) || 0,
          content: String(row[9] || ''),
          reply: String(row[10] || '')
        });
      });
    }
    return ContentService.createTextOutput(JSON.stringify({ok:true, feedbacks:result}))
      .setMimeType(ContentService.MimeType.JSON);
  }
  return ContentService.createTextOutput('Google Sheets 연결 정상')
    .setMimeType(ContentService.MimeType.TEXT);
}

function findRowById_(sheet, id) {
  if (!id || sheet.getLastRow() < 2) return -1;
  const ids = sheet.getRange(2,1,sheet.getLastRow()-1,1).getValues();
  for (let i=0; i<ids.length; i++) if (String(ids[i][0]) === String(id)) return i + 2;
  return -1;
}

function doPost(e) {
  const data = JSON.parse((e.postData && e.postData.contents) || '{}');
  const sheet = getSheet_();

  if (data.event === 'reset_feedbacks') {
    if (sheet.getLastRow() > 1) sheet.getRange(2,1,sheet.getLastRow()-1,HEADERS.length).clearContent();
    return ContentService.createTextOutput(JSON.stringify({ok:true, reset:true}))
      .setMimeType(ContentService.MimeType.JSON);
  }

  if (data.event === 'reply') {
    const row = findRowById_(sheet, data.feedback_id);
    if (row > 0) sheet.getRange(row,11).setValue(data.reply || '');
    return ContentService.createTextOutput(JSON.stringify({ok:true, updated:row > 0}))
      .setMimeType(ContentService.MimeType.JSON);
  }

  if (data.event === 'feedback') {
    const values = [
      data.feedback_id || '', data.created_at || new Date().toISOString(), 'feedback',
      data.evaluation_type || 'peer', data.sender_number || '', data.sender_name || '',
      data.target_number || '', data.target_name || '', data.score || '', data.content || '', data.reply || ''
    ];
    const row = findRowById_(sheet, data.feedback_id);
    if (row > 0) sheet.getRange(row,1,1,HEADERS.length).setValues([values]);
    else sheet.appendRow(values);
  }

  return ContentService.createTextOutput(JSON.stringify({ok:true}))
    .setMimeType(ContentService.MimeType.JSON);
}
