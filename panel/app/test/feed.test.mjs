// feed.mjs 的用例：导出物归一（五步 / 合并四态 / 折叠 / 计数）、读取源（mtime 判变 / 读失败降级）。
// 跑法：`node --test app/test/`（Python 那套 179 条不包含这里，两边互不影响）。

import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { FeedSource, normalize, stepsFor, clockOf, latestTicketOf, ExportError } from '../feed.mjs';

const NOW = 1795176000 * 1000; // 与 tests/support.py 的 NOW 同一时刻

function iso(minutesAgo) {
  return new Date(NOW - minutesAgo * 60000).toISOString().replace('Z', '+00:00');
}

function ticket(id, extra = {}) {
  return {
    id,
    title: extra.title || '',
    state: extra.state || 'running',
    merged: false,
    mergeState: extra.mergeState || 'not-merged',
    branchExists: true,
    project: extra.project || '项目一',
    elapsedMinutes: 12,
    lastStep: '会话已绑定',
    lastActivityAt: extra.rawAt || iso(extra.minutesAgo === undefined ? 1 : extra.minutesAgo),
    device: 'DESKTOP-T',
    ticketSessionId: extra.session === undefined ? 's-1' : extra.session,
    pmSessionId: 'pm-1',
  };
}

function task(session, title, project, tickets, extra = {}) {
  return {
    sessionId: session,
    title,
    project,
    tickets,
    counts: { needsYou: 0, review: 0, running: 0, queued: 0 },
    lastActivityAt: extra.rawAt || iso(extra.minutesAgo === undefined ? 1 : extra.minutesAgo),
  };
}

function payload(groups = []) {
  return { schemaVersion: 3, generatedAt: iso(0), source: {}, projects: [], groups };
}

test('一卡一项目：任务按 project 归卡，池子按 project 进「还没人认领」', () => {
  const feed = normalize(payload([
    { key: 'running', label: '在跑', tasks: [
      task('s-a', '任务甲', '项目一', [ticket('A-1')]),
      task('s-b', '任务乙', '项目二', [ticket('B-1', { project: '项目二' })]),
    ] },
    { key: 'pool', label: '池子', tickets: [ticket('B-2', { project: '项目二', session: '' })] },
  ]), NOW);

  assert.deepEqual(feed.projects.map((project) => project.name).sort(), ['项目二', '项目一'].sort());
  const card = feed.projects.find((project) => project.name === '项目二');
  assert.equal(card.total, 2);
  assert.equal(card.pool.length, 1);
  assert.equal(card.tasks.length, 1);
});

test('同一张主任务跨项目：按单自己的 project 拆到两张卡', () => {
  const feed = normalize(payload([
    { key: 'historical-direct', label: '历史直做', tasks: [
      task('s-x', '跨项目老任务', '甲项目', [
        ticket('X-1', { project: '甲项目', state: 'done', mergeState: 'recorded', session: '' }),
        ticket('X-2', { project: '乙项目', state: 'done', mergeState: 'recorded', session: '' }),
      ]),
    ] },
  ]), NOW);

  const cards = Object.fromEntries(feed.projects.map((project) => [project.name, project]));
  assert.deepEqual(Object.keys(cards).sort(), ['乙项目', '甲项目'].sort());
  assert.equal(cards['甲项目'].total, 1);
  assert.equal(cards['乙项目'].total, 1);
  assert.equal(cards['乙项目'].tasks[0].name, '跨项目老任务');
});

test('五步：组 key + mergeState 决定每一格', () => {
  const cases = [
    ['queued', 'not-merged', ['current', 'todo', 'todo', 'todo', 'todo']],
    ['pool', 'not-merged', ['current', 'todo', 'todo', 'todo', 'todo']],
    ['running', 'not-merged', ['done', 'done', 'current', 'todo', 'todo']],
    ['needs-you', 'not-merged', ['done', 'done', 'current', 'todo', 'todo']],
    ['review', 'not-merged', ['done', 'done', 'done', 'current', 'next']],
    ['unmerged', 'not-merged', ['done', 'done', 'done', 'done', 'current']],
    ['merged-unrecorded', 'landed-unrecorded', ['done', 'done', 'done', 'done', 'landed']],
    ['merge-unlanded', 'recorded-unlanded', ['done', 'done', 'done', 'done', 'unlanded']],
    ['historical-direct', 'recorded', ['done', 'done', 'done', 'done', 'done']],
  ];
  for (const [group, mergeState, expected] of cases) {
    assert.deepEqual(stepsFor(group, mergeState), expected, `${group}/${mergeState}`);
  }
});

