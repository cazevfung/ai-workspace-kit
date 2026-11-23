# 更新记录

## 未发布（AWK-104：装完 run_tests 必须真绿）

修掉「陌生人装完跑 `python tools/run_tests.py` 出 10 个 FAIL」：README 写着「应全绿」，装出来的现场却红。两处都是用例假设了源码布局，不是套件内容缺东西。

- `install.py`：新增一步把 `verify/verify_receipt.py` 装到 `<工作区>/verify/`（模板占位照旧替换）。只装这一个——`verify_install.py` / `verify_onboard.py` 要调仓库根的 `install.py` / `uninstall.py`，那是源码树才有的，装进别人的工作区等于发一份跑不起来的壳。缺脚本时在开工前就退出 2，不把失败留到装完才发现。
- `kit/tools/test_verify_receipt.py`：`SCRIPT` 改成 `find_script()`——先认 `<ROOT>/verify/`（装出来的工作区），再退到 `<ROOT>/../verify/`（源码树里的仓库根）；两处都没有就抛错，让这组用例整体报错，不静默跳过。新增 `ScriptLocationTest` 钉住解析方向（只装壳、路径取错都会红）。
- `kit/tools/test_idle_watch.py`：`DefaultWorkdirTest` 改成两种布局都成立——默认目录要么是占位，要么**就是本文件所在的工作区**（`Path(item).resolve() == ROOT.resolve()`）。占位符字面量改成拼接（`"{{" "WORKSPACE" "}}"`），否则 `install.py` 会把这行一起替换掉，装出来的工作区里根本没有占位符可测。断言没有放宽：写死另一台机器的绝对路径仍然当场红。
- `README.md` / `README.en.md`：用例数 724 → 725（`tests/` 303 + `tools/` 422），并把「应全绿」写成实测过的现场命令（临时工作区 + `--agents-home`，装完再跑 `run_tests.py` 与 `verify_receipt.py`）。
- 验证：在全新 HOME + 全新工作区里真装一遍再跑，`汇总：tests/ 303 OK；tools/ 422 OK  | 合计 725 项` / `结果：全部通过`（装前同一现场是 `tools/ 421 FAIL(10)`）。

## 未发布（AWK-103：引擎同步到 schema 16）

把 `kit/task_center` 从 schema 13 推到工作区（`D:/AI工作区`）当前的 16：套件自带脚本读同一个中心库不再报 `SCHEMA_MISMATCH`。只同步引擎与配套文档，不改业务判断规则，也不动工作区的库与历史数据（`D:/AI工作区` 一直是只读对象）。

