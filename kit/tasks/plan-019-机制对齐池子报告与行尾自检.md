# 计划 019：机制对齐——pool_report 三件、check_eol 两件、next_action 的池子报告段

## 依据

- `PRD-014-计划队列与依赖.md` v0.2（工单的 prd_path，`prd_sha256=bfb9b79e…8e861e4a`）：`planned` 允许空条件、`fix_pool` 这一类「池子里只剩领不走的单」的出处。
- `PRD-013-工单机制的执行面.md` v0.3（R10：合并记录只有 `auto_merge --record-only` 一个入口；`next_action` 只报不改）。
- `PRD-010-项目进度面板.md` v0.2 §4.3 S3 / S4（`wait_audit` 的 4 小时与 10 分钟阈值来源，本单不动它）。
- 工单 AWK-117 的六条判据（C1–C6），取自中心库 `get_ticket`。
- 现场依据（2026-11-24 本次开工实测，`git`/`diff` 自己跑过，不引用 03:38 那次的口径）：
  - master `5a7bcda` 上 `python tools/run_tests.py` 末行 `结果：全部通过`，合计 **1016 项**（`tests/` 354 + `tools/` 662）。
  - 上游 `D:/AI工作区` 比套件多：`tools/check_eol.py`(162 行)、`tools/pool_report.py`(101 行)、`tools/test_eol_check.py`(151 行)、`tools/test_pool_report.py`(119 行)、`tools/test_relative_db.py`(205 行)；`task_center/core.py` 多常量 `POOL_REPORT`、字段闭集一行、`_record_pool_report` 共 47 行；`tools/next_action.py` 多 8 个函数（`pool_report_events` / `pool_report_status` / `attach_pool_report` / `_age_text` / `_grace_text` / `_note_backoff` / `_rounds_text` / `_span_text`）与 `fix_pool` 一档。
  - 反向也有一份：套件的 `next_action` 有 `default_exemptions_path` / `ticket_branch_exists`（AWK-114 的合并口径），上游没有；`wait_audit` 的第五类 `awaiting_receipt`、`idle_watch` 的占位目录判断与 `center_config` 发现逻辑、`doc_gate` 的技能扫描、`closing.md` 的中心库发现一节，都只在套件这边。**所以这是双向合并。**
  - 两边差行数被行尾放大过一轮：套件工作树在 `core.autocrlf=true` 下检出成 CRLF，上游是 LF。去掉 CRLF 再比，`run_tests.py` 完全一致、`doc_gate.py` 只差套件那 11 行、`idle_watch.py` 只差套件那 57 行。

## 所用 skill

- `plan-gate`：它规定了这份计划必须有六节、`## 依据` 要带 PRD 编号与版本、每个点名的 skill 后面要写清它改变了什么，并把「有没有按规矩规划」变成一条能失败的脚本检查。
- `writing-plans`：它要求每个任务是「一个有独立测试环的最小单位」、每步是一个动作加一句可判的期望输出，而不是把代码抄一遍；这份计划按它的粒度切。
- `planning-and-task-breakdown`：它要求先画依赖图再排序（引擎常量 → 工具 → 依赖工具的 next_action → 文档 → 全量验收），并把「合并进来的新文件」和「套件已有的增量」分开放，别在一次改动里互相踩。
- `test-driven-development`：它把「先看见红再变绿」定为硬规则；这里每批文件都是先落测试、跑一次失败、再落实现、跑同一批通过，而不是先搬实现再补测试。
- `executing-plans`：它要求逐任务执行、每任务留一条带命令与结果的记录、偏离计划必须写成一条明账；本单用它当执行节奏与留痕方式。
- `using-git-worktrees`：它要求先确认隔离（`.worktrees/AWK-117` 已在 `ticket/AWK-117` 上、`git check-ignore .worktrees` 通过）再做基线测试，避免在主检出上并写。
- `ticket-dispatch`：它规定提交要带 `head_sha`、回执只有一条、成果路径要写实；本单的 `submit_result` 按它写。
- `verification-before-completion`：它禁止用「应该绿了」代替输出；下面每个任务都挂一条真能跑的命令与它的期望行。

