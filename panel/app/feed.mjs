// progress-export.json（schemaVersion 3）→ 浮窗页吃的那份 FEED，以及「按 mtime+size 判变」的读取源。
//
// 这是 tools/exportfeed.py 的 JS 版：同一份导出物、同一套映射（一卡一项目、五步、合并四态、
// 24 小时窗口折叠、卡头按行计数）。HTTP 那条路还走 Python 的 exportfeed + /api/feed，
// 浏览器里没有主进程，那条路不删；Electron 里走这份，不再需要 token、也不起 HTTP。
//
// 只读：这里只 stat / read 一个文件，不写盘、不起子进程、不发网络请求。

import { readFileSync, statSync } from 'node:fs';

import { sameSessions, sessionGone } from './sessions.mjs';

export const SCHEMA_VERSION = 3;
export const RECENT_WINDOW_MS = 24 * 60 * 60 * 1000;
export const TASKS_SHOWN = 3;
export const POOL_SHOWN = 2;

//: 组 key →（走到第几格 0 起，是不是「在途」组）
//: 不认识的组 key 一条都不画（CD-019）：以前兜底成「在途、第 3 格」，导出物每多一个新组，
//: 屏上就凭空多一行没人认识的单，还带着一个假的当前步。
export const GROUPS = {
  'needs-you': [2, true],
  review: [3, true],
  running: [2, true],
  queued: [0, false],
  pool: [0, false],
  unmerged: [4, false],
  'merge-unlanded': [4, false],
  'merged-unrecorded': [4, false],
  'historical-direct': [4, false],
};

export const MERGE_NOTE = {
  'landed-unrecorded': '已在 master 里，但库里没有合并记录',
  'recorded-unlanded': '库里记了合并，但那个提交不在 master 里',
};

/** 这两种合并态不出 resolve：没有下一个人要动手。
 *
 * `recorded` —— 已合，账也记了；
 * `landed-unrecorded` —— 代码已经在 master 里，只差库里那一行合并记录（账走 backfill 或豁免）。
 * 后者以前是出 resolve 的，点下去叫的是一条早就没了的老会话：宿主弹「找不到这条任务」，
 * 浮窗却写「已交回：已打开 …」（CD-022 现场）。判就写在这里一处，页面不另判一遍。 */
export const SETTLED_MERGE_STATES = ['recorded', 'landed-unrecorded'];

/** 这张单该不该出 resolve（判据只有合并态，与组 key 无关）。 */
export function hasResolve(mergeState) {
  return !SETTLED_MERGE_STATES.includes(String(mergeState || ''));
}

/**
 * 这一行的会话还能不能去（CD-021）：`live` 是会话库里的存活表，`null` 表示「不知道」。
 *
 * 三个答案分开说，别合成一个布尔：
 *   · 没有 sessionId —— 这行本来就没有会话（池子单），不是「没了」；
 *   · 不知道     —— 读不到会话库，照 CD-022 的老行为：箭头照画、文案里预告「会提示找不到」；
 *   · 没了       —— 库里没有这一行、或它是 `deleted`：箭头不画（不摆一条点了只会弹「找不到」的链接），
 *                   resolve 改走「在项目根新建会话」那一支，两处用的是同一个 `live`。
 */
export function sessionTargetOf(sessionId, live) {
  const id = String(sessionId || '');
  if (!id) return { state: 'none', arrow: false, sessionId: '' };
  if (sessionGone(id, live)) return { state: 'gone', arrow: false, sessionId: id };
  return { state: live instanceof Set ? 'live' : 'unknown', arrow: true, sessionId: id };
}

/** 取消单的五步：**五格全灰、没有当前步**（与鸿蒙 `cancelledSteps()` 同一口径）。
 *  不用 `stepsFor('queued')`：那会把第一格画成橙的当前步，读起来像「马上要开跑」。 */
export const CANCELLED_STEPS = ['todo', 'todo', 'todo', 'todo', 'todo'];

export class ExportError extends Error {}

