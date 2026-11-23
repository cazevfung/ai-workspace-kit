# 计划 014：非 Cindy 宿主也能派单——给人能照着做的手工路径与停点（AWK-106）

## 依据

- `kit/PRD/PRD-001-工作区使用与工单体系.md` v0.1（§2 一条闭环的形状：create_ticket → claim_ticket → bind_session → submit_result → review_result；§3 「归属不猜」与版本可追）
- `kit/AGENTS.md` 硬规则五（派发就是新开一个会话；自己的会话标识必须向宿主取，**禁止从临时目录名、运行目录或任何路径拆一个字符串当标识**）
- `docs/protocol.md`（`bind_session` 参数与 `PROJECT_MISMATCH` / `SELF_DISPATCH` 的判序；「派发的载体」第 3 档「两者都没有：停下来告诉人」）
- `kit/onboard.py` 当前 master 59f987b：`build_plans()` 只产出 `bootstrap`（list_projects → create_project → send_to_session → list_sessions → write_receipt）与 `dispatch_ticket`（get_ticket → claim_ticket → send_to_session → list_sessions），`CINDY_HELPER_TOOLS` 四个名字，`FAILURES` 两张表
- `README.md` 第 11 行「Cindy、Claude Code、Codex 均可」与第 166 行「其他宿主需要自己补这一步，或手工开会话再 `bind_session`」——后半句是要被替掉的那句话，前半句是没有实测支撑的声称
- 真实事故（2026-11-17，AW-007）：从临时目录名拼了一个 session id 调 `bind_session`，库照收，`list_sessions` 里根本没有那条会话。本单的「不许猜」就是这条的分界线
- 本轮目标见 `kit/tasks/round.md` R-002 第 4 条（陌生人演练）

## 所用 skill

- `plan-gate`：它改变了什么——先读 PRD 与 `round.md` 记下编号与版本，计划落成六节文件并过 `tools/doc_gate.py`，不过闸不开工。
- `ticket-dispatch`：它改变了什么——把「claim 之后再开会话、执行者自己 bind、提交带 40 位 sha」写成步骤顺序，并提醒 `PROJECT_MISMATCH` / `SELF_DISPATCH` / `STALE_ATTEMPT` 各自的处置；本单产出的 generic 计划必须与它同一套字段与错误码。
- `test-driven-development`：它改变了什么——先在 `kit/tools/test_onboard.py` 写会红的用例（generic 计划的步骤、停点、猜的 session id 被拒），跑红之后再改 `kit/onboard.py` 把它变绿。
- `planning-and-task-breakdown`：它改变了什么——把「用例 / 计划器 / 文档 / 全量闸门」切成四块，用例排在实现之前，因为停点文案与参数形状由用例先钉住。
- `writing-plans`：它改变了什么——每个任务块只留实现者写不出来的决定：函数名、参数名、每一步的回执核对项该说什么，并把每步的验证命令与期望输出写进去（计划里的"check"字段指每一步的回执核对项）。
- `executing-plans`：它改变了什么——在本工作树 `.worktrees/AWK-106` 里按任务块顺序逐条做，拿到证据再进下一块，不回主检出、不推远端。
- `verification-before-completion`：它改变了什么——说「做完了」之前必须贴 `python tools/test_onboard.py` 与 `python tools/run_tests.py` 的汇总行，不贴输出不算完。

## 交付物

主题：让「没有 `send_to_session` 的宿主」（Claude Code、Codex、手工终端）从装完走到发出第一张单，有一份能照着敲的手工计划；并且把「拿不到真实 session id 就停下」写成输出里的一句硬话，而不是一句注释。

形状：

- `kit/onboard.py`：`build_plans()` 增加 `host` 参数，取值 `cindy`（默认，行为与现在逐字一致）与 `generic`。`generic` 产出**一份**计划 `generic_dispatch`，六步：
  1. `create_ticket`（task_center）：`project_id` / `title` / `deliverable` / `prd_path` / `criteria` 的完整参数占位；
  2. `claim_ticket`（task_center）：`expected_revision` / `coordinator_session_id`（宿主自己给的标识）/ `coordinator_workdir`；
  3. `open_session`（`local` 步，不是宿主工具）：由人手工开一个终端会话、或用宿主自己的命令开一个任务；要交回的是**宿主自己报出的** session id；
  4. `bind_session`（task_center）：`attempt_id` 取第 2 步回执，`session_id` 取第 3 步拿到的真实 id，`project_root` 是登记的项目根；
  5. `submit_result`（task_center）：`summary` / `artifacts` / `evidence` / `risks` / `head_sha` / `base_sha`；
  6. `get_ticket`（task_center）：反查 `state == running` 且该 attempt 的 `session_id` 与第 3 步一字不差——这是非 Cindy 宿主唯一能做的「反查会话」，因为 `list_sessions` 不在它手里。
