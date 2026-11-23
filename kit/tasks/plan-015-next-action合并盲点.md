# 计划 015：next_action 看不见「已验收但没合并」

工单 AWK-114。工作树 `.worktrees/AWK-114`，分支 `ticket/AWK-114`（基线 `dd12d42`）。

## 依据

- `PRD/PRD-014-计划队列与依赖.md` **v0.2**（R1 `planned` 与收尾相容、R6 规划期可见性）：本文只读库、不改状态机，改的是「规划期看得见下一步」这一格。
- `PRD/PRD-013-工单机制的执行面.md` **v0.3**（R10：合并只有一个入口，不手工补记录）
- `PRD/PRD-010-巡检与无人值守.md` §4.3 的 S3/S4 阈值（本单不新增任何时间阈值）
- 既有事实：`kit/tools/auto_merge.py --list` 的分栏实现（`list_unmerged_rows` + `load_exemptions`，第 689–804 行）已经是「真待合并 / 历史豁免」的权威口径；`kit/tools/backfill_merges.py` 生成豁免清单的依据是「最新提交有没有 `head_sha`」。
- 本单开工前复现过的口径分歧（临时库，非真实库）：
  - done、无 `merges` 行、提交**有** `head_sha` → 两个工具一致（都是待合并）。
  - done、无 `merges` 行、提交**没有** `head_sha`、也不在 `docs/unmerged-exemptions.json` 里 → `auto_merge --list` 列进「真待合并」，`next_action` 却进 `skipped: no_branch_head` 并照旧给 `dispatch_execute`。**这就是工单说的矛盾下一步。**
  - `next_action` 完全不读豁免清单：`skipped` 只看 `head_sha` 一个事实，清单这一层等于没接上。

## 所用 skill

- `writing-plans`：把结论写成每条带命令与期望输出的任务块，任务按「读库口径 → 判据分层 → 用例」排，而不是按文件排。
- `planning-and-task-breakdown`：把这一单切成三块互不重叠的活（口径函数、候选与优先级、用例与文档），每块单独可验收，不并行写同一个函数。
- `test-driven-development`：每条新口径先写会红的用例（先跑一次看它红），再动 `next_action.py`；先写实现是我这一单最容易犯的错，因为它「看起来已经能报 merge 了」。
- `executing-plans`：在本会话里按任务顺序逐条做，用计划文件的勾选项当台账，不中途换人、不跳步。（读了 1–130 行，其后未读，不引用未读部分的结论。）
- `verification-before-completion`：说「做完了」之前必须贴 `python tools/run_tests.py` 的末行与 `doc_gate --all` 的输出，不接受「应该过了」。
- `ticket-dispatch`：提交成果只发一条回执（工单号 + 状态 + 提交 sha），artifacts 带 `.worktrees/AWK-114/` 前缀。

## 交付物

**主题**：`kit/tools/next_action.py` 的合并候选判据补上「历史豁免」这一层，与 `kit/tools/auto_merge.py --list` 用同一套口径；`dispatch_execute` 之前先看「done 且 merges 无行」，命中就报 `merge` 并点名单号。

**形状**：只读脚本 + 用例 + 一处文档口径（`kit/docs/无人值守派单.md` §6 的前置条件）。不新增写操作、不新增时间阈值。

**给谁看**：无人值守派单循环（读 `--json` 的 `action`）与巡检的人（读人读那一行）。

**不覆盖**：
- 不改 `kit/tools/auto_merge.py`，不改 `kit/task_center/`（本单只调用，不重写）。
- 不补合并记录、不写库、不 claim、不改工单状态；也不自动 `backfill_merges.py --apply`。
- 不重排已有优先级（验收 > 合并 > 派执行 不变），不新增第五种动作。
- 不处理「done 但一条提交都没有」这类数据不自洽——它照旧进 `stop`，本单不替它找路。

**服务本轮目标第 5 条**（把 `kit/tools` 同步到工作区当前状态）：本单让套件里的 `next_action` 与 `auto_merge --list` 对同一现场只给一个答案。

