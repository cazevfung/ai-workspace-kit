# 计划 003：契约补实、入参报错分开、插件做成能装的包

## 依据

- `PRD-001-工作区使用与工单体系.md` v0.1（`kit/PRD/PRD-001-工作区使用与工单体系.md`）：闭环形状、版本可追、执行者不自验三条约束本计划；本单不新增需求文档，只把已定版的口径搬进随包文档。
- KIT-006 工单的 deliverable 与 C1–C7：清掉指向包外需求文档的引用、把被反复引用的口径沉淀成 `kit/docs/decisions.md`、`create_ticket` 字段行写明 `deliverable` 上限、命令行把「输入文件读不到」与「参数不合格」分开报、插件补真实图标并打出 `dist/*.cindy`。
- `kit/AGENTS.md` 六条硬规则：第一条归属不猜、第四条没有工单不许改文件、第六条消息通道不许乱用（决策走落库、回执只发一条、送达看会话列表）。`docs/decisions.md` 写出来的三档与送达判据必须与第六条同一套口径，不能各写一份。

## 所用 skill

- `plan-gate`：它要求先记 PRD 编号与版本、按任务挑 skill 并真读、六节结构、过 `doc_gate` 才开工；本文件按它的四件事组织。
- `ticket-dispatch`：它定 create → claim → bind → submit → review → merge 的字段与顺序；本单的提交与回执按它走。
- `verification-before-completion`：它要求说「做完了」之前贴出真跑过的命令与输出；本计划每个任务都把可复跑命令写进验证，不写「已确认」。
- `writing-skills`：`skills/` 下 19 个文件里指向不存在 PRD 的句子要改写，它管 skill 文档里的触发词与引用怎么写得能被查到；这里只借用它「点名就要指得到」的判据。

## 交付物

- **主题**：把契约里的空引用补实、把入参报错分开、把插件做成别人能装的包。
  1. 新增随包文档 `kit/docs/decisions.md`：写清四件被反复引用的口径——消息通道三档、回执一条、送达判据、项目接入面；`kit` 与 `skills` 里所有指向包外需求文档的引用改成指向它或改成不依赖外部文档的表述，`grep -rn "PRD-00[2-9]" kit skills --include=*.md --include=*.py` 输出为空。
  2. `kit/docs/protocol.md` 的 `create_ticket` 字段行写明 `deliverable` ≤500 字；`kit/task_center/__main__.py` 把「输入文件读不到」单独报成 `INPUT_UNREADABLE` 并带路径，核心返回的 `error` 原句照原样打印。
  3. 插件补真实图标（`plugin/draft/assets/icon.png`，`ghost.json` 声明 `icon`），按 Cindy 插件规范打出 `dist/ai-workspace-kit-0.2.1.cindy` 并入库存放；新增 `kit/tools/test_plugin_pack.py` 判包内三件套与图标体积；主文档写一节「装插件 → 启动 → 它报出四步计划」，并写明真装那一步要人在 Cindy 里点。
- **不覆盖**：不改闸门判定（`work_gate` / `doc_gate` / `close_gate` / `ref_gate` 的判据）；不改 `task_center/core.py` 的校验与报错文字（只改其中指向不存在 PRD 的注释）；不把包外那几份需求文档全文搬进包里（它们带本机事实）；不动上游工作区；不做巡航与定时；不发布到 GitHub、公开市场或 SkillHub。

## 任务

### 1. 写 `kit/docs/decisions.md` 并接上引用

新文件四节：消息通道三档（入队 / 插话 / 同步卡片，各自的入口、作用于哪一轮、回执判据）、回执一条（协调会话只发一条，正文写工单号 + 状态 + sha，不写 `wake_kind`；`sent` 一张单最多一条，`failed` 可多条且算走过回执）、送达判据（`wake_kind=created` 即没打中；送达看 `list_sessions` 有没有多出以回执正文为标题的新任务）、项目接入面（`root` 是身份、`project_id` 是标签，列表即范围、登记只是记账）。每节只写判据与可判定动作，不写任何本机路径、会话标识与某一天的实测记录。

- 验证：`grep -rn "PRD-00[2-9]" kit skills --include=*.md --include=*.py` 期望输出为空、退出码 1（grep 无命中）。
- 验证：`grep -rn "decisions.md" kit --include=*.md` 期望至少命中 `kit/README.md` 与 `kit/docs/protocol.md` 各一处。
- 验证（判断类）：`decisions.md` 的三档与 `kit/AGENTS.md` 第六条、`kit/docs/protocol.md` 的回信阈值写成同一套判据。反例：同一件事在一处写「三档」、另一处仍写「见某份包外文档的 §8」，或两处对 `wake_kind` 的失败判定不一致。

### 2. 把 19 处指向不存在 PRD 的句子改实

按文件逐处改，不整段搬运：`kit/docs/protocol.md` 两处叙述 + 一条历史核法、`kit/docs/skill-loading.md` 依据行、`kit/task_center/core.py` 八条注释、`kit/task_center/db.py` 三条注释、`kit/tests/test_decision.py` 与 `test_projects.py` 的模块说明、`kit/tools/intake_gate.py` 与 `session_roster.py` 的判据出处、`kit/tools/auto_merge.py` 两处迁移说明、`kit/tools/test_auto_merge.py` 与 `test_model_report.py` 的夹具路径、`skills/generic/project-routing` 及其快照、`skills/workspace/skill-routing` 的 `gaps.md` / `nodes.md` / `index/README.md` / 两个脚本。涉及测试夹具的，把夹具里的需求文档路径换成不含 PRD 编号的等价路径（大小写不敏感的比较语义不变）。

