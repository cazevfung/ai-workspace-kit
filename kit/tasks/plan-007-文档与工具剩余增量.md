# 计划 007：文档与工具剩余增量

## 依据

- `kit/PRD/PRD-001-工作区使用与工单体系.md` v0.1：§3「归属不猜」「收尾可判定」，AC4（计划格式可校验）、AC5（一个轮次能收尾）。
- 既有事实（2026-11-22 实测）：套件与工作区的 `task_center` 已在 AWK-101 同步到 schema 13（`db.py` 两边 sha256 相同），但 `kit/PRD/` 仍只有一份精简的 `PRD-001`，工作区那边有 15 份（编号 001 到 015）；`tools/` 里仍缺 `idle_watch.py` 与 `project_doctor.py`。
- 两个实测盲点：① AWK-101 的执行会话提交后没发回执，`wait_audit.py` 一声没响（它只报「等在验收超过 4 小时」），是用户一句追问才发现；② 这次合并其实把 schema 从 9 推到 13，`auto_merge` 却打印「仓库里没有 `task_center/db.py`，跳过」——它按仓库根找，套件的在 `kit/` 下，等于 schema 闸门在这个仓库上从未生效。

## 所用 skill

- `plan-gate`：它要求先把「依据」写成可核对的事实而不是判断，所以两个盲点都写成带日期的实测样本。
- `writing-plans`：它要求每个任务写到文件与验证命令，所以任务块按「文档 / 工具 / 两处修补 / 用例」切开。
- `prd-authoring`：它要求验收条件能举反例，所以「文档同步得完整吗」这条写成判断类并点明反例。
- `verification-before-completion`：它要求完成证据是命令输出，所以每条都给了能跑的命令与期望。

## 交付物

**主题**：把工作区剩下的文档与工具增量搬进套件，并修掉两处「该报警却没报」的盲点，让套件的巡检与合并闸门与工作区同义。

**形状**：
- `kit/PRD/` 增加 `PRD-010`~`PRD-015`（正文按工作区版本，路径与示例改成套件视角）；
- `skills/` 里与工作区同名的技能文本对齐（`pdsa-workflow`、`prd-authoring`、`skill-routing`、`ticket-dispatch`）；
- `kit/tools/idle_watch.py`、`kit/tools/project_doctor.py` 及其用例；
- `wait_audit` 新增一类：`awaiting_review` 且 `notices` 里没有 `sent` 且落库超过 15 分钟——报 `awaiting_receipt`；
- `auto_merge` 的 schema 闸门定位改成 `kit/task_center/db.py` 与 `task_center/db.py` 都认。

**给谁看**：clone 套件的人（他要靠这些文档与巡检判断「单卡住了没有」）；以及验收会话。

**明确不覆盖**：不改工单状态机语义；不迁工作区 013–015 三份 PRD 里尚未实施的部分；不发版、不打 tag、不推送；不动工作区主检出；不重写技能内容（只对齐到工作区现有版本）。

**服务本轮目标第 6 条**。

## 任务

### 任务 1：搬 PRD 正文

把工作区 `PRD/PRD-010`~`PRD-015` 拷进 `kit/PRD/`，把里面的 `D:/AI工作区` 之类路径改成套件视角（`<workspace>` 或相对路径），并把「本工作区已具备」与「套件尚未具备」的差异在 README 的已知差异节写一行。

验证：`ls kit/PRD/PRD-01*.md | wc -l` 输出大于等于 6；`grep -rl "D:/AI工作区" kit/PRD/ | wc -l` 输出 0。

### 任务 2：对齐同名技能文本

`skills/workspace/` 下四份与工作区不一致的技能（`pdsa-workflow`、`prd-authoring`、`skill-routing`、`ticket-dispatch`）对齐到工作区现有版本；`skill-routing` 的 `scripts/` 与 `nodes.md` 一起搬，并跑它自己的校验。

验证：`python skills/workspace/skill-routing/scripts/check_routing.py` 退出码 0；四条技能目录与工作区对应文件的 `sha256sum` 逐条相同。

### 任务 3：补两个闸门脚本

`idle_watch.py`（谁在等一句话）与 `project_doctor.py` 连同各自 `test_*.py` 搬进来，命令与口径与工作区一致。

验证：`python kit/tools/idle_watch.py` 退出码 0 或 1（有 WAIT 就是 1），不出现 ImportError；`python tools/run_tests.py` 里能看到这两个 test 文件的用例名。

### 任务 4：两处盲点修补

`wait_audit` 新增 `awaiting_receipt` 一类（阈值 15 分钟，判据是「`awaiting_review` 且 `notices` 里没有 `sent`」）；`auto_merge` 的 schema 闸门认 `kit/task_center/db.py`。

验证：造一份「有 `awaiting_review` 单、`notices` 只有 `failed`、落库 20 分钟前」的临时库，`python kit/tools/wait_audit.py --db <临时库>` 退出码 1 且输出含 `awaiting_receipt`；`grep -c "kit/task_center/db.py" kit/tools/auto_merge.py` 输出大于等于 1。

### 任务 5：用例与闸门

新增行为都要有会失败的用例先跑红。

验证：`python tools/run_tests.py` 退出码 0，两处用例数都打印，合计不少于 640 项；`python tools/doc_gate.py --all` 退出码 0。

## 不做什么

- 不改状态机与验收规则，只补「该报警」与「该认得路径」。
- 不搬工作区里与本套件无关的业务项目文档与 `.data/`。
- 不发版、不打 tag、不推送。
- 不动 `D:/AI工作区` 主检出，也不改它的闸门（两边不同步时以工作区为准，改的是套件的副本）。
- 不重写技能内容，只对齐。

## 未决

- [待澄清：`wait_audit` 的 15 分钟阈值是否会误报] 默认：既要 `awaiting_review` 又没有 `sent`，且落库超过 15 分钟才报，写 `failed` 的算作「走过回执」不报。恢复条件：实测出现过多于一次的误报（例如执行会话本来就慢），把阈值提到 30 分钟并写清理由。
- 判断类风险：PRD 正文搬过来但示例路径仍是工作区的。默认：用 `grep -rl "D:/AI工作区" kit/PRD/` 为空作为硬判据，不靠人眼。
