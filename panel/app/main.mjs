// 420×700 常驻桌面浮窗（CD-013 窗口壳 + CD-014 数据通道 + CD-019 resolve 接线）。
//
// 它做的事：
//   · 单实例：第二份不会再开一个窗口，只会把已有那个叫到前面；
//   · 无边框、不进任务栏、始终置顶；内容区正好 420×700（useContentSize），不缩放页面；
//   · 关窗 = 藏起来（进程留在托盘），托盘「退出」才真的退；
//   · 位置记在 userData 里，下次启动回到原处；屏幕外/分辨率变了就回退到右上角；
//   · 主进程直读 progress-export.json（mtime+size 判变，2 秒看一次），把归一后的 feed
//     经 IPC 交给页面——不起 HTTP、不要 token。读不到/半截 JSON 时保留上一份并给 reason。
//   · 行尾那枚 resolve 是真接线的（CD-019）：页面点一下 → IPC 到这里 → 算路、交给宿主、
//     记一行「发出记录」，再把「发到哪了 / 为什么没发出去」回给页面。
//   · 会话还在不在是**读一次会话库**说了算（CD-021）：箭头与 resolve 用同一份判据；
//     会话没了就不画死链，resolve 改请宿主在项目根打开「新建会话」（`--open-folder`）。
//   · 页面里的 cindy://session/<id> 链接由这里交给宿主导航（不让页面自己跳，也不开浏览器）。
//   · 卡片外那圈 12px 透明留白不吃点击（CD-016）：整窗底态是穿透，指针真落在卡片上才切回可交互；
//     判据在 app/hit.mjs，指针位置由主进程自己每 50ms 问一次系统（页面里的拖动区收不到鼠标事件）。
//     真机怎么验见 tools/probe-hit-through.py。
//   · 加载的是仓库里的 docs/样张/sample.html 本体（没有副本）。

import { app, BrowserWindow, Menu, Tray, clipboard, ipcMain, nativeImage, screen, shell } from 'electron';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { FeedSource } from './feed.mjs';
import { collapsePlacement, collapsedFrameHeight } from './window-math.mjs';
import { createResolver } from './resolve.mjs';
import { createHostPorts, createResolveRecorder } from './host.mjs';
import { createSessionIndex } from './sessions.mjs';
import { cardScreenRect, shouldIgnore, validRect } from './hit.mjs';
import { createProbe } from './probe.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = dirname(HERE);
const PAGE = join(REPO_ROOT, 'docs', '样张', 'sample.html');

/** 导出物：与中心库同处的那份（可用 CINDY_WIDGET_EXPORT 指到别处，测试/换机时用） */
const EXPORT_PATH = process.env.CINDY_WIDGET_EXPORT
  || '{{WORKSPACE}}/.data/progress-export.json';

/** 卡片本体：页面里 #win 就是这么大 */
const CARD_WIDTH = 420;
const CARD_HEIGHT = 700;
/** 卡片四周的透明影子带：窗口内容区 = 444×724，阴影画在这 12px 里 */
const MARGIN = 12;
const WIDTH = CARD_WIDTH + MARGIN * 2;
const HEIGHT = CARD_HEIGHT + MARGIN * 2;
/** 收成一行时卡片高 48：窗口内容区 = 48 + 上下各 12 = 72 */
const COLLAPSED_CARD_HEIGHT = 48;
const COLLAPSED_HEIGHT = COLLAPSED_CARD_HEIGHT + MARGIN * 2;
/** 默认位置离屏幕右上角多少像素 */
const CORNER_GAP = 24;
/** 认定「这块位置还算在屏幕上」的最小可见边长 */
const MIN_VISIBLE = 80;