test('合并说明只在两种「对不上」时出现；已合与「已在 master、库里没记录」都不出 resolve', () => {
  const feed = normalize(payload([
    { key: 'merged-unrecorded', label: '缺记录', tasks: [task('s-a', '任务', '项目一', [
      ticket('A-1', { mergeState: 'landed-unrecorded' }),
    ])] },
    { key: 'merge-unlanded', label: '未落地', tasks: [task('s-b', '任务二', '项目一', [
      ticket('A-2', { mergeState: 'recorded-unlanded' }),
    ])] },
    { key: 'unmerged', label: '待合并', tasks: [task('s-c', '任务三', '项目一', [
      ticket('A-3', { mergeState: 'not-merged' }),
      ticket('A-4', { mergeState: 'recorded', session: 's-2' }),
    ])] },
  ]), NOW);

  const rows = Object.fromEntries(feed.projects[0].tasks.flatMap((item) => item.tickets)
    .map((row) => [row.id, row]));
  assert.match(rows['A-1'].note, /已在 master 里/);
  assert.match(rows['A-2'].note, /那个提交不在 master 里/);
  assert.equal(rows['A-3'].note, '');
  assert.equal(rows['A-4'].note, '');
  // landed-unrecorded 的点下去只会去叫一条早就不在的会话（CD-022）
  assert.equal(rows['A-1'].resolve, false);
  // 两种「对不上」里只有 recorded-unlanded 还有下一个人要动手；not-merged 同理
  assert.equal(rows['A-2'].resolve, true);
  assert.equal(rows['A-3'].resolve, true);
  assert.equal(rows['A-4'].resolve, false);
});

test('真实导出物的那一支形状：historical-direct 组里的 landed-unrecorded 一条都不出 resolve', () => {
  // 与 {{WORKSPACE}}/.data/progress-export.json 里 AW-006 那一行同一形状（它就在 historical-direct）
  const feed = normalize(payload([
    { key: 'historical-direct', label: '历史直做', tasks: [
      task('s-aw006', 'AW-006 临时目录名拼会话标识', 'AI工作区', [
        ticket('AW-006', { project: 'AI工作区', state: 'done', mergeState: 'landed-unrecorded', session: 's-gone' }),
      ]),
    ] },
  ]), NOW);
  const row = feed.projects[0].tasks[0].tickets[0];
  assert.equal(row.resolve, false);
  assert.equal(row.arrow, true, '会话 id 还在，箭头照画（它指向的那条会话也没了，但那是点箭头的事）');
  assert.match(row.note, /已在 master 里/);
});

test('箭头只在有执行会话时出现', () => {
  const feed = normalize(payload([
    { key: 'pool', label: '池子', tickets: [
      ticket('P-1', { session: '' }),
      ticket('P-2', { session: 's-9' }),
    ] },
  ]), NOW);
  const rows = Object.fromEntries(feed.projects[0].pool.map((row) => [row.id, row]));
  assert.equal(rows['P-1'].arrow, false);
  assert.equal(rows['P-2'].arrow, true);
});

test('折叠：在途组全露，其余只露 24 小时内动过的，且至少留一条', () => {
  const feed = normalize(payload([
    { key: 'review', label: '待验收', tasks: [task('s-a', '任务', '项目一', [
      ticket('A-1', { minutesAgo: 10 }),
      ticket('A-2', { minutesAgo: 60 * 30 }),
    ])] },
    { key: 'historical-direct', label: '历史直做', tasks: [task('s-b', '老任务', '项目一', [
      ticket('A-3', { state: 'done', mergeState: 'recorded', session: '', minutesAgo: 3 }),
      ticket('A-4', { state: 'done', mergeState: 'recorded', session: '', minutesAgo: 72 * 60 }),
    ], { minutesAgo: 72 * 60 })] },
  ]), NOW);

  const card = feed.projects[0];
  const review = card.tasks.find((item) => item.name === '任务');
  const old = card.tasks.find((item) => item.name === '老任务');
  assert.equal(review.shown, 2);
  assert.deepEqual(review.tickets.map((row) => row.show), [true, true]);
  assert.deepEqual(old.tickets.map((row) => row.show), [true, false]);
  assert.equal(old.shown, 1);
});

