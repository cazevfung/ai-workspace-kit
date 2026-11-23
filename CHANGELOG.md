# 更新记录

## 0.5.1（2026-11-23）

2026-11-23 发布。**这是一版安全修复，取代同一天发出去的 `0.5.0`。** `v0.5.0`（`46ce67d`）的 `install.py` 会无条件重写 `<agents_home>/center.json`，而那个参数的默认值就是 `~/.agents`；任何人拿下载链接试装一次，都可能把自己机器上的 `center.json` 改指到别处，于是整台机器的工作区工具一起读到空库——2026-11-23 22:10 真发生过一次（现场见下面 AWK-113 那条）。master 上早已合入修掉它的安装闸门，这一版就是把闸门发出去。

`v0.5.0` 的 tag 留在 `46ce67d`，不删、不移动、也不 `tag -f`，那个 Release 照旧在；`0.5.1` 是它的替代下载。已经从 GitHub 下过 `0.5.0` 的人不必慌：受影响的是「装到临时目录」或「`--agents-home` 指向非默认目录」的那一次，看一眼 `~/.agents/center.json` 里的 `db` 是否还指向原来那份库就知道有没有被改；已经装好的机器重装一次就换上新的 `install.py`。

### 安全修复：这版改了什么（AWK-113 的安装闸门）

- `kit/tools/install_guard.py` 是唯一判据，新增 `tree_digest` / `diff_tree` 两个只读函数：装前装后对同一棵树取快照（≤256 KB 的文件算内容哈希、更大的只记大小与 mtime，另记目录项本身），用来证明「一个字节没变」，不必先写进去再恢复。
- `install.py` 的默认行为改了。`--agents-home` 不是本机真实用户目录时（沙箱、临时、别人的目录）**默认一个用户级文件都不写**（`center.json` / `skills/` / `intake/` / 安装清单）；就是真实目录、而那里的 `center.json` 已指向**别的**库时**拒绝**，在复制第一个文件之前退出（码 2）并原文打印旧内容。要写必须显式 `--allow-user-config`；首次安装与「同一个库再装一次」行为不变。
- `--dry-run` 预报三类结果：会写哪些用户级文件、会跳过什么、会拒绝什么。
- `verify/verify_install.py` 加两项现场检查：不带开关的沙箱安装一个用户级文件都不写、真实 `~/.agents` 装前装后快照一致。用例在 `kit/tools/test_install_guard.py`（17 项）。

### 发布动作（AWK-115）

- `VERSION` 与 `plugin/draft/ghost.json` 都是 `0.5.1`；`dist/` 只留 `ai-workspace-kit-0.5.1.cindy`（旧包用 `git rm` 删掉）。打包命令是 `python tools/pack_plugin.py`：条目按名字排序、时间戳固定，同一份源码两次打出来字节相同，打完立刻拆开复核清单、入口与图标；宿主没有「打 `.cindy`」的现成能力，所以这条路就是可重复的手工方式。
- 源码包 `ai-workspace-kit-0.5.1-source.zip` 由 tag 点的树生成：`git archive --format=zip --prefix=ai-workspace-kit-0.5.1/ v0.5.1 -o dist/ai-workspace-kit-0.5.1-source.zip`（同一棵树、同一条命令，内容相同）。
- 推 `master` 与 `v0.5.1` tag，`gh release create v0.5.1` 附两个资源；Release note 指回 `0.5.0` 并写明这版取代它。三道闸门（`tools/run_tests.py` / `verify/verify_install.py` / `maintainer/redact_for_public.py --check`）全绿之后才推。
- 随发布清掉 `kit/tasks/plan-013` / `plan-014` 里残留的两处真名与一处真实会话 id（`redact_for_public.py` 的替换口径，只动文档、不动代码）：`redact_for_public.py --check` 在 master 上本来就是红的，不修它这一版过不了第三道闸门。


### 公开仓库里剩下的本机路径：18 行逐条归位（AWK-112）

2026-11-23 验收 v0.5.0 时量到一处遗留：`source.zip` 里那串本机路径命中 22 处、分布 7 个文件。这一单把它收口。收口的标准不是「`grep` 变空」：**历史记录不能为了好看而动**，所以每一处命中都要能说清自己去哪了——已改、历史记录保留、上游专名，三档之外不留未分类的行。

开工前在 `7f99910` 上按同一口径重测，得 **18 行 / 20 处、7 个文件**。与工单那份 22 处不同，是因为名单本身变了：`kit/PRD/PRD-010` 与 `kit/tasks/plan-010` 现在已经是 0 处（前面两张单已经清过），顶上来的是 `plan-013`、`plan-015`。工单里写的是当时的事实，不改。

下表用「本机路径」指代 `D:/AI工作区` 那一串，只在必要处引原文——清单自己不再把那一串抄一遍，否则清理单反过来又添二十处命中。行号是开工时基线 `7f99910` 上的；`CHANGELOG` 条目按小节定位，不靠行号，因为本节插在最前面会把它下面全部往下推。

