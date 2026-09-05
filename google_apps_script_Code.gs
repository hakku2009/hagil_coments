// Google Apps Script
// 이 스크립트는 Render 무료 인스턴스가 재시작되어도
// 학생/조/비밀번호/평가/답문을 Google Sheets에 보관하고 복구합니다.
//
// 배포:
// 1) Google 스프레드시트 > 확장 프로그램 > Apps Script
// 2) 아래 코드 전체 붙여넣기 > 저장
// 3) 배포 > 새 배포 > 웹 앱
//    실행 사용자: 나
//    액세스 권한: 모든 사용자
// 4) 생성된 /exec URL을 Render 환경변수 GOOGLE_SHEETS_WEBHOOK_URL에 입력

const FEEDBACK_SHEET = '평가내역';
const STUDENT_SHEET = '학생';
const SETTING_SHEET = '설정';

const FEEDBACK_HEADERS = ['ID','시간','이벤트','평가 종류','평가한 학생 학번','평가한 학생 이름','평가 대상 학번','평가 대상 이름','점수','코멘트','답문'];
const STUDENT_HEADERS = ['학번','이름','비밀번호','조'];
const SETTING_HEADERS = ['키','값'];

function ensureSheet_(name, headers) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    return sheet;
  }
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    return sheet;
  }

  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const existing = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
  const missing = headers.filter(h => existing.indexOf(h) < 0);
  if (missing.length) {
    sheet.getRange(1, existing.length + 1, 1, missing.length).setValues([missing]);
  }
  return sheet;
}

function headerMap_(sheet) {
  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
  const map = {};
  headers.forEach((h, i) => { if (h) map[h] = i + 1; });
  return map;
}

function value_(row, map, name) {
  return map[name] ? row[map[name] - 1] : '';
}

function readObjects_(sheet) {
  if (!sheet || sheet.getLastRow() < 2) return [];
  const map = headerMap_(sheet);
  const lastCol = sheet.getLastColumn();
  const values = sheet.getRange(2, 1, sheet.getLastRow() - 1, lastCol).getValues();
  return values.map((row, idx) => ({ row: idx + 2, data: row, map: map }));
}

function ensureLegacyFeedbackIds_(sheet) {
  const map = headerMap_(sheet);
  if (!map['ID'] || sheet.getLastRow() < 2) return;
  const items = readObjects_(sheet);
  items.forEach(item => {
    const id = value_(item.data, item.map, 'ID');
    if (!id) sheet.getRange(item.row, map['ID']).setValue('legacy-' + item.row);
  });
}

function findRow_(sheet, header, value) {
  if (!sheet || sheet.getLastRow() < 2) return -1;
  const map = headerMap_(sheet);
  if (!map[header]) return -1;
  const values = sheet.getRange(2, map[header], sheet.getLastRow() - 1, 1).getValues();
  for (let i = 0; i < values.length; i++) {
    if (String(values[i][0]) === String(value)) return i + 2;
  }
  return -1;
}