/** 页面主进程通道名：与 preload 里同名 */
const GET_CHANNEL = 'cindy:feed:get';
const UPDATE_CHANNEL = 'cindy:feed:update';
const STATE_CHANNEL = 'cindy:window:state';
const COLLAPSE_CHANNEL = 'cindy:window:collapsed';
/** resolve（CD-019）：点一下就把这张单交回它的会话 */
const RESOLVE_CHANNEL = 'cindy:ticket:resolve';
/** 留白不吃点击（CD-016）：页面报卡片在内容区里的矩形，主进程自己排指针 */
const CARD_RECT_CHANNEL = 'cindy:window:cardrect';

let mainWindow = null;
let tray = null;
/** 真退出时置 true；否则关窗只是藏起来 */
let quitting = false;
/** 现在是收成一行还是展开：跟窗口高度一起持久化 */
let collapsed = false;
/**
 * 底边锚点（DIP）：折叠/展开都以它为基准算 y（y = 锚 − 高度 − 取整差）。
 * 不拿「刚量到的底边」再算，是因为分数缩放下量到的高度会抖 ±2px，那样误差会一轮轮累加。
 * 用户拖动窗口、或钳制改写了位置时才更新它。
 */
let bottomAnchor = null;
/** 展开态时的窗口外框高度（启动时量一次，折叠高度由它减去内容高差得到） */
let expandedHeight = null;
/** setBounds 的取整差（请求 H → 实际 H+offset），第一次切换时标定一次 */
let heightOffset = null;
/** 正在折叠/展开：这期间自己挪窗口不算用户拖动，不要改锚点 */
let adjustingCollapse = false;
/**
 * 现在是不是整窗穿透（CD-016）：null = 还没设过。
 * 底态是 true（那圈 12px 留白点下去要落到下面的窗口），指针进到卡片本体里才切成 false。
 */
let ignoring = null;
/** 卡片在窗口内容区里的矩形（CSS 像素，页面 getBoundingClientRect 报上来的）；null = 还不知道 */
let cardRectLocal = null;
/** 真机验证探针：只有设了 CINDY_WIDGET_PROBE 才有（见 app/probe.mjs） */
let probe = null;
/** 盯指针的那个定时器（CD-016）：窗口在就得跑，见 startHoverWatch */
let hoverWatch = null;

/** 会话库在 `%APPDATA%/Cindy` 下（宿主自己的目录）。 */
const CINDY_DIR = join(app.getPath('appData'), 'Cindy');

/**
 * 会话存活表（CD-021）：每轮问一次，箭头与 resolve 共用这一个答案。
 * 读不到就返回 null（不知道）——不知道不等于「没了」，那时照 CD-022 的老行为走。
 */
const sessionIndex = createSessionIndex({ cindyDir: CINDY_DIR });

const feedSource = new FeedSource({
  filePath: EXPORT_PATH,
  pollMs: 2000,
  onState: (state) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send(UPDATE_CHANNEL, state);
    }
  },
  // 会话被删不会让导出物改指纹，所以每轮都重读一次存活表（只一条 SELECT，读完就关）
  sessionIndex: (payload) => sessionIndex.forPayload(payload).live,
});

// ---------------------------------------------------------------- 位置存档

function statePath() {
  return join(app.getPath('userData'), 'window-state.json');
}

function readState() {
  try {
    const saved = JSON.parse(readFileSync(statePath(), 'utf8'));
    // 只认位置：老存档里的 426×705 是 121% 缩放下量出来的外框尺寸，
    // 现在的尺寸由 useContentSize 说了算，不能因为尺寸对不上就把人的位置丢掉。
    if (Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      return saved;
    }
  } catch {
    // 没有存档、或存档坏了：都按「第一次启动」处理，不报错吓人
  }
  return null;
}

function saveState() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  const bounds = mainWindow.getBounds();
  const state = { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height, collapsed };
  try {
    mkdirSync(dirname(statePath()), { recursive: true });
    writeFileSync(statePath(), JSON.stringify(state, null, 2) + '\n', 'utf8');
  } catch (error) {
    // 写不进去不影响用：下次启动回退到右上角而已，不弹窗
    console.error('[widget] 位置没保存成：', error.message);
  }
}