| 位置 | 那一行写的是什么 | 归宿 |
| --- | --- | --- |
| `README.md:300` | 小节标题「与工作区（本机路径）的已知差异」 | **已改** |
| `README.md:304` | 第 1 条里拿本机路径当 `grep` 搜索串、说结果为空 | **已改** |
| `README.en.md:273` | 同一句的英文版，同样把本机路径当搜索串 | **已改** |
| `maintainer/sync_from_origin.py:4` | 用途句：`kit/` 与 `skills/` 来自原工作区（本机路径） | **已改** |
| `maintainer/sync_from_origin.py:11` | 用法行 `[--origin 本机路径]`，把某台机器的目录写成默认值 | **已改** |
| `maintainer/sync_from_origin.py:14` | 替换规则表：本机路径的两种写法（正斜杠、转义后的反斜杠） → 占位符 | **已改** |
| `maintainer/sync_from_origin.py:44` | `replace_paths()` 里三条写死的 `text.replace(本机路径, …)` | **已改** |
| `maintainer/sync_from_origin.py:96` | `--origin` 的 `default=os.environ.get("AI_WS_ORIGIN", 本机路径)` | **已改** |
| `CHANGELOG.md`「引擎同步到 schema 16」（AWK-103）第 103 行 | 「从 schema 13 推到工作区（本机路径）当前的 16」，该行两处 | 历史记录保留 |
| 同上第 114 行 | 「本机路径/.data/task-center.sqlite3 全程只读」 | 历史记录保留 |
| `CHANGELOG.md`「文档与工具剩余增量」（AWK-102）第 121 行 | 「`kit/PRD/` 新增六份正文……`grep -rl` 本机路径 为空」——当时真跑过的判据 | 历史记录保留 |
| 同上第 123 行 | `idle_watch` 的两处本机硬编码（默认扫本机路径）换成占位符 | 历史记录保留 |
| 同上第 128 行 | 技能比对口径里「把 `/d/AI工作区` 与本机路径 同样替换成占位符」 | 历史记录保留 |
| `CHANGELOG.md`「引擎同步到 schema 13」（AWK-101）第 132 行 | 「同步到工作区（本机路径）当前状态」 | 历史记录保留 |
| 同上第 148 行 | 「本机路径/.data/task-center.sqlite3（schema 13、195 张工单……）除本单自己那一条外一行未写」 | 历史记录保留 |
| `kit/tasks/plan-012-自动化层与0.5.0发布.md:105` | 不做什么「不动本机路径 的任何文件（只读参照）」 | 历史记录保留 |
| `kit/tasks/plan-013-沙箱安装不写用户级配置.md:98` | 不做什么「不动已装好的工作区（含本机路径）」 | 历史记录保留 |
| `kit/tasks/plan-015-next-action合并盲点.md:71` | 完成证据里那条 `next_action.py --db 本机路径/.data/…` | 历史记录保留 |

三档的理由写在这里（表里只放归宿，理由太长放不下）：

- **`README` 两处与 `README.en` 一处是「用法示例」。** 拿一串本机路径当 `grep` 的搜索串登在 README 里，读的人会以为那是默认目录。现在两版都改成中性说法：`kit/PRD/` 下搜不到上游那台机器的路径，逐行清单就是本节。小节标题也从「与工作区（本机路径）的已知差异」改成「与上游工作区的已知差异」，因为下游真正要比的是「上游那份工作区」，不是这台机器上的某个目录。
- **`maintainer/sync_from_origin.py` 五处不是文档，是「锁在一台机器上」。** `--origin` 的默认值写死成本机目录，等于「换一台机器就得改脚本」，而这段代码是公开的。现在改成必填来源：命令行没给就看环境变量 `AI_WS_ORIGIN`，两者都没有就退出码 2 并打一行用法，**不猜一个默认目录**。替换规则也不再是三条写死的常量，改由实参 `origin` 派生三种写法（正斜杠、单反斜杠、转义后的双反斜杠；按「先换更长的」排，否则双反斜杠那种写法会被拆成两半、留下孤立的单个反斜杠）——同一份代码对任何原目录都成立。
- **`CHANGELOG` 七行九处与 `kit/tasks` 三行一行未改。** 那十行写的是当时真的发生过的事（哪次同步、当时 `grep` 的结果、哪一次只读演练），改掉等于篡改记录。这一档的判据是「这句话描述的是过去的一次事实」，不是「这句话里有没有那串路径」。

两种变体写法也一并扫了，各归各档：`/d/AI工作区` 2 处——`CHANGELOG` 的 AWK-102 条目 1 处、`design/PRD/PRD-009-项目接入面.md` 的已作废条目 AC03 里 1 处，都是**历史记录保留**（后者是当时 `readlink -f` 出来的现场证据）；`d:\\ai工作区` 1 处——`kit/docs/probe.md:19` 示例 JSON 里的 `"name":"AI工作区"` 与 `"root"`，归**上游专名**（那一行是在转录原工作区这个项目在中心库里登记的 `project_id` / `name` / `root` 三者，改掉名字会让示例与真实输出对不上；它不在 `kit/PRD/` 也不是默认值，本单只登记不改）。

