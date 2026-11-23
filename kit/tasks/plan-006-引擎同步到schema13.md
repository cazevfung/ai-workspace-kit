# 计划 006：引擎同步到 schema 13

## 依据

- `kit/PRD/PRD-001-工作区使用与工单体系.md` v0.1：§2 闭环形状、§3「一次一张」与「收尾可判定」、AC2（一张单能从创建走到验收）。
- 既有事实（2026-11-22 实测）：套件的 `kit/task_center/db.py` 迁移只到第 8 版（schema 9），工作区是第 12 版（schema 13）；四次缺的迁移是 9（`criteria_version` / `base_sha`）、10（发号盘 + `ticket_links` + `prd_sha256` 可空）、11（车次 + 索引收窄）、12（`conflict_preview`）。
- 实测失败样本：`python kit/tools/auto_merge.py --ticket AWK-100 --repo D:/ai-workspace-kit --db <上游工作区>/.data/task-center.sqlite3 --dry-run` 报 `SCHEMA_MISMATCH: Unsupported task-center schema`，同一张单改用工作区的 `tools/auto_merge.py` 才能合（合并提交 `39926e1`）。
- 第二个实测样本：套件仓库根没有 `.agents/ticket-config.json`，`auto_merge` 第 2 步直接拒；用 `kit/tools/add_project.py` 补出来才走得下去。

## 所用 skill

- `plan-gate`：它要求计划先写依据与本轮目标编号，所以这份计划把「实测失败样本」当依据而不是当背景故事。
- `writing-plans`：它要求每个任务写清改哪些文件、接口是什么、先写失败用例再跑，所以任务块按「同步—读库—文档—用例」切开，每块带验证命令。
- `ticket-dispatch`：它规定验收条件只收「命令加期望输出」或「判断类加反例」，所以每条条件都能当场跑。
- `verification-before-completion`：它要求「做完了」之前贴真实输出，所以本单的完成证据一律是命令输出，不是「已同步」。

## 交付物

**主题**：把套件的引擎与闸门同步到工作区当前状态，让一个刚 clone 下来的人自带的那套脚本能读同一个共享库、走完一次真实合并。

**形状**：
- `kit/task_center/db.py` 补齐迁移 9–12 与 `SCHEMA_VERSION = 13`，`kit/task_center/core.py`、`kit/task_center/__main__.py` 补上配套行为（`criteria_version`、发号、`planned`、依赖边、车次、`conflict_preview`、回执与问答）；
- `kit/tools/` 下的 `auto_merge.py`、`work_gate.py`、`close_gate.py`、`claims_gate.py` 与工作区同名文件行为一致；
- `README.md` 增一节：clone 之后必须先跑 `add_project.py`，否则 `auto_merge` 第 2 步会拒；并写明当前与工作区的已知差异。

**给谁看**：clone 套件仓库、想用自带脚本管工单的人；以及验收会话。

**明确不覆盖**：不同步 `PRD-010`~`PRD-015` 的正文、不同步技能文本、不补 `idle_watch.py` 与 `project_doctor.py`（第二张单）；不发版、不打 tag、不推送；不迁移或备份工作区自己的库；不改业务判断规则（只让两边行为一致）。

**服务本轮目标第 5 条**（引擎同步）。

## 任务

### 任务 1：补齐四次迁移与版本号

对照工作区 `task_center/db.py` 的 9–12 段，把 `kit/task_center/db.py` 补到 `SCHEMA_VERSION = 13`，包括 `already_applied` 判据与拒绝文案。

验证：`python -c "import sys; sys.path.insert(0,'kit'); import task_center.db as d; print(d.SCHEMA_VERSION)"` 输出 `13`；`python -c "import sys; sys.path.insert(0,'kit'); import task_center.db as d; print(sorted(d.MIGRATIONS))"` 输出里含 9、10、11、12。

### 任务 2：同步核心行为与命令行

`kit/task_center/core.py` 与 `__main__.py` 对齐工作区同名文件；差异只允许出现在项目路径、配置读取这类宿主无关处，逐条写进 `summary`。先用一份 schema 9 的旧库做迁移演练（临时目录，不碰真库），确认 `migrate` 能从 9 走到 13。

验证：在一个临时的 schema 9 库上跑 `python kit/task_center/__main__.py --db <临时库> migrate`，退出码 0 且再跑一次 `list_projects` 能读出项目；输出贴进 evidence。

### 任务 3：套件自带脚本读得通当前库

`kit/tools/auto_merge.py`、`work_gate.py` 等按需同步。目标是 `python kit/tools/auto_merge.py --list` 读同一个共享库（schema 13）不再报 `SCHEMA_MISMATCH`。

验证：`python kit/tools/auto_merge.py --list --db <上游工作区>/.data/task-center.sqlite3` 退出码 0 并列出已验收未合并的工单（可以为空清单）；`python kit/tools/work_gate.py --list` 退出码 0。

### 任务 4：接项目这一步写进文档

`README.md` 增一节，说明 clone 后必须先 `python kit/tools/add_project.py <仓库>` 生成 `.agents/ticket-config.json`，否则 `auto_merge` 第 2 步会拒；同一节写清当前与工作区的已知差异（PRD 正文与技能文本尚未同步）。

验证：`grep -n "add_project.py" README.md` 有输出且落在安装/开工那一节；`grep -c "ticket-config.json" README.md` 输出大于等于 1。

### 任务 5：用例与闸门

同步新增的迁移与行为要带用例；不许把工作区已有的用例整段抄进来却不跑。

验证：`python tools/run_tests.py` 退出码 0，两处用例数都打印，合计不少于 530 项；`python tools/doc_gate.py --all` 退出码 0。

## 不做什么

- 不同步 `PRD-010`~`PRD-015` 正文、不同步技能文本、不补 `idle_watch.py` 与 `project_doctor.py`。
- 不发版、不打 tag、不推送。
- 不迁移、不备份 上游工作区 `.data` 里的真库；迁移演练只在临时目录做。
- 不改验收与合并的判断规则去迁就差异——两边不一致时改套件，不改规则。
- 不动上游工作区的主检出。

## 未决

- [待澄清：同步粒度] 是逐文件复制还是按工作区语义重写。默认：以工作区文件为准逐段搬，搬完用本单的验证命令与用例兜住；恢复条件：若某文件里有只属于工作区（例如工作区自己的项目路径）的硬编码，就地改成配置读取并在 `summary` 里逐条列出。
- 判断类风险：同步之后两边「看起来一致」但语义不同（例如 `already_applied` 判据放宽）。默认：把工作区的 `tests/` 里对应迁移用例一并搬过来，让 `python tools/run_tests.py` 判，而不是靠人眼比对。