test('卡头计数数的是行（跨组不重复），总张数按单数', () => {
  const feed = normalize(payload([
    { key: 'review', label: '待验收', tasks: [task('s-a', '任务', '项目一', [
      ticket('A-1'),
      ticket('A-2'),
    ])] },
    { key: 'historical-direct', label: '历史直做', tasks: [task('s-a', '任务', '项目一', [
      ticket('A-3', { state: 'done', mergeState: 'recorded', session: '' }),
    ])] },
  ]), NOW);

  const card = feed.projects[0];
  assert.equal(card.total, 3);
  assert.equal(card.review, 2);
  assert.equal(card.needsYou, 0);
});

test('卡与任务按最近活动降序', () => {
  const feed = normalize(payload([
    { key: 'running', label: '在跑', tasks: [
      task('s-old', '旧任务', '项目一', [ticket('A-1')], { minutesAgo: 500 }),
      task('s-new', '新任务', '项目一', [ticket('A-2')], { minutesAgo: 1 }),
      task('s-b', '别的项目', '项目二', [ticket('B-1', { project: '项目二' })], { minutesAgo: 200 }),
    ] },
  ]), NOW);

  assert.deepEqual(feed.projects.map((project) => project.name), ['项目一', '项目二']);
  assert.deepEqual(feed.projects[0].tasks.map((item) => item.name), ['新任务', '旧任务']);
});

test('时钟取 generatedAt 的秒，取不到就空串', () => {
  assert.equal(clockOf('2026-11-23T08:58:48+08:00'), '08:58:48');
  assert.equal(clockOf(''), '');
  assert.equal(clockOf(null), '');
  const feed = normalize({ ...payload(), generatedAt: '2026-11-23T09:01:02+08:00' }, NOW);
  assert.equal(feed.generatedClock, '09:01:02');
});

test('不认识的 schemaVersion 直接拒', () => {
  assert.throws(() => normalize({ ...payload(), schemaVersion: 4 }, NOW), ExportError);
  assert.throws(() => normalize({ ...payload(), schemaVersion: 2 }, NOW), ExportError);
  assert.throws(() => normalize([], NOW), ExportError);
});

// ---------------------------------------------------------------- 读取源

function withTempDir(run) {
  const dir = mkdtempSync(join(tmpdir(), 'cd014-feed-'));
  try {
    return run(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('读取源：第一次就读到，状态 ok 且带 feed 与指纹', () => withTempDir((dir) => {
  const file = join(dir, 'progress-export.json');
  writeFileSync(file, JSON.stringify(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '真任务', '真项目', [ticket('R-1', { project: '真项目' })])] },
  ])), 'utf8');
  const seen = [];
  const source = new FeedSource({ filePath: file, now: () => NOW, onState: (state) => seen.push(state) });
  const state = source.poll();
  assert.equal(state.ok, true);
  assert.equal(state.feed.projects[0].name, '真项目');
  assert.ok(state.fingerprint);
  assert.equal(seen.length, 1, '第一次就要通知一次');
}));

test('读取源：mtime 没变就不重读，也不重复通知', () => withTempDir((dir) => {
  const file = join(dir, 'progress-export.json');
  writeFileSync(file, JSON.stringify(payload()), 'utf8');
  const seen = [];
  const source = new FeedSource({ filePath: file, now: () => NOW, onState: (state) => seen.push(state) });
  const first = source.poll();
  const again = source.poll();
  assert.equal(again, first, '没变时返回同一个状态对象');
  assert.equal(seen.length, 1);
}));