- `kit/task_center/db.py`：`SCHEMA_VERSION` 13 → 16，补三次迁移——14（`tickets.requester_session_id`、`notices.message_id`）、15（`tickets.owner_session_id`，存量单按它当前尝试的 `coordinator_session_id` 回填，没有当前尝试的留 NULL）、16（`merges.source` / `verified_by` / `verified_at` / `note` 四列，存量行一律留 NULL，不批量填 `auto_merge`）；新增 v13/v14/v15 三份冻结形状，`merges` 与 `notices` 的旧形状单独冻结，不再从 `REQUIRED_COLUMNS` 做减法（加一级就会误拒本该放行的旧库）。
- **`MIGRATIONS` 的键改成「这一步装配完成后的版本号」**（`MIGRATIONS[16]` = 15 → 16 那一步），与工作区按升级前版本号登记的做法差一格，取步统一走 `migration_step()`。改的理由是「这份引擎认得哪些版本」与「当前是第几版」变成同一件事：`sorted(MIGRATIONS)` 的末项就是 `SCHEMA_VERSION`。判据本身（形状、`already_applied`、拒绝文案）逐字照抄工作区，没有放宽。
- `kit/task_center/core.py`：配套行为一并搬进来——`create_ticket` 收可选 `requester_session_id`；`get_ticket` 回 `sessions`（请求方 / 协调 / 执行 / 验收 / 回执收件人，带 `session_url`、`device`、`session_state`、`stale`，回执收件人那条带 `message_id`）；`claim_ticket` 同一句里认领 `owner_session_id`；`record_notice` 的授权改读当前 owner 并收可选 `message_id`；新增 `take_over_ticket`（接替责任人，不动 `revision` 与 origin）、`report_notice_failure`（执行会话记自己没送到的回执，操作里没有 `status`，造不出 `sent`）、`repair_backfill_command`（只改补记行 `verified_by` 的路径写法）；`merge_ticket` 收可选 `source` / `note`，补记档当场重跑 `git merge-base --is-ancestor` 才写行。
- `kit/task_center/hostsessions.py`、`kit/task_center/sessions_context.py`：两份新文件，与工作区逐字相同（读宿主会话库只走只读连接，读不到就当空表）。
- `kit/docs/protocol.md`：「操作与字段」那张表补到 schema 16 的命令面（删掉「本表停在 schema 9」横幅，`init` / `migrate` / `backup` 的号改成 16，新增 `take_over_ticket` / `report_notice_failure` / `repair_backfill_command` 三行，`create_ticket` / `get_ticket` / `list_tickets` / `claim_ticket` / `revise_ticket` / `record_notice` / `merge_ticket` 各按套件实际代码改准），并补上 `criteria` 的形状闸门那段。表下面的节仍是原文，缺的是 PRD-005/013/014/015 的正文。
- `kit/PRD/PRD-014-计划队列与依赖.md`：两处 `task_center/core.py:268` 的行号引用跟着代码位移改准（`454` / `454-460`）。`core.py` 一长，闸门就报「指向第 268 行是空行」——改引用，不改闸门。
- `README.md` / `README.en.md`：引擎状态改成 schema 16、用例数 673 → 724、「已知差异」第 6 条改成「表补到当前命令面、表以下的节仍缺 PRD 正文」。
- 用例：新增 `tests/test_sessions_context.py`（20）、`tests/test_owner.py`（9）、`tests/test_notice_failure.py`（7），`tests/test_migrate.py` +8（含 v14 头配新形状的拒绝、v15 四列全在时补完迁移、v15 只有部分列时拒绝且一个字节不改）、`tests/test_merge.py` +7；`tests/test_notice.py` 不再写死 schema 号。`python tools/run_tests.py` 汇总 724 项（`tests/` 303、`tools/` 421）。
- 迁移演练只在临时目录做：用本单开工前那份引擎（`git show 08c446a:kit/task_center`）建一份 schema 13 库、开两张单（一张 `ready`、一张 `planned`），再用本单的代码 `migrate` 到 16，两张单原样还在、`list_tickets` 仍回 2 条。
- `D:/AI工作区/.data/task-center.sqlite3` 全程只读：`python kit/tools/work_gate.py --list --db <那个库>` 退出 0、不报 `SCHEMA_MISMATCH`，跑前跑后文件 `md5` 不变，也没有做快照。
- 已知差异：`MIGRATIONS` 的键位与工作区差一格（见上），以后逐级同步时先看 `migration_step()` 的注释，别按工作区的键号直接抄。

## 未发布（AWK-102：文档与工具剩余增量）

把工作区剩下的文档与工具搬进套件，并修掉两处「该报警却没报」的盲点。判据一律以工作区为准，搬过来时把本机路径换成 `{{WORKSPACE}}`，没有反过来改工作区。

