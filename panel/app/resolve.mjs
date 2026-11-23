// 浮窗上那枚 resolve：从「装饰」变成「接线」（CD-019）。
//
// 以前那颗按钮没有任何点击处理：页面只认三种 data-* 目标，点它物理上什么都不会发生。
// 这一版给它一条真的路，而且**每一步的结论都看得见**：
//
//   页面点一下  →  经 IPC 交到主进程（cindy:ticket:resolve）
//   主进程      →  先算出这张单该往哪发（planResolve），再交给一个可替换的发送口
//   发送口      →  成功给一句「发到哪了」，失败给一句「为什么没发出去」
//   每一次尝试  →  都写一行「发出记录」（主进程的 resolve-log.jsonl），成功的、失败的都在里面
//
// 为什么发送口是可注入的：宿主（Cindy）对**外部程序**只公开两类入口——`cindy://` 导航与命令行开关
// （CD-021 把三条候选路查了一遍，结论与证据在 docs/CD-021-resolve发送边界与死链.md）：
//   · 导航能去一条已存在的会话，但不收正文；
//   · `--open-folder <目录>` 能把窗口切到那个目录的「新建会话」页，但也不收正文；
//   · 「往会话里发一条消息」「在某个目录建一条会话」都没有对外接口，引擎 CLI 在宿主之外也拿不到
//     provider 凭证（实测 `pi -p` 报 “No API key found”）。
// 所以「发一条消息」今天做不到；能做到的是「导航 / 打开新建会话页 + 把那段话放进剪贴板」与
// 「一句说得清的失败」，不假装发成功了。这两件事都在本文件之外（main.mjs + host.mjs）按宿主实际能力接；
// 这里只负责算路、记账、给结论，所以它可以用假的发送口逐条测（app/test 里就是这么做的）。
//
// 文案只陈述**能核的事**（CD-022）：请求打开了哪条链接、复制了什么。不写「已交回」「已打开」
// 这类完成态——宿主对一条删掉的会话只会弹「找不到」，浮窗不能抢在它前面宣布成功。
//
// 只读约定：不读导出物、不写业务文件；唯一的写是那条「发出记录」（由注入的 record 决定往哪写）。

/** 取消单不进这条链路：它们没有 resolve（也不需要再交给谁收口）。 */
export const RESOLVE_FAILED = '没发出去';

/**
 * 要交回给那条会话的那段话。
 *
 * 第一行就是 `涉及工单：<单号>` —— 新建会话时标题由宿主从第一条消息取，所以「标题里有单号」
 * 靠的是这条不变的关系，而不是传一个 title 参数（与鸿蒙 CH-093 同一口径）。
 * 后面三句是机制事实，与本仓库的工单体系一致：不跳验收、不降判据、解决不了就交回工作区。
 */
export function resolvePrompt(ticket) {
  const id = String((ticket && ticket.id) || '').trim();
  const title = String((ticket && ticket.title) || '').trim();
  return [
    `涉及工单：${id}${title ? ' · ' + title : ''}`,
    '',
    '这张单是从桌面浮窗上 resolve 回来的：它还停在那条轨道上，等你接手收口。',
    '先看它停在哪一步（在跑 / 待验收 / 待合并），再按工单里的验收条件走完，不要降低判据。',
    '自己解决不了的事（要人拍板的、要换项目的、要另一套判据的）按六节交接包交给 {{WORKSPACE}}。',
  ].join('\n');
}

/**
 * 这张单该往哪发。三种落点，别的一律不猜：
 *
 *   open-session —— 有 `sessionId` 且这条会话还在：交回它自己的那条会话；
 *   new-session  —— 没有会话，或原来那条已经不在了（`sessionGone`），但有项目根
 *                   （导出物 `projects[].root`）：在那个目录里开一条；
 *   blocked      —— 连项目根都读不到：说清为什么，**不拿当前目录或写死的路径顶上**
 *                   （那会在错的项目里开出一条会话，比不发更糟）。
 *
 * `sessionGone` 由 feed 层写（app/feed.mjs 的 `sessionTargetOf`）：会话库说这一行没了（或已是
 * `deleted`）才会出现这个键。那时 arrow 也一并是 false——两处用的是同一份会话存活表，
 * 不会出现「箭头把人送进一条死链、resolve 却还往回叫」这种两套话（CD-021 C5）。
 */
