# PRD-011：UI 设计 skill（把鸿蒙版 Cindy 的设计语言固化成能直接套用的技能）

- 状态：待实施
- 版本：0.1
- 日期：2026-11-21
- 所属项目：{{WORKSPACE}}
- 依据：用户 2026-11-21 的要求（「在 skills 项目做一个 ui 设计 skill，把我们做鸿蒙版 cindy 学到的设计语言、已经固定从 lucide 找 icon、去 animate-ui 采用 ui 元素和动画，都做成一个详细的 skill」）；`PRD/PRD-008-批量迁移技能内容.md` 0.2 定的技能存放与 junction 口径；`PRD/PRD-006-通用技能索引词.md` 的索引词与快照口径。
- 事实来源：`D:/cindy-harmony-app/docs/设计系统.md`（728 行，token / 间距 / 圆角 / 玻璃 / 去框化 / 字阶 / 动效 / 触控 / 图标）、`D:/cindy-harmony-app/docs/UI-设计说明.md`（278 行，三类内容 / 工作记录块 / 浮空按钮）；animate-ui 官方文档 v2.4（2026-10-27 更新）与 lucide-react 1.77.0。

## 1. 要解决什么问题

我们现在每次做界面，都要重新决定一遍：间距用哪几档、圆角多大、深浅两套色怎么取、图标从哪来、动效多快。做鸿蒙版 Cindy 的二十多张单里，这些答案被用户反复否掉、再改回来，最后才收敛成一套（去框化、"气泡 20 / 卡片 16"、44vp 触控下限、150/250/320ms 三档动效、Lucide 一套图标）。这些判断只存在于那个项目的 `docs/设计系统.md` 里，做别的界面时没人会去翻它。

代价是具体的：新界面会重新犯一遍"相邻两层用同一个间距数字""为了层次加描边""图标混着用字符和 Lucide""按钮点一下整页跳走"这些已经被否掉的错。

所以这份 skill 要交付的不是一段设计理念，而是**下一次做任何界面时，可以照着抄的一套东西**：token 表、组件形状、图标取用规则、动效参数、以及一组能把"跑偏"当场判出来的检查。

## 2. 边界

- 只管**界面怎么做**（视觉、间距、字阶、圆角、颜色、动效、图标、无障碍），不管产品需求该不该做、也不管数据怎么取。
- 不替代 `cindy-plugin-creator`（做插件）、`notice-cards` / `square4`（出图）、`prd-authoring`（写需求）、`shuo-ren-hua`（改文案）。
- 不新增设计系统的**概念**：所有数值都来自已经验收过的鸿蒙版设计系统，skill 只做归纳与可执行化。真要改数值，先改那份文档，再回来改 skill。
- 不做成"某一种框架专用"：React + Tailwind + shadcn/ui 是首选落地方式（animate-ui 就在这条线上），同时给出自包含单文件 HTML 的写法，别的地方照同一套 token 取值。

## 3. 交付物

一个技能目录，两个层次：

**入口层**（`SKILL.md`）：什么时候用、五步工作流、硬规矩、交付前必须过的闸门、以及"下一步读哪个文件"。**它不重复正文**——一份 700 行的设计系统塞进 SKILL.md，模型会只读前 200 行就动手。

**参照层**（`references/` 与 `assets/`）：

