// resolve 的用例（CD-019）：算路、发送、记账、失败也要有一句话。
// 跑法：`node --test app/test/`（Python 那一套不包含这里）。

import assert from 'node:assert/strict';
import test from 'node:test';

import { createResolver, planResolve, resolvePrompt } from '../resolve.mjs';

const TICKET = { id: 'CD-019', title: '浮窗 resolve 接线', sessionId: 's-ticket', resolve: true };
// 同一个理由：源码树里工作区根就是占位符本身，装好之后是真实路径。
const WORKSPACE_ROOT = '{{WORKSPACE}}';
const PROJECT = { name: 'cindy-dashboard', root: WORKSPACE_ROOT + '/panel' };

function collector() {
  const rows = [];
  return { rows, record: (entry) => rows.push(entry) };
}

test('prompt 的第一行就是单号（新建会话时标题靠这条关系带上单号）', () => {
  const prompt = resolvePrompt(TICKET);
  assert.equal(prompt.split('\n')[0], '涉及工单：CD-019 · 浮窗 resolve 接线');
  assert.match(prompt, /不要降低判据/);
  assert.ok(prompt.includes('交接包交给 ' + WORKSPACE_ROOT), 'prompt 里要写出交回哪个工作区');
});

test('算路：有会话 → 交回它自己的会话', () => {
  const plan = planResolve(TICKET, PROJECT);
  assert.equal(plan.kind, 'open-session');
  assert.equal(plan.sessionId, 's-ticket');
  assert.equal(plan.ticketId, 'CD-019');
  assert.match(plan.prompt, /^涉及工单：CD-019/);
});

test('算路：没有会话但有项目根 → 在那个目录里开一条', () => {
  const plan = planResolve({ id: 'CD-020', title: '池子单' }, PROJECT);
  assert.equal(plan.kind, 'new-session');
  assert.equal(plan.workdir, PROJECT.root);
  assert.equal(plan.title, '工单 CD-020 收口');
});

test('算路：连项目根都读不到 → 不发，也不猜一个目录', () => {
  for (const project of [undefined, {}, { root: '' }, { root: '   ' }]) {
    const plan = planResolve({ id: 'CD-021', title: '无根' }, project);
    assert.equal(plan.kind, 'blocked', JSON.stringify(project));
    assert.match(plan.reason, /不知道在哪开一条/);
  }
  // 没有单号：连交回给谁都说不出来，同样不发
  assert.equal(planResolve({}, PROJECT).kind, 'blocked');
});

test('发得出去：返回一句「发到哪了」，并留一行发出记录', async () => {
  const log = collector();
  const calls = [];
  const DETAIL = '已请求打开 cindy://session/s-ticket；那段话已复制到剪贴板';
  const resolver = createResolver({
    openSession: async (plan) => { calls.push(plan); return DETAIL; },
    newSession: async () => { throw new Error('不该走到这一支'); },
    record: log.record,
    now: () => 1795176000000,
  });

  const result = await resolver.resolve(TICKET, PROJECT);
  assert.equal(result.ok, true);
  assert.equal(result.kind, 'open-session');
  assert.equal(result.detail, DETAIL);
  assert.equal(result.at, '2026-11-20T12:00:00.000Z');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].sessionId, 's-ticket');

  // 发出记录：主进程那边查得到「这一下发出去了什么、发到哪」
  assert.equal(log.rows.length, 1);
  assert.equal(log.rows[0].ok, true);
  assert.equal(log.rows[0].ticketId, 'CD-019');
  assert.equal(log.rows[0].target, 's-ticket');
  assert.equal(result.recorded, true);
});

test('发不出去：失败也要有一句话，也一样留记录', async () => {
  const log = collector();
  const resolver = createResolver({
    openSession: async () => { throw new Error('宿主没接住这次打开'); },
    newSession: async () => { throw new Error('不该走到这一支'); },
    record: log.record,
  });

  const result = await resolver.resolve(TICKET, PROJECT);
  assert.equal(result.ok, false);
  assert.equal(result.reason, '宿主没接住这次打开');
  assert.equal(log.rows.length, 1);
  assert.equal(log.rows[0].ok, false);
  assert.equal(log.rows[0].reason, '宿主没接住这次打开');
  // 失败不写成功：detail 不出现在失败结果里
  assert.equal('detail' in result, false);
});

test('new-session 那一支：发送口拿到的是项目根，不是当前目录', async () => {
  const seen = [];
  const resolver = createResolver({
    openSession: async () => '不该走到这一支',
    newSession: async (plan) => { seen.push(plan); return '已在项目里开出一条会话'; },
  });

  const result = await resolver.resolve({ id: 'CD-022', title: '池子单' }, PROJECT);
  assert.equal(result.ok, true);
  assert.equal(result.kind, 'new-session');
  assert.equal(seen[0].workdir, PROJECT.root);
  assert.match(seen[0].prompt, /^涉及工单：CD-022/);
});