/** 文件「变没变」的指纹：mtime + 长度。原子替换写会让 mtime 动。 */
export function fingerprintOf(filePath) {
  const stat = statSync(filePath);
  return `${stat.mtimeMs}-${stat.size}`;
}

/** 读一份完整 JSON。不重试、不拼接、不建缓存文件。 */
export function readPayload(filePath) {
  let raw;
  try {
    raw = readFileSync(filePath);
  } catch (error) {
    throw new ExportError(`读不到导出物：${error.message}`);
  }
  try {
    return JSON.parse(raw.toString('utf8'));
  } catch (error) {
    throw new ExportError(`导出物不是完整的 JSON：${error.message}`);
  }
}

/** 「2026-11-23T08:58:48+08:00」→「08:58:48」。取不到就给空串，不拿本机时钟顶替。 */
export function clockOf(generatedAt) {
  if (typeof generatedAt !== 'string' || !generatedAt.includes('T')) {
    return '';
  }
  const rest = generatedAt.split('T')[1];
  return rest.length >= 8 ? rest.slice(0, 8) : '';
}

//: 与 Python 的 datetime.fromisoformat + tzinfo 检查同口径：只认带时区的 ISO 时刻。
//: 裸时间（没有 Z / ±hh:mm）在 Python 那边是「不知道」，在这里也不能被 Date.parse 按本机时区当成有效时间。
const ISO_WITH_ZONE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})$/;

function activityMs(text) {
  if (typeof text !== 'string' || !text || !ISO_WITH_ZONE.test(text)) {
    return null;
  }
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : null;
}

/** 五步轨道（每格一个状态）。最后一格由 mergeState 覆写，与鸿蒙 withMergeState() 同一套。
 *  不认识的组 key 返回 null：调用方据此整条不画，不再兜底成「在途、第 3 格」。 */
export function stepsFor(groupKey, mergeState) {
  if (!Object.prototype.hasOwnProperty.call(GROUPS, groupKey)) {
    return null;
  }
  const reached = GROUPS[groupKey][0];
  if (reached >= 4) {
    if (mergeState === 'recorded') {
      return ['done', 'done', 'done', 'done', 'done'];
    }
    if (mergeState === 'landed-unrecorded') {
      return ['done', 'done', 'done', 'done', 'landed'];
    }
    if (mergeState === 'recorded-unlanded') {
      return ['done', 'done', 'done', 'done', 'unlanded'];
    }
    return ['done', 'done', 'done', 'done', 'current'];
  }
  if (reached === 3) {
    return ['done', 'done', 'done', 'current', 'next'];
  }
  if (reached === 2) {
    return ['done', 'done', 'current', 'todo', 'todo'];
  }
  return ['current', 'todo', 'todo', 'todo', 'todo'];
}

function ticketOf(entry, groupKey, context = {}) {
  const mergeState = entry.mergeState || '';
  const steps = stepsFor(groupKey, mergeState);
  if (steps === null) {
    return null;
  }
  const target = sessionTargetOf(entry.ticketSessionId, context.liveSessions);
  return {
    id: String(entry.id || ''),
    title: String(entry.title || ''),
    steps,
    resolve: hasResolve(mergeState),
    arrow: target.arrow,
    // 行尾两枚图标都要一个可去的目标：箭头与取消行的绿 circle-slash 都靠它。
    // 会话没了时这里仍留着 id（prompt 与文案要引用它），但 arrow 是 false，页面不画链接。
    sessionId: target.sessionId,
    // 只在「真的知道它没了」时出现；读不到会话库就不写这个键
    ...(target.state === 'gone' ? { sessionGone: true } : {}),
    note: MERGE_NOTE[mergeState] || '',
    _at: entry.lastActivityAt || '',
    _group: groupKey,
  };
}

function orderByActivity(items) {
  return items.slice().sort((a, b) => {
    const am = activityMs(a._at);
    const bm = activityMs(b._at);
    if (am === null && bm === null) return String(a.id).localeCompare(String(b.id));
    if (am === null) return 1;
    if (bm === null) return -1;
    if (am !== bm) return bm - am;
    return String(a.id).localeCompare(String(b.id));
  });
}