/** 默认位置：主屏工作区右上角（让出任务栏与一点边距） */
function cornerPosition() {
  const area = screen.getPrimaryDisplay().workArea;
  return {
    x: Math.round(area.x + area.width - WIDTH - CORNER_GAP),
    y: Math.round(area.y + CORNER_GAP),
  };
}

/**
 * 把位置夹进某块屏的工作区：窗口变大后旧位置可能贴边。
 * 留 8px 余量——分数缩放下系统给的内容区会比 WIDTH/HEIGHT 多 1–2px，
 * 贴着边创建时 Windows 会为了「不出屏」再改一次窗口尺寸（实测多出 6px），留量就不触发。
 */
const EDGE_SLACK = 8;

function clampIntoWorkArea(position, height = HEIGHT) {
  const display = screen.getDisplayNearestPoint({ x: position.x, y: position.y });
  const area = display.workArea;
  return {
    x: Math.min(Math.max(position.x, area.x), area.x + area.width - WIDTH - EDGE_SLACK),
    y: Math.min(Math.max(position.y, area.y), area.y + area.height - height - EDGE_SLACK),
  };
}

/** 存档里的位置还能不能看见：任一块与任一屏的工作区相交够大就算 */
function stillVisible(saved) {
  return screen.getAllDisplays().some(({ workArea }) => {
    const overlapWidth = Math.min(saved.x + WIDTH, workArea.x + workArea.width) - Math.max(saved.x, workArea.x);
    const overlapHeight = Math.min(saved.y + HEIGHT, workArea.y + workArea.height) - Math.max(saved.y, workArea.y);
    return overlapWidth >= MIN_VISIBLE && overlapHeight >= MIN_VISIBLE;
  });
}

// ---------------------------------------------------------------- 窗口