| 文件 | 管什么 |
| --- | --- |
| `references/design-language.md` | 设计取向、颜色语义与两套取值、间距六档与三层规则、圆角四档、字阶四档、去框化的做法与代价、液态玻璃三件套、深浅两套的分工 |
| `references/motion.md` | animate-ui 怎么用（包、CLI、Provider、tempo）、primitive 与界面部位的对照表、动效参数表（时长/曲线/回弹预算）、五条纪律、减弱动效、"这次不该加动效"的判据、`npx @animate-ui/cli audit` |
| `references/icons.md` | 只从 Lucide 取图标的理由与做法、命名与尺寸、描边 1.75、着色走 token、图标什么时候可以不要文字、动画图标、反例 |
| `references/quality-gate.md` | 品质判据十条，每条带反例；能跑的命令；评审时逐条打分的方法 |
| `references/stacks.md` | 三处落地的差异：React + Tailwind + shadcn（首选）、自包含单文件 HTML（无构建、可直接预览）、ArkUI（鸿蒙现状） |
| `assets/tokens.css` | 直接可复制的 CSS 变量：浅色一套、深色一套、间距、圆角、字阶、动效、层级 |
| `assets/page-template.html` | 自包含单文件页面模板：把 token 内联，含按钮/卡片/列表行/消息气泡/底部面板/toast 六种组件与深浅切换，打开就是这套语言 |
| `scripts/check_ui.py` | 把判据变成退出码：色值越界、间距/圆角/字号/时长不在档上、`transition: all`、缺 `prefers-reduced-motion`、字符当图标、`outline: none` 去焦点 |
| `scripts/test_check_ui.py` | 上面那个脚本自己的反例用例：每条规则造一次红，改对后必须变绿 |
| `scripts/fixtures/bad-page.html` | 一份故意写坏的反例页：色值、间距、圆角、时长、字符图标、缺 reduced-motion、去掉焦点各犯一次，用来证明检查脚本真的会红 |

## 4. 需求条目

| 编号 | 需求 |
| --- | --- |
| R1 | **一套 token，两个主题。** 颜色只写语义名，浅色与深色各定一套值、key 集合一致；十六进制只允许出现在 token 定义处。判定：页面文件里没有裸 `#RRGGBB`。 |
| R2 | **间距、圆角、字号、时长都成档。** 间距 4/8/12/16/24/32；圆角 4/16/20/24/9999；字阶 11/12/13/15/16/17/18/20/24；动效 150/250/320ms（另有 40ms 错峰、900ms 呼吸、1100ms 微光）。档外不许出现字面量。 |
| R3 | **层次靠留白与字重，不靠加框。** 同级靠间距分开；描边只留给"浮起来的表面"（代码块、浮层）与需要读出边界的输入；卡片靠淡底而不是描边+投影。 |
| R4 | **图标只从 Lucide 取。** 24 网格、描边 1.75、尺寸按位取 14/16/18/20/22、颜色走 token；禁止字符图标（`›`、`⋯`、`☰`）与 emoji、禁止混用第二套图标库。 |
| R5 | **动效只解释空间关系。** 从底部来的从底部来；按下 96%（浮空控件按下是 105%）；有即时反馈；就地展开优先于整页跳转；尊重"减弱动效"，且动画不是状态生效的路径。 |
| R6 | **UI 元素与动画优先用 animate-ui。** 用 `@animate-ui/react` + `motion`，CLI 把 shadcn 组件换成 Animate UI 版；用 `AnimateUIProvider` 的 tempo 统一节奏；参数从 `@animate-ui/react/utils/motion` 取，不手写 spring。 |
| R7 | **可检查。** 交付前能跑两条命令：`python skills/ui-design/scripts/check_ui.py <文件或目录>` 与 `python skills/ui-design/scripts/test_check_ui.py`；两者退出码为 0 才算过。 |
| R8 | **接进路由。** 技能落在 `skills/ui-design/`，`~/.agents/skills/ui-design` 是指向仓库的 junction；`installed-skills.md` 有处置行（接入 N14），`index/expected-terms.json` 有 approved 词条与必含触发词，`index/snapshot/ui-design/SKILL.md` 是逐字节副本；`check_index.py` 与 `check_routing.py` 都退出 0。 |

## 5. 验收条件