test('算不出落点时发送口一次都不被调，但记录照写', async () => {
  let called = 0;
  const log = collector();
  const resolver = createResolver({
    openSession: async () => { called += 1; return 'x'; },
    newSession: async () => { called += 1; return 'x'; },
    record: log.record,
  });

  const result = await resolver.resolve({ id: 'CD-023', title: '无根' }, {});
  assert.equal(result.ok, false);
  assert.equal(result.kind, 'blocked');
  assert.equal(called, 0);
  assert.equal(log.rows.length, 1);
  assert.equal(log.rows[0].ok, false);
  assert.equal(log.rows[0].target, '');
});

test('同一张单在飞时再点一次：只发一遍', async () => {
  let calls = 0;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const resolver = createResolver({
    openSession: async () => {
      calls += 1;
      await gate;
      return '发完了';
    },
    newSession: async () => '不该走到这一支',
  });

  const first = resolver.resolve(TICKET, PROJECT);
  const second = resolver.resolve(TICKET, PROJECT);
  release();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(calls, 1);
  assert.equal(a, b);
  assert.equal(a.ok, true);

  // 发完之后再点，是另一次发送（不是永远缓存）
  await resolver.resolve(TICKET, PROJECT);
  assert.equal(calls, 2);
});

test('发送口没给结论时：兜底文案也不写完成态', async () => {
  for (const kind of ['open-session', 'new-session']) {
    const resolver = createResolver({
      openSession: async () => '',
      newSession: async () => '',
    });
    const ticket = kind === 'open-session' ? TICKET : { id: 'CD-020', title: '池子单' };
    const result = await resolver.resolve(ticket, PROJECT);
    assert.equal(result.ok, true, kind);
    assert.doesNotMatch(result.detail, /已交回|已打开/, kind + '：' + result.detail);
    assert.match(result.detail, /没有给结论/, kind);
  }
});

test('记录写不进去不改发送结论（记录是记录，发送是发送）', async () => {
  const resolver = createResolver({
    openSession: async () => '已请求打开 cindy://session/s-ticket',
    newSession: async () => 'x',
    record: () => { throw new Error('磁盘满了'); },
  });
  const result = await resolver.resolve(TICKET, PROJECT);
  assert.equal(result.ok, true);
  assert.equal(result.recorded, false);
  assert.equal(result.recordError, '磁盘满了');
});

// ---------------------------------------------------------------- 会话没了（CD-021 C5）

test('算路：会话已经不在 → 不再去叫它，改在项目根新建（与箭头同一套判定）', () => {
  const ticket = { id: 'AW-006', title: '历史直做', sessionId: 's-deleted', sessionGone: true };
  const plan = planResolve(ticket, PROJECT);
  assert.equal(plan.kind, 'new-session', '死链不许再点进去');
  assert.equal(plan.workdir, PROJECT.root);
  assert.equal(plan.sessionGone, true, '文案要知道「原来那条没了」，而不是「还没人接手」');
  assert.match(plan.prompt, /^涉及工单：AW-006/, 'prompt 与鸿蒙那份逐字一致，不因为这一支改一个字');
});

test('算路：会话没了、连项目根也读不到 → blocked，理由说的是「会话没了」而不是「还没有会话」', () => {
  const plan = planResolve({ id: 'AW-006', sessionId: 's-deleted', sessionGone: true }, {});
  assert.equal(plan.kind, 'blocked');
  assert.match(plan.reason, /原来那条会话已经不在/);
  assert.match(plan.reason, /不知道在哪新建/);
});

test('算路：会话还在（live/unknown）→ 仍然交回它自己的会话', () => {
  for (const ticket of [
    { id: 'CD-021', sessionId: 's-live' },
    { id: 'CD-021', sessionId: 's-unknown', sessionGone: false },
  ]) {
    assert.equal(planResolve(ticket, PROJECT).kind, 'open-session', JSON.stringify(ticket));
  }
});

test('发出去：会话没了那一支走 new-session，结果文案说「原来那条没了」与「还差发出去」', async () => {
  const calls = [];
  const resolver = createResolver({
    openSession: async () => '不该走到这一支',
    newSession: async (plan) => {
      calls.push(plan);
      return '已请求 Cindy 在 {{WORKSPACE}}/panel 打开新建会话（--open-folder），要交回的那段话已复制到剪贴板；把剪贴板里那段话发出去，会话就开在这个项目里';
    },
    record: () => {},
  });
  const result = await resolver.resolve(
    { id: 'AW-006', title: '历史直做', sessionId: 's-deleted', sessionGone: true },
    { name: 'cindy-dashboard', root: PROJECT.root },
  );
  assert.equal(result.ok, true);
  assert.equal(result.kind, 'new-session');
  assert.equal(calls.length, 1);
  assert.match(result.detail, /--open-folder/);
  assert.doesNotMatch(result.detail, /已发送|已交回|已建好/);
});
