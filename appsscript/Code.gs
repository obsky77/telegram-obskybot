// ── Telegram Bot — Apps Script Handler ────────────────────
// Разверни как Web App: Execute as Me, Anyone can access
// Вставь URL в Railway: APPS_SCRIPT_URL = https://script.google.com/...

var SPREADSHEET_ID = "12PGjDfUKdpo0oCPJXWIJXigEC78cIchhd2ySyfzJkc4";
var SPRINT_SHEET_GID = 469759902;  // gid вкладки спринта

// ── Entry point ────────────────────────────────────────────

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);

    if (payload.action === "update_comment") {
      return updateComment(payload);
    }

    if (payload.action === "update_field") {
      return updateField(payload);
    }

    if (payload.sheet === "sprint") {
      return addToSprint(payload);
    }

    if (payload.sheet === "Входящие") {
      return addToInbox(payload);
    }

    return jsonResponse("error", "Unknown action or sheet");

  } catch (err) {
    return jsonResponse("error", err.toString());
  }
}

// ── 1. Обновить комментарий к задаче ──────────────────────

function updateComment(payload) {
  var sheet = getSprintSheet();
  if (!sheet) return jsonResponse("error", "Sprint sheet not found");

  var data = sheet.getDataRange().getValues();
  var headers = data[0];

  var taskCol = findCol(headers, "Task");
  var comCol  = findCol(headers, "Com");

  if (taskCol === -1) return jsonResponse("error", "Column 'Task' not found");
  if (comCol  === -1) return jsonResponse("error", "Column 'Com' not found");

  var searchName = (payload.task || "").toLowerCase().trim();
  if (!searchName) return jsonResponse("error", "Task name is empty");

  // Находим последний маркер «Запланированные задачи» — начало актуального спринта
  var sprintStart = 1; // по умолчанию — после заголовков
  for (var r = 1; r < data.length; r++) {
    for (var c = 0; c < data[r].length; c++) {
      if (String(data[r][c]).indexOf("Запланированные задачи") !== -1) {
        sprintStart = r + 1; // строки после маркера
      }
    }
  }

  for (var row = sprintStart; row < data.length; row++) {
    var cellValue = String(data[row][taskCol]).toLowerCase().trim();
    if (!cellValue) continue;

    // Partial match в обе стороны — «ПМФ» найдёт «ПМФ по ролику»
    if (cellValue.indexOf(searchName) !== -1 || searchName.indexOf(cellValue) !== -1) {
      var existing = String(data[row][comCol]).trim();
      var newCom = existing ? existing + " | " + payload.com : payload.com;
      sheet.getRange(row + 1, comCol + 1).setValue(newCom);
      return jsonResponse("ok", "Updated: " + data[row][taskCol]);
    }
  }

  return jsonResponse("error", "Task not found: " + payload.task);
}

// ── 2. Обновить поле задачи (дедлайн или приоритет) ───────

function updateField(payload) {
  var sheet = getSprintSheet();
  if (!sheet) return jsonResponse("error", "Sprint sheet not found");

  var data = sheet.getDataRange().getValues();
  var headers = data[0];

  var taskCol  = findCol(headers, "Task");
  var fieldCol = findCol(headers, payload.field || "");

  if (taskCol  === -1) return jsonResponse("error", "Column 'Task' not found");
  if (fieldCol === -1) return jsonResponse("error", "Column '" + payload.field + "' not found");

  var searchName = (payload.task || "").toLowerCase().trim();
  if (!searchName) return jsonResponse("error", "Task name is empty");

  // Only search current sprint (after last marker)
  var sprintStart = 1;
  for (var r = 1; r < data.length; r++) {
    for (var c = 0; c < data[r].length; c++) {
      if (String(data[r][c]).indexOf("Запланированные задачи") !== -1) {
        sprintStart = r + 1;
      }
    }
  }

  for (var row = sprintStart; row < data.length; row++) {
    var cellValue = String(data[row][taskCol]).toLowerCase().trim();
    if (!cellValue) continue;
    if (cellValue.indexOf(searchName) !== -1 || searchName.indexOf(cellValue) !== -1) {
      sheet.getRange(row + 1, fieldCol + 1).setValue(payload.value || "");
      return jsonResponse("ok", "Updated " + payload.field + " for: " + data[row][taskCol]);
    }
  }

  return jsonResponse("error", "Task not found: " + payload.task);
}

// ── 4. Добавить задачу в спринт ───────────────────────────

function addToSprint(payload) {
  var sheet = getSprintSheet();
  if (!sheet) return jsonResponse("error", "Sprint sheet not found");

  var data    = sheet.getDataRange().getValues();
  var headers = data[0];

  // Собираем строку по заголовкам — не зависит от порядка колонок
  var newRow = new Array(headers.length).fill("");
  setCol(newRow, headers, "Task",    payload.task     || "");
  setCol(newRow, headers, "Priority",payload.priority || "П2");
  setCol(newRow, headers, "DD",      payload.dd       || "");
  setCol(newRow, headers, "Lid",     payload.lid      || "");
  setCol(newRow, headers, "Lid #2",  payload.lid2     || "");
  setCol(newRow, headers, "From",    payload.from     || "");
  setCol(newRow, headers, "Com",     payload.com      || "");

  sheet.appendRow(newRow);
  return jsonResponse("ok", "Added to sprint: " + payload.task);
}

// ── 5. Добавить задачу во Входящие ────────────────────────

function addToInbox(payload) {
  var ss    = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheet = ss.getSheetByName("Входящие");
  if (!sheet) return jsonResponse("error", "Sheet 'Входящие' not found");

  var headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];

  if (headers.length === 0 || headers[0] === "") {
    // Если заголовков нет — добавляем строку напрямую
    sheet.appendRow([
      payload.date || new Date().toLocaleDateString("ru-RU"),
      payload.task || "",
      payload.from || "",
      payload.com  || ""
    ]);
  } else {
    var newRow = new Array(headers.length).fill("");
    setCol(newRow, headers, "Date",  payload.date || new Date().toLocaleDateString("ru-RU"));
    setCol(newRow, headers, "Дата",  payload.date || new Date().toLocaleDateString("ru-RU"));
    setCol(newRow, headers, "Task",  payload.task || "");
    setCol(newRow, headers, "From",  payload.from || "");
    setCol(newRow, headers, "Com",   payload.com  || "");
    sheet.appendRow(newRow);
  }

  return jsonResponse("ok", "Added to inbox: " + payload.task);
}

// ── Helpers ────────────────────────────────────────────────

function getSprintSheet() {
  var ss     = SpreadsheetApp.openById(SPREADSHEET_ID);
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (sheets[i].getSheetId() === SPRINT_SHEET_GID) {
      return sheets[i];
    }
  }
  return null;
}

function findCol(headers, name) {
  for (var i = 0; i < headers.length; i++) {
    if (String(headers[i]).trim() === name) return i;
  }
  return -1;
}

function setCol(row, headers, name, value) {
  var idx = findCol(headers, name);
  if (idx !== -1) row[idx] = value;
}

function jsonResponse(status, message) {
  return ContentService
    .createTextOutput(JSON.stringify({ status: status, message: message }))
    .setMimeType(ContentService.MimeType.JSON);
}
