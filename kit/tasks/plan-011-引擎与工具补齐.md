# 计划 011：引擎与工具补齐（AWK-110）

## 依据

- `PRD/PRD-013-工单机制的执行面.md` v0.3（执行面：状态机、尝试、提交、验收、合并记录）
- `PRD/PRD-014-计划队列与依赖.md` v0.2（`planned` 池、发号、依赖边、车次）
- `PRD/PRD-015-多主任务派单.md` v0.3（冲突预演、owner 与 origin、反复改动可见性）
- `PRD/PRD-010-项目进度面板.md` v0.2（待验收与悬空会话的阈值，`wait_audit` 的四类判据来源）
- 上游工作区（master 5dbc694）当前实测：`task_center` 24 个操作、`tools/` 下 6 个本套件没有的工具、835 项用例全绿
- 本套件 master 8cb0cb6 当前实测：`python tools/run_tests.py` 736 项全绿；`task_center` 22 个操作
- 本轮目标见 `tasks/round.md` R-002（引擎同步、文档与工具剩余增量）

## 所用 skill

- `plan-gate`：它改变了什么——计划必须先读 PRD 与 round.md 记下版本，落成六节文件并过 `doc_gate.py`，不先开工。
- `test-driven-development`：它改变了什么——每搬一个能力先让对应用例在套件里跑红，再补齐到绿，不靠读代码判断「已经搬过来了」。
- `executing-plans`：它改变了什么——按本文件的任务块顺序逐条做，一条做完拿到证据再进下一条，不并行改同一批文件。
- `verification-before-completion`：它改变了什么——说「补齐了」之前必须贴出真跑的命令与输出（归档一张 done 单、改一次项目显示名、`auto_merge --list` 找到测试命令），只贴 diff 不算。

## 交付物

主题：把 `kit/` 的工单引擎与工具补齐到与上游工作区同功能，同时保住套件自己独有的四类增量。

形状：`kit/task_center/`（补 `archive_ticket` 与 `rename_project` 两个操作，其余差异按工作区对齐）、`kit/tools/`（补 6 个工具 + `merge-projects.json`，`work_gate` 补 planned 超期与背书，`auto_merge` 补按项目测试命令，`close_gate` 与 `notice_payload` 对齐）、`kit/docs/`（搬 4 份文档）、`kit/tests/` 与 `kit/tools/test_*.py`（搬用例），并在 README 与 README.en 里改掉用例数。

给谁看：下一个在这套件里开工的会话——它读到的 `docs/protocol.md`、跑到的闸门、撞到的错误码，应该和工作区里的一致。

明确不覆盖：不发版（不动 `VERSION` / `CHANGELOG.md`）、不推送、不动 `panel/`、`plugin/`、`dist/`（AWK-109 的地盘），不改 `docs/INSTALL.md`（AWK-105 的地盘）。

**服务本轮目标第 5 条与第 6 条。**

## 任务

### 一、建工作树并绑定尝试

`git worktree add .worktrees/AWK-110 -b ticket/AWK-110 master`；`claim_ticket` → `bind_session` 用宿主 `get_current_session_id` 取到的标识。

证据：`git worktree list` 输出里出现 `.worktrees/AWK-110 ... [ticket/AWK-110]`；`get_ticket` 回执的 `state` 是 `running`、`session_id` 是本会话标识，退出码 0。

### 二、`task_center/core.py` 按工作区对齐（含两个新操作）

工作区那份是引擎面的超集：`archive_ticket`（第十个终态 `archived`，只从 `done` 进，连带 `pending_blockers` 认归档为满足、`cancel_ticket` / `take_over_ticket` 认归档为终态）、`rename_project`（只改 `projects.name`）、`review_dispatched`、`criteria(state)` 的 `planned` 空条件、`_batch_of_ticket` 的跨项目脏行判据、三档 review checks、`source=manual`。逐块搬过来，而不是覆盖式复制后丢掉套件这边的注释与错误码文案。

证据：`python -c "import re,pathlib;t=pathlib.Path('task_center/core.py').read_text(encoding='utf-8');print('archive_ticket' in t, 'rename_project' in t)"` → 期望输出 `True True`；`python tools/run_tests.py` 里 `tests/test_archive.py`、`tests/test_projects.py` 的用例名全部 OK。