- `kit/PRD/` 新增 `PRD-010`~`PRD-015` 六份正文（`PRD-010-项目进度面板`、`PRD-011-UI设计技能`、`PRD-012-共用资源的身份与占用`、`PRD-013-工单机制的执行面`、`PRD-014-计划队列与依赖`、`PRD-015-多主任务派单`），路径与示例改写成套件视角，`grep -rl "D:/AI工作区" kit/PRD/` 为空。`PRD-015` 里两条复算命令原来钉在工作区的历史轮次表与收尾包行号上，改成「原工作区的第 N 行」——套件不带那些历史文件，照原样写就是两个断链，`ref_gate` 会当场拒。
- `skills/workspace/` 的 `pdsa-workflow`、`prd-authoring`、`skill-routing`、`ticket-dispatch` 四个技能对齐到工作区现有文本（内容不改写），并补上 `skill-routing` 里 `nodes.md` 的 N30 指向、却在套件里一直缺着的 `shared-resource-identity`。`installed-skills.md` 给套件自带的 `bootstrap-workspace` 写了一行「明确不接（安装面）」并同步来源计数——不然它自己就成了一个「装了没接」的漏项。
- `kit/tools/idle_watch.py`（谁在等一句话）与 `kit/tools/project_doctor.py`（安装面体检）连同各自用例搬进来。`idle_watch` 的两处本机硬编码（默认扫 `D:/AI工作区` 与 `D:/cindy-harmony-app`）换成 `{{WORKSPACE}}` 占位，装好之后才是真实目录；占位没被替换时（在套件仓库里直接跑）打一行提示说明「这次一个会话都扫不到」，不让 0 条被误读成没有悬空。
- `kit/tools/wait_audit.py` 搬进来并新增第五类 `awaiting_receipt`：工单 `awaiting_review`、最新提交已过 15 分钟、`notices` 里没有任何一条 `sent`（写 `failed` 的不算走过回执）就报。起因是 2026-11-22 的实测——AWK-101 的执行会话提交完没发回执，当时只有「等在验收超过 4 小时」一条，工具一声没响，靠人追问才发现。这一条与「4 小时没人验收」是两条链，各数各的；已有 review 行、或已有 `sent` 回执的不报，跳过原因写进 `--json` 的 `ignored`。末行合计、`--json` 的 `summary`、退出码都跟着变成五类。另修一处：`--db` 给相对路径时报的是一屏 traceback（`as_uri()` 只收绝对路径），现在先归一成绝对路径，缺库仍报 `DB_NOT_FOUND` 并退出 2。
- `wait_audit` 的判据口径写死一句：**只有 `sent` 算送达，`failed` 一样要报**——`failed` 只说明曾经想发没发成，拿它放行等于把「想发过」当成「发到了」，AWK-101 的沉默就是这么来的。`kit/tasks/plan-007-文档与工具剩余增量.md` 的「未决」里原来写着「写 `failed` 的算作走过回执不报」，那句话是错的，随本单改成正确口径（阈值仍是 15 分钟，恢复条件不变）。
- `kit/tools/auto_merge.py` 补上「写记录前的 schema 闸门」：代码要求更高就先做文件层快照、用**合并后的代码**在短命子进程里 `migrate`、再把进程里那份 `task_center` 按磁盘重载，失败停在第 7 步并报 `SCHEMA_MIGRATE_FAILED` / `SCHEMA_DB_AHEAD` / `SCHEMA_SNAPSHOT_FAILED` / `SCHEMA_RELOAD_MISMATCH`。闸门认两个位置：`task_center/db.py` 与 `kit/task_center/db.py`——2026-11-22 合 AWK-101 时 schema 从 9 推到 13，它只按仓库根找，于是打印「仓库里没有 `task_center/db.py`，跳过」，在这个仓库上从来没生效过。闸门只在本工具自己打开中心库时建立：调用方传进来的记录函数是桩（用例），不该拿真库做实验。
- 用例：`tools/test_idle_watch.py`（34）、`tools/test_project_doctor.py`（6）、`tools/test_wait_audit.py`（30，含新的 `AwaitingReceiptTest` 与退出码用例）、`tools/test_auto_merge.py` 的 `SchemaGateTest`（13）。两处合计 590 → 673 项（`tests/` 252 + `tools/` 421）。新增行为都先写过会失败的用例：`wait_audit` 的十条一次跑红，schema 闸门的十三条在改前的 `auto_merge.py` 上全部报错。
- 技能的比对口径（判据 v2）：把工作区那份里的 `/d/AI工作区` 与 `D:/AI工作区` 同样替换成 `{{WORKSPACE}}` 之后，四个 `SKILL.md` 的 `sha256` 与套件逐条相同（`/d/AI工作区` 那种形态也一起替换，所以四份都是 SAME）。未替换时 `ticket-dispatch/SKILL.md` 只差第 23 行那一处路径，其余三份本来就逐字节相同。`skills/workspace/skill-routing/scripts/check_routing.py` 在本机退出 1，报的三条都是本机 `~/.agents/skills` 里后装、本单未处置的 skill（`cangjie-skill` / `impeccable` / `reverse-engineer-anything`）——同一个脚本在 `D:/AI工作区` 上退出码与报错完全相同。分得清的判据是「输出里不出现 `missing-skill` / `unknown-skill` / `missing-owner` / `unknown-node` / N0x 缺号」：实测 0 条。

## 未发布（AWK-101：引擎同步到 schema 13）

把 `kit/task_center` 与 `kit/tools` 同步到工作区（`D:/AI工作区`）当前状态。本单只同步引擎，不改业务判断规则：两边不一致时一律改套件去就工作区的判据，一处也没有反过来改工作区。