本单自己也添了三行带那串路径的文本，都归**历史记录保留**，理由与其它计划文件一样（记录当时发生了什么，改了就对不上）：`kit/tasks/plan-016-公开仓库本机路径清理.md` 里两行是复现命令（`grep -rn -F` 全仓扫描与判据 C1 那条），本节第 11 行那句是用来定义「本机路径」这个代称本身的。

所以今天的账是：基线那 18 行里，**改了 8 行**（`README` 2 + `README.en` 1 + `maintainer` 5）、**保留 10 行**（`CHANGELOG` 7 + `kit/tasks` 3）。加上本单自己添的 3 行，今天全仓 `grep -rn -F` 剩 **13 行 / 15 处、5 个文件**，每一处都在上面那张表里（含本单自己那 3 行）。判据 C1 那条 `grep`（只扫 `README.md` / `README.en.md` / `docs/` / `kit/PRD/`）现在无输出。

刻意没做：不改 `CHANGELOG`「未发布」以下的任何一条、不改 `kit/tasks/plan-0*` 的任何一行、不改 `kit/PRD/`（已是 0 处）、不改 `design/PRD/PRD-009` 的历史证据行、不改 `panel/docs/` 的样张；不动 git 历史（不 `amend`、不 `rebase`、不 `force push`）、不重打 tag、不回写已发布的 Release；不改 `task_center/`、不改闸门判断逻辑、不加用例（合计仍是 1012 项）、不升 `VERSION`、不推送。`README` 里那个 `合计 1004 项` 在开工前就已经是 1012（属 AWK-114 的读数），本单不顺手改。

### `next_action` 不再看不见「已验收但没合并」（AWK-114）

2026-11-23 当晚真实发生三次（`AW-139`、`AWK-113`、`AWK-106`）：单已经 `done`、`merges` 里没有行，`auto_merge.py --list` 把它列进「真待合并」，`next_action.py` 却报 `dispatch_execute` 去开下一张单，每次都要人或巡检把合并这一步抳回来。

- 根因不是顺位（合并本来就在派执行之前），是**判据少了一层**：`next_action` 只看 `submissions.head_sha` 一个事实，既不读历史豁免清单，也不看 `ticket/<单号>` 分支在不在。于是「没有分支头、清单里也没有它」的单进了 `skipped: no_branch_head`，`action` 照旧是 `dispatch_execute`——同一现场两个答案。
- 现在三档判据逐条对齐 `auto_merge.py --list`：有 `head_sha` → 待合并；没有 `head_sha` 但在 `<项目根>/docs/unmerged-exemptions.json` 里 → 历史豁免（`skipped: exempted`）；清单里也没有 → 问 git `ticket/<单号>` 分支在不在（在 → 待合并，走 `--record-only`；不在 → `skipped: no_branch_head` 并把「快照该刷新了」写在 `detail` 里；查不了 → 进 `stop` 当故障报）。
- 豁免清单与分支判断**直接调 `auto_merge` 的 `load_exemptions` / `exists`**，不重写这套规则；`auto_merge.py` 与 `task_center/` 一个字节未改。合并命中时人读那一行点名单号，多张命中时把其余单号一并列出；提交里没有分支头时在 `stop` 里写明合并记录会按 `unverified` 落库。
- 这一路径仍然只读：不 `claim`、不写库、不改工单状态、不自动补合并记录。退出码口径不变（0 有活 / 2 没活 / 1 出错）。
- 用例：`kit/tools/test_next_action.py` 新增 `MergeCriteriaTest` 七项——分支已合入且池子里另有可领单时仍报 `merge`、补上 `merges` 行后不再报、有 `ticket/<单号>` 分支但提交没有分支头的算待合并、历史直做与豁免单都不算、以及一条交叉用例（同一份临时库上 `auto_merge --list` 的「真待合并/历史豁免」两栏与 `next_action` 的 `merge` 候选单号与顺序逐字相等）。合计 1004 → 1012 项（`tests/` 354 + `tools/` 658）。
- 刻意没做：不改 `auto_merge.py`、不改 `task_center/`、不把「done 但一条提交都没有」升级成阻塞派发、不自动跑 `backfill_merges.py --apply`、不升 `VERSION`、不打 tag、不推送。

### 非 Cindy 宿主也能派单：`--host generic` 与「不许猜 session id」（AWK-106）

2026-11-17 的 AW-007：从临时目录名拼了一个 session id 调 `bind_session`，库照收，`list_sessions` 里根本没有那条会话。根因在于文档只写了一句「其他宿主需要自己补这一步」，没有可照着做的形状，也没有停点。

