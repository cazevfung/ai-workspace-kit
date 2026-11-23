// 折叠 / 展开时窗口落在哪：**底边不动**，只在会跑出屏幕的那一侧钳制。
//
// 抽成纯函数是为了能单测：窗口本身的取整（分数缩放）只能靠真窗口验，
// 但「方向对不对、钳哪一侧、会不会一轮轮漂」是可以在这里钉死的。

/**
 * @param {object} input
 * @param {number} input.anchorBottom  底边的目标位置（DIP）。折叠/展开都以它为基准
 * @param {number} input.height        这次要落到的窗口高度（外框，DIP）
 * @param {number} [input.offset]     setBounds 的取整差（请求 H → 实际 H+offset），默认 0
 * @param {boolean} input.collapsed    这次是折叠还是展开
 * @param {{x:number,y:number,width:number,height:number}} input.area  工作区
 * @returns {{y:number, clamped:boolean}} y 与「是否被钳制改写过」
 */
export function collapsePlacement({ anchorBottom, height, offset = 0, collapsed, area }) {
  const ideal = anchorBottom - height - offset;
  let y = ideal;
  if (collapsed) {
    // 折叠：底边不能低于工作区底（那一条不能掉到任务栏下面）
    y = Math.min(y, area.y + area.height - height - offset);
  } else {
    // 展开：顶边不能高于工作区顶（不能把标题推出屏幕）
    y = Math.max(y, area.y);
  }
  y = Math.max(y, area.y);
  return { y: Math.round(y), clamped: Math.round(y) !== Math.round(ideal) };
}

/** 折叠时窗口外框该多高：展开态外框高度减去「内容高差」 */
export function collapsedFrameHeight(expandedFrameHeight, expandedContentHeight, collapsedContentHeight) {
  return expandedFrameHeight - (expandedContentHeight - collapsedContentHeight);
}
