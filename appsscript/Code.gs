// ── Telegram Bot — Apps Script Handler ────────────────────
// Разверни как Web App: Execute as Me, Anyone can access
// Вставь URL в Railway: APPS_SCRIPT_URL = https://script.google.com/...

var SPREADSHEET_ID = "12PGjDfUKdpo0oCPJXWIJXigEC78cIchhd2ySyfzJkc4";
var SPRINT_SHEET_GID = 469759902;  // gid вкладки спринта
var DRIVE_FOLDER_ID  = "1oWKcFJpliR9GxnBZ56Els8nCdkfuE8s_";  // корневая папка с проектами

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

    if (payload.action === "add_feedback") {
      return addFeedback(payload);
    }

    if (payload.action === "search_drive") {
      return searchDrive(payload);
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

  var sender = payload.sender || "";
  var newPart = payload.com + (sender ? " (" + sender + ")" : "");

  for (var row = sprintStart; row < data.length; row++) {
    var cellValue = String(data[row][taskCol]).toLowerCase().trim();
    if (!cellValue) continue;

    if (isTaskMatch(cellValue, searchName)) {
      var cell = sheet.getRange(row + 1, comCol + 1);
      var existing = String(data[row][comCol]).trim();

      var highlightStyle = SpreadsheetApp.newTextStyle()
          .setForegroundColor("#1a73e8")  // синий цвет
          .setBold(true)
          .build();

      if (!existing) {
        // Пустая ячейка — просто пишем новый комментарий с подсветкой
        var rt = SpreadsheetApp.newRichTextValue()
          .setText(newPart)
          .setTextStyle(0, newPart.length, highlightStyle)
          .build();
        cell.setRichTextValue(rt);
      } else {
        // Есть текст — добавляем через " | " с подсветкой нового
        var fullText = existing + " | " + newPart;
        var newStart = existing.length + 3; // после " | "
        var rt = SpreadsheetApp.newRichTextValue()
          .setText(fullText)
          .setTextStyle(newStart, fullText.length, highlightStyle)
          .build();
        cell.setRichTextValue(rt);
      }

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
    if (isTaskMatch(cellValue, searchName)) {
      sheet.getRange(row + 1, fieldCol + 1).setValue(payload.value || "");
      return jsonResponse("ok", "Updated " + payload.field + " for: " + data[row][taskCol]);
    }
  }

  return jsonResponse("error", "Task not found: " + payload.task);
}

// ── 3. Записать фидбек/сообщение для команды ──────────────

function addFeedback(payload) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  // Ищем лист по имени, затем берём третий по индексу
  var sheet = ss.getSheetByName("\u041e\u0442\u0437\u044b\u0432\u044b")   // "Отзывы"
           || ss.getSheetByName("\u0424\u0438\u0434\u0431\u0435\u043a")   // "Фидбек"
           || ss.getSheetByName("Feedback")
           || ss.getSheets()[2];  // 3-й лист по порядку

  if (!sheet) return jsonResponse("error", "Feedback sheet not found");

  var headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  var hasHeaders = headers.some(function(h) { return String(h).trim() !== ""; });

  if (hasHeaders) {
    var newRow = new Array(headers.length).fill("");
    // Пробуем по вариантам названий колонок
    setCol(newRow, headers, "\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435", payload.message || ""); // Сообщение
    setCol(newRow, headers, "Message",    payload.message || "");
    setCol(newRow, headers, "\u041e\u0442 \u043a\u043e\u0433\u043e",  payload.from    || ""); // От кого
    setCol(newRow, headers, "From",       payload.from    || "");
    setCol(newRow, headers, "\u041a\u043e\u0433\u0434\u0430",    payload.date    || ""); // Когда
    setCol(newRow, headers, "Date",       payload.date    || "");
    sheet.appendRow(newRow);
  } else {
    // Нет заголовков — пишем напрямую
    sheet.appendRow([
      payload.message || "",
      payload.from    || "",
      payload.date    || new Date().toLocaleDateString("ru-RU")
    ]);
  }

  return jsonResponse("ok", "Feedback saved");
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

// ── 6. Добавить задачу во Входящие ────────────────────────

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

// ── 5. Поиск папки проекта в Google Drive ─────────────────

function searchDrive(payload) {
  var query = String(payload.query || "").trim();
  if (!query) return driveJsonResponse("error", "Query is empty");

  var rootFolder;
  try {
    rootFolder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
  } catch (err) {
    return driveJsonResponse("error", "Cannot access root folder: " + err.toString());
  }

  var queryLower = query.toLowerCase();
  var subfolders;
  try {
    subfolders = rootFolder.getFolders();
  } catch (err) {
    return driveJsonResponse("error", "Cannot list subfolders: " + err.toString());
  }

  var matches = [];
  var folderCount = 0;
  var MAX_FOLDERS = 100;
  var MAX_FILES   = 50;

  while (subfolders.hasNext() && folderCount < MAX_FOLDERS) {
    folderCount++;
    var folder = subfolders.next();
    var folderName      = folder.getName();
    var folderNameLower = folderName.toLowerCase();

    // Bidirectional substring or token overlap (handles "Тося Чайкина" ↔ "Тося чайника/самокат")
    var isMatch = isTaskMatch(folderNameLower, queryLower);
    if (!isMatch) continue;

    var filesResult = [];
    var fileCount = 0;
    try {
      var files = folder.getFiles();
      while (files.hasNext() && fileCount < MAX_FILES) {
        fileCount++;
        var file = files.next();
        filesResult.push({ name: file.getName(), url: file.getUrl() });
      }
    } catch (fileErr) {
      // папка возвращается даже если файлы недоступны
    }

    matches.push({ name: folderName, url: folder.getUrl(), files: filesResult });
  }

  if (matches.length === 0) {
    return driveJsonResponse("not_found", "No folders matching: " + query);
  }

  return ContentService
    .createTextOutput(JSON.stringify({ status: "ok", matches: matches }))
    .setMimeType(ContentService.MimeType.JSON);
}

function driveJsonResponse(status, message) {
  return ContentService
    .createTextOutput(JSON.stringify({ status: status, message: message }))
    .setMimeType(ContentService.MimeType.JSON);
}

// ── Helpers ────────────────────────────────────────────────

/**
 * Token-based overlap match.
 * Splits both strings into words (≥3 chars), returns true if any word
 * from 'a' is a substring of any word from 'b', or vice versa.
 * Handles cases like "Тося Чайкина" ↔ "Тося чайника/самокат".
 */
function tokensOverlap(a, b) {
  function tokenize(s) {
    return s.toLowerCase().split(/[\s\/\-_.,;:!?()]+/).filter(function(t) { return t.length >= 3; });
  }
  var aT = tokenize(a);
  var bT = tokenize(b);
  for (var i = 0; i < aT.length; i++) {
    for (var j = 0; j < bT.length; j++) {
      if (bT[j].indexOf(aT[i]) !== -1 || aT[i].indexOf(bT[j]) !== -1) {
        return true;
      }
    }
  }
  return false;
}

function isTaskMatch(cellValue, searchName) {
  // Primary: bidirectional substring
  if (cellValue.indexOf(searchName) !== -1 || searchName.indexOf(cellValue) !== -1) return true;
  // Fallback: token overlap (handles typos, different word forms, slash-separated names)
  return tokensOverlap(cellValue, searchName);
}

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
