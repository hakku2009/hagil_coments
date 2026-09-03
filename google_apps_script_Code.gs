// Google Apps Script
// 1) 새 Google 스프레드시트를 만든 뒤 확장 프로그램 > Apps Script에 붙여넣습니다.
// 2) DEPLOY > New deployment > Web app
//    Execute as: Me / Who has access: Anyone with the link
// 3) 생성된 Web app URL을 Flask 환경변수 GOOGLE_SHEETS_WEBHOOK_URL에 넣습니다.
function doPost(e) {
  const data = JSON.parse(e.postData.contents || '{}');
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName('평가내역');
  if (!sheet) {
    sheet = ss.insertSheet('평가내역');
    sheet.appendRow(['시간','이벤트','평가한 학생 학번','평가 대상 학번','점수','코멘트']);
  }
  sheet.appendRow([
    data.created_at || new Date().toISOString(),
    data.event || '',
    data.sender_number || '',
    data.target_number || '',
    data.score || '',
    data.content || ''
  ]);
  return ContentService.createTextOutput(JSON.stringify({ok:true})).setMimeType(ContentService.MimeType.JSON);
}
