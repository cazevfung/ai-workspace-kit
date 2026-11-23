// 宿主端口（CD-019 / CD-021）：把「主进程要做的几件真事」从 Electron 里拿出来，好逐条测。
//
// 这个文件只做四件事，别的一概不做：
//   createResolveRecorder —— 一次 resolve 一行「发出记录」，落成 JSONL，事后查得到；
//   createHostPorts       —— 宿主现在的两个去处：交回它自己的会话，或在项目根打开「新建会话」；
//   newSessionDetail      —— 没有会话那一支的结果文案；
//   resolveLogPath        —— 那条记录文件在哪儿。
//
// 为什么「交回会话」仍然是「导航 + 剪贴板」而不是「发一条消息」：CD-021 把三条候选路查了一遍
// （宿主深链的参数、鸿蒙那套 device-link、本机 loopback 端口），没有一条是窗口外程序能合法用的
// 发送/建会话接口；证据与可复核的命令在 `docs/CD-021-resolve发送边界与死链.md`。
// 所以这里不假装发成功了，只把能做的两件事做到位：
//   · 有会话 —— 真的把那一条会话叫到前面，并把要交回的那段话放进剪贴板（结果文案照实说这两件事）；
//   · 没会话（或会话已经不在了） —— 真的请宿主在项目根打开「新建会话」（`--open-folder <目录>`，
//     宿主自己的命令行开关），那段话进剪贴板，结果文案写清「还差把这段话发出去」这一步。
// 等宿主对外开了发送/新建的接口，只需要替换这两个函数，resolve 的其余部分一个字都不用动。
//
// 文案只说**可核的事**（CD-022 / CD-021）：请求打开了哪条链接、请求宿主打开了哪个目录、复制了什么。
// 宿主对一条早就删掉的会话只会弹「找不到这条任务，它可能已被删除」——那时写「已打开 … 的会话」就是假话。

import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';

/** 发出记录落在哪：调用方给 userData 目录（主进程是 app.getPath('userData')）。 */
export function resolveLogPath(userDataDir) {
  return join(userDataDir, 'resolve-log.jsonl');
}

/**
 * 记一行「发出记录」。
 *
 * 一行一个 JSON：`{ok, kind, ticketId, at, detail?, reason?, target, recorded}`。
 * 成功与失败都记——失败那一行才是「按钮没人接」这件事唯一的证据。
 * 写不进去会抛出去，由调用方（resolve.mjs）降级成 `recorded:false`：记录是记录，发送是发送。
 */
export function createResolveRecorder({ dir, append = appendFileSync, log = console.log }) {
  const path = resolveLogPath(dir);
  return (entry) => {
    mkdirSync(dirname(path), { recursive: true });
    append(path, JSON.stringify(entry) + '\n', 'utf8');
    log(`[widget] resolve ${JSON.stringify(entry)}`);
  };
}

/**
 * 两个去处。
 *
 * @param {object} options
 * @param {(url: string) => Promise<void>} options.openExternal  交给宿主导航（实测入口是 cindy://）
 * @param {(text: string) => void} options.writeClipboard        把要交回的那段话放进剪贴板
 * @param {(plan) => Promise<void>} [options.openNewSession]     请宿主在项目根打开「新建会话」
 *        可注入：主进程实现成 `Cindy.exe --open-folder <目录>`。不传就回到「只把话放进剪贴板」。
 */
export function createHostPorts({ openExternal, writeClipboard, openNewSession }) {
  return {
    /** 有会话：叫到前面 + 那段话进剪贴板。
     *  文案给两件可核的事：请求了哪条链接、剪贴板里放进了什么。「请求」不是「已打开」——
     *  宿主找不找得到那条会话是它的事（CD-021 起，能提前知道会话已被删的情况已不再走这一支），
     *  找不到时就把「找不到」也预告出来。 */
    async openSession(plan) {
      const url = `cindy://session/${encodeURIComponent(plan.sessionId)}`;
      await openExternal(url);
      writeClipboard(plan.prompt);
      return `已请求打开 ${plan.ticketId} 的会话（${url}），要交回的那段话已复制到剪贴板；`
        + '若那条会话已被删除，Cindy 会提示找不到';
    },

    /** 没有会话（或原来那条已经不在了）：真的请宿主在项目根打开新建会话，再说清还剩哪一步。
     *
     * 为什么是两步而不是一步：宿主对窗口外程序只公开了导航与命令行开关（CD-021 C1 查过三条
     * 候选路，结论写进 docs/CD-021-resolve发送边界与死链.md）；`--open-folder <目录>` 能把
     * Cindy 切到那个目录的「新建会话」页，但没有带正文建会话的形态，所以那段话只能进剪贴板。
     * 文案因此只说两件真发生的事：请求了哪个开关、复制了什么。 */
    async newSession(plan) {
      writeClipboard(plan.prompt);
      if (typeof openNewSession !== 'function') {
        throw new Error(noLauncherReason(plan));
      }
      // 抛错就照实往上走（resolve.mjs 会把它变成「没发出去：…」），不在这里编一个成功
      await openNewSession(plan);
      return newSessionDetail(plan);
    },
  };
}

/** 「没有会话」那一支的结果文案：只说请求了什么、复制了什么。 */
export function newSessionDetail(plan) {
  const id = String((plan && plan.ticketId) || '');
  const where = String((plan && plan.workdir) || '项目根');
  const gone = Boolean(plan && plan.sessionGone);
  return `${gone ? `${id} 原来那条会话已经不在 Cindy 里了。` : `${id} 还没有接手的人。`}`
    + `已请求 Cindy 在 ${where} 打开新建会话（--open-folder），要交回的那段话已复制到剪贴板；`
    + `把剪贴板里那段话发出去，会话就开在这个项目里`;
}

/** 宿主没有启动口时的老文案（测试与别的运行环境用得上）：把下一步写清，不假装交回。 */
function noLauncherReason(plan) {
  const id = String((plan && plan.ticketId) || '');
  const where = String((plan && plan.workdir) || '');
  const gone = Boolean(plan && plan.sessionGone);
  return `${id} ${gone ? '原来那条会话已经不在 Cindy 里了' : '还没有接手的人'}`
    + `（导出物里没有它的会话）。要交回的那段话已复制到剪贴板；`
    + `去 ${where} 新建一条会话，标题带上「${id}」`;
}