- 新增 `SESSION_ID_PLACEHOLDER` 与 `check_session_id(value)`：空串、含 `<`/`>`、含 `placeholder` / `占位` / `unknown` / `todo` / `tbd` / `猜`、含路径分隔符（`/` 或 `\`，AW-007 的形态）、超过 500 字符，一律判不通过并给出原因；`--session-id` 传进来的值先过这道判据，不过就退出 2，不打印计划。
- `onboard.py --host generic` 的 JSON 增顶层 `host` / `host_capabilities` / `stop_points` / `session_identity` / `must_not`，其中 `stop_points` 含那句硬话：「没有宿主建会话能力时就停在第 3 步，不许拿猜的 session_id 顶替（2026-11-17 AW-007：从临时目录名拼了一个 session id，库照收，list_sessions 里根本没有）」。
- `README.md` 与 `README.en.md`：新增同一口径的一节，用一张表写清两种宿主的差别（Cindy 有 `create_project` / `send_to_session` / `schedule_create`；其他 agent 只有文件、脚本与 cron），并写清非 Cindy 宿主怎么拿到真实 session id、拿不到时停在哪一步；第 11 行那句「Cindy、Claude Code、Codex 均可」改成「Cindy 已实测；其他宿主只保证文件、脚本与 cron 这一层，未实测」。
- `docs/INSTALL.md`、`docs/SHARING.md` 各改一句，把「其他宿主自己补」指向 `python onboard.py --host generic`。
- `kit/tools/test_onboard.py`：新增 generic 计划、停点、session id 判据与 CLI 四组用例。

给谁看：手上有 Claude Code / Codex / 一台终端、装完套件却不知道派单那一步怎么走的人，以及替他们写文档的人。他要能照着六步敲完，且在任何一步拿不到真实标识时知道该停。

明确不覆盖：

- 不实现任何宿主适配器，不写 `send_to_session` 的替代品，不声称适配过某个宿主（没真跑过的就不写「支持」）。
- 不改 `task_center/`（schema、状态机、任何操作一行不动），不改 `send_to_session` 相关逻辑，不动已发布的 0.5.0（不改 `VERSION`、不重打 `dist/`、不打 tag、不推送）。
- 不改 `plugin/draft/main.js` 的 PLANS：插件只在 Cindy 里用，`tools/test_plugin_plan.py` 比的是默认（cindy）计划，generic 计划不进插件。
- 不做「自动新建会话」的替代方案（子进程 `claude -p`、`codex exec` 之类）：那是适配器，本单只给路径与停点。

**服务本轮目标第 4 条**：陌生人演练要真装、真派单；非 Cindy 宿主那一段若只有一句「自己补」，演练到派单就断在那里。

## 任务

### 一、工作树与尝试绑定

`git worktree add .worktrees/AWK-106 -b ticket/AWK-106 master`；会话标识用 `cindy_helper` 的 `call_tool` 调 `get_current_session_id` 现取，原样写进 `bind_session`（attempt 取 `get_ticket` 的 `current_attempt`；`project_root` 取库里登记的项目根 `D:/ai-workspace-kit`，不是工作树路径）。

证据：`git worktree list` 输出含 `.worktrees/AWK-106 [ticket/AWK-106]`；`get_ticket` 回执里 `state` 是 `running`、`attempts[-1].session_id` 等于本会话标识；两条命令退出码都是 0。

### 二、先写会红的用例

在 `kit/tools/test_onboard.py` 增加：

- `GenericPlanTest.test_generic_plan_is_manual_and_step_order`：`build_plans(ws, host="generic")` 的计划名是 `generic_dispatch`，步骤工具依次是 `create_ticket` / `claim_ticket` / `open_session` / `bind_session` / `submit_result` / `get_ticket`，且**没有任何** `server == "cindy_helper"` 的步骤。
- `test_generic_bind_step_names_a_real_session_id`：`bind_session` 步的 `session_id` 等于 `onboard.SESSION_ID_PLACEHOLDER`，该步 `requires_real_session_id` 为真，`check` 里含「真实」与「不许猜」。
- `test_generic_stop_points_name_the_aw007_incident`：`plans["generic_dispatch"]["stop_points"]` 里有一句同时含 `AW-007`、`list_sessions` 与「停在第 3 步」。
- `test_generic_open_session_is_a_local_step`：`open_session` 的 `server` 与 `kind` 都是 `local`，且不在 `CINDY_HELPER_TOOLS` / `TASK_CENTER_TOOLS` 里。
- `SessionIdGuardTest`：`check_session_id("a31d514c-8517-4ab9-80bf-5519cc7d65cd")` 通过；`""`、`"<真实 session_id>"`、`"D:/tmp/awk106"`、`"unknown"`、`"placeholder"`、`"x"*501` 全部不通过且各有原因。
- `CliTest.test_generic_json_carries_bind_template_and_stop`：`--host generic --json` 退出 0，`plans` 只有 `generic_dispatch`，`host_capabilities.send_to_session` 是 `False`，`stop_points` 非空。
- `CliTest.test_generic_rejects_a_guessed_session_id`：`--host generic --session-id D:/tmp/awk106 --json` 退出 2，stderr 含 `不许猜`。
- `CliTest.test_generic_fills_a_real_session_id`：给出合法 id 时 `bind_session` 步的 `session_id` 就是它，且 `session_id_source` 变成「命令行给出」。
- `CliTest.test_session_id_only_for_generic`：`--session-id` 配默认（cindy）退出 2。

证据：在 `kit/` 下跑 `python tools/test_onboard.py -v`，期望输出 `AttributeError` 或 `KeyError`（红），失败条数为新增用例数；实现后同一条命令期望 `OK`、退出码 0。

### 三、计划器与判据

改 `kit/onboard.py`：

- `TASK_CENTER_TOOLS = ("get_ticket", "claim_ticket", "create_ticket", "bind_session", "submit_result")`（`CINDY_HELPER_TOOLS` 一个名字都不动）。
- 新增 `_generic_open_step()`、`_generic_plan(...)` 与 `FAILURES["generic_dispatch"]`（六个工具各一条停点，`open_session` 那条写「没有宿主建会话能力就停在这里，不许拿临时目录名或任何路径拆一个字符串顶替（AW-007）」）。
- `build_plans(..., host="cindy", session_id=None)`：`host == "generic"` 时只返回 `{"generic_dispatch": ...}`，其余参数不变；`session_id` 合法时填进第 4 步。
- `check_session_id(value) -> tuple[bool, str]` 与 `SESSION_ID_PLACEHOLDER`，规则见交付物。
- `render_text()` 改成按 `plans` 里的计划名逐张打印（不再写死两张），generic 追加打印 `stop_points` 与宿主能力表；cindy 的文本输出保持原有工具名与「失败停点」字样。
- CLI 增 `--host {cindy,generic}`（默认 `cindy`）与 `--session-id`；`--session-id` 配 cindy 退出 2；generic 且 id 不合法时打印 `check_session_id` 的原因并退出 2。
- 模块 docstring 补一段「两种宿主」：Cindy 有 `create_project` / `send_to_session`；其他宿主只有文件、脚本与 cron，第 3 步要人或宿主自己开会话。

证据：`python tools/test_onboard.py` 期望 `OK` 且用例数比改动前多 9 条；`python onboard.py --host generic --json` 退出 0，`python -c` 读该输出打印 `plans` 的键、`host_capabilities` 与 `stop_points[0]`，期望键是 `['generic_dispatch']`、`send_to_session` 为 `False`、停点含 `AW-007`。

### 四、文档与全量闸门

- `README.md`：新一节 `### 两种宿主：Cindy 与「只有文件、脚本和 cron」的 agent`（放在「安装」段内，`### 接项目这一步` 之前），含差别表、`python onboard.py --host generic --json` 用法、真实 session id 的取法、停句与 AW-007；第 11 行与「已知限制」第一条改成指向这一节。
- `README.en.md`：同一段的英文版，含同一张表与同一句停点。
- `docs/INSTALL.md`「在 agent 里使用」、`docs/SHARING.md` §6 第一条：把「其他宿主自己补」改成指向 `python onboard.py --host generic`，并写一句「拿不到真实 session id 就停在开会话那一步」。
- `CHANGELOG.md`「未发布」加一条 AWK-106。