test('读取源：原子替换后（mtime 变）读到新内容', () => withTempDir((dir) => {
  const file = join(dir, 'progress-export.json');
  const write = (title) => writeFileSync(file, JSON.stringify(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1', { title })])] },
  ])), 'utf8');
  write('旧标题');
  const source = new FeedSource({ filePath: file, now: () => NOW });
  assert.equal(source.poll().feed.projects[0].tasks[0].tickets[0].title, '旧标题');
  write('换过的标题');
  const later = new Date(Date.now() + 5000);
  utimesSync(file, later, later);
  const state = source.poll();
  assert.equal(state.ok, true);
  assert.equal(state.feed.projects[0].tasks[0].tickets[0].title, '换过的标题');
}));

test('读取源：半截 JSON → ok:false、保留上一份 feed、reason 说得清', () => withTempDir((dir) => {
  const file = join(dir, 'progress-export.json');
  writeFileSync(file, JSON.stringify(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1')])] },
  ])), 'utf8');
  const source = new FeedSource({ filePath: file, now: () => NOW });
  const good = source.poll();
  assert.equal(good.ok, true);
  writeFileSync(file, '{"schemaVersion": 3, "groups": [', 'utf8');
  const later = new Date(Date.now() + 5000);
  utimesSync(file, later, later);
  const state = source.poll();
  assert.equal(state.ok, false);
  assert.match(state.reason, /JSON/);
  assert.deepEqual(state.feed, good.feed, '上一份好数据一个字都不能动');
}));

test('读取源：文件被移走 → ok:false、feed 保留、reason 指向读不到', () => withTempDir((dir) => {
  const file = join(dir, 'progress-export.json');
  writeFileSync(file, JSON.stringify(payload([
    { key: 'pool', label: '池子', tickets: [ticket('P-1')] },
  ])), 'utf8');
  const source = new FeedSource({ filePath: file, now: () => NOW });
  assert.equal(source.poll().ok, true);
  rmSync(file);
  const state = source.poll();
  assert.equal(state.ok, false);
  assert.match(state.reason, /读不到导出物/);
  assert.equal(state.feed.projects[0].pool[0].id, 'P-1');
}));

test('读取源：一开始就没有文件 → ok:false 且 feed 为 null（不编一份空数据顶上）', () => withTempDir((dir) => {
  const source = new FeedSource({ filePath: join(dir, 'missing.json'), now: () => NOW });
  const state = source.poll();
  assert.equal(state.ok, false);
  assert.equal(state.feed, null);
  assert.match(state.reason, /读不到导出物/);
}));


test('最新的一张单：跨组取 lastActivityAt 最大，并列按 id 升序，裸时间不算', () => {
  const payload = {
    schemaVersion: 3,
    generatedAt: iso(0),
    groups: [
      { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [
        ticket('OLD-1', { minutesAgo: 500 }),
        ticket('NEW-1', { minutesAgo: 5, title: '最新的那张' }),
      ])] },
      { key: 'pool', label: '池子', tickets: [
        ticket('POOL-1', { minutesAgo: 50 }),
        ticket('NAIVE-1', { rawAt: '2026-11-20T20:00:00' }),
      ] },
    ],
  };
  assert.deepEqual(latestTicketOf(payload), { id: 'NEW-1', title: '最新的那张' });

  // 并列时取 id 小的那个
  const tied = {
    schemaVersion: 3,
    generatedAt: iso(0),
    groups: [{ key: 'pool', label: '池子', tickets: [
      ticket('B-1', { minutesAgo: 10 }),
      ticket('A-1', { minutesAgo: 10 }),
    ] }],
  };
  assert.equal(latestTicketOf(tied).id, 'A-1');

  // 全是裸时间 → 没有「最近活动」的那张
  const naive = {
    schemaVersion: 3,
    generatedAt: iso(0),
    groups: [{ key: 'pool', label: '池子', tickets: [ticket('N-1', { rawAt: '2026-11-20T20:00:00' })] }],
  };
  assert.equal(latestTicketOf(naive), null);
});

test('读取源把「最近活动的那张单」带在状态里，换文件后跟着变', () => withTempDir((dir) => {
  const file = join(dir, 'progress-export.json');
  const write = (newestId) => writeFileSync(file, JSON.stringify({
    schemaVersion: 3,
    generatedAt: iso(0),
    groups: [{ key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [
      ticket('OLD-1', { minutesAgo: 300 }),
      ticket(newestId, { minutesAgo: 1 }),
    ])] }],
  }), 'utf8');
  write('NEW-1');
  const source = new FeedSource({ filePath: file, now: () => NOW });
  assert.equal(source.poll().latest.id, 'NEW-1');
  write('NEW-2');
  const later = new Date(Date.now() + 5000);
  utimesSync(file, later, later);
  assert.equal(source.poll().latest.id, 'NEW-2');
}));

