# 计划 002：补上启动层与按项目配置

## 依据

- `PRD-001-工作区使用与工单体系.md` v0.1（`kit/PRD/PRD-001-工作区使用与工单体系.md`）：闭环形状、版本可追、执行者不自验三条直接约束本计划。
- KIT-005 工单的 deliverable 与 C1–C7：启动层（`onboard.py` + `bootstrap-workspace` + 插件）、按项目配置（`add_project.py` + `auto_merge` 读配置）、一条真装真走的 `verify/verify_onboard.py`。
- `kit/AGENTS.md` 的六条硬规则：先开工单、分支按工单走、验收者不能是执行者。

## 所用 skill

- `plan-gate`：它把「先读 PRD 并记版本、按任务挑 skill、六节结构、过 `doc_gate`」定成落盘前的四件事；本文件的节结构与证据写法按它组织。
- `ticket-dispatch`：它规定 create → claim → bind → submit → review → merge 的字段与顺序；`verify_onboard.py` 的九步闭环照它调用。
- `verification-before-completion`：它要求在说「做完了」之前贴出真跑过的命令与输出；本计划每个任务都把可复跑的命令写进验证，而不是只写结论。

## 交付物

- 主题：装上就能开工。新增 `kit/onboard.py`（打印「create_project → 开协调会话 → 第一条指令」的调用计划，`--json` 给每步完整参数，`--dry-run` 零副作用）；新增 `kit/tools/add_project.py`（给业务仓库写 `.agents/ticket-config.json` 并把 `core.hooksPath` 指向套件 `.githooks`）；`kit/tools/auto_merge.py` 改读该配置，缺配置时非零退出并提示跑 `add_project.py`；新增 `skills/workspace/bootstrap-workspace`；把 `plugin/draft/` 升级成可安装的清单与入口，暴露 `bootstrap` 与 `dispatch_ticket`；新增 `verify/verify_onboard.py`，在临时 HOME 里跑完九步。
- 不覆盖：不重写 `task_center/` 的操作与 schema；不改 `work_gate` / `close_gate` / `doc_gate` 的判定规则（`doc_gate` 只补「源码树里技能放在 `skills/workspace` 与 `skills/generic`」这条发现路径）；不动上游工作区；不做契约瘦身、不补新 PRD、不做巡航、不发布。

## 任务

### 1. 让源码树里的计划闸门能看见自己的 skill

在 `kit/tools/doc_gate.py` 的 `skill_dirs` 里补上 `<仓库>/skills/workspace` 与 `<仓库>/skills/generic` 两个发现路径，并补一条用例钉住它；判定条件本身不动。

- 验证：`python -m unittest tools.test_doc_gate` 期望末行 `OK`，退出码 0。

### 2. 写启动层计划器 `kit/onboard.py`

默认打印三步的人读计划；`--json` 输出每步完整参数（`list_projects` 预检、`create_project`、`send_to_session`、`list_sessions` 反查）；`--dry-run` 不写任何文件、不调宿主。创建参数与 `cindy_helper` 的真实 schema 对齐（`create_project` 的 `name` / `working_dir`，`send_to_session` 的 `project_path` / `title` / `message`）。

- 验证：`python onboard.py --json` 期望输出同时含 `create_project` 与 `send_to_session`，退出码 0；`python onboard.py --dry-run` 退出码 0。
- 验证：`python -m unittest tools.test_onboard` 期望 `OK`，退出码 0。

### 3. 写按项目配置 `kit/tools/add_project.py`

为传入的业务仓库写 `.agents/ticket-config.json`（含 `test_cmd`、`target_branch`），并把该仓库的 `core.hooksPath` 指向套件 `.githooks`；路径不存在、不是 git 仓库、配置已存在且未加 `--force` 时非零退出并说明。

- 验证：`python tools/add_project.py <临时仓库绝对路径>` 期望退出码 0，且 `git -C <临时仓库> config core.hooksPath` 指向套件 `.githooks`。
- 验证：`python -m unittest tools.test_project_config` 期望 `OK`，退出码 0。

