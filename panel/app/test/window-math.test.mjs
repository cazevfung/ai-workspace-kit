// 折叠 / 展开的落位数学：底边不动、单侧钳制、连续切换不漂。
// 跑法：`cd app && npm test`（node --test）。

import assert from 'node:assert/strict';
import test from 'node:test';

import { collapsePlacement, collapsedFrameHeight } from '../window-math.mjs';

const AREA = { x: 0, y: 0, width: 2116, height: 1151 };
const EXPANDED_FRAME = 727;       // 展开态窗口外框（实测 121% 缩放下的典型值）
const OFFSET = 1;                 // setBounds 请求 H → 实际 H+1（标定值）

const frame = (collapsed) => (collapsed
  ? collapsedFrameHeight(EXPANDED_FRAME, 724, 72)
  : EXPANDED_FRAME);

const place = (anchorBottom, collapsed, offset = OFFSET) => collapsePlacement({
  anchorBottom, height: frame(collapsed), offset, collapsed, area: AREA,
});

test('中部折叠：底边不动、顶边下移；再展开回来仍停在同一底边', () => {
  const anchor = 400 + EXPANDED_FRAME; // 展开时 y=400，底边 1127

  const collapsed = place(anchor, true);
  assert.equal(collapsed.clamped, false);
  assert.equal(collapsed.y + frame(true) + OFFSET, anchor, '折叠后底边 = 原底边');
  assert.ok(collapsed.y > 400, '顶边确实下移了');

  const expanded = place(anchor, false);
  assert.equal(expanded.clamped, false);
  assert.equal(expanded.y + frame(false) + OFFSET, anchor, '展开后底边仍是原底边');
  assert.equal(expanded.y, 400 - OFFSET, '展开回到原来的 y（差一个标定过的取整量）');
});

test('贴顶：展开时顶边被钳在工作区顶，不推出屏幕', () => {
  const anchor = 300; // 展开需要 y = 300-727-1 < 0，会被钳
  const expanded = place(anchor, false);
  assert.equal(expanded.clamped, true);
  assert.equal(expanded.y, AREA.y);
});

test('贴底：折叠时那一条不会掉到工作区下面', () => {
  const anchor = AREA.height + 2; // 底边本来就贴着/略过工作区底
  const collapsed = place(anchor, true);
  assert.equal(collapsed.clamped, true);
  const bottom = collapsed.y + frame(true) + OFFSET;
  assert.ok(bottom <= AREA.height, `折叠条底边 ${bottom} 应在工作区底 ${AREA.height} 以内`);
});

test('展开时只钳顶边：不会顺手把底边也钳一下（两边同时钳会互相打架）', () => {
  const anchor = 800; // 展开需要 y = 800-727-1 = 72，远离工作区顶，不该被钳
  const expanded = place(anchor, false);
  assert.equal(expanded.clamped, false);
  assert.equal(expanded.y, anchor - frame(false) - OFFSET);
});

test('连续折叠展开 10 次：每次落到同一个 y，不漂移', () => {
  const anchor = 400 + EXPANDED_FRAME;
  const yCollapsed = new Set();
  const yExpanded = new Set();
  for (let i = 0; i < 10; i += 1) {
    yCollapsed.add(place(anchor, true).y);
    yExpanded.add(place(anchor, false).y);
  }
  assert.equal(yCollapsed.size, 1, `折叠 y 有 ${yCollapsed.size} 个不同值`);
  assert.equal(yExpanded.size, 1, `展开 y 有 ${yExpanded.size} 个不同值`);
});

test('折叠高度 = 展开高度 − 内容高差', () => {
  assert.equal(collapsedFrameHeight(727, 724, 72), 75);
  assert.equal(collapsedFrameHeight(726, 724, 72), 74);
});