test('不认识的组 key：整条不画，不退化成「在途、第 3 格」', () => {
  // stepsFor 自己也不给一个猜出来的轨道：返回 null，调用方据此不画
  assert.equal(stepsFor('mystery-group', 'not-merged'), null);
  assert.equal(stepsFor('', 'not-merged'), null);

  const feed = normalize(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1')])] },
    { key: 'mystery-group', label: '没见过的组', tasks: [
      task('s-m', '任务', '项目一', [ticket('MY-1')]),
    ], tickets: [ticket('MY-2', { project: '项目一', session: '' })] },
  ]), NOW);

  const card = feed.projects[0];
  const ids = card.tasks.flatMap((item) => item.tickets).map((row) => row.id);
  assert.deepEqual(ids, ['A-1']);
  assert.deepEqual(card.pool.map((row) => row.id), []);
  // 卡头张数也不把它算进去
  assert.equal(card.total, 1);
  assert.equal(card.review, 0);
  assert.equal(card.needsYou, 0);
});

test('取消单：五步全灰、无 resolve、有会话才可点进会话、不算进卡头', () => {
  const raw = payload([
    { key: 'review', label: '待验收', tasks: [task('s-a', '任务', '项目一', [ticket('A-1')])] },
  ]);
  raw.cancelled = [
    { id: 'X-1', title: '取消甲', project: '项目一',
      cancelledAt: '2026-11-22T10:00:00+08:00', sessionId: 's-x1' },
    { id: 'X-2', title: '取消乙', project: '', cancelledAt: '', sessionId: '' },
  ];
  const feed = normalize(raw, NOW);

  assert.equal(feed.cancelled.length, 2);
  const [first, second] = feed.cancelled;
  assert.deepEqual(first.steps, ['todo', 'todo', 'todo', 'todo', 'todo']);
  assert.deepEqual(second.steps, ['todo', 'todo', 'todo', 'todo', 'todo']);
  assert.equal(first.resolve, false);
  assert.equal(second.resolve, false);
  assert.equal(first.arrow, true);
  assert.equal(first.sessionId, 's-x1');
  assert.equal(second.arrow, false);
  assert.equal(second.sessionId, '');
  // 行下那一句：`项目 · 取消 MM-DD`；两次都读不到就整行不画
  assert.equal(first.note, '项目一 · 取消 11-22');
  assert.equal(second.note, '');

  // 取消单不进卡头两个计数，也不撑大张数
  const card = feed.projects[0];
  assert.equal(card.total, 1);
  assert.equal(card.review, 1);
  assert.equal(card.needsYou, 0);
});

test('读不到 cancelled（旧导出物）时是空数组，页面照常', () => {
  const feed = normalize(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1')])] },
  ]), NOW);
  assert.deepEqual(feed.cancelled, []);
  assert.equal(feed.projects.length, 1);

  // 键在但不是数组：同样当「读不到」，不抛错、不猜一个形状出来
  const odd = payload();
  odd.cancelled = { id: 'X-9' };
  assert.deepEqual(normalize(odd, NOW).cancelled, []);
});

test('项目根：导出物 projects[].root 落在卡上，读不到就是空串', () => {
  const raw = payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1')])] },
    { key: 'pool', label: '池子', tickets: [ticket('B-1', { project: '项目二', session: '' })] },
  ]);
  raw.projects = [
    { id: 'p-1', name: '项目一', root: 'D:/项目一' },
    { id: 'p-2', name: '项目二' },
  ];
  const cards = Object.fromEntries(normalize(raw, NOW).projects.map((card) => [card.name, card]));
  assert.equal(cards['项目一'].root, 'D:/项目一');
  assert.equal(cards['项目二'].root, '');

  // 整份导出物没有 projects 那一栏（CD-012 那几版）：每张卡都是空串，不抛错
  const bare = payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1')])] },
  ]);
  assert.equal(normalize(bare, NOW).projects[0].root, '');
});