- `kit/onboard.py` 新增 `--host generic`：计划从 `create_project / send_to_session` 换成 `create_ticket → claim_ticket → open_session → bind_session → submit_result → get_ticket`，全程不出现在 `cindy_helper` 工具里；`open_session` 标 `local`，由人或宿主自己开，**要交回宿主自己报出的真实 session id**。
- 新增 `check_session_id()` 与 `--session-id`：**只收 UUID 形状**（`8-4-4-4-12` 位小写十六进制，可带前缀如 `session_<uuid>`），其余一律拒，退出码 2；拒绝理由逐类写清（空串、`<…>` 占位、路径分隔符、占位词、`session_D_AI` / `D_AI工作区` 这类把目录名 slug 化出来的串），并带一句「本仓库里的真实会话 id 都是 UUID；你的宿主若不是这个形状，就在计划里写『此处由你填写真实 id』，不要猜、不要从路径拼」。`build_plans(host="generic")` 默认把这个占位值摆在 `bind_session` 的 `session_id` 上，并在同一步写 `requires_real_session_id: true`——计划本身不会拿假 id 交差。**闸门只核形状、不核存在**：形状合法的随手 id 也过得了，所以计划里不写「已验证存在」，只写「这个 id 必须来自宿主回执或用户」。
- 输出顶层的 `host_capabilities` 把差异写成字段：`create_project` / `send_to_session` / `schedule_create` 为 `false`，`files_scripts_cron` 为 `true`；`stop_points` 把「拿不到真实 id 就停在第 3 步」与 AW-007 原文一并给出。`--host generic` 的 payload 里**不出现 `cindy_helper_tools`**，`failure_paths` 只有 `generic_dispatch`——bootstrap / dispatch_ticket 的停点全是 Cindy 专用，混进这份 payload 就与 `host_capabilities.create_project=false` 自相矛盾。
- 默认 `--host cindy` 的两张计划、`CINDY_HELPER_TOOLS`、插件 `PLANS` 一致性检查一字未改；`TASK_CENTER_TOOLS` 补了 `create_ticket` / `bind_session` / `submit_result`（本来就是引擎操作，只是之前没进计划）。
- `README` / `README.en` 新增「两种宿主」一节（差别表 + 取真实 session id 的方法 + 停点 + `check_session_id` 的 UUID 判据），并贴了一段真跑出来的 generic 输出（顶层键与 `failure_paths` 分区），用例比着真输出核「一字不差」；开头那句「Claude Code、Codex 均可」改成「Cindy 已实测，其他宿主只保证文件、脚本与 cron 这一层」；`docs/INSTALL.md` / `docs/SHARING.md` 的同一句指向 `--host generic`。
- 用例：`kit/tools/test_onboard.py` 加 20 项（generic 计划形状、`local` 步、停点含 AW-007、能力字段、session id 判据——空串 / 占位 / 路径 / 占位词 / 目录名 slug / 带前缀 UUID / 形状合法但随手写，CLI 六条，加 README 输出一致性一条）。合计 984 → 1004 项（`tests/` 354 + `tools/` 650）。
- 刻意没做的：不实现任何宿主适配器，不改 `task_center/` 与 `send_to_session`，不改 `plugin/` 与 `dist/`，不升 `VERSION`、不打 tag、不推送。

### 沙箱安装不再改写真实的 `~/.agents`（AWK-113）

2026-11-23 22:10 的真实事故：一个验收会话把工作区装到临时目录 `D:\awk109-verify\ws`，`--agents-home` 用了默认值（真实 `~/.agents`），安装器无条件写 `<agents_home>/center.json`，于是真实 `center.json` 被改指到沙箱的空库。后果不是「这次装错了」而是「所有工作区工具一起读到空库」——`work_gate` / `auto_merge` / 钩子全报 0 张在途，巡检据此发出错误指令。

- 新增 `kit/tools/install_guard.py`：「这次安装允不允许写用户级配置」只有这一处判据，外加一个只读的 `tree_digest` / `diff_tree`（装前装后对同一棵树取快照，用来证明「一个字节没变」，不必先写进去再恢复）。
- `install.py` 新增 `--allow-user-config`。两种默认行为改了：`--agents-home` 落在真实用户目录以外时**一个用户级文件都不写**（`center.json` / `skills/` / `intake/` / 安装清单）；`--agents-home` 就是真实目录而那里的 `center.json` 已指向**别的**库时**拒绝**，在复制第一个文件之前退出（码 2）并把原内容原样打印。首次安装与「同一个库再装一次」行为不变。
- 跳过用户级配置时工作区指针 `<工作区>/.agents/center.json` 也不写：它记的是 `agents_home`，而那里没有 `center.json` 时闸门按「指针指丢」报 `DB_NOT_FOUND` 不回落；不写指针，闸门走「本工作区 `.data`」那一步，装出来的工作区照常可用。
- `--dry-run` 预报这三类结果：会写哪些用户级文件、会跳过什么、会拒绝什么。
- 用例：新增 `kit/tools/test_install_guard.py`（17 项）——沙箱默认不写（真跑安装，临时 `--agents-home`）、显式开关才写、首次安装与同库重装不变、真装重指默认拒绝且文件字节不变、`--dry-run` 三类预报、快照能认出「改一个字节」与「多一个文件」。端到端用例全部拒绝写真实 `~/.agents`：真装重指那条用假 HOME（`USERPROFILE` / `HOME`），并在跑之前先确认子进程的 `Path.home()` 真是它，不是就跳过。合计 967 → 984 项（`tests/` 354 + `tools/` 630），AWK-106 再加 20 项后是 1004 项（`tools/` 650）。
- `verify/verify_install.py`：安装命令补开关，并加两项现场检查——不带开关的沙箱安装一个用户级文件都不写、真实 `~/.agents` 装前装后快照一致（只读比对）。现在 23 项全过。
- `docs/INSTALL.md` / `README` / `README.en` / `bootstrap-skill/ai-workspace-setup`：写清默认跳过什么、怎么显式开、拒绝时怎么办；`kit/docs/closing.md` 的「中心库怎么被发现」补一句「沙箱默认安装不写指针，走第 5 步」。
- 刻意没做的：不改 `task_center/`，不改 `center_config` 的发现顺序，不迁移历史安装，不自动备份或轮转旧 `center.json`，不升 `VERSION`、不打 tag、不推送。