/** 标出默认露哪些单：在途那组的全露；其余只露 24 小时内动过的，且至少留最新那一条。 */
export function markShown(tickets, groupKey, nowMs) {
  const inFlight = GROUPS[groupKey][1];
  let shown = 0;
  tickets.forEach((ticket, index) => {
    const ms = activityMs(ticket._at);
    const visible = inFlight || index === 0 || (ms !== null && nowMs - ms <= RECENT_WINDOW_MS);
    ticket.show = visible;
    if (visible) shown += 1;
  });
  return shown;
}

/**
 * 最近活动的那一张单（折叠态那一行要它）：全导出物里 lastActivityAt 最大的一张。
 * 时间无效（无时区/非法）的不参与；并列按 id 升序取第一个，与其它排序同一把尺子。
 */
export function latestTicketOf(payload) {
  let best = null;
  let bestMs = null;
  const consider = (entry) => {
    if (!entry || typeof entry !== 'object') return;
    const ms = activityMs(entry.lastActivityAt);
    if (ms === null) return;
    const id = String(entry.id || '');
    if (best === null || ms > bestMs || (ms === bestMs && id < best.id)) {
      best = { id, title: String(entry.title || '') };
      bestMs = ms;
    }
  };
  for (const group of payload.groups || []) {
    for (const task of group.tasks || []) {
      for (const ticket of task.tickets || []) consider(ticket);
    }
    for (const ticket of group.tickets || []) consider(ticket);
  }
  return best;
}

/** 导出物 → FEED。返回的就是页面直接吃的那一份。 */
/**
 * 取消行的行下那一句：`项目 · 取消 11-22`。
 * 只拿时刻字符串里的 `MM-DD`，**不做时区换算**：两端（Python / JS）必须逐字相等。
 * 读不到时刻就不写，也不编一个日期。
 */
export function cancelledNote(project, cancelledAt) {
  let when = '';
  if (typeof cancelledAt === 'string' && cancelledAt.includes('T') && cancelledAt.length >= 10) {
    when = `取消 ${cancelledAt.slice(5, 10)}`;
  }
  const name = String(project || '');
  if (!name) return when;
  return when ? `${name} · ${when}` : name;
}

/**
 * 导出物顶层 `cancelled` → 页面那一段取消单。
 * 读不到（旧导出物没有这个键 / 不是数组）就是空数组——**页面上什么都不多、也不报错**。
 * 排序不在这里做：导出器已按「取消时刻新的在前」排好，重排一次就是两端口径分叉。
 */
export function cancelledOf(payload, context = {}) {
  const raw = payload.cancelled;
  if (!Array.isArray(raw)) return [];
  const rows = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue;
    // 取消单的绿 circle-slash 也是一条会话链接：没了就不画链接（与卡内箭头同一套判定）
    const target = sessionTargetOf(entry.sessionId, context.liveSessions);
    rows.push({
      id: String(entry.id || ''),
      title: String(entry.title || ''),
      project: String(entry.project || ''),
      cancelledAt: String(entry.cancelledAt || ''),
      steps: CANCELLED_STEPS.slice(),
      // 取消单没有 resolve（不需要再交给谁收口），也不在卡头两个计数里
      resolve: false,
      arrow: target.arrow,
      sessionId: target.sessionId,
      ...(target.state === 'gone' ? { sessionGone: true } : {}),
      note: cancelledNote(entry.project, entry.cancelledAt),
    });
  }
  return rows;
}

/**
 * 项目名 / 项目 id → 项目根（导出物 `projects[].root`，CH-093 起有）。
 * 两个键都记：卡是按**单自己的 project 名**分的，而旧导出物里那一栏有时是 id。
 * 读不到就没有这个键，卡上的 root 是空串——**不拿当前目录、不猜路径**。
 */
export function rootsOf(payload) {
  const roots = new Map();
  for (const entry of payload.projects || []) {
    if (!entry || typeof entry !== 'object') continue;
    const root = String(entry.root || '');
    if (!root) continue;
    for (const key of [entry.name, entry.id]) {
      if (typeof key === 'string' && key && !roots.has(key)) roots.set(key, root);
    }
  }
  return roots;
}

