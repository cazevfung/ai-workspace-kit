// 真机验证用的探针（CD-016）：只有设了 `CINDY_WIDGET_PROBE=<状态文件绝对路径>` 才存在。
//
// 为什么要在生产代码里留这一段：C1 要的不是「代码看起来对」，是「点下去落到哪个窗口」。
// 真机验证要两样东西，这两样外面拿不到：
//   1. 回读 —— 这一版到底把窗口设成了穿孔还是拦截、卡片当前在哪（getContentBounds 是 Electron
//      自己的口径，外部用 ClientToScreen 算出来的可能差 1–2px）；
//   2. 驱动 —— 折叠/展开/藏到托盘/打开，都是页面点了才发生的事，而验证时不该去动真鼠标点别人的窗口。
// 所以探针做两件事：把状态写进一个 JSON 文件；每 100ms 看一次 `<状态文件>.cmd.json`，
// 里面写了 `{"id":1,"name":"collapse","args":[true]}` 就执行一次（id 是幂等的：同一个 id 只跑一次）。
//
// 没设那个环境变量时 `createProbe` 返回 null，主进程里一次文件读、一个定时器都不会有。

import { readFileSync, writeFileSync } from 'node:fs';

/** 命令轮询间隔：验证脚本发一条命令最多等这么久 */
const POLL_MS = 100;
/** 状态文件里保留最近多少条事件（够看清「什么时候切成了什么」） */
const MAX_EVENTS = 80;

export function createProbe({ filePath, actions = {} }) {
  if (!filePath) {
    return null;
  }
  const cmdPath = filePath + '.cmd.json';
  const state = {
    probe: 'CD-016 hit-through',
    pid: process.pid,
    startedAt: new Date().toISOString(),
    events: [],
  };
  let lastCmdId = null;
  let timer = null;

  const flush = () => {
    try {
      writeFileSync(filePath, JSON.stringify(state, null, 2) + '\n', 'utf8');
    } catch (error) {
      // 探针写不进去不影响浮窗本身：验证看日志就行，别把它变成一条崩溃
      console.error('[probe] 状态没写进去：', error.message);
    }
  };

  const record = (entry) => {
    const stamped = { at: new Date().toISOString(), ...entry };
    state.events.push(stamped);
    if (state.events.length > MAX_EVENTS) {
      state.events.splice(0, state.events.length - MAX_EVENTS);
    }
    state.last = stamped;
    flush();
  };

  const runCommand = (cmd) => {
    const name = String(cmd && cmd.name || '');
    const fn = actions[name];
    if (typeof fn !== 'function') {
      return { name, ok: false, error: '没有这个命令' };
    }
    try {
      const value = fn(...(Array.isArray(cmd.args) ? cmd.args : []));
      return { name, ok: true, result: value === undefined ? null : value };
    } catch (error) {
      return { name, ok: false, error: String((error && error.message) || error) };
    }
  };

  const poll = () => {
    let raw;
    try {
      raw = readFileSync(cmdPath, 'utf8');
    } catch {
      return; // 还没有命令文件：这是常态
    }
    let cmd;
    try {
      cmd = JSON.parse(raw);
    } catch {
      return; // 半截 JSON（对方正在写）：下一轮再看
    }
    const id = cmd && cmd.id;
    if (id === undefined || id === null || id === lastCmdId) {
      return;
    }
    lastCmdId = id;
    const outcome = runCommand(cmd);
    state.lastCommand = { id, at: new Date().toISOString(), ...outcome };
    flush();
  };

  flush();
  timer = setInterval(poll, POLL_MS);
  // 别让探针的定时器把进程钉在托盘里：浮窗该退就退
  if (typeof timer.unref === 'function') {
    timer.unref();
  }

  return {
    record,
    flush,
    get state() {
      return state;
    },
    stop() {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    },
  };
}