## 0.5.0（2026-11-23）

2026-11-23 发布。这一版把三件事凑齐发出去：Windows 常驻浮窗面板并入套件（`panel/`）、工单引擎与工具补齐到与工作区同功能、无人值守那两条调度做进套件（`automation/` 与 `tools/schedule_plan.py`），连同此前累计还没发出去的几段成果一起；闸门判定、工单核心的状态机与面板的判据一行未改。

### 自动化层进套件：两条调度与一条闸门命令（AWK-109）

无人值守那一层以前只以「这台机器上的两条定时任务」存在，别人装完套件拿不到。这一版把它变成可复制的产物：正文是模板，参数由脚本打印，**建调度这一步仍然留给人**。

- 新增 `kit/automation/`：`queue-watch.md`（队列值守，每 30 分钟巡一遍会话）与 `dispatcher.md`（无人值守派单器，每 10 分钟读一次库）。两份都是从本机在跑的正文里取下来的，逐段抹掉本机绑定物——`targetSessionId`、36 位的调度 / 会话 id、`AppData/Roaming/Cindy` 下的会话库路径、模型与 provider、别的项目的根——工作区路径统一写成安装时替换的占位符。判据段落（四类要看的、粗筛前移、红线、必须叫人的三种情况）一段没删。
- 新增 `kit/tools/schedule_plan.py`：输入一个工作区路径，输出 `schedule_create` 的完整 args（`name` / `cronExpr` / `timezone` / `recurring` / `workingDir` / `prompt` / `silentWhenIdle` / `preRunHook`）。**它不建调度、不写任何宿主状态**：源码里没有 `schedule_*` 调用、没有子进程、没有网络、不落一个文件，用例把这五条逐个钉住。
- 闸门就是退出码：派单器的 `preRunHook` 直接跑 `python <工作区>/tools/next_action.py`，`0` 放行、`2` 跳过本轮不烧 token、其它 fail-closed——与 `next_action.py` 自己的三档口径一一对上，不另包一层脚本。队列值守**不设闸门**：它要看的是「会话卡住了」，而这件事不在工单库里，加了闸门等于把唯一能发现它的一路关掉。
- `kit/docs/无人值守派单.md` 新增 §10「在自己的 Cindy 里建这条调度」：两条调度各多久跑一次、闸门的三档、参数怎么拿、建调度时哪三件事必须人定（模型、跑在哪条会话、要不要通知），以及离开 Cindy 用系统 cron 或 Windows 计划任务加同一个退出码替代（含 `|| true` 会把出错当成没活的写法警告）。§11 变更记录跟着加一行。
- `README` / `README.en` 新增「自动化：无人值守跑起来」一节，指向那份文档与脚本；「装了什么」的目录树补上 `automation/`。
- `verify/verify_install.py` 加两项现场检查：`automation` 两份模板已装到工作区、占位符已换成这台机器的真实路径（只证明「没留下什么」不算过）。
- 用例：新增 `kit/tools/test_schedule_plan.py`（35 项）——两份模板在、占位符按布局两种都成立、没有写死盘符、没有 UUID / 会话库 / `targetSessionId` / 模型名、两条调度的 cron 与 `silentWhenIdle`、派单器钩子指向 `next_action.py` 而队列值守没有钩子、`--only` 与 `--json` 两条分支、相对路径与非绝对路径报错、模板里混进会话 id 时宁可报错也不输出。合计 932 → 967 项（`tests/` 354 + `tools/` 613）。
- 发布动作：`VERSION` 与 `plugin/draft/ghost.json` 都是 `0.5.0`，重打 `dist/ai-workspace-kit-0.5.0.cindy`，`dist/` 只留这一个包；推 `master` 与 `v0.5.0` tag，`gh release create` 附两个资源（包与 `ai-workspace-kit-0.5.0-source.zip`）。
- 刻意没做的：不替人建调度（脚本只打印参数）、不动 `task_center/`、不发 SkillHub、不上架插件市场。

### 工单系统与工具补齐到与工作区同功能（AWK-110）

引擎与工具面追平工作区，判据一处没有反过来改工作区；套件自己独有的增量（`wait_audit` 的第五类、`idle_watch` 的占位判断、`doc_gate` 的两处源码扫描、`center_config` 的中心库发现、schema 闸门认 `kit/task_center/db.py`）一条不丢。

