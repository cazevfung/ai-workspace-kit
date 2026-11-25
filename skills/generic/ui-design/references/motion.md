# 动效：animate-ui、参数表、纪律与审查

动效只解释空间关系。这一份管「什么该动、动多久、动什么」，不管「页面怎么排版」——那在 `design-language.md`。

## 1. 安装与接入

```bash
npm install @animate-ui/react motion
```

`@animate-ui/react` 依赖一个 primitive 库，用项目里已有的那一个，不为了它再装第二个：`radix-ui`、`@base-ui/react`、`react-aria-components` 三个都支持。

### 1.1 已经有一批 shadcn 组件时

```bash
npx @animate-ui/cli@latest animate all                    # 全部换掉
npx @animate-ui/cli@latest animate accordion --dry-run    # 先看单个会改哪几行
```

CLI 做三件事：把 primitive 的 import 换成 Animate UI 版、把 Motion 已经取代的 CSS 动画类删掉、把 `data-*` 属性保留下来。`--dry-run` 先看 diff 再动手，尤其是已经改过的组件。

### 1.2 统一节奏：Provider

```tsx
<AnimateUIProvider tempo={1.2} checkAnimation="pop">
```

`tempo` 乘所有时长：`0.8` 更脆、`1.2` 更松、`1` 是出厂值。整站只调这一个数，不要逐个组件改时长——那样下次换节奏要改几十处。

`checkAnimation="pop"` 让勾选「弹」出来，而不是「画」出来；勾选是要被看见的状态变化，画一条线反而慢。

### 1.3 三条路径

| 手上是什么 | 怎么做 | 要保住什么 |
| --- | --- | --- |
| shadcn 组件 | 走 CLI `animate` | 组件已有的 props 与样式类 |
| Radix / Base UI / React Aria 裸 primitive | 换 import 到 Animate UI 版 | 全部 part、prop、`data-*` 属性（样式与测试往往挂在它们身上） |
| 自己的标记（div + 手写样式） | 用 `motion` 按本文件参数表写，或直接取目录里已有的 primitive | 参数从 `@animate-ui/react/utils/motion` 取，不手写 spring |

**动画不等于状态生效的路径。** 该变的状态先变，动画只是解释它怎么变；关掉动效时状态一步到位，这条在 §4 第五点。

### 1.4 参数从工具函数取

`@animate-ui/react/utils/motion` 导出的助手函数（`spring` / `tween` 那两个）就是参数表的代码形态，两个数字分别是响应度与阻尼（`0.4, 0.8` 这种写法）。

- 用它们，**不要手写 `stiffness` / `damping`**：手写出来的曲线与本表无关，下一个改的人无从对账。
- 只有平台不认 spring（纯 CSS、老浏览器）时，才退回 `tokens.css` 里那三条 `cubic-bezier` 近似值。
- 想整体调松紧就调 `AnimateUIProvider` 的 `tempo`，不要逐处改数字。

## 2. primitive → 界面部位

`用` 表示这个部位默认该用它，`不用` 表示这个部位不该加，`看情况` 表示按内容量决定。

| primitive | 用在哪 | 判断 |
| --- | --- | --- |
| `slide` | 面板、抽屉、新内容从某一侧进入 | 用：进场方向要说明「它从哪来」 |
| `zoom` | 对话框、放大查看、浮空控件出现 | 用：从中心长出，说明「它盖在什么上面」 |
| `fade` | 提示、渐显的内容块、遮罩 | 用：无方向的变化一律淡入淡出 |
| `blur` | 面板背后的内容、玻璃层出现 | 看情况：底层内容简单时用，复杂时模糊会糊成一片 |
| `spring` | 按压、吸附、可打断的位移 | 用：需要「有回弹但不甩」的动作 |
| `tabs` | 页签、顶部切换 | 用：指示条跟着内容走 |
| `bottom-navigation` | 底部三格、rail | 用：当前格的指示条滑动，切页不重建 |
| `drawer` | 侧边抽屉、从侧面滑出的列表 | 用：手机上比整页跳转便宜 |
| `sheet` | 底部面板、操作菜单 | 用：就地展开的首选载体 |
| `sidebar` | 宽屏常驻侧栏、可折叠回 rail | 用：宽窗下的导航形态 |
| `accordion` | 一组可各自展开的段落 | 用：一次只关心一段时 |
| `collapsible` | 单块内容的展开收起 | 用：长消息、详情 |
| `auto-height` | 高度不确定、内容异步到位 | 用：避免手算高度导致跳一下 |
| `scroll-fade` | 栏下沿的渐隐、列表两端 | 用：内容不要被硬线切断 |
| `scroll-progress` | 长文档、长对话的阅读进度 | 看情况：只在「还有多少」真的有用时加 |
| `ripple-button` | 主按钮、列表行的按压涟漪 | 用：可点就要当场有反馈 |
| `icon-button` | 工具栏图标按钮 | 用：热区 44、按下缩放 0.96 |
| `button` | 普通按钮（不需要涟漪时） | 看情况：与 `ripple-button` 二选一，不要同屏混用 |
| `copy-button` | 代码块复制、复制结论 | 用：复制完要把状态改成「已复制」 |
| `liquid-button` | 强调动作、一处就够 | 看情况：一屏最多一个，多了变装饰 |
| `toast` | 轻提示，替代弹窗 | 用：不阻塞操作，2 秒自动收 |
| `theme-toggler` | 浅色 / 深色 / 跟随系统 | 用：三段滑块，写主题并持久化 |
| `counting-number` | 耗时、计数、进度数字 | 看情况：数字每秒跳一次就够，别做花式滚动 |
| `scrolling-number` | 需要滚动感的计量 | 不用：长列表里滚动的数字会抢注意力 |
| `typing-text` | 「正在思考」「正在执行」这类占位 | 用：说明它在动，不要写成「请稍候」 |
| `shimmering-text` | 骨架屏的亮带 | 用：加载用骨架，不转圈 |
| `pagination` | 分页切换 | 看情况：内容真的分页时用 |
| `progress` | 有确定进度的任务 | 看情况：进度不确定时用呼吸点，不要用假进度条 |
| `tooltip` | 纯图标按钮的名字 | 用：图标省了字就得有退路 |