## 交付物

**主题**：把套件对齐到上游当前机制——新增 `tools/check_eol.py`（行尾自检，CD-020 那条 LF 口径的落地）与 `tools/pool_report.py`（给空判据单落一条「已经点名报过」，AW-140 的写入口），配齐三份用例（行尾、池子报告、只读工具的相对路径），把 `next_action` 的池子报告段（退避 2h→6h→24h、报到第 3 轮升级、宽限期内不催）与 `fix_pool` 一档搬进来，并把四处文档合并到同一口径。

**形状**：六个任务、四次提交（`core+tool+test` → `eol` → `相对路径` → `next_action` → `docs` → `全员验收`），每个任务一批可独立跑的用例；最终判据是 `python tools/run_tests.py` 末行 `结果：全部通过` 且合计不少于 1012 项并比 1016 多。

**给谁看**：装这套件的人（他拿到的是一份能自己核行尾、能自己记账、派单器不再每轮重报同一张单的工具集），以及下一个要合并这张单的协调会话（它靠 `## 任务` 里的命令复核，不靠我的自述）。

**不覆盖**：
- 不发版：不动 `VERSION`、`CHANGELOG.md`、`plugin/`、`dist/`、`panel/`，不升版本号、不打 tag（C6 的反例）。
- 不改已发布的历史 tag，不改 `PRD/` 正文。
- 不搬上游的 `merge_candidates`（套件那份按项目根问豁免清单与 `ticket/<单号>` 分支，比上游多一层），也不把 `next_action` 的 `idle_watch` / `wait_audit` / `doc_gate` 的套件增量换成上游口径。
- 不拿真库做写入实验：`record_pool_report` 的用例一律在临时库里跑。

**服务本轮目标第 5、6 条**：`tasks/round.md` 第 5 条「引擎同步」与第 6 条「文档与工具剩余增量」——本单就是它剩下的那一段（工具与文档的差集），不新开方向。

## 任务

### 任务 1：`record_pool_report` 落库能力与它的写入口（池子报告的「记账」那一端）

**为什么不是「只搬代码」**：`pool_report` 解决的是「一张永远领不走的空判据单，被点名之后怎么记住**已经点过**」。库里只有 `events` 一张表能记这件事，而写它必须是 `task_center` 的写操作（`next_action` 拿到的库是 `mode=ro`）。所以这一档由三件组成：引擎一个操作 + 一个 CLI + 一份用例。少了引擎那一段，CLI 能拼出 payload 但落不了库；少了 CLI，每个巡检会话都得自己拼 JSON。

**文件**：
- 测试：`kit/tools/test_pool_report.py`（搬上游那一份，用例形状不动）
- 实现：`kit/task_center/core.py`（常量 `POOL_REPORT`、`FIELDS` 一行、`_record_pool_report`）、`kit/tools/pool_report.py`（搬上游那一份）

**接口**：
- `task_center.core.POOL_REPORT = "record_pool_report"`，`FIELDS[POOL_REPORT] = {"project_id","ticket_id","session_id","note"}`
- 读侧产出：`{"ticket_id","state","revision","session_id","note","reported_rounds","reported_at"}`
- 任务 4 消费 `POOL_REPORT` 这个常量名与 `events.operation` 这一列。

**步骤**：
1. 搬 `test_pool_report.py` 进 `kit/tools/`，跑它 → 红（`ModuleNotFoundError: pool_report`）。
2. 合并 `core.py` 的三段（常量、字段闭集、`_record_pool_report`），再搬 `pool_report.py`。
3. 重跑同一批用例 → 全绿。