- `kit/task_center/core.py`：补 `archive_ticket`（第十个终态 `archived`，只能从 `done` 进、必带理由、`revision` 不变）与 `rename_project`，连带 `pending_blockers` 认归档为满足、`cancel_ticket` / `take_over_ticket` 认归档为终态、`review_dispatched`、三档验收（`pass` / `unverified` / `fail`）、`planned` 允许空条件、跨项目车次脏行的判据。`db.py` 保留套件自己的迁移键位（`MIGRATIONS` 的键是「这一步装配完成后的版本号」，与工作区差一格，统一走 `migration_step()`）。
- `kit/tools/`：搬进 `next_action.py`、`receipt_payload.py`、`handover_owner.py`、`coordinator_state.py`、`archive_legacy.py`、`backfill_merges.py` 与 `merge-projects.json`；`work_gate` 补池子年龄线（`PLANNED_MAX_DAYS`）与收尾包改动背书；`auto_merge` 补「按项目配置测试命令」那一层（`merge-projects.json` → 仓库自己的 `.agents/ticket-config.json` → 缺省 `run_tests.py`）；`close_gate` 按「每条」判第 5 节落点；`run_tests` 换成两处并行，仍然两处的用例数都打印。
- `kit/docs/`：搬进 `无人值守派单.md`、`schema-shift.md`、`unmerged-exemptions.md`（与 `.json`），`protocol.md` 与 `closing.md` 按工作区对齐。
- 用例：`kit/tests/` 与 `kit/tools/test_*.py` 两处同步，新增 `test_archive` / `test_parking` / `test_review_dispatched` / `test_review_unverified` / `test_answer_owner` 与六个工具各自的用例文件。合计 724 → 932 项（`tests/` 354 + `tools/` 578）。
- 这一版不发版、不推送、不动 `panel/`、`VERSION`、`CHANGELOG`、`plugin/`、`dist/`——发版留给 AWK-108 之后的发布单收口。

### 中心库只有一处发现路径（AWK-105）

以前五个闸门各自找一遍库，装到别人的机器上就出现「闸门读的是另一份库」。现在发现链只有一处。

- 新增 `tools/center_config.py`：`--db` → `TASK_CENTER_DB` → `TASK_CENTER_AGENTS_HOME` → 工作区指针 `.agents/center.json` 记的 `agents-home` → 本工作区 `.data` → `~/.agents`。指针指丢就报 `DB_NOT_FOUND`，不回落、不新建库。
- `install.py` 在工作区写 `.agents/center.json`（只记 `agents_home`，不进版本库），`uninstall --all` 一并删掉；`work_gate` / `auto_merge` / `close_gate` 与 `idle_watch` / `wait_audit` 都改从这一处取，`work_gate --list` 打出「凭什么读到这一份」。
- `kit/docs/closing.md` 新增「中心库怎么被发现」；`docs/INSTALL.md`、`docs/SHARING.md` 跟着对齐；`verify/verify_install.py` 加两项现场检查（读到本工作区的库、配置缺失报 `DB_NOT_FOUND`）。
- 用例：`test_work_gate` 9 条、`test_auto_merge` 2 条，逐条锁住七步与默认档。

### 浮窗面板并入套件（AWK-108，Windows 常驻浮窗 420×700）

把已经在 `cindy-dashboard` 里跑起来的桌面浮窗搬进套件，作为 `panel/` 的一部分；判据、配色、版式与用例期望一行未改，只把本机路径换成占位符、把安装与自检接上。

