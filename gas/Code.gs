/**
 * 得点帳 / 園の登降園予定データを集約する Apps Script Web App。
 *
 * 使い方:
 *   1. スクリプトプロパティに SPREADSHEET_ID と SECRET を設定する
 *   2.  Sheets -> シート を作っておく（ attendance / staff / shift）
 *   3.  「デプロイ -> 新しいデプロイ -> ウェブアプリ」で /exec を発行する
 *
 * クライアント（shiftai.gas_client）との約束:
 *   GET  /exec?action=ping|read&sheet=<name>&secret=<SECRET>
 *   POST /exec   {action:write|append, sheet, columns, data, secret}
 * 応答は常に {ok, action, rows, columns, data, updatedAt} 形式の JSON。
 */

var VERSION = '1.0.0';

/** 論理名と実シート名の対応。 */
var SHEETS = {
  attendance: '登降園予定',
  staff: '職員',
  shift: 'シフト'
};

var ACTIONS = ['ping', 'read', 'write', 'append'];

/* ------------------------------------------------------------------ */
/* 入口                                                                */
/* ------------------------------------------------------------------ */

function doGet(e) {
  return handle(e);
}

function doPost(e) {
  return handle(e);
}

function handle(e) {
  var param = (e && e.parameter) || {};
  var body = readBody(e);
  var action = String(body.action || param.action || 'ping').toLowerCase();
  var secret = String(body.secret || param.secret || '');
  try {
    verifySecret(secret);
    if (ACTIONS.indexOf(action) === -1) {
      throw new Error('unknown action: ' + action);
    }
    var sheet = resolveSheet(body.sheet || param.sheet || 'attendance');
    if (action === 'ping') {
      return respond({ ok: true, action: 'ping', message: 'connection ok', version: VERSION });
    }
    if (action === 'read') {
      return respond(readSheet(sheet));
    }
    return respond(writeSheet(sheet, body, action));
  } catch (err) {
    Logger.log('[shiftai] error: %s', err && err.stack ? err.stack : err);
    return respond({
      ok: false,
      action: action,
      error: String(err && err.message ? err.message : err),
      updatedAt: now()
    });
  }
}

/** POST 本文を JSON として読む（読めなければ空オブジェクト）。 */
function readBody(e) {
  if (!e || !e.postData || !e.postData.contents) {
    return {};
  }
  var raw = String(e.postData.contents);
  if (!raw) {
    return {};
  }
  try {
    return JSON.parse(raw);
  } catch (err) {
    return {};
  }
}

/**
 * スクリプトプロパティの SECRET と照合する。
 *
 * 単純な !== 比較は文字列を先頭から順に照合するため、応答時間の差から
 * SECRET を 1 文字ずつ推測できる（タイミング攻撃）。長さによらず一定の
 * 時間で比較する。
 */
function verifySecret(given) {
  var expected = String(PropertiesService.getScriptProperties().getProperty('SECRET') || '');
  if (!expected) {
    throw new Error('SECRET is not set');
  }
  if (!timingSafeEqual(expected, String(given || ''))) {
    throw new Error('SECRET Mismatch');
  }
}