## 任务

### 任务 1：把「历史豁免」这一层接进 `next_action`（只读）

先用例后实现。三条新用例在 `kit/tools/test_next_action.py` 的 `MergeTest` 里：

- 豁免清单里的单（done、无 `merges`、无 `head_sha`、`--exemptions` 指向临时文件且写着它的单号）→ 不报 `merge`，进 `skipped: exempted`。
- 不在清单里、也没有 `ticket/<单号>` 分支的单 → 不报 `merge`，进 `skipped: no_branch_head`（历史直做）。
- 不在清单里、但临时 git 仓库里存在 `ticket/<单号>` 分支的单 → 报 `merge` 并点名单号（这一条是当前会红的、也是与 `auto_merge --list` 对齐的关键）。

实现要点：豁免清单从 `<project_root>/docs/unmerged-exemptions.json` 读，`--exemptions` 可覆盖路径；解析复用 `auto_merge.load_exemptions`（不重写这套规则）；分支判断复用 `auto_merge.exists(project_root, "refs/heads/ticket/<单号>")`；查不了分支时进 `stop`（当故障报出来，不猜成历史单）。

完成证据：`python kit/tools/test_next_action.py -v` 输出里出现这三条用例名且全部 `ok`；实现前先跑一次，看到第三条 `FAIL`（`assertEqual('nothing', 'merge')`）。

### 任务 2：候选顺序与点名单号

在 `scan()` 里保持「先合并、后派执行」，并把命中写成一条可读的结论：`reason_code=done_not_merged`、`dispatch.ticket_id` 是那张单、`human` 里出现「合并 <单号>」；同时 `candidates.merge` 列出全部命中单号。`_finish()` 的尾注要能说清两类不派动作的 done 单（`no_branch_head` 与 `exempted`），不把它们悄悄吞掉。

完成证据：`python kit/tools/test_next_action.py -v` 期望末行 `OK`，并有一条「池子里同时有可领单」的用例证明 `action` 是 `merge` 而不是 `dispatch_execute`（输出里该用例为 `ok`；反例：`action` 变成 `dispatch_execute`、或 `dispatch` 里没有那张单号即为红）。

### 任务 3：两边口径对齐的交叉用例

用同一份临时库与临时 git 仓库，把 `auto_merge.list_unmerged_rows`（喂一个只读的假 center）判出的「真待合并」集合，与 `next_action.scan()` 的 `candidates.merge` 集合逐字比对。

完成证据：`python kit/tools/test_next_action.py -v` 里那条交叉用例 `ok`；故意把豁免清单换成空文件后重跑，期望它仍然相等（反例：两边集合差一张即为红）。

### 任务 4：文档口径与全量回归

`kit/docs/无人值守派单.md` §6「前置条件」第 2 条改成与实现一致的三档（有分支头 / 有 `ticket/<单号>` 分支 / 在豁免清单里不算），并在「合并流程之前直接做在 master 上的单」一节补上豁免清单这一层；`CHANGELOG.md` 加一行。

完成证据：`python kit/tools/doc_gate.py kit/tasks/plan-015-next-action合并盲点.md` 退出码 0，输出 `闸门通过：1 个文件`；`python tools/run_tests.py` 末行 `结果：全部通过`，且 `python kit/tools/next_action.py --db D:/AI工作区/.data/task-center.sqlite3 --project ai-workspace-kit` 退出码 0 或 2（真实库只读，不做写入）。

## 不做什么

- 不改 `kit/tools/auto_merge.py`、不改 `kit/task_center/` 任何文件。
- 不在真实中心库上做写入实验；用例一律临时库 + 临时 git 仓库。
- 不实现「自动补合并记录」「自动清分支」「自动改工单状态」。
- 不把 `done` 无提交的数据不自洽单升级成阻塞派发的新规则。
- 不动 `fix_pool` 等套件里还没同步的增量，也不发版、不打 tag、不推送。

## 未决

无。