- 新增 `panel/`：`app/`（`main` / `feed` / `resolve` / `host` / `sessions` / `hit` / `window-math` / `probe` 八个 `.mjs` 加 `preload.cjs`、清单与 `package-lock.json`、`tray.png`、七个 `node:test` 用例）、`tools/`（只读取数层、只读服务、导出、留白探针与两个 `verify-*.mjs`）、`docs/样张/`（`sample.html`、`对照.md`、四张 420×700 PNG）、`tests/fixtures/`（黄金对照的两份夹具）与**改写过的** `README.md`。不带 `node_modules/`、`dashboard.html`（生成物）、`docs/evidence/`、`docs/CD-*.md`，也不带面板仓库自己的 Python 用例。
- 本机绝对路径一律换占位符：导出物默认落 `<工作区>/.data/progress-export.json`、样张里的样例项目根写成 `<工作区>/panel`、会话库默认目录改成读 `APPDATA`（不写死某台机器的用户名）。`app/test/` 里那几条路径断言改成拿同一个常量比对，源码树（占位符还在）与装出来的工作区（已换成真实路径）两种布局下都成立——期望值一个没动。
- `install.py`：`TEXT_SUFFIXES` 补 `.mjs` / `.cjs` / `.html`——不补的话，装完的 `main.mjs` 会拿着一串没替换的占位符去读导出物，样张里也全是占位符；新增 `copy_panel()` 与「附：把浮窗面板装到 `<工作区>/panel`」一步（编号不动，接在 `7/7` 之后，与「附：为其他 agent 建技能链接」同一个写法）。center DB 发现那一段一行未动。
- `verify/verify_install.py`：新增两项检查——`panel 已装到工作区`、`panel 占位已换成真实路径`（后者既要没有残留占位符，也要 `main.mjs` 里真的有那个临时工作区的路径，不能只证明「没留下什么」）。顺手把两处长期没跟着技能增减改的计数从 6 / 26 改成 7 / 27：这两条在 master 上本来就是红的，红的原因不是这次改动。
- 新增 `tools/pack_plugin.py`：宿主没有「打 `.cindy`」的现成能力，所以补一条可重复的打包命令——条目按名字排序、时间戳固定（同一份源码两次打出来的字节相同），写完立刻重新拆开读一遍（包根有清单、入口非空、图标是真 PNG），验过才删旧包，失败时 `dist/` 里留着的仍是上一个能装的包。
- 面板的跑法多一个入口：node v24 把 `--test` 的目录形参当模块解析（`node --test app/test` 直接 `Cannot find module`，退出 1）。新增 `app/run-tests.mjs` 与 `app/test/package.json`（`main` 指向它）让目录形参有落点；入口放在 `app/` 而不是 `app/test/`，否则默认发现规则会把它当第八个测试文件、把七份跑成两份。77 项用例照旧。
- `README.md` / `README.en.md` / `plugin/README.md` 同步版本、用例数与包名，并各加一节「浮窗面板」（写明要 Node 18+ 与 `npm install electron` 才能跑）；`kit/PRD/PRD-010` 头部把「实施在外部目录」改成套件内的 `panel/`（版本 0.2）；新增 `kit/PRD/PRD-016-Windows常驻浮窗.md`，把面板仓库那份浮窗规格按套件视角模板化（版本 0.1）。
- `maintainer/redact_for_public.py`：发布前脱敏这条在 master 上本来就是红的（3 个文件残留），原因不是真漏了本机信息——它把夹具里的**人造** UUID（`aaaaaaaa-1111-4111-8111-…`）也当成真实会话 id 替换，而 `kit/tools/test_idle_watch.py` 的夹具同时造三条会话，全换成 `<session-id>` 就撞 `sessions.id` 的唯一约束、整组用例直接 ERROR（这单真跑过一次）；另一处命中的是没被解析的环境变量 `%USERPROFILE%`，不是某一台机器的用户名。改两条判据：夹具模块（`tests/test_*.py`、`tools/test_*.py`）跳过 UUID 那条规则，真名那条（`C:/Users/` 下带用户名的路径）一个字没放宽；`%USERPROFILE%` 不再算残留。`kit/tools/` 下的文件一个没动，现在 `--check` 无残留。

### 装完 run_tests 必须真绿（AWK-104）

修掉「陌生人装完跑 `python tools/run_tests.py` 出 10 个 FAIL」：README 写着「应全绿」，装出来的现场却红。两处都是用例假设了源码布局，不是套件内容缺东西。

- `install.py`：新增一步把 `verify/verify_receipt.py` 装到 `<工作区>/verify/`（模板占位照旧替换）。只装这一个——`verify_install.py` / `verify_onboard.py` 要调仓库根的 `install.py` / `uninstall.py`，那是源码树才有的，装进别人的工作区等于发一份跑不起来的壳。缺脚本时在开工前就退出 2，不把失败留到装完才发现。
- `kit/tools/test_verify_receipt.py`：`SCRIPT` 改成 `find_script()`——先认 `<ROOT>/verify/`（装出来的工作区），再退到 `<ROOT>/../verify/`（源码树里的仓库根）；两处都没有就抛错，让这组用例整体报错，不静默跳过。新增 `ScriptLocationTest` 钉住解析方向（只装壳、路径取错都会红）。
- `kit/tools/test_idle_watch.py`：`DefaultWorkdirTest` 改成两种布局都成立——默认目录要么是占位，要么**就是本文件所在的工作区**（`Path(item).resolve() == ROOT.resolve()`）。占位符字面量改成拼接（`"{{" "WORKSPACE" "}}"`），否则 `install.py` 会把这行一起替换掉，装出来的工作区里根本没有占位符可测。断言没有放宽：写死另一台机器的绝对路径仍然当场红。
- `README.md` / `README.en.md`：用例数 724 → 725（`tests/` 303 + `tools/` 422），并把「应全绿」写成实测过的现场命令（临时工作区 + `--agents-home`，装完再跑 `run_tests.py` 与 `verify_receipt.py`）。
- 验证：在全新 HOME + 全新工作区里真装一遍再跑，`汇总：tests/ 303 OK；tools/ 422 OK  | 合计 725 项` / `结果：全部通过`（装前同一现场是 `tools/ 421 FAIL(10)`）。

### 引擎同步到 schema 16（AWK-103）

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

### 文档与工具剩余增量（AWK-102）

把工作区剩下的文档与工具搬进套件，并修掉两处「该报警却没报」的盲点。判据一律以工作区为准，搬过来时把本机路径换成 `{{WORKSPACE}}`，没有反过来改工作区。