**明确不用**：`particles`、`motion-grid`、`cursor`、`tilt`。它们与「扫一眼就知道在干什么」冲突，长列表里会拖慢滚动，鼠标跟随在触屏设备上等于没有。

## 3. 参数表

参数只从 `@animate-ui/react/utils/motion` 取（`spring`、`tween`、`duration` 那些），**不手写 spring 的 stiffness / damping**。只有写纯 CSS、没有 Motion 时才用下表右列的近似值。

| 发生什么 | 动什么 | 时长 | 曲线 | 回弹 / 幅度 |
| --- | --- | --- | --- | --- |
| 按钮按下 | 缩放 1 → 0.96 | 120ms | `spring(0.4, 0.8)` | 无 |
| 浮空控件按下 | 缩放 1 → 1.05（被「拿起来」） | 120ms | `spring(0.42, 0.9)` | 无 |
| 列表项、页签指示条、就地展开 | 位移 / 透明度 / 缩放 0.98 → 1 | 250ms | `spring(0.35, 0.85)` | 无 |
| 面板开合、抽屉、遮罩淡入 | 从屏幕外进入 | 320ms | `spring(0.4, 0.8)` | 无 |
| 长内容展开 / 收起 | 容器高度 | 250ms | `spring(0.4, 0.8)` | **回弹 ≤4px** |
| 浮空控件松手吸附 | 圆心追到最近那条边 | 250ms（160 冲 + 90 回落） | `spring(0.42, 0.9)` | **过冲 ≤3px** |
| 列表首屏错峰 | 逐项进入 | 每项 40ms（行 30ms） | 同标准 | 最多 8 项，之后不再延迟 |
| 「正在思考」三点 | 透明度呼吸 | 900ms 一轮 | 缓入缓出 | 无 |
| 骨架屏亮带 | 一条亮带扫过 | 1100ms 一圈 | 线性 | 无 |

CSS 近似值（`tokens.css` 里的 `--spring-soft` / `--spring-standard` / `--spring-snap` 就是这三条）：`spring(0.4, 0.8)` ≈ `cubic-bezier(0.22, 1.12, 0.36, 1)`，`spring(0.35, 0.85)` ≈ `cubic-bezier(0.26, 1.06, 0.4, 1)`，`spring(0.42, 0.9)` ≈ `cubic-bezier(0.3, 1.04, 0.44, 1)`。原生支持 spring 的平台（ArkUI 的 `springMotion`、CSS 的 `linear()` 生成曲线）优先用原生 spring，近似值只在只能写 cubic-bezier 时用。

**回弹预算的用处**：`≤4px` 大约是正文行高的六分之一，量级上看得见但不至于让文字上下抖。谁把 spring 调硬（响应度升高），就会先在这条数字上露出来。

### 3.1 只能写 CSS 时

```css
.button {
  transition: transform var(--duration-tap) var(--spring-soft);
}
.button:active { transform: scale(0.96); }

.sheet {
  transition: transform var(--duration-panel) var(--spring-soft);
}

@media (prefers-reduced-motion: reduce) {
  .button, .sheet { transition: none; }
  .button:active { transform: none; }
}
```