| 编号 | 条件 | 证据 |
| --- | --- | --- |
| AC01 | 技能目录结构齐全：`SKILL.md` + 5 份 `references/` + 2 份 `assets/` + 2 个 `scripts/` + 1 份反例 fixture | `find skills/ui-design -type f \| wc -l` 期望不少于 11；`ls skills/ui-design/references/*.md \| wc -l` 期望 5 |
| AC02 | `SKILL.md` frontmatter 有 `name: ui-design` 与 `description`，且 description 与 `expected-terms.json` 的 `approved` 一字不差、含全部必含词、长度 ≤ 240 | `python .agents/skills/skill-routing/scripts/check_index.py --only desc --only terms` 退出码 0 |
| AC03 | `assets/tokens.css` 是唯一色值来源：浅色、深色两套都齐，间距/圆角/字阶/动效变量齐全 | `python skills/ui-design/scripts/check_ui.py skills/ui-design/assets/tokens.css` 退出码 0；`grep -c '\-\-color-' skills/ui-design/assets/tokens.css` 期望 ≥ 24 |
| AC04 | 检查脚本真能判出跑偏（不是永远绿） | `python skills/ui-design/scripts/test_check_ui.py` 退出码 0 且输出里每个规则名各出现一次（期望 `PASS` 行数 ≥ 12）；`python skills/ui-design/scripts/check_ui.py skills/ui-design/scripts/fixtures/bad-page.html` 退出 1 且判出至少 8 条问题 |
| AC05 | 模板是自包含单文件：不引用任何外链资源 | `grep -c "https\?://\|src=\"[^\"]*\.\(js\|css\)\"" skills/ui-design/assets/page-template.html` 期望 0（文档里解释 animate-ui 用的命令行除外，那部分在 references 里） |
| AC06 | 接入校验：路由与索引都绿 | `python .agents/skills/skill-routing/scripts/check_index.py` 与 `python .agents/skills/skill-routing/scripts/check_routing.py` 均退出码 0 |
| AC07 | 仓库测试全绿，两处都非零、且项数不下降（基线 2026-11-21：`tests/` 188 + `tools/` 265 = 453） | `python tools/run_tests.py` 退出码 0，输出里两处各自的项数都不为 0、合计不少于 453 |
| AC08 | 文档不写死会变的数：版本号与"当前有多少图标"这类数必须带日期，或指向权威表 | `grep -n "1\.7[0-9]\.[0-9]\|v2\.[0-9]" skills/ui-design -r` 的每一行都带日期；`grep -rn "TODO\|TBD" skills/ui-design` 期望 0 |
| AC09 | 触发词不抢：description 只说"什么时候用"，不写工作流摘要；「改文案」仍归 `shuo-ren-hua`、「写需求」仍归 `prd-authoring` | 读 `SKILL.md` 的 frontmatter 与第一节；反例：description 里出现"五步走完再交付"这类流程摘要，或把"改一段中文"也写成触发条件 |
| AC10 | 硬规矩可判定：SKILL.md 里每条"不许"都配一条"看到什么算不合格" | `grep -c "^| " skills/ui-design/references/quality-gate.md` 期望 ≥ 10，且每条判据行都带反例列 |

## 6. 不做什么

- 不新建设计系统 PRD，不改 `D:/cindy-harmony-app` 的任何文件（只读它的两份文档）。
- 不发布到 Cindy SkillHub（那要走 `cindy-skill-creator`），只接进本机 `~/.agents/skills`。
- 不写 React 组件库、不建 npm 工程、不装 `@animate-ui/react`（本机没有对应前端工程；skill 只给用法与参数）。
- 不改 `check_routing.py` / `check_index.py` 的规则；不新增节点（N01–N29 不变，ui-design 挂在 N14 上）。
- 不动 `tools/run_tests.py` 的跳过规则，不把 skill 的测试塞进主套件。

## 7. 待决策

| 编号 | 卡在哪 | 谁决定 |
| --- | --- | --- |
| D1 | 触控热区下限：鸿蒙定的是 44vp（≈ 44 CSS px），Web 上 WCAG 2.2 的 AA 是 24 CSS px。skill 统一按 44px 写，会不会让桌面端按钮过大 | 用户下次过目时定；当前取 44px（与鸿蒙口径一致，宁大不小），并在 `stacks.md` 里写明桌面端可降到 32px 的例外条件 |
| D2 | 是否把 animate-ui 的官方 skill（`npx skills add imskyleen/animate-ui`）作为前置依赖写进本 skill | 当前写"有则更省事、没有则按本 skill 的参数表手写"，不强制安装 |
| D3 | 是否给 `notice-cards` / `square4` 这两个出图 skill 也回指本 skill 的 token | 本轮不动那两个 skill，避免一次改三处 |

## 8. 变更记录

| 版本 | 日期 | 改了什么 |
| --- | --- | --- |
| 0.1 | 2026-11-21 | 首版：R1–R8、AC01–AC10、D1–D3。依据是鸿蒙版 Cindy 已验收的设计系统与用户当天「把设计语言做成可套用 skill」的要求 |