export function normalize(payload, nowMs, context = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new ExportError('导出物顶层不是一个对象');
  }
  if (payload.schemaVersion !== SCHEMA_VERSION) {
    throw new ExportError(`导出物 schemaVersion 是 ${JSON.stringify(payload.schemaVersion)}，只会读 ${SCHEMA_VERSION}`);
  }
  // 会话存活表由调用方给（main.mjs 从导出物自报的会话库里读）；不给就是「不知道」
  const liveSessions = context.liveSessions instanceof Set ? context.liveSessions : null;

  const buckets = new Map();
  const roots = rootsOf(payload);
  const bucket = (name) => {
    if (!buckets.has(name)) {
      buckets.set(name, { name, tasks: [], pool: [] });
    }
    return buckets.get(name);
  };

  for (const group of payload.groups || []) {
    if (!group || typeof group !== 'object') continue;
    const groupKey = String(group.key || '');
    // 不认识的组 key：整组不画（CD-019）。以前兜底成「在途、第 3 格」，屏上凭空多一行。
    if (!Object.prototype.hasOwnProperty.call(GROUPS, groupKey)) continue;
    for (const task of group.tasks || []) {
      if (!task || typeof task !== 'object') continue;
      // 一张主任务名下的单可能跨项目：按单自己的 project 分卡（与 exportfeed.py 同一口径）
      const byProject = new Map();
      for (const entry of task.tickets || []) {
        if (!entry || typeof entry !== 'object') continue;
        const row = ticketOf(entry, groupKey, { liveSessions });
        if (row === null) continue;
        const name = String(entry.project || task.project || '');
        if (!byProject.has(name)) byProject.set(name, []);
        byProject.get(name).push(row);
      }
      for (const [project, tickets] of byProject) {
        const ordered = orderByActivity(tickets);
        const board = bucket(project);
        board.tasks.push({
          key: String(task.sessionId || '') || `task-${board.tasks.length}`,
          name: String(task.title || ''),
          tickets: ordered,
          shown: markShown(ordered, groupKey, nowMs),
          _at: String(task.lastActivityAt || ''),
        });
      }
    }
    for (const entry of group.tickets || []) {
      if (!entry || typeof entry !== 'object') continue;
      const row = ticketOf(entry, groupKey, { liveSessions });
      if (row === null) continue;
      bucket(String(entry.project || '')).pool.push(row);
    }
  }

  const projects = [];
  for (const board of buckets.values()) {
    const tasks = board.tasks.slice().sort((a, b) => (activityMs(b._at) || 0) - (activityMs(a._at) || 0));
    const pool = orderByActivity(board.pool);
    let total = 0;
    let latest = 0;
    for (const task of tasks) {
      total += task.tickets.length;
      latest = Math.max(latest, activityMs(task._at) || 0);
      delete task._at;
    }
    for (const ticket of pool) {
      latest = Math.max(latest, activityMs(ticket._at) || 0);
    }
    let review = 0;
    let needsYou = 0;
    const settle = (ticket) => {
      delete ticket._at;
      const group = ticket._group;
      delete ticket._group;
      if (group === 'review') review += 1;
      else if (group === 'needs-you') needsYou += 1;
    };
    for (const task of tasks) {
      for (const ticket of task.tickets) settle(ticket);
    }
    for (const ticket of pool) settle(ticket);
    total += pool.length;
    projects.push({
      name: board.name,
      total,
      review,
      needsYou,
      // resolve 在项目里开会话时用得到（CH-093 的导出物带上了 root）；读不到就是空串
      root: roots.get(board.name) || '',
      tasks,
      shownTasks: Math.min(TASKS_SHOWN, tasks.length),
      pool,
      shownPool: Math.min(POOL_SHOWN, pool.length),
      _latest: latest,
    });
  }
  projects.sort((a, b) => b._latest - a._latest);
  for (const project of projects) delete project._latest;

  return {
    generatedAt: String(payload.generatedAt || ''),
    generatedClock: clockOf(payload.generatedAt),
    schemaVersion: SCHEMA_VERSION,
    projects,
    // 取消单：单独一段，不进任何项目卡、也不算进卡头两个计数。读不到就是空数组。
    cancelled: cancelledOf(payload, { liveSessions }),
  };
}

