// 拿**真导出物 + 真会话库**核对「箭头与 resolve 是不是同一套判定」（CD-021 C5）。
//
// 为什么单独一个脚本：`python tools/run_tests.py` 与 `app/test` 只能用夹具，不能依赖用户电脑上
// 某一份导出物或某一份会话库。而这一单的现场是「会话早就不在了，箭头照旧点进去、宿主只回一句
// 找不到这条任务」——判据要能对着真数据重跑：
//
//   node tools/verify-session-targets.mjs [导出物路径] [会话库路径]
//
// 默认读 {{WORKSPACE}}/.data/progress-export.json（套件源码树里这会留着占位符，跑的时候传第一个参数），会话库取导出物自报的 `source.sessionsDb`
// （读不到就退回 %APPDATA%/Cindy 下 mtime 最新的 cindy-*.db）。判据（任何一条不成立就退出 1）：
//   1. 屏上每一行都满足 `arrow === 有 sessionId 且会话没被删`——没有第三套判法；
//   2. 箭头与 resolve 同一个答案：arrow 为真的行算出来必须是 open-session，
//      arrow 为假而带 sessionId 的行（会话已删）必须落 new-session（没有项目根才是 blocked）；
//   3. 池子里没有会话的行仍然落 new-session（不是 blocked，也不是「交回」）；
//   4. 演练：把一条真会话当成已删（只改这一份内存里的存活表），那一行必须立刻变成
//      「不画箭头 + 去项目根新建会话」——这一条证明「会话没了」这一支真的接到了屏上，
//      而不是等某天碰巧有条会话被删才第一次跑。
//
// 只读：读一个 JSON、读一次会话库（一条 SELECT），不写盘、不发请求、不开窗口、不调 openExternal。

import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

import { normalize } from '../app/feed.mjs';
import { planResolve } from '../app/resolve.mjs';
import { pickSessionsDb, readLiveSessions } from '../app/sessions.mjs';

const DEFAULT_EXPORT = '{{WORKSPACE}}/.data/progress-export.json';
const exportPath = process.argv[2] || DEFAULT_EXPORT;
const cindyDir = process.env.CINDY_WIDGET_CINDY_DIR
  || join(process.env.APPDATA || homedir(), 'Cindy');

const failures = [];
const ok = (condition, message) => {
  if (!condition) failures.push(message);
  console.log((condition ? '  ✔ ' : '  ✘ ') + message);
};

const payload = JSON.parse(readFileSync(exportPath, 'utf8'));
const dbPath = process.argv[3] || pickSessionsDb(payload, cindyDir);
const live = readLiveSessions(dbPath);

console.log(`导出物：${exportPath}`);
console.log(`会话库：${dbPath || '（没找到）'}${live ? `，${live.size} 条可去` : '，读不到 → 判据按「不知道」走'}`);
if (!live) {
  console.log('  ! 读不到会话库时本脚本只能核「不知道」那一支：箭头照画、resolve 照旧交回。');
}

const feed = normalize(payload, Date.now(), { liveSessions: live });

const rows = [];
for (const project of feed.projects) {
  for (const task of project.tasks) for (const row of task.tickets) rows.push({ row, project });
  for (const row of project.pool) rows.push({ row, project });
}
// 取消单只核箭头（它们本来就不出 resolve，也不带项目根）
const cancelledRows = feed.cancelled.map((row) => ({ row, project: { name: row.project, root: '' } }));
const all = rows.concat(cancelledRows);

console.log(`生成时刻：${feed.generatedAt}（${feed.projects.length} 张项目卡，${rows.length} 条行 + ${cancelledRows.length} 条取消单）`);

// 1. 每行的箭头只由「有没有会话 + 会话还在不在」决定
const arrowWrong = all.filter(({ row }) => row.arrow !== (Boolean(row.sessionId) && !row.sessionGone));
ok(arrowWrong.length === 0,
  `arrow 与「有会话且没被删」一致（${all.length} 行，不一致 ${arrowWrong.length} 行）`);
for (const { row } of arrowWrong) {
  console.log(`     反例：${row.id} arrow=${row.arrow} sessionId=${JSON.stringify(row.sessionId)} gone=${Boolean(row.sessionGone)}`);
}

// 2. 箭头与 resolve 同一个答案
const planMiss = [];
const counts = { 'open-session': 0, 'new-session': 0, blocked: 0 };
for (const { row, project } of rows) {
  const plan = planResolve(row, project);
  counts[plan.kind] += 1;
  if (row.arrow) {
    if (plan.kind !== 'open-session') planMiss.push(`${row.id}: 箭头在，算路却是 ${plan.kind}`);
    if (plan.sessionId !== row.sessionId) planMiss.push(`${row.id}: 箭头去的会话与算路的不一致`);
  } else if (row.sessionId) {
    if (plan.kind === 'open-session') planMiss.push(`${row.id}: 会话已删，算路还去叫它`);
    if (plan.kind === 'blocked' && project.root) planMiss.push(`${row.id}: 项目根有却没有落 new-session`);
  } else if (plan.kind === 'blocked' && project.root) {
    planMiss.push(`${row.id}: 池子行有项目根，却报 blocked`);
  }
}
ok(planMiss.length === 0, `箭头与 resolve 同一套判定（${JSON.stringify(counts)}）`);
for (const message of planMiss) console.log('     ' + message);

// 3. 屏上到底有几行「会话没了」
const gone = all.filter(({ row }) => row.sessionGone);
console.log(`  此行真被删掉的会话：${gone.length} 条${gone.length ? '：' + gone.map(({ row }) => row.id).join(' / ') : '（今天这份导出物里没有被删的）'}`);

// 4. 演练：把一条真会话当成已删，看那一行是不是立刻改道
const sample = rows.find(({ row }) => row.sessionId && !row.sessionGone);
if (!sample) {
  console.log('  ! 没有可演练的行（屏上一条带会话的行都没有）');
} else {
  const id = sample.row.sessionId;
  const rehearsed = new Set(live ? [...live] : []);
  rehearsed.delete(id);
  const drillFeed = normalize(payload, Date.now(), { liveSessions: rehearsed });
  const drilled = [];
  for (const project of drillFeed.projects) {
    for (const task of project.tasks) for (const row of task.tickets) drilled.push({ row, project });
    for (const row of project.pool) drilled.push({ row, project });
  }
  const after = drilled.find(({ row }) => row.id === sample.row.id);
  ok(after && after.row.sessionGone === true && after.row.arrow === false,
    `演练：把 ${sample.row.id} 的会话当成已删 → 箭头不画、标记 sessionGone`);
  const plan = after ? planResolve(after.row, after.project) : null;
  ok(Boolean(plan) && plan.kind !== 'open-session',
    `演练：同一行的 resolve 改去项目根新建会话（实际 ${plan && plan.kind}）`);
  if (plan && plan.kind === 'new-session') console.log(`     落点：${plan.workdir}`);
}

if (failures.length) {
  console.error(`\n判据不成立（${failures.length} 条）：`);
  for (const message of failures) console.error('  · ' + message);
  process.exit(1);
}
console.log('\n全部判据成立。');