- `kit/task_center/db.py`：`SCHEMA_VERSION` 9 → 13，补迁移 9（`criteria_version` / `base_sha`）、10（发号盘 `projects.next_number`、`ticket_links`、`prd_sha256` 改可空——这一级要重建 `tickets` 表）、11（`batches` 车次表 + `active_session` 索引收窄到 `batch_key IS NULL`）、12（`submissions.conflict_preview`）；冻结形状改成 `V3_*` 起点逐级长出来，并补 `column_notnull()`：只看列集合分不出 10 与 11，而 11 要放宽的正是 `tickets.prd_sha256` 的 NOT NULL。
- `kit/task_center/core.py`：补状态机与命令面——`planned` 池子与 `--planned` 视图（`claimable` / `blocked_by`）、库内发号（`create_ticket` 不再收自报 `ticket_id`）、`blocked_by` / `related` / `rides_with` 依赖边与成环拒收、车次归属、提交时的 `git merge-tree` 冲突预演（算不出来落 NULL，不冒充「跑过且干净」）、`criteria_version` 与「改判据不再默认作废在飞尝试」、拒收留一行事件、`list_tickets` 的表头与 `behind`、`list_unmerged` 的 `since_days`。
- `kit/task_center/__main__.py`：加 `--planned` 这条 CLI 糖（只对 `list_tickets` 生效）；保留套件自己的 `INPUT_UNREADABLE`；新增直接跑文件的入口——`python kit/task_center/__main__.py --db <库> migrate` 以前会因为相对导入当场 ImportError，现在把套件根放上 `sys.path` 再按顶层包名导入同一份 core。
- `kit/tools/work_gate.py`：同步到工作区版——在途集合只有一份定义（`CLOSING_BLOCKING_STATES` / `WAITING_STATES`，`planned` 不算在途）、`--list` 六列带 `started_at` / `last_event_at`、`--list --json`、`--stalled-minutes` 阈值判红、项目现场一次读全（`project_snapshot` / `describe_in_flight`）。用法行保留 `{{WORKSPACE}}`。
- `kit/tools/close_gate.py`：在途集合从 `work_gate` 导入，不再自己写一份四个状态的元组（AW-094 就是两份定义给出两个答案）。
- `kit/tools/ref_gate.py`：一处也没核到时多打一行 `EMPTY`（仍退 0，但看得出这次闸门空转了）。
- `kit/tools/run_tests.py`：自检跳过 `skills/`（技能随身的脚本用例有自己的交付线）。
- `kit/docs/closing.md`：补「在途集合只有一份定义」一节——`tools/test_close_gate.py` 直接断言代码与文档是同一个六状态集合。
- `README.md` / `README.en.md`：新增「接项目这一步：clone 之后先跑 `add_project.py`」一节，写明缺 `.agents/ticket-config.json` 时 `auto_merge` 第 2 步会拒，并逐条列出与工作区的已知差异（PRD 正文与技能文本未同步、`auto_merge` 不做自动迁移、保留占位符、`doc_gate` 多认两处源码路径、夹具已脱敏）。
- 随引擎同步一起修掉的三处「照旧写法会被新引擎拒」的地方：`kit/examples/demo.py` 与 `kit/examples/dual_project_drill.py` 不再自报 `ticket_id`、验收条件改成闸门认的形状、`submit_result` 补 `base_sha`；`verify/verify_onboard.py` 同样三处，并改成从 `tasks/round.md` 读轮次号（以前写死 R-001，套件轮到 R-002 之后收尾那一步必然红）。九步自检本次实跑 9/9 通过。
- `kit/README.md` 的第一张工单示例不再传 `ticket_id`；`kit/onboard.py` 与 `plugin/draft/main.js` 的派发说明把可领状态从「ready / rework」改成「planned / ready / rework」。
- `kit/docs/protocol.md`：操作表上方加一条醒目的「本表停在 schema 9」横幅，逐条列出六处照旧表写会被拒或读错的地方（发号、`planned`、`base_sha`、`revise_ticket` 的 `restart`、`list_tickets` 的列与池子视图、schema 号 13）。正文没有重写——补齐协议要把 PRD 正文一起带进来。
- 用例：`tests/` 与 `tools/test_*.py` 两处都同步到工作区版（新增 `test_batch` / `test_planned_queue` / `test_judgeable` / `test_conflict_preview` / `test_revise_semantics`，并补齐迁移 9–13 的用例）。用例数 504 → 590（`tests/` 252 + `tools/` 338）。
- 已知没同步的：`kit/docs/protocol.md` 仍是同步前的命令面（`planned` / 车次 / 依赖边 / 冲突预演几节都还没有），补齐它要把 PRD-005/013/014/015 正文一起带进来，不属于本单；当下以 `task_center/core.py` 的 `ALLOWED` 与 `get_ticket` 实际回的字段为准。
- 迁移演练只在临时目录做：拿本单开工前那份 schema 9 引擎建一份旧库，再用本单的代码 `migrate` 到 13，原有工单与指纹逐字保留、`next_number` 按已有最大号初始化；`D:/AI工作区/.data/task-center.sqlite3`（schema 13、195 张工单、7 个项目）除本单自己的那一条 `bind_session` 事件外一行未写，也没有跑过 `migrate`。

