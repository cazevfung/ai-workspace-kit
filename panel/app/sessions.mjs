// 「这条会话还在不在」——浮窗唯一的会话存活判据（CD-021）。
//
// 为什么需要它：浮窗行尾那枚箭头与 resolve 都要去 `cindy://session/<id>`。宿主对一条早就删掉的
// 会话只会弹「找不到这条任务，它可能已被删除」（CD-022 的现场：AW-006）。以前两处各自判一次，
// 于是箭头照旧去撞一条死链，resolve 也照旧说「请求打开了 … 的会话」。这一版把判定收到一处：
// **能不能去那条会话**，由这里读到的东西说了算，箭头与 resolve 同一个答案。
//
// 数据从哪来：导出物自己写了它的会话库在哪（`source.sessionsDb`，导出器就是从这个库生成
// 「一卡一任务」的）；读不到这个键时退回 `%APPDATA%/Cindy` 下 mtime 最新的 `cindy-*.db`。
// 判据只有一条：`sessions` 里**有这一行、且 status 不是 `deleted`**。归档会话照旧算在（宿主能
// 打开归档会话，菜单里有「取消归档」），删掉的才算不在。
//
// 只读约定：`mode: ro` 打开、只跑一条 `SELECT`，读完立刻关连接。不复制库、不做 WAL checkpoint、
// 不写一个字节。读不到（没装 node:sqlite、库不在、被占用、表结构变了）就**返回 null 表示
// 「不知道」**——不知道不等于「没了」，那时箭头与 resolve 回到 CD-022 的老行为（照旧可点，
// 文案里预告「若那条会话已被删除，Cindy 会提示找不到」）。

import { createRequire } from 'node:module';
import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

/** 库里这个状态的行不再可去：会话被删掉了。 */
export const GONE_STATUSES = ['deleted'];

const DB_NAME = /^cindy-.*\.db$/;

function mtimeOf(path) {
  try {
    return statSync(path).mtimeMs;
  } catch {
    return -1;
  }
}

function isFile(path) {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

/**
 * 会话库在哪：先认导出物自报的那一份（`source.sessionsDb`），没有才翻目录。
 *
 * 翻目录时只认 `cindy-*.db` 这一层，跳过 `.bak.` / `.pre-bot-` 这些备份文件名——
 * 它们的 mtime 可能比正在用的那份还新，取错了读到的是历史快照。
 */
export function pickSessionsDb(payload, cindyDir = '') {
  const declared = payload && payload.source && payload.source.sessionsDb;
  if (typeof declared === 'string' && declared.trim() && isFile(declared)) {
    return declared;
  }
  if (!cindyDir) return '';
  let names;
  try {
    names = readdirSync(cindyDir);
  } catch {
    return '';
  }
  const candidates = names
    .filter((name) => DB_NAME.test(name) && !name.includes('.bak.') && !name.includes('.pre-bot-'))
    .map((name) => join(cindyDir, name))
    .filter(isFile);
  if (!candidates.length) return '';
  return candidates.reduce((best, path) => (mtimeOf(path) > mtimeOf(best) ? path : best));
}

function loadSqlite() {
  // 宿主那台机器上有没有 node:sqlite 不由我们定：Electron 45 带 Node 24，模块在；
  // 换个环境读不到就退回「不知道」，不把浮窗一起拖死。
  try {
    const require = createRequire(import.meta.url);
    return require('node:sqlite');
  } catch {
    return null;
  }
}

/**
 * 读一次会话存活表：返回 `Set<sessionId>`（可去的那些），读不到返回 `null`（不知道）。
 *
 * `context.sqlite` 只为用例注入——生产路径一律走 node:sqlite。
 */
export function readLiveSessions(dbPath, context = {}) {
  if (!dbPath || !isFile(dbPath)) return null;
  const sqlite = context.sqlite === undefined ? loadSqlite() : context.sqlite;
  if (!sqlite || typeof sqlite.DatabaseSync !== 'function') return null;
  let db = null;
  try {
    db = new sqlite.DatabaseSync(dbPath, { readOnly: true });
    const rows = db.prepare('SELECT id, status FROM sessions').all();
    const live = new Set();
    for (const row of rows) {
      const id = String((row && row.id) || '');
      if (!id) continue;
      const status = String((row && row.status) || '');
      if (GONE_STATUSES.includes(status)) continue;
      live.add(id);
    }
    return live;
  } catch {
    return null;
  } finally {
    try {
      if (db) db.close();
    } catch {
      // 关不上不影响结论：这一轮的答案已经算出来了
    }
  }
}

/** 两个存活表是不是同一份（每轮都重新读一次，变了才重建卡片）。 */
export function sameSessions(a, b) {
  if (a === b) return true;
  if (!(a instanceof Set) || !(b instanceof Set)) return false;
  if (a.size !== b.size) return false;
  for (const id of a) {
    if (!b.has(id)) return false;
  }
  return true;
}

/** 这条会话还能不能去：`live` 为 null（不知道）时一律说「能」，与 CD-022 的老行为一致。 */
export function sessionGone(sessionId, live) {
  const id = String(sessionId || '');
  if (!id) return false;
  if (!(live instanceof Set)) return false;
  return !live.has(id);
}

/** 一次组装好的判据：给 main.mjs 与用例同一份口径。 */
export function createSessionIndex({
  cindyDir = '',
  read = readLiveSessions,
} = {}) {
  let lastPath = '';
  return {
    /** 本轮的存活表，顺便把「用的是哪一份库」告诉调用方（发出记录与文档要引它）。 */
    forPayload(payload) {
      lastPath = pickSessionsDb(payload, cindyDir);
      return { path: lastPath, live: read(lastPath) };
    },
    get path() {
      return lastPath;
    },
  };
}
