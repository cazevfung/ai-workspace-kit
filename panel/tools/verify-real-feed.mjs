// 拿**真导出物**核对「哪一行出 resolve」（CD-022）。
//
// 为什么单独一个脚本：`python tools/run_tests.py` 与 `app/test` 只能用夹具，不能依赖用户电脑上
// 某一份导出物。而这一单的起点就是一张真实现场的截图（AW-006 点 resolve → 宿主弹「找不到这条任务」），
// 所以要有一条能对着真数据重跑的命令，把判据落在它身上：
//
//   node tools/verify-real-feed.mjs [导出物路径]
//
// 默认读 {{WORKSPACE}}/.data/progress-export.json（套件源码树里这会留着占位符，跑的时候传第一个参数）。判据（任何一条不成立就退出 1）：
//   1. AW-006 那一行在屏上（它属于 historical-direct 组），且**没有** resolve；
//   2. 导出物里每一张 landed-unrecorded / recorded 的单，在屏上都不带 resolve；
//   3. 还有下一个人要动手的单（not-merged / recorded-unlanded）**仍然带** resolve——
//      一律关掉 resolve 是这一单的反例；
//   4. 没有会话的池子行会被算成 new-session（它不会去叫宿主）。
//
// 只读：读一个文件、跑一遍归一，不写盘、不发请求、不开窗口。

import { readFileSync } from 'node:fs';

import { normalize } from '../app/feed.mjs';
import { planResolve } from '../app/resolve.mjs';

const DEFAULT_EXPORT = '{{WORKSPACE}}/.data/progress-export.json';
const exportPath = process.argv[2] || DEFAULT_EXPORT;

const failures = [];
const ok = (condition, message) => {
  if (!condition) failures.push(message);
  console.log((condition ? '  ✔ ' : '  ✘ ') + message);
};

const payload = JSON.parse(readFileSync(exportPath, 'utf8'));
const feed = normalize(payload, Date.now());

// 导出物里的 mergeState 索引：屏上的行不带它，判据只认 feed 层的结果，两边对起来看
const mergeStates = new Map();
for (const group of payload.groups || []) {
  const entries = [
    ...(group.tasks || []).flatMap((task) => task.tickets || []),
    ...(group.tickets || []),
  ];
  for (const entry of entries) {
    mergeStates.set(String(entry.id || ''), String(entry.mergeState || ''));
  }
}

const rows = [];
for (const project of feed.projects) {
  for (const task of project.tasks) for (const row of task.tickets) rows.push({ row, project });
  for (const row of project.pool) rows.push({ row, project });
}

console.log(`导出物：${exportPath}`);
console.log(`生成时刻：${feed.generatedAt}（${feed.projects.length} 张项目卡，${rows.length} 条单）`);

// 1. 真实现场那一行
const aw006 = rows.find(({ row }) => row.id === 'AW-006');
ok(Boolean(aw006), 'AW-006 这一行在屏上（它在 historical-direct 组里，组被认出来了）');
if (aw006) {
  console.log(`     AW-006 · ${aw006.row.title}`);
  console.log(`     合并态=${mergeStates.get('AW-006')}  行下说明="${aw006.row.note}"`);
  ok(mergeStates.get('AW-006') === 'landed-unrecorded', 'AW-006 的合并态是 landed-unrecorded');
  ok(aw006.row.resolve === false, 'AW-006 那一行没有 resolve（点了只会去叫一条早就没了的会话）');
}

// 2. 「没人要动手」的单一条都不许带 resolve
const settled = rows.filter(({ row }) => ['landed-unrecorded', 'recorded'].includes(mergeStates.get(row.id)));
const settledWithResolve = settled.filter(({ row }) => row.resolve);
ok(settled.length > 0, `导出物里有 ${settled.length} 张 landed-unrecorded / recorded 的单可以核对`);
ok(settledWithResolve.length === 0,
  '它们一条都不带 resolve' + (settledWithResolve.length ? `（还在的：${settledWithResolve.map(({ row }) => row.id).join(', ')}）` : ''));

// 3. 还有下一个人要动手的，resolve 仍然在（反例：一律关掉）
const open = rows.filter(({ row }) => ['not-merged', 'recorded-unlanded'].includes(mergeStates.get(row.id)));
const openWithResolve = open.filter(({ row }) => row.resolve);
ok(open.length > 0, `导出物里有 ${open.length} 张还要人动手的单`);
ok(openWithResolve.length === open.length,
  '它们每一张都还带 resolve（不许一律关掉）' + (openWithResolve.length === open.length
    ? '' : `（缺的：${open.filter(({ row }) => !row.resolve).map(({ row }) => row.id).join(', ')}）`));
if (open.length) {
  console.log(`     例：${openWithResolve.slice(0, 3).map(({ row }) => row.id).join(', ')}`);
}

// 4. 没有会话的池子行：算成 new-session，带出项目根，不去叫宿主
const noSession = rows.filter(({ row }) => !row.sessionId);
const wrong = noSession.filter(({ row, project }) => {
  const plan = planResolve(row, project);
  return plan.kind !== 'new-session' || !plan.workdir;
});
ok(noSession.length > 0, `导出物里有 ${noSession.length} 条没有会话的行`);
ok(wrong.length === 0,
  '它们都算成「去项目根新建会话」且根目录读得到' + (wrong.length
    ? `（不对的：${wrong.map(({ row }) => row.id).join(', ')}）` : ''));
if (noSession.length) {
  const sample = noSession[0];
  console.log(`     例：${sample.row.id} → ${JSON.stringify(planResolve(sample.row, sample.project))}`);
}

if (failures.length) {
  console.error(`\n失败 ${failures.length} 条：`);
  failures.forEach((item, index) => console.error(`  ${index + 1}. ${item}`));
  process.exit(1);
}
console.log('\n全部成立：真导出物上，该出 resolve 的出、不该出的不出。');