## 0.4.0（2026-11-21）

2026-11-21 发布。把 KIT-005「装上就能开工」与 KIT-006「契约补实、插件可装」两轮成果合成一个版本；本单只做版本号、变更记录与插件包对齐，闸门判定与核心逻辑一行未改。

- 版本与包对齐：`VERSION` 到 `0.4.0`；`plugin/draft/ghost.json` 的 `version` 同步到 `0.4.0`，重新打包为 `dist/ai-workspace-kit-0.4.0.cindy`，`dist/` 下只留这一个包。`README` / `README.en` / `docs` / `kit/README` / `plugin/README` 里的版本号、用例数与包名同步到这次发布。
- 装上就能开工：新增 `onboard.py`，打印「`create_project` → 开协调会话 → 第一条指令」的调用计划，`--json` 给每步完整参数，`--dry-run` 零副作用；`dispatch_ticket` 计划复用同一份 `send_to_session` 参数。新增 `skills/workspace/bootstrap-workspace`：写清经 `cindy_mcp_call_tool` 调 `cindy_helper` 的确切参数、预演与事后 `list_sessions` 反查。
- `plugin/draft/` 升级为可安装清单：`ghost.json` 声明 `bootstrap` 与 `dispatch_ticket`，`main.js` 返回与 `onboard.py --json` 同源的步骤与失败停点，补 `locales/`。
- 新增 `tools/add_project.py`：为业务仓库写 `.agents/ticket-config.json`（`test_cmd` / `target_branch`）并把 `core.hooksPath` 指向套件 `.githooks`。`tools/auto_merge.py` 改读项目配置：缺配置在第 2 步非零退出并提示跑 `add_project.py`，不再回落 `tools/run_tests.py`；目标分支默认取配置值。
- `tools/doc_gate.py` 补源码树的技能发现路径（`skills/workspace` 与 `skills/generic`），修掉 0.3.0 里「在源码目录直接跑测试有 7 项 `test_doc_gate` 失败」的已知问题；判定条件未改。
- 新增 `verify/verify_onboard.py`：临时 HOME 里跑 install → add-project → create_ticket → claim → bind → submit → review → merge → close 九步。
- 新增 `kit/docs/decisions.md`：把被反复引用的四条口径——消息通道三档、回执一条、送达判据、项目接入面——沉淀成随包文档。`kit` 与 `skills` 里原来指向包外需求文档的 19 个文件全部改指向它，或改成不依赖外部文档的表述；`grep -rn "PRD-00[2-9]" kit skills --include=*.md --include=*.py` 已经没有命中。
- `kit/docs/protocol.md`：`create_ticket` 的字段行写明 `deliverable` ≤500 字；新增命令行层错误码 `INPUT_UNREADABLE`，读不到 `--input` 的文件时带回原样的路径与系统原因，不再和「参数不合格」共用一个 `INVALID_INPUT`。核心返回的 `error` 由命令行原句打印，核心的校验与文案一行未改。
- 插件补真实图标：`plugin/draft/assets/icon.png`（1024×1024，由 Cindy 的图片能力生成一次），`ghost.json` 声明 `icon`——KIT-006 先打成 `0.2.1`，本单随套件升到 `0.4.0` 重打。新增 `kit/tools/test_plugin_pack.py` 拆包自检（清单、`main.js`、图标是真图且大于 1024 字节、包与源码一致）。
- `.gitignore`：`dist/` 改成只放行 `dist/*.cindy`，其他构建输出照旧忽略。
- `plugin/README.md` 与根 `README.md` 补「装插件 → 启动 → 它报出四步计划」一节，写明装那一步必须人在 Cindy 里点确认，并写明「打包 ≠ 安装 ≠ 上架」。
- 返工（KIT-006 验收 reject 的 C4）：补 `tools/test_plugin_pack.py` 仓库根转发层，与 `tools/run_tests.py`、`tools/doc_gate.py` 同形，`python tools/test_plugin_pack.py` 现在在仓库根就能跑；顺手清掉两处指不到文件的引用——`docs/protocol.md` 里那份不在包内的计划文件名，以及 `pdsa-workflow` 指向 PRD 第 8 节 / `PRD-001` §7 的两处。
- 用例数 453 → 491。