function createWindow() {
  const saved = readState();
  collapsed = Boolean(saved && saved.collapsed);
  const height = collapsed ? COLLAPSED_HEIGHT : HEIGHT;
  const position = clampIntoWorkArea(saved && stillVisible(saved) ? saved : cornerPosition(), height);

  mainWindow = new BrowserWindow({
    x: position.x,
    y: position.y,
    width: WIDTH,
    height,
    // 444×724 指**页面**尺寸（卡片 420×700 + 四周 12px 影子带）
    useContentSize: true,
    // 透明窗口：卡片以外的 12px 要露出桌面，圆角与阴影由页面自己画
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    backgroundColor: '#00000000',
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // 沙箱保持打开（不写 sandbox:false）：preload 是 CommonJS，沙箱下照样能跑
      preload: join(HERE, 'preload.cjs'),
    },
  });

  // 'floating' 这一档在 Windows 上比默认更稳：别的窗口激活时仍然压在它上面
  mainWindow.setAlwaysOnTop(true, 'floating');

  // 底态：整窗穿透。卡片外那圈 12px 留白点下去要落到下面那个窗口；
  // 指针真落到卡片上才切回可交互（CD-016）。
  applyIgnore(true, 'create');
  startHoverWatch();

  mainWindow.loadFile(PAGE);

  // 页面里的会话链接（cindy://session/<id>）交给宿主，不在浮窗里跳：
  // 浮窗这一页永远只有一张进度卡，导航出去就回不来了。
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (openExternalIfCindy(url)) {
      event.preventDefault();
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    openExternalIfCindy(url);
    return { action: 'deny' };
  });

  // 这一版不靠 zoom 调尺寸，但 Chromium 会按 origin 记住上一次的 zoom（老版本校过 1.0048）。
  // 每次启动归 1，保证卡片是按 100% 渲染的 CSS 像素。
  mainWindow.webContents.once('did-finish-load', () => {
    mainWindow.webContents.setZoomFactor(1);
    fitShell();
  });

  // 透明窗口在 Windows 上给的内容区与请求值不是 1:1（121% 下请求 444×724 量到 446×722）。
  // 做法：先量一次，按偏移反推一次请求值；如果还是没到「卡片 + 每边 12px」，就只往大补，
  // 绝不再往小收（小了会把卡片贴到边、阴影被裁）。多出来的像素由页面居中吸收。
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let fitting = false;

  const measureInner = async () => {
    const size = await mainWindow.webContents.executeJavaScript(
      '[window.innerWidth, window.innerHeight]');
    return { width: size[0], height: size[1] };
  };

  const MIN_WIDTH = CARD_WIDTH + MARGIN * 2;
  // 折叠时只要一行的高度够用：目标随状态走，不然会把窗口又撑回 724
  const minHeight = () => (collapsed ? COLLAPSED_HEIGHT : HEIGHT);

  const fitShell = async () => {
    if (!mainWindow || mainWindow.isDestroyed() || fitting) return;
    fitting = true;
    try {
      const MIN_HEIGHT = minHeight();
      const inner = await measureInner();
      if (inner.width === MIN_WIDTH && inner.height === MIN_HEIGHT) return;
      const [contentWidth, contentHeight] = mainWindow.getContentSize();
      mainWindow.setContentSize(
        contentWidth - (inner.width - MIN_WIDTH),
        contentHeight - (inner.height - MIN_HEIGHT),
      );
      await wait(400);
      const after = await measureInner();
      if (after.width >= MIN_WIDTH && after.height >= MIN_HEIGHT - 1) return;
      const [retryWidth, retryHeight] = mainWindow.getContentSize();
      mainWindow.setContentSize(
        retryWidth + Math.max(0, MIN_WIDTH - after.width) + 4,
        retryHeight + Math.max(0, MIN_HEIGHT - after.height) + 4,
      );
    } catch (error) {
      console.error('[widget] 窗口内容区校正失败：', error.message);
    } finally {
      fitting = false;
    }
  };

  // 页面里的 #win 自己就是 420×700，四周的透明留白由 body 的居中布局吸收：
  // Windows 在分数缩放下给的内容区会比请求的多出 1–2px（121% 时 444×724 会变成 445/446），
  // 页面把卡片居中就还是「四边等宽的一圈留白」，卡片本身一像素都不缩。
  // 不去追「inner 必须精确等于 444×724」：透明窗口那圈隐形的窗口边框在 121% 下会取整跳 1–2px，
  // 用 setContentSize 追会反复被系统弹回（实测停在 446×726），用 zoom 追又会把高度一起缩掉。

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  // 藏起来再打开、换高度、被挑出路：这三个时刻指针可能一动没动，但卡片相对它的位置变了。
  // 一律按「指针现在在哪」重算一次，别把上一态留在那儿（C3 量的就是这一段）。
  mainWindow.on('show', () => rearmFromCursor('show'));
  mainWindow.on('hide', () => applyIgnore(true, 'hide'));
  mainWindow.on('resize', () => rearmFromCursor('resize'));

  // 关窗 = 藏起来；只有托盘「退出」才真的退
  mainWindow.on('close', (event) => {
    saveState();
    if (!quitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  // 位置随动随记（防抖 400ms）：不等关窗，进程被强杀也不丢最后一次位置。
  // 'moved' 只在用户拖动结束时来一次（Windows 的 WM_EXITSIZEMOVE），'move' 每次移动都会来。
  let moveTimer = null;
  const scheduleSave = () => {
    clearTimeout(moveTimer);
    moveTimer = setTimeout(saveState, 400);
  };
  const rememberBottom = () => {
    if (!adjustingCollapse) {
      const bounds = mainWindow.getBounds();
      bottomAnchor = bounds.y + bounds.height;
    }
    scheduleSave();
  };
  mainWindow.on('move', rememberBottom);
  mainWindow.on('moved', () => {
    rememberBottom();
    rearmFromCursor('moved');
    saveState();
  });
  mainWindow.on('closed', () => clearTimeout(moveTimer));
}

function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  mainWindow.show();
  mainWindow.focus();
}

function quitForReal() {
  quitting = true;
  saveState();
  app.quit();
}

function createTray() {
  const icon = nativeImage.createFromPath(join(HERE, 'tray.png'));
  tray = new Tray(icon);
  tray.setToolTip('cindy-dashboard 浮窗');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开', click: showWindow },
    { label: '退出', click: quitForReal },
  ]));
  // 左键点图标也当作「打开」（菜单仍是那两项）
  tray.on('click', showWindow);
}