**验证**：`python tools/test_pool_report.py` 末行 `OK`；`python tools/run_tests.py` 末行 `结果：全部通过`，合计比 1016 多出这一批的条数。

### 任务 2：行尾自检落地（CD-020 的 LF 口径）

**为什么要它**：`.gitattributes` 只护着 `skills/**`，全仓文本没有钉死——同一份内容在这台 `core.autocrlf=true` 的机器上检出成 CRLF、在别人机器上是 LF，而 `task_center/artifacts.py` 记的是工作树字节的 sha256，于是换个人复核就只会得到 `ARTIFACT_CHANGED`。规则写进属性里只是声明，所以要有脚本把「声明面（`check-attr eol`）」与「落地面（工作树字节里没有 CRLF）」两件都核。

**文件**：
- 测试：`kit/tools/test_eol_check.py`（搬上游那一份，改两处：仓库根解析成 git 检出根、内容根是它的子目录）
- 实现：`kit/tools/check_eol.py`（原样搬）、仓库根 `.gitattributes` 追加 `eol=lf`

**接口**：
- `check_eol.scan(root) -> Report`，`Report.managed` / `.problems` / `.skipped`
- 任务 6 的验收命令 `python tools/check_eol.py --root .worktrees/AWK-117/kit` 消费它。

**为什么测试要改**：上游的 `REPO = Path(__file__).parent.parent` 在套件里是 `kit/`，而 `kit/` 不是 git 检出根（`.git` 在仓库根），`git clone kit/` 直接报 `does not appear to be a git repository`；断言里的 `PRD/PRD-001-Cindy会话工作体系.md` 在套件里叫 `PRD-001-工作区使用与工单体系.md`。所以：`git rev-parse --show-toplevel` 取检出根克隆，扫的是克隆里的 `<前缀>/`（套件仓库里前缀是 `kit`，装出来的工作区里是 `.`），属性文件仍读克隆根的 `.gitattributes`。

**步骤**：
1. 搬 `test_eol_check.py` 并改上述两处，跑 → 红（`ModuleNotFoundError: check_eol`）。
2. 搬 `check_eol.py`，改 `.gitattributes` 为 `* text=auto eol=lf`，重跑 → 绿。
   **注意**：`.gitattributes` 只影响新检出，索引里的 blob 已经是 LF（`git ls-files --eol` 里 `i/crlf` 的 41 个文件全在 `skills/**`，而那是 `-text`），所以不做 `--renormalize`、不改任何文本字节。
3. 顺手在 `kit/AGENTS.md` 的常用命令里追加一行 `python tools/check_eol.py`（这一行是「怎么自己核」的入口，不放就没有人知道有这条命令）。

**验证**：`python tools/test_eol_check.py` 末行 `OK`；`python tools/check_eol.py --root .` 退出码 0 且首行以 `行尾自检通过` 开头——在**新检出**上跑才算数，当前工作树仍是规则落地前检出的 CRLF，红在预期之内，命令与判据写进提交说明。

### 任务 3：只读工具的相对路径 `--db` 统一（`test_relative_db`）

**为什么要它**：`Path.as_uri()` 不接相对路径，`--db .data/task-center.sqlite3` 会从 `ValueError: relative paths can't be expressed as file URIs` 里结束——一条用法错误看起来像工具崩了。上游把这条对整份清单钉成了用例，且用例本身反查源码（「自带 `read_only()` 又走 `as_uri()`」的模块必须都在清单里）。这一条上来就会命中套件这边一个真缺口：`kit/tools/idle_watch.py` 的 `read_only()` 是 `path = Path(path)`，相对路径直接崩。

**文件**：
- 测试：`kit/tools/test_relative_db.py`（原样搬）
- 实现：`kit/tools/idle_watch.py`（`read_only()` 先归一成绝对路径，与 `wait_audit` 的写法同口径）；若清单里还有别的工具红，一并按同一条规则改

