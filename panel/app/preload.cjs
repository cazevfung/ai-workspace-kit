// 预加载脚本（CommonJS）：沙箱开着，也能跑。
//
// 只往外放这几样东西，一个不多：
//   platform          —— 平台名（'win32'）
//   feed()            —— 主动要一次当前数据（首帧用，不必等主进程推）
//   onFeed()          —— 订阅后续更新，返回取消订阅的函数
//   initialCollapsed  —— 上次退出时是不是收成了一行（主进程记着）
//   setCollapsed()    —— 页面把折叠/展开告诉主进程，由它改窗口高度
//   resolve()         —— 把这张单交回它的会话（CD-019）：主进程算路、发送、记一行「发出记录」，
//                        再把「发到哪了 / 为什么没发出去」回给页面（Promise）
//   reportCardRect()  —— 卡片在窗口内容区里的矩形（CSS 像素），折叠/展开后重报一次
//
// 这里不暴露指针位置、也不报「指针在不在卡片里」：那件事主进程自己每 50ms 问一次系统
// （卡片顶部的拖动区收不到鼠标事件，靠页面报会出现「折叠整行点不动」）。
//
// 这里不暴露 ipcRenderer、不暴露 require、不转发任意通道：页面只能跟这几个固定通道说话。

const { contextBridge, ipcRenderer } = require('electron');

const GET_CHANNEL = 'cindy:feed:get';
const UPDATE_CHANNEL = 'cindy:feed:update';
const STATE_CHANNEL = 'cindy:window:state';
const COLLAPSE_CHANNEL = 'cindy:window:collapsed';
const RESOLVE_CHANNEL = 'cindy:ticket:resolve';
const CARD_RECT_CHANNEL = 'cindy:window:cardrect';

// 同步取一次「上次是不是收着」：页面首帧之前就要知道，异步会先画出一整张卡再跳成一行
const initialState = ipcRenderer.sendSync(STATE_CHANNEL) || {};

contextBridge.exposeInMainWorld('cindyWidget', Object.freeze({
  platform: process.platform,
  initialCollapsed: Boolean(initialState.collapsed),
  setCollapsed: (flag) => ipcRenderer.send(COLLAPSE_CHANNEL, Boolean(flag)),
  feed: () => ipcRenderer.invoke(GET_CHANNEL),
  // resolve(ticket, project)：只传这两个由导出物里来的对象，页面不拼通道、不拼命令
  resolve: (ticket, project) => ipcRenderer.invoke(RESOLVE_CHANNEL, {
    ticket: ticket && typeof ticket === 'object' ? ticket : {},
    project: project && typeof project === 'object' ? project : {},
  }),
  // 留白不吃点击（CD-016）：只报一个事实——卡片在内容区里的矩形。
  // 矩形只收有限数，其余当没报（主进程还会再校一次）
  reportCardRect: (rect) => {
    const safe = rect && typeof rect === 'object' ? rect : {};
    ipcRenderer.send(CARD_RECT_CHANNEL, {
      left: Number(safe.left),
      top: Number(safe.top),
      right: Number(safe.right),
      bottom: Number(safe.bottom),
    });
  },
  onFeed: (handler) => {
    if (typeof handler !== 'function') {
      return () => {};
    }
    const listener = (_event, state) => handler(state);
    ipcRenderer.on(UPDATE_CHANNEL, listener);
    return () => ipcRenderer.removeListener(UPDATE_CHANNEL, listener);
  },
}));