// ---------------------------------------------------------------- 留白不吃点击（CD-016）

/**
 * 整窗切「穿透 / 拦截」。状态没变就一次系统调用都不做。
 *
 * `forward: true` 让穿透期间 mousemove 仍然被转给页面：命中判定不靠它了（靠每 50ms 问一次系统），
 * 但留着它，页面里的 hover 态（行高亮、按钮）在指针扫过时不会卡在上一帧，刚切回可交互那一下也干净。
 */
function applyIgnore(next, reason) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  if (ignoring === next) {
    return;
  }
  ignoring = next;
  mainWindow.setIgnoreMouseEvents(next, { forward: true });
  if (probe) {
    probe.record({ kind: 'ignore', ignore: next, reason, ...hitSnapshot() });
  }
}

/** 卡片现在的屏幕矩形（DIP）；不知道卡片在哪就是 null，那时按穿透处理 */
function cardRectScreen() {
  if (!cardRectLocal || !mainWindow || mainWindow.isDestroyed()) {
    return null;
  }
  const content = mainWindow.getContentBounds();
  return cardScreenRect(cardRectLocal, { x: content.x, y: content.y });
}

/**
 * 按指针「现在」在哪定一次状态。
 *
 * 什么时候用：窗口显示出来、折叠/展开换了高度、窗口被挪、页面报来新的卡片矩形。
 * 这几个时刻指针可能一动没动，但卡片相对指针的位置变了；只等 mousemove 会留下一段错的
 * 状态（刚展开时留白仍然拦着、或者反过来卡片点不动），C3 量的就是这一段。
 */
function rearmFromCursor(reason) {
  if (!mainWindow || mainWindow.isDestroyed() || !mainWindow.isVisible()) {
    return;
  }
  const point = screen.getCursorScreenPoint();
  const cardRect = cardRectScreen();
  applyIgnore(shouldIgnore({ point, cardRect, interactive: ignoring === false }), reason);
}

/** 探针的一份现状快照（没人设 CINDY_WIDGET_PROBE 时不会被调用） */
function hitSnapshot() {
  const alive = mainWindow && !mainWindow.isDestroyed();
  let hwnd = null;
  if (alive) {
    const handle = mainWindow.getNativeWindowHandle();
    hwnd = handle.length >= 8 ? Number(handle.readBigUInt64LE(0)) : handle.readUInt32LE(0);
  }
  return {
    hwnd,
    visible: alive ? mainWindow.isVisible() : false,
    collapsed,
    bounds: alive ? mainWindow.getBounds() : null,
    contentBounds: alive ? mainWindow.getContentBounds() : null,
    cardRectLocal,
    cardRectScreen: cardRectScreen(),
    cursor: alive ? screen.getCursorScreenPoint() : null,
  };
}