function doGet(e) {
  const action = e && e.parameter ? e.parameter.action : '';
  if (action === 'export') {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const feedbackSheet = ss.getSheetByName(FEEDBACK_SHEET);
    const studentSheet = ss.getSheetByName(STUDENT_SHEET);
    const settingSheet = ss.getSheetByName(SETTING_SHEET);

    const result = {
      ok: true,
      feedback_sheet_exists: !!feedbackSheet,
      students_sheet_exists: !!studentSheet,
      settings_sheet_exists: !!settingSheet,
      feedbacks: [],
      students: [],
      settings: {}
    };

    if (feedbackSheet) {
      // 구버전 평가내역 시트에도 ID/답문 등 새 열이 없으면 추가합니다.
      ensureSheet_(FEEDBACK_SHEET, FEEDBACK_HEADERS);
      ensureLegacyFeedbackIds_(feedbackSheet);
      const map = headerMap_(feedbackSheet);
      readObjects_(feedbackSheet).forEach(item => {
        const sender = String(value_(item.data, map, '평가한 학생 학번') || '');
        const target = String(value_(item.data, map, '평가 대상 학번') || '');
        if (!sender || !target) return;
        const rawId = value_(item.data, map, 'ID');
        result.feedbacks.push({
          id: String(rawId || ('legacy-' + item.row)),
          created_at: value_(item.data, map, '시간') instanceof Date ? value_(item.data, map, '시간').toISOString() : String(value_(item.data, map, '시간') || ''),
          event: String(value_(item.data, map, '이벤트') || 'feedback'),
          evaluation_type: String(value_(item.data, map, '평가 종류') || 'peer'),
          sender_number: sender,
          sender_name: String(value_(item.data, map, '평가한 학생 이름') || ''),
          target_number: target,
          target_name: String(value_(item.data, map, '평가 대상 이름') || ''),
          score: Number(value_(item.data, map, '점수')) || 0,
          content: String(value_(item.data, map, '코멘트') || ''),
          reply: String(value_(item.data, map, '답문') || '')
        });
      });
    }

    if (studentSheet) {
      const map = headerMap_(studentSheet);
      readObjects_(studentSheet).forEach(item => {
        const n = String(value_(item.data, map, '학번') || '');
        if (!n) return;
        result.students.push({
          student_number: n,
          name: String(value_(item.data, map, '이름') || n),
          password: String(value_(item.data, map, '비밀번호') || '1234'),
          group_name: String(value_(item.data, map, '조') || '')
        });
      });
    }

    if (settingSheet) {
      const map = headerMap_(settingSheet);
      readObjects_(settingSheet).forEach(item => {
        const key = String(value_(item.data, map, '키') || '');
        if (key) result.settings[key] = String(value_(item.data, map, '값') || '');
      });
    }

    return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
  }

  return ContentService.createTextOutput('Google Sheets 연결 정상').setMimeType(ContentService.MimeType.TEXT);
}

function upsertStudent_(data) {
  const sheet = ensureSheet_(STUDENT_SHEET, STUDENT_HEADERS);
  const map = headerMap_(sheet);
  const row = findRow_(sheet, '학번', data.student_number);
  const values = {
    '학번': data.student_number || '',
    '이름': data.name || '',
    '비밀번호': data.password || '1234',
    '조': data.group_name || ''
  };
  if (row > 0) {
    Object.keys(values).forEach(h => {
      if (map[h]) sheet.getRange(row, map[h]).setValue(values[h]);
    });
  } else {
    const out = new Array(sheet.getLastColumn()).fill('');
    Object.keys(values).forEach(h => {
      if (map[h]) out[map[h] - 1] = values[h];
    });
    sheet.appendRow(out);
  }
}

function upsertSetting_(key, value) {
  const sheet = ensureSheet_(SETTING_SHEET, SETTING_HEADERS);
  const row = findRow_(sheet, '키', key);
  if (row > 0) sheet.getRange(row, 2).setValue(value || '');
  else sheet.appendRow([key, value || '']);
}

function findFeedbackRow_(sheet, id, data) {
  let row = findRow_(sheet, 'ID', id);
  if (row > 0) return row;

  // 구버전 행에 ID가 없었던 경우 기존 평가와 매칭해서 중복 생성을 방지합니다.
  if (sheet.getLastRow() >= 2) {
    const map = headerMap_(sheet);
    const items = readObjects_(sheet);
    for (let i = 0; i < items.length; i++) {
      const r = items[i];
      if (String(value_(r.data, map, '평가한 학생 학번')) === String(data.sender_number || '') &&
          String(value_(r.data, map, '평가 대상 학번')) === String(data.target_number || '') &&
          String(value_(r.data, map, '평가 종류') || 'peer') === String(data.evaluation_type || 'peer')) {
        return r.row;
      }
    }
  }
  return -1;
}

