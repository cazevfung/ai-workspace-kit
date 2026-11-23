// 卡片命中区（CD-016）：留白该穿透、卡片本体该拦住、边缘不许来回抖。
// 跑法：`cd app && npm test`（node --test）。
//
// 这里钉的是判据本身；「点下去到底落到哪个窗口」只能在真机上用量（tools/probe-hit-through.py）。

import assert from 'node:assert/strict';
import test from 'node:test';

import { EDGE_SLOP, cardScreenRect, insideRect, shouldIgnore, validRect } from '../hit.mjs';

// 内容区 444×724，卡片 420×700 居中：四边各 12px 留白（分数缩放下会带小数）
const LOCAL = { left: 12, top: 12, right: 432, bottom: 712 };
const ORIGIN = { x: 1600, y: 40 };
const CARD = cardScreenRect(LOCAL, ORIGIN);

const ignore = (point, interactive = false) => shouldIgnore({ point, cardRect: CARD, interactive });

test('卡片矩形从内容区坐标搬到屏幕坐标：加的是内容区原点，不是窗口外框', () => {
  assert.deepEqual(CARD, {
    left: 1612, top: 52, right: 2032, bottom: 752, width: 420, height: 700,
  });
});

test('卡片本体里的点不穿透：正中、四角、边线上都算里面', () => {
  assert.equal(ignore({ x: 1822, y: 402 }), false, '正中');
  assert.equal(ignore({ x: CARD.left, y: CARD.top }), false, '左上角（边线算里面）');
  assert.equal(ignore({ x: CARD.right, y: CARD.bottom }), false, '右下角（边线算里面）');
  assert.equal(ignore({ x: CARD.right, y: 402 }), false, '右边线上');
});

test('卡片外 5px 的点要穿透（C1 量的就是这个位置）', () => {
  assert.equal(ignore({ x: CARD.left - 5, y: 402 }), true, '左边外 5px');
  assert.equal(ignore({ x: CARD.right + 5, y: 402 }), true, '右边外 5px');
  assert.equal(ignore({ x: 1822, y: CARD.top - 5 }), true, '上边外 5px');
  assert.equal(ignore({ x: 1822, y: CARD.bottom + 5 }), true, '下边外 5px');
});

test('12px 留白的角上（最外侧）也穿透：留白里没有一个点该归浮窗', () => {
  assert.equal(ignore({ x: ORIGIN.x + 1, y: ORIGIN.y + 1 }), true, '内容区左上角');
  assert.equal(ignore({ x: ORIGIN.x + 443, y: ORIGIN.y + 723 }), true, '内容区右下角');
});

test('边缘滞回：已经拦住时 outward 放 slop，穿透态往回收按严格边界', () => {
  const justOutside = { x: CARD.right + 1, y: 402 };
  assert.equal(ignore(justOutside, true), false, '拦住时 1px 外仍然拦住（不抖）');
  assert.equal(ignore({ x: CARD.right + EDGE_SLOP, y: 402 }, true), false, 'slop 边线上仍然拦住');
  assert.equal(ignore({ x: CARD.right + EDGE_SLOP + 0.5, y: 402 }, true), true, '过了 slop 就放开');
  assert.equal(ignore(justOutside, false), true, '穿透态要真的进到卡片里才拦住');
});

test('不知道卡片在哪就穿透：不许整块挡着桌面', () => {
  assert.equal(shouldIgnore({ point: { x: 1, y: 1 }, cardRect: null, interactive: false }), true);
  assert.equal(shouldIgnore({ point: { x: 1, y: 1 }, cardRect: null, interactive: true }), true);
  assert.equal(shouldIgnore({ point: null, cardRect: CARD, interactive: false }), true);
  assert.equal(shouldIgnore({ point: { x: Number.NaN, y: 1 }, cardRect: CARD, interactive: false }), true);
});

test('折叠成一行：同一套判据，只是矩形矮了（C2）', () => {
  const foldedLocal = { left: 12, top: 12, right: 432, bottom: 60 }; // 卡片高 48，窗口高 72
  const folded = cardScreenRect(foldedLocal, ORIGIN);
  const foldedIgnore = (point, interactive = false) => shouldIgnore({ point, cardRect: folded, interactive });

  assert.equal(foldedIgnore({ x: 1822, y: 90 }), false, '这一行本体可点');
  assert.equal(foldedIgnore({ x: 1822, y: folded.bottom + 5 }), true, '行下方留白穿透');
  assert.equal(foldedIgnore({ x: 1822, y: folded.top - 5 }), true, '行上方留白穿透');
});

test('insideRect 的 slop 是往外放，不是往里收', () => {
  const rect = { left: 0, top: 0, right: 10, bottom: 10 };
  assert.equal(insideRect({ x: -3, y: 5 }, rect, 0), false);
  assert.equal(insideRect({ x: -3, y: 5 }, rect, 4), true);
  assert.equal(insideRect({ x: 5, y: 5 }, rect, 4), true);
});

test('页面报上来的矩形要过一道校验：非数字、空矩形都不认', () => {
  assert.equal(validRect(LOCAL), true);
  assert.equal(validRect({ left: 1, top: 1, right: 1, bottom: 5 }), false, '宽 0 的矩形');
  assert.equal(validRect({ left: 1, top: 1, right: 5, bottom: 1 }), false, '高 0 的矩形');
  assert.equal(validRect({ left: 0, top: 0, right: Number.NaN, bottom: 5 }), false);
  assert.equal(validRect({ left: '0', top: 0, right: 5, bottom: 5 }), false, '字符串不认');
  assert.equal(validRect(null), false);
});