// 页面只报一个事实：卡片在窗口内容区里的矩形。切不切穿透全由这里定。
//
// 为什么不靠页面的 mousemove（试过，不行）：卡片顶部那一行是窗口拖动区
// （`-webkit-app-region: drag`），那一块上的鼠标事件到不了页面；收成一行之后整行都是拖动区，
// 靠页面报就会变成「整个折叠行点不动」（C2 的反例）。所以指针位置由主进程直接问系统。
ipcMain.on(CARD_RECT_CHANNEL, (_event, rect) => {
  if (!validRect(rect)) {
    return;
  }
  cardRectLocal = { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
  rearmFromCursor('card-rect');
});

/**
 * 盯着指针：50ms 一次问系统「鼠标现在在哪」，落在卡片里就拦、落在留白里就穿透。
 *
 * 一个定时器把四件事一起盖住了：拖动区收不到鼠标事件、窗口藏起来再打开、折叠换高度、
 * 窗口被挪动。频率取 50ms：一笔真实的「移过去再点」里，指针在卡片上停留的时间远长于一帧，
 * 50ms 既追得上，一秒钟也才 20 次 GetCursorPos。
 */
const HOVER_WATCH_MS = 50;

function startHoverWatch() {
  if (hoverWatch) {
    return;
  }
  hoverWatch = setInterval(() => rearmFromCursor('watch'), HOVER_WATCH_MS);
  if (typeof hoverWatch.unref === 'function') {
    hoverWatch.unref();
  }
}

// ---------------------------------------------------------------- 数据通道

// 页面首帧主动要一次：先 poll 一遍，保证第一次 feed() 就有东西
feedSource.poll();
ipcMain.handle(GET_CHANNEL, () => feedSource.getState());

// 页面首帧之前会同步问一次「上次是收成一行吗」——只回这一个布尔，不做别的事
ipcMain.on(STATE_CHANNEL, (event) => {
  event.returnValue = { collapsed };
});

// ---------------------------------------------------------------- resolve 接线（CD-019 / CD-021）

/*
 * 三块：算路与结论（resolve.mjs）、两个去处与那一行「发出记录」（host.mjs）、通道（下面那五行）。
 * 这里只负责把 Electron 的那几样（shell / clipboard / 启动宿主 / userData）接到它们上面，
 * 所以「点了会发生什么」可以在 app/test 里逐条测，不必开窗口。
 *
 * 没会话（或会话已经不在了）那一支要的一个真动作：请宿主在那个项目根打开「新建会话」。
 * 宿主的窗口外入口只有两种（CD-021 查过）：cindy:// 导航与命令行开关。
 *   · `cindy://session/<id>` 只能导航到已存在的会话，而且只能带 message/device 两个**标识符**参数；
 *   · `--open-folder <绝对目录>` 是宿主自己的开关，会把窗口切到那个目录的「新建会话」页（先选好项目，
 *     正文仍然得人自己发）。没有第三种。
 * 所以这里用后者：真的把宿主叫到那个目录，不假装已经建出来了。
 */
const CINDY_PROTOCOL_KEY = 'HKCU\\Software\\Classes\\cindy\\shell\\open\\command';
let cindyExePromise = null;

/** 宿主可执行文件从协议注册项里读（同一台机器上 PRD-001 §151 记的就是这条命令）。 */
function cindyExePath() {
  if (!cindyExePromise) {
    cindyExePromise = new Promise((resolve) => {
      execFile('reg', ['query', CINDY_PROTOCOL_KEY], { windowsHide: true, encoding: 'utf8' },
        (error, stdout) => {
          if (error || !stdout) {
            resolve('');
            return;
          }
          // 形如：    (默认)    REG_SZ    "C:\Program Files\Cindy\Cindy.exe" "%1"
          const match = /"([^"]+\.exe)"/i.exec(stdout) || /([A-Za-z]:\\[^\r\n"]+\.exe)/i.exec(stdout);
          resolve(match ? match[1] : '');
        });
    });
  }
  return cindyExePromise;
}