**接口**：清单 `TOOLS`（六个工具，返回 `Connection`）与 `PAIRED_TOOLS`（`notice_payload`，返回 `(conn, how)`）；`test_list_covers_every_read_only_tool` 按源码反查，新工具漏登记会红。

**步骤**：
1. 搬 `test_relative_db.py`，跑 → 红在 `idle_watch`（`ValueError`，不是 `DB_NOT_FOUND`）。
2. 把 `idle_watch.read_only()` 的相对路径归一成绝对（保留它原有的 `mode=ro` + `PRAGMA query_only`）。
3. 重跑 → 绿；再跑一遍 `python tools/test_wait_audit.py`，确认没把第五类 `awaiting_receipt` 的用例带红。

**验证**：`python tools/test_relative_db.py` 末行 `OK` 且 `test_list_covers_every_read_only_tool` 在其中通过；`python tools/run_tests.py` 末行 `结果：全部通过`。

### 任务 4：`next_action` 的池子报告段（这一档的「怎么报」那一端）

**它到底干什么**：池子里可能存在状态可领、验收条件却空着的单。这种单永远领不走（`claim_ticket` 报 `CRITERIA_NOT_JUDGEABLE`），以前它只是 `skipped` 里一行，`action` 仍是 `nothing`——「池子里有活、只是没人能领」被报成「没活」。这一段把三件事补齐：
- **几层**：`execute_candidates` 把躺过 `BLANK_CRITERIA_GRACE_SECONDS`（24h）的空判据单另收一份到 `stale`，`scan()` 据此给出第五档 `fix_pool`（属于「有活」，退出 0）。
- **等太久**：`_age_text` / `_grace_text` 把「已经几天」「宽限期几天」说成人话，读不出建单时间时**按超期算**（宁可多报一次，也不让一行脏时间把单静默压住）。
- **该催**：`pool_report_events` 读 `events` 里 `operation=record_pool_report` 的行当台账（只认带 `result` 的行，被拒那行不算），`pool_report_status` 从**最后一次**点名算退避（2h→6h→24h，之后一直 24h），`attach_pool_report` 再判「这一轮该不该报、是不是第 3 轮该升级」；`_rounds_text` / `_span_text` / `_note_backoff` 负责把这些说清。

**文件**：
- 测试：`kit/tools/test_next_action.py`（在套件那一份上补齐上游的 `hours_ago`、`add_ticket(created_at=…)`、`FixPoolTest`、`FixPoolThrottleTest` 两批用例）
- 实现：`kit/tools/next_action.py`

**接口**：
- 新常量：`BLANK_CRITERIA_GRACE_SECONDS = 24*60*60`、`POOL_REPORT_BACKOFF_SECONDS = (7200, 21600, 86400)`、`POOL_REPORT_ESCALATE_AFTER = 3`
- 新动作名 `FIX_POOL = "fix_pool"`，进 `ACTIONS` 与 `WORKING_ACTIONS`（退出 0）
- `execute_candidates(conn, project_id, skipped, stale=None, now=None)`（多两个参数，套件现有调用点只有 `scan()` 一处）
- `--json` 多两节：`fix_pool`（每张带 `due` / `reported_rounds` / `seconds_until_next_report` / `escalate`）与 `pool_report`（`operation` / `backoff_seconds` / `escalate_after`）
- 任务 1 提供 `task_center.core.POOL_REPORT`。

**必须留住的套件增量**（这单最容易做砸的地方）：
- `merge_candidates` 保持套件签名 `(conn, project_id, project_root, skipped, stop)` 与三档判据（有分支头 / 清单里豁免 / 问 git `ticket/<单号>` 分支）；`default_exemptions_path`、`ticket_branch_exists` 两个函数与 `SKIP_EXEMPTED` / `SKIP_NO_BRANCH_HEAD` 两个名字不许被上游那份覆盖。
- `_finish()` 里 `exempted` 与 `no_branch_head` 两条「另有 N 张」照旧，只**追加**上游那条空判据的「另有 N 张空判据的单已经躺过 X 天」。
- `wait_audit` 的第五类、`idle_watch` 的占位与 `center_config`、`doc_gate` 的技能扫描、`run_tests` 的两处布局，一行不动。
- 本机绝对路径一律换成花括号占位（`WORKSPACE`），仓库里不得出现上游那台机器的目录名。