### 三、六闸门与巡检工具合并不是覆盖

逐处取舍：`work_gate.py` 保 `center_config` 发现 + 搬 `PLANNED_MAX_DAYS` / `ticket_backing` / `describe_pool` / 收尾包改动那一支；`auto_merge.py` 保 `center_config` + 搬 `merge-projects.json` 那一层；`close_gate.py` 保 `center_config` + 搬第 5 节落点判据；`run_tests.py` 保两处布局自检 + 搬并行跑法；`wait_audit.py` 保第五类 `awaiting_receipt` + 搬归档说明；`idle_watch.py` / `doc_gate.py` / `center_config.py` 保持套件版不动。

证据：`grep -c awaiting_receipt kit/tools/wait_audit.py` 与 `grep -c "WORKSPACE" kit/tools/idle_watch.py` 都大于 0（占位保留）；`grep -c PLANNED_MAX_DAYS kit/tools/work_gate.py` 大于 0；`ls kit/tools/center_config.py` 列出文件，退出码 0。

### 四、搬六个工具与 `merge-projects.json`

`next_action`、`receipt_payload`、`handover_owner`、`coordinator_state`、`archive_legacy`、`backfill_merges` 与 `merge-projects.json` 连同各自的用例一起搬；搬进来之后把本机绝对路径一律换成 install.py 认的工作区占位。

证据：`ls kit/tools/next_action.py kit/tools/receipt_payload.py kit/tools/handover_owner.py kit/tools/coordinator_state.py kit/tools/archive_legacy.py kit/tools/backfill_merges.py` 期望六行都列出；`grep -rnE "D:/[A-Za-z]+|202108" kit/` 期望无输出、退出码 1。

### 五、搬四份文档并对齐既有文档

`docs/无人值守派单.md`、`docs/schema-shift.md`、`docs/unmerged-exemptions.md` 与 `.json` 新搬；`docs/protocol.md`、`docs/closing.md`、`docs/skill-loading.md`、`docs/intake.md`、`docs/probe.md`、`docs/claims-format.md` 按工作区对齐，但套件的工作区占位与已合并内容保留。

证据：`grep -c "archive_ticket" kit/docs/protocol.md` 期望输出大于 0（新操作写进了协议表）；`grep -rnE "D:/[A-Za-z]+" kit/docs/` 期望无输出、退出码 1（搬进来的文档不带本机绝对路径）。

### 六、跑测试、改用例数、真跑三条新行为

`python tools/run_tests.py -v` 必须末行 `结果：全部通过`；把汇总行的合计写进 `README.md` 与 `README.en.md` 的那几句话；另在临时库上真跑 `archive_ticket`（一张 done 单 → `archived`）、`rename_project`（真改一次显示名）、`auto_merge --list`（读到 `merge-projects.json` 的项目测试命令）。

证据：`python tools/run_tests.py` 末行 `结果：全部通过`；临时库上 `archive_ticket` 回执里 `state":"archived"`、`rename_project` 回执里 `"old_name"` 与 `"name"` 两个不同值、`python kit/tools/auto_merge.py --list` 退出码 0，三项输出原文写进 `submit_result` 的 `evidence`。

## 不做什么

- 不发版：不动 `VERSION`、`CHANGELOG.md`、`plugin/`、`dist/`（AWK-109 的活）。
- 不改 `docs/INSTALL.md`（AWK-105 的地盘）。
- 不动 `panel/`、`kit/task_center/db.py` 的迁移键位改法（套件独有的「键＝装配完成后的版本」不动）。
- 不为了凑用例数写没有判据的填充用例：用例数按真实跑出来的写。
- 不改上游工作区任何文件，不推送，不删工作树。
- 不把「搬完了」当成验收：「三条新行为真跑过」与「四类套件增量都还在」都要有输出。

## 未决

- [待澄清：C8 要求合计不少于 1200 项，而两边当前分别是 736 与 835；并集若真实低于 1200，按实际跑出来的数写进 README，并在回执里点名这一条没有达到——不用填充用例凑数。无人值守走这条默认路。]