### 4. 让 `auto_merge` 读项目配置

新增 `load_project_config(repo)`；合并前缺配置就停在第 2 步并提示跑 `add_project.py`；`run_tests_in` 改用配置里的 `test_cmd`，缺配置时不得回落 `tools/run_tests.py`；目标分支默认取 `target_branch`，`--target` 仍可覆盖。套件仓库自己补一份 `kit/.agents/ticket-config.json`。

- 验证：`python -m unittest tools.test_project_config` 期望含缺配置用例的 `OK`，退出码 0。
- 验证：`python tools/auto_merge.py --list` 期望正常列出清单、退出码 0。

### 5. 写启动技能 `bootstrap-workspace`

在 `skills/workspace/bootstrap-workspace/SKILL.md` 写清经 `cindy_mcp_call_tool` 调 `cindy_helper` 的确切参数、`--dry-run` 预演、创建后用 `list_sessions` 反查 title 与 workingDir，以及失败路径（项目已存在、路径不存在、会话没建出来）。

- 验证（判断类）：装到临时工作区后该技能出现在 `.agents/skills` 与用户技能目录。反例：`install.py` 装完技能计数仍是 5 / 25，或技能里的工具名在 `cindy_helper` 清单里查不到。

### 6. 把插件升级成可安装入口

补 `plugin/draft/ghost.json` 的 v3 必填字段与 `bootstrap` / `dispatch_ticket` 两个工具，补 `main.js`、`locales/`、`README.md`；两个工具与 `onboard.py --json` 的步骤和失败路径同源，不出现清单里没有的 `cindy_helper` 工具名。

- 验证：`python -m unittest tools.test_plugin_plan` 期望 `OK`，退出码 0（用例比对 `onboard.py --json` 的步骤名与 `ghost.json` 的工具/步骤声明）。
- 验证：`python -c "import json;json.load(open('plugin/draft/ghost.json',encoding='utf-8'))"` 期望退出码 0。

### 7. 写九步自检 `verify/verify_onboard.py`

在临时 HOME 与临时目录里真装一遍，建假业务仓库接项目，然后用库 API 走完 `create_ticket → claim → bind → submit → review → merge`，最后写收尾包并跑 `close_gate` 的结构检查；逐行打印 `install / add-project / create_ticket / claim / bind / submit / review / merge / close` 九个步骤名，任一步失败非零退出。

- 验证：`python verify/verify_onboard.py` 期望九个步骤名逐行出现、末行 `9/9`、退出码 0。

### 8. 全量回归与文档

跑两处测试，更新 `verify/verify_install.py` 的技能计数（6 / 26）与 `README` 里新增命令的入口，确认上游工作区一个字节都没动。

- 验证：`python tools/run_tests.py` 期望末行 `结果：全部通过`、两处用例数都打印、合计不少于开工基线（那个数记在 `CHANGELOG.md` 的 0.3.0 条目里，此处不再抄一遍），退出码 0。
- 验证：`git -C <上游工作区> status --porcelain` 期望输出为空，退出码 0。

## 不做什么

- 不重写 `task_center/` 的代码、操作表与 schema；不改 `work_gate` / `close_gate` / `doc_gate` 的判定条件。
- 不动上游工作区的任何文件，不把 `.data/` 与 `_sandbox/` 纳入提交。
- 不做契约瘦身、不补新 PRD、不做巡航、不发布插件到市场、不推送远端。
- 验收与合并不由执行会话做：本计划只产出可被独立会话复核的成果。

## 未决

- 插件图标按 Cindy 规范需要真实图片，不能放纯色占位图；本轮先不声明 `icon` 字段（清单允许省略），图标留给发布前用真实素材补。
- `add_project.py` 是否要顺手登记中心库，本轮按「只写配置与钩子」实现，登记仍由 `create_ticket` 的 `root` 或 `register_project` 完成。