export function planResolve(ticket, project) {
  const ticketId = String((ticket && ticket.id) || '').trim();
  const prompt = resolvePrompt(ticket || {});
  if (!ticketId) {
    return { kind: 'blocked', ticketId: '', prompt, reason: '这张单没有单号，不知道要交回哪一条' };
  }
  const sessionId = String((ticket && ticket.sessionId) || '').trim();
  const sessionGone = Boolean(ticket && ticket.sessionGone);
  if (sessionId && !sessionGone) {
    return { kind: 'open-session', ticketId, sessionId, prompt };
  }
  const workdir = String((project && project.root) || '').trim();
  if (workdir) {
    return {
      kind: 'new-session',
      ticketId,
      workdir,
      sessionGone,
      // 标题由宿主从第一条消息取；这一句只是给「调试/日志」看的目标名
      title: `工单 ${ticketId} 收口`,
      prompt,
    };
  }
  return {
    kind: 'blocked',
    ticketId,
    prompt,
    sessionGone,
    reason: sessionGone
      ? `原来那条会话已经不在 Cindy 里了，导出物里也没有这个项目的目录——不知道在哪新建`
      : '这张单还没有会话，导出物里也没有这个项目的目录——不知道在哪开一条',
  };
}

/**
 * 发送器：算路 → 发送 → 记账 → 给结论。
 *
 * @param {object} options
 * @param {(plan) => Promise<string>} options.openSession  把这张单交回它自己的会话；返回一句给人看的结果
 * @param {(plan) => Promise<string>} options.newSession   在项目根开一条会话；返回一句给人看的结果
 * @param {(entry) => void} [options.record]               写一行「发出记录」；写不进去不改变发送结论
 * @param {() => number} [options.now]                     当前时刻（毫秒），测试里钉死
 */
export function createResolver({ openSession, newSession, record = () => {}, now = () => Date.now() } = {}) {
  // 同一张单正在发的时候再点一次：复用同一次的结果，不发第二遍（连点两下不许开出两条会话）
  const inFlight = new Map();

  async function send(plan) {
    if (plan.kind === 'open-session') {
      const detail = await openSession(plan);
      // 兜底也不写成完成态：发送口没给结论，就把这句老实话给它
      return { ok: true, detail: detail || '发送口没有给结论，具体看发出记录' };
    }
    const detail = await newSession(plan);
    return { ok: true, detail: detail || '发送口没有给结论，具体看发出记录' };
  }

  async function run(ticket, project) {
    const plan = planResolve(ticket, project);
    const at = new Date(now()).toISOString();
    let result;
    if (plan.kind === 'blocked') {
      result = { ok: false, kind: plan.kind, ticketId: plan.ticketId, at, reason: plan.reason };
    } else {
      try {
        result = { ok: true, kind: plan.kind, ticketId: plan.ticketId, at, ...(await send(plan)) };
      } catch (error) {
        result = {
          ok: false,
          kind: plan.kind,
          ticketId: plan.ticketId,
          at,
          reason: String((error && error.message) || error || '未知原因'),
        };
      }
    }
    // 发出记录：成功与失败都记。它不改变结论——写不进去是记录的问题，不是发送的问题。
    try {
      record({ ...result, target: plan.kind === 'blocked' ? '' : (plan.sessionId || plan.workdir) });
      result.recorded = true;
    } catch (error) {
      result.recorded = false;
      result.recordError = String((error && error.message) || error);
    }
    return result;
  }

  return {
    /** 页面点一次 resolve 走这里。同一个 ticketId 在飞时复用同一次结果。 */
    resolve(ticket, project) {
      const key = String((ticket && ticket.id) || '').trim() || '∅';
      if (inFlight.has(key)) return inFlight.get(key);
      const promise = run(ticket, project).finally(() => inFlight.delete(key));
      inFlight.set(key, promise);
      return promise;
    },
  };
}