- 验证：`grep -rn "PRD-00[2-9]" kit skills --include=*.md --include=*.py` 期望输出为空。
- 验证：`python -m unittest tools.test_auto_merge tools.test_model_report tools.test_ref_gate` 期望末行 `OK`、退出码 0。

### 3. `create_ticket` 的 `deliverable` 上限写进协议

`kit/docs/protocol.md` 的 `create_ticket` 行在字段列写明 `deliverable` ≤500 字；同一节里补一句「核心回的是原句，命令行不改写它」。

- 验证：`grep -n "deliverable" kit/docs/protocol.md` 期望 `create_ticket` 那一行同时出现 `deliverable` 与 `500`。

### 4. 命令行把「读不到输入文件」与「参数不合格」分开报

`kit/task_center/__main__.py`：读 `--input` 指向的文件时单独捕获 `OSError`，返回 `INPUT_UNREADABLE`，`message` 形如 `读不到输入文件：<绝对路径>（<系统原因>）`；`ValueError` / `UnicodeError` 与 argparse 失败仍是 `INVALID_INPUT`；核心已经返回的 `error` 原样打印，不再套一层通用文案。

- 验证：`python -m task_center --db <临时库> list_tickets --input <不存在的文件>` 期望输出含 `INPUT_UNREADABLE`、`读不到输入文件` 与该路径，退出码 1。
- 验证：`python -m unittest tests.test_cli` 期望三条新用例（读不到文件、501 字 deliverable、正常创建）都 `OK`，退出码 0。

### 5. 插件补真实图标、打出 `.cindy`、补打包用例

用 Cindy 的图片能力生成一张方形图标，落到 `plugin/draft/assets/icon.png`，`ghost.json` 声明 `icon`；版本升到 `0.2.1`。再用 `ghost_forge_pack` 打包，把产物放进 `dist/` 并允许入库（`.gitignore` 放行 `dist/*.cindy`）。新增 `kit/tools/test_plugin_pack.py`：`dist/` 下恰有一个 `.cindy`，它是合法 zip，根部有 `ghost.json`、`main.js` 与清单声明的图标文件，图标大于 1024 字节，`ghost.json` 的 `id` 与版本和源码一致。

- 验证：`python tools/test_plugin_pack.py` 期望打印包名与三件套检查结果、末尾 `OK`、退出码 0。
- 验证：`python -m unittest tools.test_plugin_pack tools.test_plugin_plan` 期望末行 `OK`、退出码 0。

### 6. 主文档写「装插件 → 启动 → 四步计划」

`plugin/README.md` 与根 `README.md` 的插件一节改成可照做的三段：在 Cindy 里导入 `dist/*.cindy`（真装那一步要人在客户端点确认，Agent 不代点）、启动后 `$ai-workspace <工作区路径>` 触发 `bootstrap`、它报出 `list_projects → create_project → send_to_session → list_sessions` 四步计划并写明每步的失败停点；同时写明「打包 ≠ 安装 ≠ 上架」。

- 验证（判断类）：按文档能数出四步、每步带一个失败停点，且写明真装那一步由人点。反例：文档只写「安装插件」不写谁点确认，或步骤数与 `onboard.py --json` 对不上。

### 7. 全量回归与记录

跑两处测试与三道闸门，更新 `CHANGELOG.md` 的未发布节与用例数（`VERSION` 留到发版时再升，本轮不冒充已发布）；确认上游工作区一个字节没动。

- 验证：`python tools/run_tests.py` 期望两处用例数都打印、合计不少于 481、末行 `结果：全部通过`、退出码 0。
- 验证：`python verify/verify_onboard.py` 期望末行 `9/9`、退出码 0。
- 验证：`git -C <上游工作区> status --porcelain` 期望输出为空、退出码 0。

## 不做什么

- 不改 `work_gate` / `doc_gate` / `close_gate` / `ref_gate` / `claims_gate` 的判定条件；不改 `task_center/core.py` 的字段校验、错误码与报错文案。
- 不把包外那两份需求文档全文搬进套件，也不在套件里新建它们的副本；只沉淀判据。
- 不动上游工作区的任何文件；不把 `.data/` 与 `_sandbox/` 纳入提交；不推送远端。
- 不发布插件到公开市场或 SkillHub，不写 `releaseNotes` 冒充已上架版本。
- 不做巡航、定时自动化、面板；不重写 README 的既有结构。

## 未决

- 插件图标由 Cindy 图片能力生成一次；若图片能力不可用，本轮保持 `icon` 缺省并在提交说明里写明「图标未取得」，不补占位图，C4 的图标断言随之只能等下一轮。
- `INPUT_UNREADABLE` 是本轮新增的命令行层错误码，只在 `docs/protocol.md` 的错误码节登记；核心层不产生它，也不因它改任何校验。
- `dist/` 此前被 `.gitignore` 忽略，本轮改成只放行 `dist/*.cindy`；其它构建产物继续忽略。这条改动影响仓库的忽略规则，验收时可以单独看。
