// 会话存活表的用例（CD-021）：哪一份库、什么算「没了」、读不到怎么降级。
// 跑法：`node --test app/test/`。
//
// 用例自己造一个小 SQLite 库（node:sqlite，本机 Node 24 自带）：不碰用户电脑上那份真库，
// 也不依赖它此刻长什么样。

import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

const require = createRequire(import.meta.url);

import {
  GONE_STATUSES,
  createSessionIndex,
  pickSessionsDb,
  readLiveSessions,
  sameSessions,
  sessionGone,
} from '../sessions.mjs';

function withDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'widget-sessions-'));
  try {
    return fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/** 造一份最小的会话库：只要 id / status 两列，跟真库的用法一致。 */
function makeDb(path, rows) {
  const sqlite = require('node:sqlite');
  const db = new sqlite.DatabaseSync(path);
  db.exec('CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT)');
  const insert = db.prepare('INSERT INTO sessions (id, status) VALUES (?, ?)');
  for (const [id, status] of rows) insert.run(id, status);
  db.close();
}

test('存活表：有这一行、且不是 deleted 才算能去（归档仍算在）', () => withDir((dir) => {
  const db = join(dir, 'cindy-test.db');
  makeDb(db, [
    ['s-active', 'active'],
    ['s-archived', 'archived'],
    ['s-deleted', 'deleted'],
  ]);
  const live = readLiveSessions(db);
  assert.ok(live instanceof Set);
  assert.deepEqual([...live].sort(), ['s-active', 's-archived']);
  assert.deepEqual(GONE_STATUSES, ['deleted'], '只有 deleted 算没了：归档会话宿主还能打开');
  assert.equal(sessionGone('s-deleted', live), true);
  assert.equal(sessionGone('s-never-existed', live), true, '库里没有这一行也算没了');
  assert.equal(sessionGone('s-archived', live), false);
}));

test('存活表：读不到就返回 null（不知道），绝不返回空集合', () => withDir((dir) => {
  assert.equal(readLiveSessions(''), null, '没有路径');
  assert.equal(readLiveSessions(join(dir, 'nope.db')), null, '文件不存在');

  const junk = join(dir, 'junk.db');
  writeFileSync(junk, 'not a database at all', 'utf8');
  assert.equal(readLiveSessions(junk), null, '不是 SQLite 库');

  // 环境里没有 node:sqlite（换个运行环境）：同样按不知道处理，不让浮窗崩
  assert.equal(readLiveSessions(junk, { sqlite: null }), null);
}));

test('存活表：读到一半坏了也不抛（浮窗不能因为一次读库失败就白屏）', () => withDir((dir) => {
  const db = join(dir, 'cindy-broken.db');
  makeDb(db, [['s-1', 'active']]);
  const sqlite = require('node:sqlite');
  const fake = {
    DatabaseSync: class {
      constructor() {
        throw new Error('database is locked');
      }
    },
  };
  assert.equal(readLiveSessions(db, { sqlite: fake }), null);
  // 真模块仍然读得出来（证明上面失败是注入的那个假对象，不是库坏了）
  assert.equal(readLiveSessions(db, { sqlite }).size, 1);
}));

test('哪一份库：先认导出物自报的 sessionsDb，其次 mtime 最新的 cindy-*.db', () => withDir((dir) => {
  const declared = join(dir, 'declared.db');
  makeDb(declared, [['s-1', 'active']]);
  const other = join(dir, 'cindy-old.db');
  makeDb(other, [['s-2', 'active']]);

  // 导出物自报的那一份，即使目录里还有别的库也认它
  assert.equal(pickSessionsDb({ source: { sessionsDb: declared } }, dir), declared);
  // 自报的那份不在了 → 退回翻目录
  assert.equal(pickSessionsDb({ source: { sessionsDb: join(dir, 'missing.db') } }, dir), other);
  // 导出物没写（旧导出物）→ 也是翻目录
  assert.equal(pickSessionsDb({}, dir), other);
  // 目录都读不到 → 空串，不猜路径
  assert.equal(pickSessionsDb({}, join(dir, 'no-such-dir')), '');
  assert.equal(pickSessionsDb({}, ''), '');
}));

test('哪一份库：备份文件不算候选（它们的 mtime 可能比正在用的还新）', () => withDir((dir) => {
  const live = join(dir, 'cindy-main.db');
  makeDb(live, [['s-1', 'active']]);
  const backup = join(dir, 'cindy-main.db.bak.2026-11-13T03-04-18-762Z');
  writeFileSync(backup, 'x', 'utf8');
  const future = new Date(Date.now() + 60000);
  utimesSync(backup, future, future);
  assert.equal(pickSessionsDb({}, dir), live);
}));

test('两份存活表比较：同一份算没变（不重建卡片）', () => {
  assert.equal(sameSessions(null, null), true);
  assert.equal(sameSessions(new Set(['a', 'b']), new Set(['b', 'a'])), true);
  assert.equal(sameSessions(new Set(['a']), new Set(['a', 'b'])), false);
  assert.equal(sameSessions(new Set(['a']), null), false, '不知道 ≠ 知道');
  assert.equal(sameSessions(null, new Set(['a'])), false);
});

test('组装好的判据：每份导出物都先按它自报的库问一次', () => withDir((dir) => {
  const declared = join(dir, 'declared.db');
  makeDb(declared, [['s-1', 'active']]);
  const index = createSessionIndex({ cindyDir: dir });
  const answer = index.forPayload({ source: { sessionsDb: declared } });
  assert.equal(answer.path, declared);
  assert.equal(answer.live.has('s-1'), true);
  assert.equal(index.path, declared, '用的是哪一份库要查得到（发出记录与文档都引它）');
}));
