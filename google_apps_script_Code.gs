// Google Apps Script
// 1) Google 스프레드시트에서 확장 프로그램 > Apps Script에 붙여넣습니다.
// 2) 배포 > 새 배포 > 웹 앱
//    실행 사용자: 나 / 액세스 권한: 모든 사용자
// 3) 생성된 웹 앱 URL을 Render의 GOOGLE_SHEETS_WEBHOOK_URL에 넣습니다.

const SHEET_NAME = '평가내역';
const HEADERS = ['시간','이벤트','평가 종류','평가한 학생 학번','평가 대상 학번','점수','코멘트'];

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow(HEADERS);
  }
  return sheet;
}

function doGet(e) {
  return ContentService
    .createTextOutput('Google Sheets 연결 정상')
    .setMimeType(ContentService.MimeType.TEXT);
}

function doPost(e) {
  const data = JSON.parse((e.postData && e.postData.contents) || '{}');
  const sheet = getSheet_();

  // Flask에서 '평가 초기화'를 누르면 평가내역 시트의 데이터도 삭제하고
  // 첫 번째 헤더 행은 남겨둡니다.
  if (data.event === 'reset_feedbacks') {
    const lastRow = sheet.getLastRow();
    const lastColumn = Math.max(sheet.getLastColumn(), HEADERS.length);
    if (lastRow > 1) {
      sheet.getRange(2, 1, lastRow - 1, lastColumn).clearContent();
    }
    return ContentService
      .createTextOutput(JSON.stringify({ok:true, reset:true}))
      .setMimeType(ContentService.MimeType.JSON);
  }

  // 헤더가 오래된 버전이라면 부족한 열을 추가합니다.
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
  }

  sheet.appendRow([
    data.created_at || new Date().toISOString(),
    data.event || '',
    data.evaluation_type || '',
    data.sender_number || '',
    data.target_number || '',
    data.score || '',
    data.content || ''
  ]);

  return ContentService
    .createTextOutput(JSON.stringify({ok:true}))
    .setMimeType(ContentService.MimeType.JSON);
}