// ---------------------------------------------------------------- 会话还在不在（CD-021）

test('会话还在：箭头照画，不带 sessionGone（与 CD-022 一样）', () => {
  const feed = normalize(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1', { session: 's-live' })])] },
  ]), NOW, { liveSessions: new Set(['s-live', 's-other']) });
  const row = feed.projects[0].tasks[0].tickets[0];
  assert.equal(row.arrow, true);
  assert.equal(row.sessionId, 's-live');
  assert.equal('sessionGone' in row, false, '还活着的一行不许写这个键');
});

test('会话没了：箭头不画（不摆死链），id 留着给 resolve 说明，sessionGone 标出来', () => {
  const feed = normalize(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1', { session: 's-gone' })])] },
  ]), NOW, { liveSessions: new Set(['s-live']) });
  const row = feed.projects[0].tasks[0].tickets[0];
  assert.equal(row.arrow, false, '页面靠 arrow && sessionId 画链接，false 就是一个空盒子');
  assert.equal(row.sessionId, 's-gone', 'id 留着：resolve 的结果文案要引用它');
  assert.equal(row.sessionGone, true);
});

test('读不到会话库（null）就是不知道：照旧可点，不把「不知道」说成「没了」', () => {
  const feed = normalize(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1', { session: 's-x' })])] },
  ]), NOW, { liveSessions: null });
  const row = feed.projects[0].tasks[0].tickets[0];
  assert.equal(row.arrow, true);
  assert.equal('sessionGone' in row, false);
});

test('取消单的绿图标也是会话链接：会话没了就不画链接', () => {
  const raw = payload();
  raw.cancelled = [
    { id: 'C-1', title: '取消一', project: '项目一', cancelledAt: iso(30), sessionId: 's-gone' },
    { id: 'C-2', title: '取消二', project: '项目一', cancelledAt: iso(20), sessionId: 's-live' },
  ];
  const rows = normalize(raw, NOW, { liveSessions: new Set(['s-live']) }).cancelled;
  assert.equal(rows[0].arrow, false);
  assert.equal(rows[0].sessionGone, true);
  assert.equal(rows[1].arrow, true);
  assert.equal('sessionGone' in rows[1], false);
});

test('读取源：导出物没变、会话被删了 → 也重建一次（arrow 摘掉），并通知一次', () => withTempDir((dir) => {
  const file = join(dir, 'progress-export.json');
  writeFileSync(file, JSON.stringify(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1', { session: 's-1' })])] },
  ])), 'utf8');
  let live = new Set(['s-1']);
  const seen = [];
  const source = new FeedSource({
    filePath: file,
    now: () => NOW,
    onState: (state) => seen.push(state),
    sessionIndex: () => live,
  });
  const first = source.poll();
  assert.equal(first.feed.projects[0].tasks[0].tickets[0].arrow, true);

  live = new Set(); // 会话被删掉，导出物一个字节都没动
  const second = source.poll();
  assert.notEqual(second, first, '会话没了必须重建');
  assert.equal(second.feed.projects[0].tasks[0].tickets[0].arrow, false);
  assert.equal(second.feed.projects[0].tasks[0].tickets[0].sessionGone, true);
  assert.equal(seen.length, 2, '重建了就要通知页面一次');

  // 再问一次、存活表没变 → 不再重建（每 2 秒问一次会话库不等于每 2 秒重画一遍）
  const third = source.poll();
  assert.equal(third, second);
  assert.equal(seen.length, 2);
}));

test('读取源：会话库问不出来（抛错）就按不知道处理，feed 照常出', () => withTempDir((dir) => {
  const file = join(dir, 'progress-export.json');
  writeFileSync(file, JSON.stringify(payload([
    { key: 'running', label: '在跑', tasks: [task('s-a', '任务', '项目一', [ticket('A-1', { session: 's-1' })])] },
  ])), 'utf8');
  const source = new FeedSource({
    filePath: file,
    now: () => NOW,
    sessionIndex: () => { throw new Error('库被占用'); },
  });
  const state = source.poll();
  assert.equal(state.ok, true);
  assert.equal(state.feed.projects[0].tasks[0].tickets[0].arrow, true);
}));
