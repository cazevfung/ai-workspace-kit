# 三处落地：React + Tailwind + shadcn、单文件 HTML、ArkUI

三处共用同一套 token 取值，只有单位和 API 名不同。**换框架不换数值**：间距还是 4/8/12/16/24/32，圆角还是 4/16/20/24/9999，时长还是 120/250/320ms。改数值只有一条路：先改源头（鸿蒙版的设计系统文档），再回来改 `assets/tokens.css`，最后同步三处。

## 1. 对照表

| 项 | React + Tailwind + shadcn/ui（首选） | 自包含单文件 HTML | ArkUI（鸿蒙现状） |
| --- | --- | --- | --- |
| 单位 | `px`（Tailwind 的 `spacing` 直接映射 `--space-*`） | `px` | `vp`（`vp` ≈ CSS px，不要混着写 `px`） |
| 取值来源 | `tokens.css` 作 CSS 变量源，`tailwind.config` 的主题映射到这些变量 | `<style>` 里内联 `:root` 与 `:root[data-theme="dark"]` | `theme/Tokens.ets` 里的常量对象（`Palette` / `Spacing` / `AppRadius` / `TypeScale` / `Motion`） |
| 主题切换 | `document.documentElement.dataset.theme` | 同一个 `data-theme`，没有构建步骤 | `ThemeManager` 写 `AppStorage`，组件读 `@StorageProp` |
| 圆角写法 | `rounded-[var(--radius-card)]` 或映射成 `rounded-card` | `border-radius: var(--radius-card)` | `borderRadius(AppRadius.card)` |
| 动效怎么落 | `@animate-ui/react` 的 primitive + `motion`，参数从 `utils/motion` 取 | CSS `transition` / `animation` + 参数表的 cubic-bezier 近似值 | `animateTo` + `springMotion(0.4, 0.8)`，参数收在 `Motion` 一处 |
| 图标怎么染 | `lucide-react` 组件 + `className="text-text-secondary"` | 内联 SVG，`stroke="currentColor"` | `Image($r('app.media.ic_lucide_x'))` + `colorFilter` 颜色矩阵 |
| 玻璃怎么做 | `backdrop-filter: blur()` + 1px 高光边 + 渐隐层 | 同 CSS 写法 | `backgroundBlurStyle(...)`，栏用 `BACKGROUND_THIN`、面板用 `COMPONENT_THICK` |
| 触控热区 | `min-height: 44px`，桌面端按 §4 的例外降到 32 | 同 CSS | `responseRegion` / 容器尺寸 ≥ 44vp |

## 2. React + Tailwind + shadcn/ui（首选）

这条路最省事，因为 animate-ui 就在这条线上。

1. 把 `assets/tokens.css` 拷进项目（例如 `src/styles/tokens.css`），在入口 import 一次。
2. Tailwind 主题映射到这些变量，不要让 Tailwind 自己再算一套色：

```js
// tailwind.config.js（节选）
theme: {
  extend: {
    colors: {
      surface: "var(--color-surface)",
      "text-primary": "var(--color-text-primary)",
      "text-secondary": "var(--color-text-secondary)",
      cta: "var(--color-cta)",
      "cta-text": "var(--color-cta-text)",
    },
    spacing: {
      1: "var(--space-1)", 2: "var(--space-2)", 3: "var(--space-3)",
      4: "var(--space-4)", 5: "var(--space-5)", 6: "var(--space-6)",
    },
    borderRadius: {
      micro: "var(--radius-micro)", card: "var(--radius-card)",
      bubble: "var(--radius-bubble)", sheet: "var(--radius-sheet)", pill: "var(--radius-pill)",
    },
  },
}
```

3. 已有的 shadcn 组件用 `npx @animate-ui/cli@latest animate all` 换成 Animate UI 版，外层套 `<AnimateUIProvider tempo={1.2}>`（见 `motion.md` §1）。
4. **不要**再引第三个间距体系（MUI 的 `sx`、Ant Design 的 token、Bootstrap 的 gutter 各是一套）。混进第二套，`check_ui.py` 的 `space` 规则会开始红，那是好事。

## 3. 自包含单文件 HTML（无构建、能直接看）

`assets/page-template.html` 就是这个形态，直接抄。