/** 長さによらず一定の時間で 2 つの文字列を比較する。 */
function timingSafeEqual(a, b) {
  var diff = a.length ^ b.length;
  var max = Math.max(a.length, b.length);
  for (var i = 0; i < max; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

/** 論理名を実シートへ解決する。 */
function resolveSheet(name) {
  var key = String(name || 'attendance');
  var mapped = SHEETS[key];
  if (mapped) {
    return mapped;
  }
  var ss = openSpreadsheet();
  if (ss.getSheetByName(key)) {
    return key;
  }
  throw new Error('sheet not found: ' + key);
}

function openSpreadsheet() {
  var id = String(PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID') || '');
  if (!id) {
    throw new Error('SPREADSHEET_ID is not set');
  }
  return SpreadsheetApp.openById(id);
}

/* ------------------------------------------------------------------ */
/* 読み書き                                                            */
/* ------------------------------------------------------------------ */

/** シート全体を 2 次元配列（文字列）として読む。 */
function readSheet(sheetName) {
  var sheet = openSpreadsheet().getSheetByName(sheetName);
  if (!sheet) {
    throw new Error('sheet not found: ' + sheetName);
  }
  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();
  if (lastRow < 1 || lastCol < 1) {
    return { ok: true, action: 'read', sheet: sheetName, rows: 0, columns: [], data: [], updatedAt: now() };
  }
  var values = sheet.getRange(1, 1, lastRow, lastCol).getDisplayValues();
  var header = values[0] || [];
  var body = values.slice(1);
  var data = body.filter(function (row) {
    return row.join('') !== '';
  });
  return {
    ok: true,
    action: 'read',
    sheet: sheetName,
    rows: data.length,
    columns: header,
    data: data,
    updatedAt: now()
  };
}

/** シートへ書き込む。action='write' は全置換、'append' は末尾追記。 */
function writeSheet(sheetName, body, action) {
  var ss = openSpreadsheet();
  var sheet = ss.getSheetByName(sheetName);
  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
  }
  var columns = toStringMatrix(body.columns)[0] || [];
  var data = toStringMatrix(body.data);
  if (data.length === 0) {
    data = [];
  }
  var header = columns.length ? columns : guessHeader(data);
  var records = data.map(function (row) {
    return header.map(function (_, i) {
      return row[i] === undefined || row[i] === null ? '' : String(row[i]);
    });
  });

  if (action === 'append') {
    var start = sheet.getLastRow() + 1;
    if (start > 1) {
      sheet.getRange(start, 1, records.length, header.length).setValues(records);
    }
    return {
      ok: true,
      action: 'append',
      sheet: sheetName,
      rows: records.length,
      columns: header,
      data: [],
      updatedAt: now()
    };
  }

  var totalRows = header.length ? records.length + 1 : 0;
  if (sheet.getMaxRows() < Math.max(totalRows, 1)) {
    sheet.insertRowsAfter(sheet.getMaxRows(), Math.max(totalRows, 1) - sheet.getMaxRows());
  }
  if (sheet.getMaxColumns() < Math.max(header.length, 1)) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), Math.max(header.length, 1) - sheet.getMaxColumns());
  }
  sheet.getRange(1, 1, Math.max(sheet.getMaxRows(), 1), Math.max(sheet.getMaxColumns(), 1)).clearContent();
  if (header.length) {
    sheet.getRange(1, 1, 1, header.length).setValues([header]);
  }
  if (records.length) {
    sheet.getRange(2, 1, records.length, header.length).setValues(records);
  }
  return {
    ok: true,
    action: 'write',
    sheet: sheetName,
    rows: records.length,
    columns: header,
    data: [],
    updatedAt: now()
  };
}

/** 2 次元配列をすべて文字列にする。 */
function toStringMatrix(value) {
  if (!value || !value.length) {
    return [];
  }
  if (!Array.isArray(value[0])) {
    return [[String(value[0])]];
  }
  return value.map(function (row) {
    return row.map(function (cell) {
      if (cell === null || cell === undefined) {
        return '';
      }
      return String(cell);
    });
  });
}

/** ヘッダが無いときの代用列名。 */
function guessHeader(data) {
  if (!data.length) {
    return [];
  }
  var out = [];
  for (var i = 0; i < data[0].length; i += 1) {
    out.push('列' + (i + 1));
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* 応答                                                                */
/* ------------------------------------------------------------------ */

function respond(payload) {
  payload = payload || {};
  payload.rows = payload.rows || 0;
  payload.columns = payload.columns || [];
  payload.data = payload.data || [];
  payload.updatedAt = payload.updatedAt || now();
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

function now() {
  return Utilities.formatDate(new Date(), 'Asia/Tokyo', "yyyy-MM-dd'T'HH:mm:ssXXX");
}

/** 動作確認用（エディタから手実行できる）。 */
function selfTest() {
  Logger.log('version=%s', VERSION);
  Logger.log('sheets=%s', JSON.stringify(SHEETS));
  try {
    Logger.log(JSON.stringify(readSheet(resolveSheet('attendance'))));
  } catch (err) {
    Logger.log('read failed: %s', err);
  }
}