## 0.3.0

同步到原工作区 2026-11-20 13:32（master `6bc6d32`），对应上游 R-007 到 R-009 三轮。

- 中心库 schema 8 → 9：提交时记下被验收那棵树的 head sha，合并前与被合并的树比对，合错树会被挡下。
- 新增 `tools/claims_gate.py` 与 `docs/claims-format.md`：把文档里写的数（计数、合计、比例、摘要、字节数、行号锚点、清单条数）变成一次能失败的复算。`--coverage` 只列没有 claim 覆盖的活数位置，退出码恒为 0。
- 新增 `tools/ref_gate.py`：行号引用与「见 某文件 的「某标题」」锤点，指向不存在就非零退出。
- `tools/auto_merge.py` 随 schema 变化更新；`ticket-dispatch` 技能补上新的验收与合并口径。
- 用例数 381 → 453。
- 已知：在侢架目录直接跑 `python tools/run_tests.py` 会有 7 项 `test_doc_gate` 失败，因为那些用例要求项目里存在 `.agents/skills`。安装之后跑是全绿的，`verify_install.py` 验的就是安装后的状态。

## 0.2.1

- 修安装器：技能与交接协议里的 `{{WORKSPACE}}` 以前会原样留在用户机器上，现在复制时就换成真实路径。注入统一用正斜杠，因为 `.json` 里的反斜杠要转义，直接塞 Windows 路径会把 JSON 弄坏。
- `verify_install.py` 加一条检查：装完之后 `skills` 与 `intake` 下不能残留占位，现在 14 项全过才算通过。
- README 补「从需求到合并：它管的是整条线」：PRD 与版本、两次评审、工单派到不同项目、别的项目请工作区接管，以及交接链在包里的四个件。

## 0.2.0

同步到原工作区 2026-11-19 12:02（master `688ae7f`）的状态。

- `tools/doc_gate.py`：新增「完成证据必须可判定」判据（任务块里要有可跑命令 + 期望输出，或写成判断类并给出反例）。
- `tools/evidence_baseline.txt`：历史计划豁免清单。原工作区里的条目不会随套件分发，装出来是空基线。
- `skills/workspace/skill-routing/`：新增 `scripts/`（路由检查、索引检查、上游漂移检查、技能重新链接）与 `index/` 快照，路由表可直接自检。
- `docs/skill-loading.md`：技能内容入库与加载验证的判定方式。
- `AGENTS.md`：回执口径改为「一条回执 + 一次落库 + 一次会话列表核对」；终态只落库不唤醒。
- `skills/workspace/prd-authoring`、`ticket-dispatch`、`installed-skills.md` 同步更新。
- 用例数 376 → 381。
- 新增 `maintainer/redact_for_public.py`：同步之后清掉真实用户名与会话 id（`tests/fixtures` 是原样复制的，光靠同步脚本清不掉）。

## 0.1.0

首个可分发版本。

- 工作区骨架：`task_center/` 工单核心、`tools/` 四道闸门与 `auto_merge`、`tests/` 全量用例。
- 25 个技能：5 个工作区技能 + 20 个通用技能，随仓库分发。
- 一键安装：`install.py`（跨平台）+ `install.ps1` / `install.sh`。
- 卸载：`uninstall.py`，按安装清单只删本套件装进去的东西。
- 文档：`docs/SHARING.md`（载体选型分析）、`docs/INSTALL.md`、`docs/ARCHITECTURE.md`。
- 设计史：`design/` 保留原始 PRD 与 AGENTS 记录。