function upsertFeedback_(data) {
  const sheet = ensureSheet_(FEEDBACK_SHEET, FEEDBACK_HEADERS);
  ensureLegacyFeedbackIds_(sheet);
  const id = data.feedback_id || Utilities.getUuid();
  const row = findFeedbackRow_(sheet, id, data);
  const values = {
    'ID': id,
    '시간': data.created_at || new Date().toISOString(),
    '이벤트': 'feedback',
    '평가 종류': data.evaluation_type || 'peer',
    '평가한 학생 학번': data.sender_number || '',
    '평가한 학생 이름': data.sender_name || '',
    '평가 대상 학번': data.target_number || '',
    '평가 대상 이름': data.target_name || '',
    '점수': data.score || '',
    '코멘트': data.content || '',
    '답문': data.reply || ''
  };
  const map = headerMap_(sheet);
  if (row > 0) {
    Object.keys(values).forEach(h => {
      if (map[h]) sheet.getRange(row, map[h]).setValue(values[h]);
    });
  } else {
    const out = new Array(sheet.getLastColumn()).fill('');
    Object.keys(values).forEach(h => {
      if (map[h]) out[map[h] - 1] = values[h];
    });
    sheet.appendRow(out);
  }
}

function deleteStudent_(studentNumber) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const studentSheet = ss.getSheetByName(STUDENT_SHEET);
  const feedbackSheet = ss.getSheetByName(FEEDBACK_SHEET);
  if (studentSheet) {
    const row = findRow_(studentSheet, '학번', studentNumber);
    if (row > 0) studentSheet.deleteRow(row);
  }
  if (feedbackSheet) {
    const map = headerMap_(feedbackSheet);
    if (map['평가한 학생 학번'] && map['평가 대상 학번']) {
      for (let row = feedbackSheet.getLastRow(); row >= 2; row--) {
        const sender = String(feedbackSheet.getRange(row, map['평가한 학생 학번']).getValue() || '');
        const target = String(feedbackSheet.getRange(row, map['평가 대상 학번']).getValue() || '');
        if (sender === String(studentNumber) || target === String(studentNumber)) feedbackSheet.deleteRow(row);
      }
    }
  }
}

function bulkSync_(data) {
  const students = data.students || [];
  const feedbacks = data.feedbacks || [];
  students.forEach(upsertStudent_);
  feedbacks.forEach(upsertFeedback_);
  if (data.teacher_password !== undefined) upsertSetting_('teacher_password', data.teacher_password);
}

function doPost(e) {
  try {
    const raw = (e && e.postData && e.postData.contents) || '{}';
    const data = JSON.parse(raw);
    const event = data.event || '';

    if (event === 'reset_feedbacks') {
      const sheet = ensureSheet_(FEEDBACK_SHEET, FEEDBACK_HEADERS);
      if (sheet.getLastRow() > 1) sheet.getRange(2, 1, sheet.getLastRow() - 1, sheet.getLastColumn()).clearContent();
      return ContentService.createTextOutput(JSON.stringify({ok:true, reset:true})).setMimeType(ContentService.MimeType.JSON);
    }

    if (event === 'bulk_sync') {
      bulkSync_(data);
      return ContentService.createTextOutput(JSON.stringify({ok:true, bulk:true})).setMimeType(ContentService.MimeType.JSON);
    }

    if (event === 'student_upsert') {
      upsertStudent_(data);
    } else if (event === 'student_delete') {
      deleteStudent_(data.student_number || '');
    } else if (event === 'teacher_password') {
      upsertSetting_('teacher_password', data.teacher_password || '');
    } else if (event === 'reply') {
      const sheet = ensureSheet_(FEEDBACK_SHEET, FEEDBACK_HEADERS);
      ensureLegacyFeedbackIds_(sheet);
      let row = findRow_(sheet, 'ID', data.feedback_id);
      if (row > 0) {
        const map = headerMap_(sheet);
        if (map['답문']) sheet.getRange(row, map['답문']).setValue(data.reply || '');
      } else {
        throw new Error('feedback ID not found: ' + data.feedback_id);
      }
    } else if (event === 'feedback') {
      upsertFeedback_(data);
    } else {
      throw new Error('unknown event: ' + event);
    }

    return ContentService.createTextOutput(JSON.stringify({ok:true, event:event})).setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    console.error(err);
    return ContentService.createTextOutput(JSON.stringify({ok:false, error:String(err)})).setMimeType(ContentService.MimeType.JSON);
  }
}