/** 请宿主在某个项目根打开新建会话（`--open-folder`）；打不开就抛，理由写清。 */
async function openNewSessionInHost(plan) {
  const workdir = String((plan && plan.workdir) || '');
  if (!workdir) throw new Error('不知道在哪个目录新建会话');
  if (!existsSync(workdir) || !statSync(workdir).isDirectory()) {
    throw new Error(`${workdir} 不是一个存在的目录，没有请宿主新建会话`);
  }
  const exe = await cindyExePath();
  if (!exe) throw new Error('没找到 Cindy 的可执行文件（注册表里没有 cindy:// 的处理程序）');
  // detached + unref：浮窗不跟着宿主的生命周期走，也不等它退出
  spawnDetached(exe, ['--open-folder', workdir]);
}

function spawnDetached(exe, args) {
  const child = spawn(exe, args, { detached: true, stdio: 'ignore', windowsHide: true });
  child.on('error', (error) => {
    console.error('[widget] 请宿主打开新建会话没成功：', error.message);
  });
  child.unref();
}

const resolver = createResolver({
  ...createHostPorts({
    openExternal: (url) => shell.openExternal(url),
    writeClipboard: (text) => clipboard.writeText(text),
    openNewSession: openNewSessionInHost,
  }),
  record: createResolveRecorder({ dir: app.getPath('userData') }),
});

ipcMain.handle(RESOLVE_CHANNEL, (_event, payload) => {
  const ticket = (payload && payload.ticket) || {};
  const project = (payload && payload.project) || {};
  return resolver.resolve(ticket, project);
});

/** 页面里的 cindy://session/<id> 链接交给宿主：不在窗口里跳、也不开浏览器。 */
function openExternalIfCindy(url) {
  if (typeof url === 'string' && url.startsWith('cindy://session/')) {
    shell.openExternal(url).catch((error) => {
      console.error('[widget] 打开会话没成功：', error.message);
    });
    return true;
  }
  return false;
}

// 折叠 / 展开：窗口内容区跟着改成 72 或 724，并把状态跟位置一起写盘
ipcMain.on(COLLAPSE_CHANNEL, (_event, flag) => {
  applyCollapsed(Boolean(flag));
});

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 折叠 / 展开：底边不动。
 * 高度用「展开态高度」与「内容高差」算出来，位置一次 setBounds 同时给到——
 * 不再分两步（先改尺寸再量高度再挪），两步在 121% 缩放下会因为取整差来回抖。
 */
async function applyCollapsed(flag) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  const before = mainWindow.getBounds();
  if (expandedHeight === null) {
    expandedHeight = before.height;
  }
  if (bottomAnchor === null) {
    bottomAnchor = before.y + before.height;
  }

  const height = flag
    ? collapsedFrameHeight(expandedHeight, HEIGHT, COLLAPSED_HEIGHT)
    : expandedHeight;
  const offset = heightOffset === null ? 0 : heightOffset;
  adjustingCollapse = true;

  // 只钳「会跑到屏幕外的那一侧」：折叠时防底边掉到工作区下面，展开时防顶边顶出屏幕。
  // 两边同时钳会互相打架（夹在中间来回弹）。
  const display = screen.getDisplayNearestPoint({ x: before.x, y: before.y });
  const area = display.workArea;
  const place = (h, useOffset) => collapsePlacement({
    anchorBottom: bottomAnchor,
    height: h,
    offset: useOffset,
    collapsed: flag,
    area,
  });

  const placed = place(height, offset);
  const placedY = placed.y;
  const clamped = placed.clamped;

  collapsed = flag;
  mainWindow.setBounds({
    x: Math.round(Math.min(Math.max(before.x, area.x), area.x + area.width - before.width)),
    y: placedY,
    width: before.width,
    height,
  });

  // 第一次切换时标定「请求高度 → 实际高度」的取整差，并顺手把这一次的底边补准
  if (heightOffset === null) {
    await wait(220);
    if (mainWindow && !mainWindow.isDestroyed()) {
      const after = mainWindow.getBounds();
      heightOffset = after.height - height;
      mainWindow.setPosition(after.x, place(after.height, heightOffset).y);
    }
  }
  if (clamped) {
    await wait(120);
    if (mainWindow && !mainWindow.isDestroyed()) {
      const after = mainWindow.getBounds();
      bottomAnchor = after.y + after.height;
    }
  }
  adjustingCollapse = false;
  // 高度换了，卡片相对指针的位置也换了：按指针现在在哪重算一次（C3）
  rearmFromCursor('collapse');
  if (probe) {
    probe.record({ kind: 'collapsed', ignore: ignoring, ...hitSnapshot() });
  }
  saveState();
}