/**
 * 读取源：每 pollMs 看一眼 mtime+size，变了才读整份并归一；读失败保留上一份并给原因。
 *
 * 会话存活表（CD-021）每一轮都问一次：导出物几分钟才换一份，而一条会话随时可能被删。
 * 两个信号任一变了就重建 feed：文件指纹变了（导出物换了），或存活表变了（有人删了会话）。
 * 两样都没变时仍然一个字节都不读、也不重建卡片——与以前一致。
 *
 * 状态形状（页面与 feed() 拿到的就是它）：
 *   { ok, reason, feed, fingerprint, checkedAt }
 *   - ok=true：这一轮读成功，feed 是最新的；
 *   - ok=false：这一轮没读到 / 解析失败，reason 说明原因，feed 仍是上一份好数据（可能为 null）。
 */
export class FeedSource {
  constructor({
    filePath,
    now = () => Date.now(),
    pollMs = 2000,
    onState = () => {},
    // 每轮问一次「哪些会话还能去」：返回 Set，或 null 表示不知道（读不到会话库）
    sessionIndex = () => null,
  }) {
    this.filePath = filePath;
    this.now = now;
    this.pollMs = pollMs;
    this.onState = onState;
    this.sessionIndex = sessionIndex;
    this.fingerprint = null;
    this.payload = null;
    this.liveSessions = null;
    this.feed = null;
    this.latest = null;
    this.reason = '';
    this.timer = null;
    this.build = 0;
    this.state = { ok: false, reason: '还没读到导出物', feed: null, latest: null, fingerprint: null, build: 0, checkedAt: 0 };
  }

  getState() {
    return this.state;
  }

  /** 看一眼文件与会话库；返回最新状态。两样都没变时一个字节都不读、也不重建卡片。 */
  poll() {
    const checkedAt = this.now();
    let fingerprint = null;
    try {
      fingerprint = fingerprintOf(this.filePath);
    } catch (error) {
      return this._publish({ ok: false, reason: `读不到导出物：${error.message}` });
    }
    const fileChanged = fingerprint !== this.fingerprint || !this.feed;
    if (fileChanged) {
      try {
        this.payload = readPayload(this.filePath);
      } catch (error) {
        return this._publish({ ok: false, reason: error.message || String(error) });
      }
    }
    let liveSessions = null;
    try {
      liveSessions = this.sessionIndex(this.payload);
    } catch {
      // 会话库问不到就按「不知道」处理：箭头与 resolve 回到 CD-022 的老行为，不把 feed 拖死
      liveSessions = null;
    }
    const sessionsChanged = !sameSessions(liveSessions, this.liveSessions);
    this.liveSessions = liveSessions;
    if (!fileChanged && !sessionsChanged) {
      return this.state;
    }
    try {
      const feed = normalize(this.payload, checkedAt, { liveSessions });
      this.fingerprint = fingerprint;
      this.feed = feed;
      this.latest = latestTicketOf(this.payload);
      this.build += 1;
      return this._publish({ ok: true, reason: '' });
    } catch (error) {
      return this._publish({ ok: false, reason: error.message || String(error) });
    }
  }

  _publish({ ok, reason }) {
    const next = {
      ok,
      reason,
      feed: this.feed,
      latest: this.latest,
      fingerprint: this.fingerprint,
      // 卡片重建的次数（CD-021）：导出物没变但会话被删时也要通知页面一次
      build: this.build,
      checkedAt: this.now(),
    };
    const changed = !this.state
      || next.ok !== this.state.ok
      || next.reason !== this.state.reason
      || next.fingerprint !== this.state.fingerprint
      || next.build !== this.state.build;
    this.state = next;
    if (changed) {
      this.onState(next);
    }
    return next;
  }

  start() {
    if (this.timer) return;
    this.poll();
    this.timer = setInterval(() => this.poll(), this.pollMs);
    if (this.timer.unref) this.timer.unref();
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