证据：`grep -c "generic" README.md` 期望输出 ≥ `1`；`grep -n "AW-007" README.md README.en.md` 两侧各命中一次；`python tools/doc_gate.py --all` 退出码 0；`python tools/run_tests.py` 退出码 0 且两处用例数都打印、合计不少于 984 项。

### 五、提交与回执

在 `.worktrees/AWK-106` 分支上提交（commit 钩子会调 `work_gate.py`，需 `TASK_CENTER_TICKET=AWK-106` 且尝试为 `running`）；`submit_result` 的 `head_sha` 取 `git rev-parse HEAD`，`base_sha` 取 `git merge-base HEAD master`，`artifacts` 每条带 `.worktrees/AWK-106/` 前缀；回执只发一条（工单号 + 状态 + 提交 sha）给协调会话 `33da2d7b-16c1-4541-92e0-a070e436bcd0`。

证据：`git rev-parse HEAD` 输出 40 位十六进制且与 `submit_result` 的 `head_sha` 一致；`get_ticket` 回执 `state` 为 `awaiting_review`、`submissions[-1].artifacts` 每条以 `.worktrees/AWK-106/` 开头；两条命令退出码 0。

## 不做什么

- 不做宿主适配器、不写 `send_to_session` 的替代实现、不跑 `claude -p` / `codex exec` 之类子进程。
- 不声称「支持 Claude Code / Codex」：README 里只写「文件、脚本与 cron 这一层可用」，实测与否写清楚。
- 不改 `task_center/`、不改 `kit/AGENTS.md` 的硬规则五、不改 `plugin/` 与 `dist/`、不改 `VERSION`、不打 tag、不推送。
- 不把 generic 计划塞进 Cindy 插件（插件是 Cindy 专用入口，多一张非 Cindy 计划只会误导）。
- 不动主检出 `D:/ai-workspace-kit` 的工作树，全部改动留在 `.worktrees/AWK-106`。

## 未决

- 无。一项已在本单内定下：非 Cindy 宿主的「反查会话」用 `get_ticket` 核 `state` 与 `session_id`，不假装有 `list_sessions`——宿主没有的能力不进计划。
