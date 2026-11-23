// 黄金夹具：同一份导出物喂给 Python 的 tools/exportfeed.py 与 JS 的 app/feed.mjs，
// 两边的输出必须逐字段相等。以后谁改了一边没改另一边，这条就会红。
//
// 夹具：tests/fixtures/export-golden.json（脱敏；覆盖全部 9 个 group key、mergeState 四态、
// 池子单、并列时间、无时区时间戳、空 sessionId 的兜底 key）
// 期望：tests/fixtures/export-golden.expected.json
//
// 重新生成期望值的命令（**改了夹具或 Python 侧映射之后必须重跑**）：
//   python tools/exportfeed.py tests/fixtures/export-golden.json \
//     --now 1795176000 --out tests/fixtures/export-golden.expected.json
//
// 1795176000 是夹具里「现在」那一刻（2026-11-20T20:00:00+08:00），折叠的 24 小时窗口以它为准。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import test from 'node:test';

import { normalize } from '../feed.mjs';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const FIXTURE = join(ROOT, 'tests', 'fixtures', 'export-golden.json');
const EXPECTED = join(ROOT, 'tests', 'fixtures', 'export-golden.expected.json');
const NOW_SECONDS = 1795176000;

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'));
}

test('黄金夹具：JS 的归一结果与 Python 的完全一致', () => {
  const payload = readJson(FIXTURE);
  const expected = readJson(EXPECTED);
  const actual = normalize(payload, NOW_SECONDS * 1000);
  assert.deepStrictEqual(actual, expected);
});

test('黄金夹具覆盖到的口径都真的在（防止夹具退化）', () => {
  const payload = readJson(FIXTURE);
  const expected = readJson(EXPECTED);

  const groups = new Set(payload.groups.map((group) => group.key));
  for (const key of ['needs-you', 'review', 'running', 'queued', 'pool',
    'unmerged', 'merge-unlanded', 'merged-unrecorded', 'historical-direct']) {
    assert.ok(groups.has(key), `夹具缺 group：${key}`);
  }

  const mergeStates = new Set();
  const ids = new Set();
  for (const group of payload.groups) {
    for (const task of group.tasks || []) {
      for (const ticket of task.tickets) {
        mergeStates.add(ticket.mergeState);
        ids.add(ticket.id);
      }
    }
    for (const ticket of group.tickets || []) {
      mergeStates.add(ticket.mergeState);
      ids.add(ticket.id);
    }
  }
  for (const state of ['recorded', 'landed-unrecorded', 'recorded-unlanded', 'not-merged']) {
    assert.ok(mergeStates.has(state), `夹具缺 mergeState：${state}`);
  }

  const cardByName = Object.fromEntries(expected.projects.map((project) => [project.name, project]));
  // 池子单：Python 那边没有 show 键，JS 也不许有
  for (const project of expected.projects) {
    for (const ticket of project.pool) {
      assert.equal('show' in ticket, false, `池子单 ${ticket.id} 不该带 show`);
    }
  }
  // 并列时间按 id 升序，无时区时间戳排最后
  const history = cardByName['项目丙'].tasks.find((task) => task.key === 'task-2');
  assert.deepEqual(history.tickets.map((ticket) => ticket.id), ['H-4', 'H-1', 'H-2', 'H-3']);
  // 折叠：老的那张（超过 24 小时）默认不露
  const merged = cardByName['项目丙'].tasks.find((task) => task.key === 's-md');
  assert.deepEqual(merged.tickets.map((ticket) => ticket.show), [true, false]);
  assert.ok(ids.size >= 15);
});
