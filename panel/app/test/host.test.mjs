// 宿主端口的用例（CD-019）：两个去处各做一件真事，失败说清下一步；发出记录落成 JSONL。
// 跑法：`node --test app/test/`。

import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { createHostPorts, createResolveRecorder, resolveLogPath } from '../host.mjs';
import { createResolver } from '../resolve.mjs';

// 路径断言不写死某一台机器的绝对路径：套件源码树里 PROJECT_ROOT 就是占位符本身，
// 装到工作区之后 install.py 把它换成真实路径，两种布局下同一条断言都成立。
const PROJECT_ROOT = '{{WORKSPACE}}/panel';

const PLAN_SESSION = {
  kind: 'open-session',
  ticketId: 'CD-019',
  sessionId: 's-ticket',
  prompt: '涉及工单：CD-019 · 浮窗 resolve 接线\n\n正文',
};

const PLAN_NEW = {
  kind: 'new-session',
  ticketId: 'CD-020',
  workdir: PROJECT_ROOT,
  title: '工单 CD-020 收口',
  prompt: '涉及工单：CD-020',
};

function withDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'widget-host-'));
  try {
    fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('有会话：真的交给宿主导航，并把那段话放进剪贴板', async () => {
  const opened = [];
  const copied = [];
  const ports = createHostPorts({
    openExternal: async (url) => { opened.push(url); },
    writeClipboard: (text) => { copied.push(text); },
  });

  const detail = await ports.openSession(PLAN_SESSION);
  assert.deepEqual(opened, ['cindy://session/s-ticket']);
  assert.deepEqual(copied, [PLAN_SESSION.prompt]);
  // 只说两件可核的事：请求打开了哪条链接、剪贴板里放了什么
  assert.match(detail, /已请求打开 CD-019 的会话/);
  assert.match(detail, /cindy:\/\/session\/s-ticket/);
  assert.match(detail, /复制到剪贴板/);
  // 宿主报「找不到」时不许自称成功（CD-022）
  assert.doesNotMatch(detail, /已打开/);
  assert.doesNotMatch(detail, /已交回/);
  assert.doesNotMatch(detail, /已发送/);
  // 反过来，要把「找不到」预告在前
  assert.match(detail, /已被删除/);
  assert.match(detail, /找不到/);
});

test('有会话但宿主没接住：错误原样往上抛（页面会照实说「没发出去」）', async () => {
  const ports = createHostPorts({
    openExternal: async () => { throw new Error('没有 cindy:// 的处理程序'); },
    writeClipboard: () => {},
  });
  await assert.rejects(() => ports.openSession(PLAN_SESSION), /没有 cindy:\/\/ 的处理程序/);
});

test('没有会话：不调 openExternal，明说没接手的人，并把项目根目录带出来', async () => {
  const copied = [];
  let opened = 0;
  const ports = createHostPorts({
    openExternal: async () => { opened += 1; },
    writeClipboard: (text) => { copied.push(text); },
  });

  await assert.rejects(() => ports.newSession(PLAN_NEW), (error) => {
    // 池子里的单本来就没有接手的人——就这么说，不假装交回
    assert.match(error.message, /还没有接手的人/);
    assert.doesNotMatch(error.message, /已交回/);
    assert.doesNotMatch(error.message, /已打开/);
    // 根目录必须给出来（C4 的反例就是只叫人「到项目里新建」却不给路径）
    assert.ok(error.message.includes(PROJECT_ROOT), '错误文案里要给出项目根目录本身');
    assert.match(error.message, /CD-020/);
    return true;
  });
  assert.equal(opened, 0, '没有会话的池子行一次都不去叫宿主（不调 openExternal）');
  assert.deepEqual(copied, [PLAN_NEW.prompt]);
});

test('发出记录：一次 resolve 一行 JSON，落在 userData 下', () => withDir((dir) => {
  const lines = [];
  const record = createResolveRecorder({ dir, log: (line) => lines.push(line) });
  record({ ok: true, kind: 'open-session', ticketId: 'CD-019', at: '2026-11-23T04:00:00.000Z', target: 's-ticket' });
  record({ ok: false, kind: 'new-session', ticketId: 'CD-020', at: '2026-11-23T04:01:00.000Z', reason: '没接住', target: 'D:/x' });

  const text = readFileSync(resolveLogPath(dir), 'utf8').trim().split(/\r?\n/);
  assert.equal(text.length, 2);
  assert.equal(JSON.parse(text[0]).ok, true);
  assert.equal(JSON.parse(text[1]).reason, '没接住');
  // 控制台也留一行：进程被关掉之后仍然看得见刚才发生了什么
  assert.equal(lines.length, 2);
  assert.match(lines[0], /\[widget\] resolve/);
}));

test('整条链路：页面那一下 → 算路 → 宿主端口 → 发出记录', () => withDir(async (dir) => {
  const opened = [];
  const copied = [];
  const resolver = createResolver({
    ...createHostPorts({
      openExternal: async (url) => { opened.push(url); },
      writeClipboard: (text) => { copied.push(text); },
    }),
    record: createResolveRecorder({ dir, log: () => {} }),
    now: () => 1795176000000,
  });

  const sent = await resolver.resolve(
    { id: 'CD-019', title: '浮窗 resolve 接线', sessionId: 's-ticket' },
    { name: 'cindy-dashboard', root: PROJECT_ROOT },
  );
  assert.equal(sent.ok, true);
  assert.equal(opened.length, 1);
  assert.equal(copied.length, 1);

  // 池子单（没有会话）：走 new-session 那一支，结果是一条说得清的失败
  const openedBefore = opened.length;
  const missed = await resolver.resolve({ id: 'CD-020', title: '池子单' }, { root: PROJECT_ROOT });
  assert.equal(missed.ok, false);
  assert.match(missed.reason, /还没有接手的人/);
  assert.ok(missed.reason.includes(PROJECT_ROOT), '失败文案里要给出项目根目录本身');
  assert.doesNotMatch(missed.reason, /已交回/);
  assert.equal(opened.length, openedBefore, '这一支没有去叫宿主（openExternal 一次都不调）');
  assert.equal(copied.length, 2, '失败的这一支也把 prompt 放进了剪贴板');

  const rows = readFileSync(resolveLogPath(dir), 'utf8').trim().split(/\r?\n/).map((line) => JSON.parse(line));
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.ok), [true, false]);
  assert.deepEqual(rows.map((row) => row.target), ['s-ticket', PROJECT_ROOT]);
}));