- `<style>` 里内联整套 token，浅色在 `:root`、深色在 `:root[data-theme="dark"]`；
- 切换主题只改 `<html data-theme="dark">`，不需要构建、不需要 CDN、不引外部字体与图片（图片要么不画，要么内联 SVG）；
- 玻璃用 `backdrop-filter`，但要接受一个事实：`backdrop-filter` 在部分环境里不渲染（预览器、旧浏览器），**兜底是让底色本身够看**，不要靠模糊才有对比度；
- 这种形态最适合「给同事看一个页面的样子」「做一个原型」，不适合当生产页面：生产页面走 §2。

## 4. ArkUI（鸿蒙现状）

这条路已经有生产代码，`D:/cindy-harmony-app` 的 `docs/设计系统.md` 是它的完整记录。三条最容易踩的：

1. **单位是 vp。** 设计稿按 px 量的值在 1x 屏上可以直接当 vp 用，但不要在同一个文件里混写 `px` 与 `vp`——`Path.commands` 的单位是 px，一混就会缩到左上角一小块。
2. **动效走 `animateTo` + `springMotion`。** 按压 0.96 用 `springMotion(0.4, 0.8)`，标准用 `springMotion(0.35, 0.85)`，参数与 `motion.md` §3 是同一组。关动效时直接改状态（`MotionSpec.apply` 只执行一次），不要靠动画回调让状态生效。
3. **图标用 `Image(svg) + colorFilter`，不用 `Shape` + `Path` 自绘。** 颜色矩阵的四行是「RGB 全 0 + 常数偏移 = 目标色、alpha 原样」；常数偏移是**归一化 0–1**，按 0–255 填会被钳到白。

玻璃这一处要分开看：ArkUI 的模糊是「材料」，栏用窗口背景材料（`BACKGROUND_THIN`）、面板用构件材料（`COMPONENT_THICK`）；用错材料，浅色玻璃会合成出一整块灰绿。另外窗口底必须设成页面底，否则玻璃采的是窗口那层，浅色下一样发灰。

## 5. 一次落地的顺序

不管落在哪一处，顺序是一样的，换框架不换顺序：

1. **先定取向**（`design-language.md` §1）：排版主导还是色彩主导、松一档还是密一档、留白分层还是加框分层。这一步不定，后面每个决定都要重开一次。
2. **先把 token 就位**：前端两处拷 `assets/tokens.css`，ArkUI 对着它把 `Tokens.ets` 的常量对齐。此时页面还是白的，没关系。
3. **先做一屏**：顶部栏（玻璃或实底）+ 一组列表行 + 一张卡片 + 三个按钮。这一屏能看出间距、圆角、字阶、描边四件事对不对。
4. **图标按位取**：先把这一屏用到的几个图标列出来，按 14/16/18/20/22 分好尺寸，统一描边 1.75。
5. **动效最后加**：每加一处先过 `motion.md` §5 的「多常用、为什么动」，两问答不上就不加。
6. **过闸门**：`check_ui.py` 退出 0，再过 `quality-gate.md` 那张十二行的表，只留不及格项。
7. **三处对齐**：真要在两个以上地方落地时，用 `grep -rnoE "(padding|margin|gap)[a-z-]*: *[0-9]+(px|vp)"` 对比两边的数字集合，对不上就是有人在某一处自己造了值。

第 3 步之所以先做一屏而不是先搭全套：间距与圆角的问题在第二屏才会暴露，一屏就能把它们定下来，等于少改一次。

## 6. 三处共同的收口

| 收口 | 怎么做 |
| --- | --- |
| 一份 token | 三处都从 `assets/tokens.css` 抄值，不各写一套；改值先改源头文档 |
| 一份参数 | 时长、曲线、回弹预算只有 `motion.md` §3 那一张表 |
| 一份图标 | 只有 Lucide，尺寸按位取 14/16/18/20/22，描边 1.75 |
| 一条闸门 | 前端两处能跑 `check_ui.py`；ArkUI 跑它自己的判据（`设计系统.md` §9 那一组 grep） |

**桌面端的 44px 例外**（三处都适用）：指针设备上可降到 32px，前提是没有滑动只剩点击、相邻可点元素间距 ≥8px、触屏断点回到 44px；三条缺一条就按 44px 判。写在代码注释里，不要只写在评审记录里。