三条要点：时长与曲线都从 token 取，不要写 `0.2s ease`；`transition` 里只列具体属性，不写 `all`；`prefers-reduced-motion` 里除了关掉 `transition`，还要把 `:active` 的缩放一起去掉——否则关掉动效的人按下按钮，按钮仍然会「跳」一下。

## 4. 五条纪律

1. **可点元素按下就要有反馈**，不等业务结果。按下去到结果回来之间，用户需要知道「这一下点到了」。
2. **就地展开优先于整页跳转。** 次要内容用面板、抽屉、折叠；整页跳转只留给「换了一个上下文」这种情况（进一个会话、进一个设备）。
3. **动效只解释空间关系。** 从底部来的从底部来，从右侧来的从右侧来；不旋转、不无限循环、不做装饰性的抖动。
4. **返回手势永远可用。** 动效不能吃掉返回：面板开着时返回先收面板，不能因为动画在跑就把返回吞掉。
5. **尊重 `prefers-reduced-motion`，并且动画不是状态生效的路径。** 系统要求减弱动效时，动画关掉、状态照样一步到位；反过来，如果某处状态只在动画结束的回调里才生效，那关掉动效就等于功能坏了。

文本一律用淡入淡出，**不要缩放文字**：缩放只发生在按下那一刻（0.96 / 1.05），而且只缩容器或控件，不缩正文。文字缩放会先糊再清晰，读起来像失焦。

## 5. 这次不该加动效

四类情况，加了都是负担：

1. **每次都在用的高频控件**（主菜单、列表滚动、页签切换的高频点击）：第一次觉得顺滑，第十次就是延迟。
2. **只是状态刷新**（数据变了、计数变了）：变完在那了，动一下不解释任何空间关系。
3. **长列表逐项**：超过 8 项还在错峰，用户等它演完才能看到内容。
4. **动效会让用户等它演完才看到内容**：任何「为了看完动画而挡住内容」的做法，都是把动效当目的。

判据很简单：**这个元素多常用、它为什么动。** 答不出「为什么动」，就不动。

## 6. 审查

```bash
npx @animate-ui/cli audit
```

它会列出页面里什么在动、每个属性对浏览器渲染的代价、以及值得看的问题：`transition: all`、`ease-in` 进场、时长过长、hover 缩放、缺 `prefers-reduced-motion` 路径。

拿到清单之后不要一把梭，按 §5 的判据逐条问「这个元素多常用、它为什么动」，只留能答上来的。

`prefers-reduced-motion` 与 `transition: all` 这两条已经由 `scripts/check_ui.py` 的 `motion` / `reduced-motion` 规则兜住，跑它是第一道；audit 看的是「该不该动」，脚本看的是「动得对不对」。

### 6.1 audit 里最常见的五类告警怎么改

| 告警 | 改法 |
| --- | --- |
| `transition: all` | 列出真正要变的属性（多数时候只有一个 `transform` 或 `background-color`），其余删掉 |
| `ease-in` 进场 | 进场用 ease-out 或 spring：慢起快收看起来像「卡了一下」 |
| 时长过长（≥400ms 的控件反馈） | 按压回到 120ms、面板回到 320ms；长的只留「整页切换」这类换上下文的动作 |
| hover 缩放 | 删掉。hover 缩放会让鼠标划过列表时整片在抖；按压缩放留着 |
| 缺 reduced-motion 路径 | 补媒体查询，并且确认关掉后状态仍然生效 |

## 7. 六条质量规则

1. **reduced motion 有路径**：每个 `transition` / `animation` 都能在减弱动效时被关掉，且状态仍然生效。
2. **键盘可达**：动效不改变 TAB 顺序，不把焦点困在动画里。
3. **hover 只在能 hover 的指针设备上**：用 `@media (hover: hover)` 包起来，否则触屏上 hover 态会粘住。
4. **origin 跟着触发元素**：面板从触发它的按钮那边长出来，`transform-origin` 要跟着走，否则看起来像凭空出现。
5. **只动 `transform` 与 `opacity`**：动 `width` / `height` / `top` / `left` 会触发布局重排，长列表里立刻掉帧；必须动尺寸时（展开收起）用 `auto-height` 或直接改状态。
6. **可打断，且尺寸变化时不拉伸文字**：动画跑到一半再点一次要能接住；容器变高变矮时，里面的文字不能被拉伸变形。

## 8. 与 animate-ui 官方 skill 的关系

装过 `npx skills add imskyleen/animate-ui` 的话，它的文档能省掉查参数的时间；没装也不影响——本文件的参数表、primitive 对照与五条纪律足够按同一套做法手写。**它不是前置依赖**，不为了这一条去装。