// ---------------------------------------------------------------- 没有会话那一支：真把宿主叫过去（CD-021 C2）

test('没有会话：真的请宿主在项目根打开新建会话，文案只说请求了什么、复制了什么', async () => {
  const launched = [];
  const copied = [];
  const ports = createHostPorts({
    openExternal: async () => { throw new Error('不该走到这一支'); },
    writeClipboard: (text) => { copied.push(text); },
    openNewSession: async (plan) => { launched.push(plan); },
  });

  const detail = await ports.newSession(PLAN_NEW);
  assert.equal(launched.length, 1, '真动作：宿主被叫到那个目录的新建会话页');
  assert.equal(launched[0].workdir, PROJECT_ROOT);
  assert.deepEqual(copied, [PLAN_NEW.prompt]);
  assert.ok(detail.includes('已请求 Cindy 在 ' + PROJECT_ROOT + ' 打开新建会话'), '结果文案要写清请求了哪个目录');
  assert.match(detail, /--open-folder/);
  assert.match(detail, /复制到剪贴板/);
  // 宿主没有「带正文建会话」的形态：不许说已经建好、已经发送
  assert.doesNotMatch(detail, /已发送|已交回|已新建|已经建好/);
  assert.match(detail, /把剪贴板里那段话发出去/, '还剩哪一步要说清');
});

test('会话已经不在了：同一支，但文案要说「原来那条没了」，而不是「还没人接手」', async () => {
  const ports = createHostPorts({
    openExternal: async () => {},
    writeClipboard: () => {},
    openNewSession: async () => {},
  });
  const plan = { ...PLAN_NEW, ticketId: 'AW-006', sessionGone: true };
  const detail = await ports.newSession(plan);
  assert.match(detail, /AW-006 原来那条会话已经不在 Cindy 里了/);
  assert.doesNotMatch(detail, /还没有接手的人/);
});

test('宿主没起来（启动口抛错）：错误原样往上抛，不编一个成功', async () => {
  const ports = createHostPorts({
    openExternal: async () => {},
    writeClipboard: () => {},
    openNewSession: async () => { throw new Error('没找到 Cindy 的可执行文件'); },
  });
  await assert.rejects(() => ports.newSession(PLAN_NEW), /没找到 Cindy 的可执行文件/);
});

test('整条链路：会话被删之后，页面那一下改请宿主新建，记录里写的是目录', () => withDir(async (dir) => {
  const launched = [];
  const resolver = createResolver({
    ...createHostPorts({
      openExternal: async () => { throw new Error('不该去叫一条已删的会话'); },
      writeClipboard: () => {},
      openNewSession: async (plan) => { launched.push(plan); },
    }),
    record: createResolveRecorder({ dir, log: () => {} }),
    now: () => 1795176000000,
  });
  const result = await resolver.resolve(
    { id: 'AW-006', title: '历史直做', sessionId: 's-deleted', sessionGone: true },
    { name: 'cindy-dashboard', root: PROJECT_ROOT },
  );
  assert.equal(result.ok, true);
  assert.equal(result.kind, 'new-session');
  assert.equal(launched.length, 1);
  const row = JSON.parse(readFileSync(resolveLogPath(dir), 'utf8').trim());
  assert.equal(row.kind, 'new-session');
  assert.equal(row.target, PROJECT_ROOT);
}));