**步骤**：
1. 先把上游那两批用例补进 `kit/tools/test_next_action.py`，跑 `python tools/test_next_action.py` → 红（`NameError`/`AttributeError`：`fix_pool`、`attach_pool_report` 不存在）。
2. 按上面的接口搬 8 个函数与常量，改 `execute_candidates` 签名，在 `scan()` 里加 `fix_pool` 那一格（排在「验收在飞」之后、`idle` 之前），补 `_finish` 的追加段与 `_note_backoff`。
3. 重跑 `python tools/test_next_action.py` → 绿；特别核这三条用例：`test_inside_the_backoff_window_it_is_not_repeated`（宽限内不催）、`test_third_report_escalates_instead_of_repeating`（第 3 轮升级）、`test_stale_blank_criteria_becomes_fix_pool`（报告里看得出池子层数）。C5 的反例是「只搬了函数名、行为与上游不一致」，所以这三条必须真绿，不是 `skip`。

**验证**：`python tools/test_next_action.py` 末行 `OK`；`python tools/run_tests.py` 末行 `结果：全部通过`；`grep -c "pool_report_status" kit/tools/next_action.py` 输出大于 0（C2）。

### 任务 5：四处文档合并到同一口径

**为什么要逐节合而不是整份覆盖**：这四份文档两边各有一批新内容。上游多的：`record_pool_report` 行、空条件那一档的 `fix_pool` / 退避 / 升级三节、行尾口径（`* text=auto eol=lf`、AW-137 那一段）；套件多的：`INPUT_UNREADABLE`、schema 16 的命令面说明、发布类工单的提交落点（AWK-116）、中心库怎么被发现（`center_config` 七步）、`skipped` 的 `exempted` 一栏。整份覆盖会一次丢掉套件那五处。

**文件**：
- `kit/docs/protocol.md`：加 `record_pool_report` 一行；把「空条件的单领不走」那段换成上游那份（含 `fix_pool` 与退避/升级）；行尾那一段换成上游的 LF 口径。**保留**套件的 `INPUT_UNREADABLE` 段、schema 16 说明、「发布类工单的提交落在哪里」整节与 `--record-only` 那段的两行。
- `kit/docs/无人值守派单.md`：加 §3.1（`fix_pool`）与 §3.2（退避、台账、升级）；把「四个动作 / 三个动作」「`candidates` 三类 / 四类」「`reason_code` 表」三处按上游口径；**保留**套件的 §10「在自己的 Cindy 里建这条调度」（含 `schedule_plan`）与合并候选那一段的 `exempted` 表，变更记录两行并成一张表（按版本倒序，`0.3` 只有套件那条 `schedule_plan`，`0.4` 是上游的节流，本单再加一行 `0.5`）。
- `kit/docs/closing.md`：**不动**「中心库怎么被发现」（只在套件这边）；只补上游提到 `record_pool_report` / `fix_pool` 的地方（若上游该文件里没有，就确认它本来就无需改，并把这个结论写进提交说明）。
- `kit/docs/claims-format.md`：行尾那一段换成 LF 口径（与 `.gitattributes` 一致），示例里的 `"note"` 保持套件的脱敏写法。
- `README.md`：只改用例数那两句（`1012` → 新合计、`658` → 新 tools 数），不动版本号、不动 CHANGELOG。