- `kit/PRD/` 新增 `PRD-010`~`PRD-015` 六份正文（`PRD-010-项目进度面板`、`PRD-011-UI设计技能`、`PRD-012-共用资源的身份与占用`、`PRD-013-工单机制的执行面`、`PRD-014-计划队列与依赖`、`PRD-015-多主任务派单`），路径与示例改写成套件视角，`grep -rl "D:/AI工作区" kit/PRD/` 为空。`PRD-015` 里两条复算命令原来钉在工作区的历史轮次表与收尾包行号上，改成「原工作区的第 N 行」——套件不带那些历史文件，照原样写就是两个断链，`ref_gate` 会当场拒。
- `skills/workspace/` 的 `pdsa-workflow`、`prd-authoring`、`skill-routing`、`ticket-dispatch` 四个技能对齐到工作区现有文本（内容不改写），并补上 `skill-routing` 里 `nodes.md` 的 N30 指向、却在套件里一直缺着的 `shared-resource-identity`。`installed-skills.md` 给套件自带的 `bootstrap-workspace` 写了一行「明确不接（安装面）」并同步来源计数——不然它自己就成了一个「装了没接」的漏项。
- `kit/tools/idle_watch.py`（谁在等一句话）与 `kit/tools/project_doctor.py`（安装面体检）连同各自用例搬进来。`idle_watch` 的两处本机硬编码（默认扫 `D:/AI工作区` 与 `D:/cindy-harmony-app`）换成 `{{WORKSPACE}}` 占位，装好之后才是真实目录；占位没被替换时（在套件仓库里直接跑）打一行提示说明「这次一个会话都扫不到」，不让 0 条被误读成没有悬空。
- `kit/tools/wait_audit.py` 搬进来并新增第五类 `awaiting_receipt`：工单 `awaiting_review`、最新提交已过 15 分钟、`notices` 里没有任何一条 `sent`（写 `failed` 的不算走过回执）就报。起因是 2026-11-22 的实测——AWK-101 的执行会话提交完没发回执，当时只有「等在验收超过 4 小时」一条，工具一声没响，靠人追问才发现。这一条与「4 小时没人验收」是两条链，各数各的；已有 review 行、或已有 `sent` 回执的不报，跳过原因写进 `--json` 的 `ignored`。末行合计、`--json` 的 `summary`、退出码都跟着变成五类。另修一处：`--db` 给相对路径时报的是一屏 traceback（`as_uri()` 只收绝对路径），现在先归一成绝对路径，缺库仍报 `DB_NOT_FOUND` 并退出 2。
- `wait_audit` 的判据口径写死一句：**只有 `sent` 算送达，`failed` 一样要报**——`failed` 只说明曾经想发没发成，拿它放行等于把「想发过」当成「发到了」，AWK-101 的沉默就是这么来的。`kit/tasks/plan-007-文档与工具剩余增量.md` 的「未决」里原来写着「写 `failed` 的算作走过回执不报」，那句话是错的，随本单改成正确口径（阈值仍是 15 分钟，恢复条件不变）。
- `kit/tools/auto_merge.py` 补上「写记录前的 schema 闸门」：代码要求更高就先做文件层快照、用**合并后的代码**在短命子进程里 `migrate`、再把进程里那份 `task_center` 按磁盘重载，失败停在第 7 步并报 `SCHEMA_MIGRATE_FAILED` / `SCHEMA_DB_AHEAD` / `SCHEMA_SNAPSHOT_FAILED` / `SCHEMA_RELOAD_MISMATCH`。闸门认两个位置：`task_center/db.py` 与 `kit/task_center/db.py`——2026-11-22 合 AWK-101 时 schema 从 9 推到 13，它只按仓库根找，于是打印「仓库里没有 `task_center/db.py`，跳过」，在这个仓库上从来没生效过。闸门只在本工具自己打开中心库时建立：调用方传进来的记录函数是桩（用例），不该拿真库做实验。
- 用例：`tools/test_idle_watch.py`（34）、`tools/test_project_doctor.py`（6）、`tools/test_wait_audit.py`（30，含新的 `AwaitingReceiptTest` 与退出码用例）、`tools/test_auto_merge.py` 的 `SchemaGateTest`（13）。两处合计 590 → 673 项（`tests/` 252 + `tools/` 421）。新增行为都先写过会失败的用例：`wait_audit` 的十条一次跑红，schema 闸门的十三条在改前的 `auto_merge.py` 上全部报错。
- 技能的比对口径（判据 v2）：把工作区那份里的 `/d/AI工作区` 与 `D:/AI工作区` 同样替换成 `{{WORKSPACE}}` 之后，四个 `SKILL.md` 的 `sha256` 与套件逐条相同（`/d/AI工作区` 那种形态也一起替换，所以四份都是 SAME）。未替换时 `ticket-dispatch/SKILL.md` 只差第 23 行那一处路径，其余三份本来就逐字节相同。`skills/workspace/skill-routing/scripts/check_routing.py` 在本机退出 1，报的三条都是本机 `~/.agents/skills` 里后装、本单未处置的 skill（`cangjie-skill` / `impeccable` / `reverse-engineer-anything`）——同一个脚本在 `D:/AI工作区` 上退出码与报错完全相同。分得清的判据是「输出里不出现 `missing-skill` / `unknown-skill` / `missing-owner` / `unknown-node` / N0x 缺号」：实测 0 条。

### 引擎同步到 schema 13（AWK-101）

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
