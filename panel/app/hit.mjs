// 卡片命中区（CD-016）：窗口内容区四周那圈 12px 透明留白不该吃点击。
//
// 窗口的底态是「穿透」——`setIgnoreMouseEvents(true, { forward: true })`；
// 只有指针真的落在卡片本体上时才切回可交互，离开就切回穿透。
// `forward: true` 是关键：穿透之后系统不再把点击给这个窗口，但仍然把 mousemove 转给页面，
// 页面才知道指针什么时候又回到卡片上。否则指针一走进留白，就再也没有「回到卡片」这个信号了。
//
// 这里只放能单测的纯函数：矩形换算、点是否落在卡片里、这个点该不该穿透。
// 真窗口的取整、真系统的手感只能用 tools/probe-hit-through.py 在这台机器上量（C1/C2/C3）。

/** 指针贴着卡片边走时放宽的像素：落在边线上时不要来回切（一次来回就是两次系统调用） */
export const EDGE_SLOP = 2;

/**
 * 把「卡片在窗口内容区里的矩形」（CSS 像素，页面 getBoundingClientRect 给的）
 * 搬到屏幕坐标（DIP，主进程 getContentBounds 给的内容区原点）。
 *
 * 为什么不在页面里算屏幕坐标：页面拿得到 screenX/screenY，但那套语义要跟窗口的
 * 分数缩放、outer/inner 差对上，一旦对不上就是整套命中的系统性偏移。这里只让页面报
 * 「卡片在我这个内容区里的矩形」，屏幕那一半由主进程用 Electron 的 DIP 坐标加，两边各说各的实话。
 *
 * @param {{left:number,top:number,right:number,bottom:number}} rect  卡片在内容区里的矩形
 * @param {{x:number,y:number}} origin                               内容区左上角在屏幕上的位置
 * @returns {{left:number,top:number,right:number,bottom:number,width:number,height:number}}
 */
export function cardScreenRect(rect, origin) {
  const left = origin.x + rect.left;
  const top = origin.y + rect.top;
  const right = origin.x + rect.right;
  const bottom = origin.y + rect.bottom;
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

/** 点落没落在这个矩形里（边线算里面） */
export function insideRect(point, rect, slop = 0) {
  return point.x >= rect.left - slop && point.x <= rect.right + slop
    && point.y >= rect.top - slop && point.y <= rect.bottom + slop;
}

/**
 * 这个点该不该让点击穿过去。
 *
 * @param {object} input
 * @param {{x:number,y:number}} input.point   指针的屏幕位置（DIP）
 * @param {object|null} input.cardRect        卡片在屏幕上的矩形；不知道卡片在哪时传 null
 * @param {boolean} input.interactive         现在是不是可交互（上一态）；用于边缘滞回
 * @param {number} [input.slop]               滞回像素，默认 EDGE_SLOP
 * @returns {boolean} true = 该穿透（点落到下面的窗口），false = 该拦着（点归卡片）
 */
export function shouldIgnore({ point, cardRect, interactive, slop = EDGE_SLOP }) {
  // 不知道卡片在哪、或指针位置没读到：按穿透处理。宁可点穿过去，也不能整块挡着桌面。
  if (!cardRect || !point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) {
    return true;
  }
  // 已经在拦着的时候往外放 slop：指针压在边线上抖动不会把状态来回切；
  // 反过来，从穿透往里收时按严格边界算——多进 5px 才认，C1 量的是卡片外 5px。
  const slack = interactive ? slop : 0;
  return !insideRect(point, cardRect, slack);
}

/** 页面报上来的矩形信不信：四条边都得是有限数，且不是空矩形 */
export function validRect(rect) {
  if (!rect || typeof rect !== 'object') return false;
  const values = [rect.left, rect.top, rect.right, rect.bottom];
  if (!values.every((value) => Number.isFinite(value))) return false;
  return rect.right > rect.left && rect.bottom > rect.top;
}