// ---------------------------------------------------------------- 真机验证探针（CD-016）

/*
 * 只有设了 `CINDY_WIDGET_PROBE=<状态文件>` 才存在（见 app/probe.mjs）：
 * 把「现在是穿孔还是拦截」与卡片矩形写出去，并接外部发来的 collapse / expand / hide / show / rearm。
 * 折叠与藏到托盘本来是「人点一下才发生」的事，验证时不能去动真鼠标点别人的窗口，
 * 就让一个命令文件代手；这比在测试里模拟点击诚实——走的还是页面那条 IPC 到底的那同一个函数。
 */
/**
 * 探针驱动折叠/展开：点一下页面里那颗箭头。
 * 走的是人点它的那条路（页面切自己的状态 → setCollapsed → 主进程改高度），
 * 不是直接调 applyCollapsed 抄近道——不然窗口矮了、页面还画着整卡，量出来的全不对。
 */
async function probeSetCollapsed(flag) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return false;
  }
  const isCollapsed = await mainWindow.webContents.executeJavaScript(
    'document.getElementById("win").classList.contains("is-collapsed")');
  if (Boolean(isCollapsed) === Boolean(flag)) {
    return true;
  }
  await mainWindow.webContents.executeJavaScript(
    'document.getElementById("collapse-toggle").click(), true');
  return true;
}

const probeRun = (promise) => {
  Promise.resolve(promise).catch((error) => {
    console.error('[probe] 命令没跑完：', (error && error.message) || error);
  });
  return true;
};

probe = createProbe({
  filePath: process.env.CINDY_WIDGET_PROBE || '',
  actions: {
    collapse: () => probeRun(probeSetCollapsed(true)),
    expand: () => probeRun(probeSetCollapsed(false)),
    hide: () => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.hide();
      }
      return true;
    },
    move: (x, y) => {
      // 验证时把浮窗挪到一个干净的角落：开始位置是屏幕右上角，说不定正压着别人开着的那个旧浮窗
      if (!mainWindow || mainWindow.isDestroyed()) {
        return false;
      }
      if (!Number.isFinite(x) || !Number.isFinite(y)) {
        return false;
      }
      mainWindow.setPosition(Math.round(x), Math.round(y), false);
      return true;
    },
    show: () => {
      showWindow();
      return true;
    },
    rearm: () => {
      rearmFromCursor('probe');
      return ignoring;
    },
    snapshot: () => {
      if (probe) {
        probe.record({ kind: 'snapshot', ignore: ignoring, ...hitSnapshot() });
      }
      return ignoring;
    },
  },
});

// ---------------------------------------------------------------- 生命周期

// 单实例：第二个进程只会把已有窗口叫到前面，然后自己退掉
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);

  app.whenReady().then(() => {
    createWindow();
    createTray();
    feedSource.start();
  });

  // 关掉窗口不退出：进程留在托盘里（所以这个 handler 是空的，故意不 app.quit）
  app.on('window-all-closed', () => {});

  app.on('before-quit', () => {
    quitting = true;
  });

  app.on('will-quit', () => {
    feedSource.stop();
    if (hoverWatch) {
      clearInterval(hoverWatch);
      hoverWatch = null;
    }
    if (tray) {
      tray.destroy();
      tray = null;
    }
  });
}