**步骤**：
1. 按上面逐处改，每改完一份，跑 `python tools/doc_gate.py --all`。
2. 全改完再跑一次 `python tools/doc_gate.py --all` → 退出 0。
3. 核「套件的五处增量还在」：`grep -c "INPUT_UNREADABLE" kit/docs/protocol.md`、`grep -c "发布类工单的提交落在哪里" kit/docs/protocol.md`、`grep -c "中心库怎么被发现" kit/docs/closing.md`、`grep -c "schedule_plan" kit/docs/无人值守派单.md` 四条都大于 0。

**验证**：`python tools/doc_gate.py --all` 退出码 0；上面四条 `grep -c` 各输出大于 0；`grep -rn "D:\\\\AI工作区" kit/ --include=*.md --include=*.py` 输出为空（退出 1 也算对，因为它就是「一个都没有」）。

### 任务 6：全量验收与「没碰发版」的现场核对

**为什么单独成一档**：前五档各自绿，不等于整棵树绿——`next_action` 与 `wait_audit`、`pool_report` 与 `core` 是跨文件的契约，只有一处被旧口径带着跑就会红在别处。这一档只跑命令、只贴输出，不加新东西。

**步骤**：
1. `python tools/run_tests.py` → 末行 `结果：全部通过`，合计 ≥ 1012 且 > 1016。
2. 逐条对 C1–C3 的命令。
3. 核 C6：`git diff --name-only master...HEAD` 里没有 `VERSION`、`CHANGELOG.md`、`plugin/`、`dist/`、`panel/`。
4. `git add -A` 之前先看 `git status --short`，确认没有把 `.data/` 或临时克隆带进来。

**验证**：
- `python tools/run_tests.py` 末行 `结果：全部通过`
- `ls tools/check_eol.py tools/pool_report.py tools/test_eol_check.py tools/test_pool_report.py tools/test_relative_db.py` 五行都列出（C1；在 `kit/` 下按套件口径，C1 的 `tools/` 指 `kit/tools/`）
- `grep -c "awaiting_receipt" tools/wait_audit.py` 输出大于 0、`ls tools/install_guard.py tools/schedule_plan.py tools/center_config.py` 三行都列出（C3）
- `git diff --name-only master...HEAD` 输出里不出现 `VERSION` / `CHANGELOG.md` / `plugin` / `dist` / `panel`（C6 的反例是顺手升了版本号）

## 不做什么

- 不改 `VERSION`、`CHANGELOG.md`、`plugin/`、`dist/`、`panel/`；不打 tag、不推送、不合并（合并由协调会话在验收后走 `auto_merge`）。
- 不改已发布的历史 tag 与已发布的 0.5.2。
- 不搬上游的 `merge_candidates` / `_finish` 的 skipped 部分（套件那份按项目根判豁免与分支，比上游多一层）。
- 不把 `wait_audit` 第五类、`idle_watch` 占位、`doc_gate` 技能扫描、`center_config`、`run_tests` 两处布局、`install_guard`、`schedule_plan`、`automation/` 两份模板换成上游口径，哪怕上游文件看起来「更新」。
- 不做 `git add --renormalize`，不改任何文本的工作树字节（`.gitattributes` 只影响之后的检出）。
- 不拿 `D:/AI工作区/.data/task-center.sqlite3` 做写入实验：`record_pool_report` 的用例一律用临时库；读库一律显式 `--db`。
- 不在 `tasks/round.md` 上改状态、不写收尾包（收尾是协调会话那一轮的事）。

## 未决

无。[待澄清] 三处已自查：C1 的 `tools/` 指 `kit/tools/`（套件仓库的源码树里工作区内容在 `kit/` 下，`kit/tools/run_tests.py` 的 `SUITES` 也是这两处）；`README.md` 的用例数是否要跟着改已按「改」处理（它自述以 `run_tests.py` 汇总行为准）；`check_eol.py --root .` 在当前工作树上会红，已按「在新检出上算数」处理并写进任务 2 的验证里。
