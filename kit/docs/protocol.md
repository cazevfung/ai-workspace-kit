# 本机工单协议 v0.1

## 调用约定

Python：`Center(absolute_db_path).execute(operation, payload, request_id=None)`。
CLI：`python -m task_center --db <绝对路径> <operation> --request-id <id> --input <UTF-8 JSON文件或->`。

所有业务写入必须使用 request_id。读取、init、migrate 和 backup 不要求它。重试必须保持 operation 和完整 payload 不变。重试返回的是**当时的提交回执**，不是当前状态；需要最新进度时调用 get_ticket。

### request_id 的命名空间：全库共用，撞了不自动改名

`requests.id` 是主键，**全库共用**——不按项目分区，也不按会话分区；两个会话各自取一个看起来唯一的编号，仍然可能撞在一起（2026-11-21 一天里就撞过两次：`AW-091`/`AW-093` 两个号被另一个协调会话先建走，`req-create-aw093` 这个编号在 10:52:33 同号不同参数报 `REQUEST_CONFLICT`）。

判据只有一条，按「operation + payload 的 sha256」这个指纹比：

- **同号同参数 = 幂等重放**：返回那次调用当时存下的回执，不再执行一次。
- **同号不同参数 = `REQUEST_CONFLICT`**：那一个编号已经属于另一次调用，报错且一行不写（连事件里的 `request_id` 都写 null，因为分不出是哪一次）。

写法建议 `<发起方>-<操作>-<序号>`，发起方用会话短号或工单号（例如 `b6cde03a-claim-2`、`aw097-bind-1`）。**撞了不自动改名**：改名会把「同一次调用」变成两次，两条都落库。这件事仍是调用方的责任——本工具只发工单号，不发 request_id，但它不再允许一个编号「看着像唯一」。

失败不会保存成功回执，不会残留半条工单、事件或执行记录——**一个例外**：`record_notice` 收到 `wake_kind=created` 时，那条 `failed` 回信与它的事件是**先提交再报错**（回执带 `alert: NOTIFY_CREATED_TASK`、命令非零退出）。这不是残留：宿主把它当新任务建了一条会话，这件事必须留在库里，回滚掉就查不出来「回执到底发到哪去了」。其余失败仍不留痕。冲突不是“已成功”，调用方应查询当前状态。时间为 UTC ISO 8601，内部对象标识由调用方或 UUID 产生，session_id 作为不透明字符串保存。回信那条例外的完整口径见「回信落库：一张单最多一条 `sent`，没过回执的单合不进去」。

结果形状：

```json
{"ok":true,"result":{"state":"awaiting_review"}}
```

```json
{"ok":false,"error":{"code":"STALE_ATTEMPT","message":"This is not the current execution attempt"}}
```

### 命令行 JSON 输入的写法

`--input` 默认是 `-`（标准输入），所以最常见的做法是把 JSON 用管道喂进去。两种 shell 在管道左边的写法不一样，抄错那一侧不会报语法错，只会安静地送进去一个空对象。

PowerShell（Windows PowerShell 5.1 与 pwsh 7 都一样——引号字符串本身就是一个值，可以直接当管道左端）：

```powershell
'{"project_id":"ai-workspace","ticket_id":"AW-010"}' | python -m task_center --db {{WORKSPACE}}/.data/task-center.sqlite3 get_ticket
```

bash（必须先 `echo`：bash 里 `'…'` 是**命令名**，不是字符串值）：

```bash
echo '{"project_id":"ai-workspace","ticket_id":"AW-010"}' | python -m task_center --db {{WORKSPACE}}/.data/task-center.sqlite3 get_ticket
```

把 PowerShell 的写法搬进 bash，会同时发生两件事：bash 报 `{"project_id":...}: command not found`（到 stderr），而 python 那一侧收到的是**空**标准输入。空输入被当成空对象，于是 `list_tickets` 这类读取操作照样返回 `ok: true`、退出码 0；只有需要字段的写操作才会当场报 `INVALID_INPUT`。所以「看着成功」不能作为写法正确的证据。

用文件喂 JSON 时，文件必须是 UTF-8：

```bash
python -m task_center --db <绝对路径> <operation> --request-id <id> --input payload.json
```

读文件按 `utf-8-sig` 解码，无 BOM 与带 UTF-8 BOM 都能读。Windows PowerShell 的 `>` 重定向默认写 **UTF-16LE**，`'{"…"}' > payload.json` 产出的文件开头是 `FF FE`，交给 `--input` 只会得到 `INVALID_INPUT`（解码失败与参数错误共用这个错误码，看不出是编码问题）。要落盘就显式指定编码，或者干脆别落盘：

```powershell
'{"project_id":"ai-workspace"}' | Out-File -Encoding utf8 payload.json
```

**路指错了不是参数错。** `--input` 指向的文件不存在、是个目录、或没权限读时，命令行层报 `INPUT_UNREADABLE`，`message` 里带**原样的那条路**与系统原因（例如 `读不到输入文件：/tmp/payload.json（No such file or directory）`）；这比「参数不合格」能多告诉你一件事：要改的是路径，不是 JSON 内容。编码、语法、未知字段这三类仍是 `INVALID_INPUT`。核心已经返回的 `error` 由命令行**原句**打印，不再套一层通用文案——冒烟时看到的 `500`、「at most 4000 characters」这类具体数字，就是核心原话。

## 操作与字段

下表中的字段除明确标注外均为必填。未知字段拒绝，避免拼错后静默忽略。

> **本表是 schema 16 的命令面**（`task_center/db.py` 的 `SCHEMA_VERSION`）。它只讲操作与字段，不搬 PRD 正文：判据为什么这样定、每个错误码背后的现场，在 `PRD-005` / `PRD-013` / `PRD-014` / `PRD-015` 里（那几份还没进套件，见 `README.md` 的「已知差异」）。表与代码不一致时以代码为准：`task_center/core.py` 的 `FIELDS` 是字段闭集，`get_ticket` 实际回的字段就是回执形状。

| 操作 | 输入字段 | 结果与边界 |
| --- | --- | --- |
| init | 空对象 | 显式建库并写入当前 schema 版本（16）；已有同版本数据库不重复建表，旧版本和其他库拒绝 |
| migrate | 空对象 | 只做已知升级链（3 → 4：attempts 加 coordinator_session_id；4 → 5：新建 merges 表；5 → 6：新建 questions / answers 两张表，并按「blocked 也算活跃尝试」重建 active_session 索引；6 → 7：attempts 加 coordinator_workdir 列、新建 notices 表与一张单一条 sent 的部分唯一索引；7 → 8：notices 加 target_session_id 与 wake_kind 两列，存量行留空串表示「历史未记」；8 → 9：submissions 加 head_sha、merges 加 reviewed_sha，存量行留 NULL 表示「那时没有这一项」——老提交没有可核对的 sha，补一个猜测值就把推定写成了事实；9 → 10：tickets 与 attempts 各加 criteria_version（存量行填 1，不填 0——它们派发时确实只有一版判据）、submissions 加 base_sha（老提交留 NULL，表示那时没有记录过基座）；10 → 11：`tickets.prd_sha256` 放宽成可空（SQLite 只能重建 tickets 表，逐列抄写、不补任何值）、`projects` 加发号盘 `next_number`（按该单已有最大号初始化，`AW-096` → 下一次 `AW-097`）、新建 `ticket_links` 与它的反向索引；11 → 12：新建 `batches` 表（`id` / `project_id` / `resource_key` / `owner_session_id` / `planned_at` / `closed_at`，**没有单独的状态列**——`closed_at` 空即仍占用）、`tickets` 加可空 `batch_id`、`attempts` 加 `batch_key`，并把 `active_session` 重建为只守无车次的单（`WHERE batch_key IS NULL AND …`）；存量单的 `batch_id` / `batch_key` 一律留 NULL，不回溯、不补值，所以升级后的行为与升级前逐字一致）；12 → 13：`submissions` 加可空 `conflict_preview`（JSON 文本），存量提交留 NULL 表示「那时没有这一项」——补成 `{"conflicts":[]}` 等于声称那些历史提交都预演过且干净，那是一句编出来的事实；`NULL` 与空数组在这一列不是同一件事：前者是「没跑过／算不出来」，后者是「跑过且没撞」（PRD-015 R2）；13 → 14：`tickets` 加可空 `requester_session_id`（谁开的这张单）、`notices` 加可空 `message_id`（哪条消息带的回执），存量行留 NULL——两列都不许拿协调会话或时间戳凑一个（PRD-015 R5；AW-106 先落地，正式库现在就是这一版）；14 → 15：`tickets` 加可空 `owner_session_id`（现在谁负责收尾），存量单按它**当前尝试**的 `coordinator_session_id` 回填——那是当时的责任人，不是「现在还活着的那条会话」；没有当前尝试的单留 NULL，给它们编一个 owner 等于指名一条从没派发过这张单的会话）；15 → 16：`merges` 加 `source` / `verified_by` / `verified_at` / `note` 四列（这条记录是谁写的、拿哪条命令核对过、什么时候核的、补记的原因），**存量合并行四列一律留 NULL** 表示「那时没有这一项」——批量填 `auto_merge` 会把「后来补的」与「当时记的」永久混在一起（AW-116；判据原文写的是 14 → 15，但 15 已被 AW-114 占，所以顺延一级）。**补完一半的迁移**：四列都在、只有版本号还停在 15 时不报形状错——按「缺哪列加哪列」把版本补到 16，已写下的值原样保留（2026-11-22 正式库真实遇到：另一个会话为了让 master 上的旧代码还能读库，把版本号单独改回 15）；保留全部工单、尝试、提交、验收、事件与既有回信记录；已是 16 返回「已是最新」且不改任何行；其他版本号或不是本工具的库报 SCHEMA_MISMATCH 且不改文件；旧版本头配新形状（例如 user_version=5 却已有 questions 表、user_version=6 却已有 notices 表、user_version=7 却已有 target_session_id 或 wake_kind、user_version=8 却已有 head_sha 或 reviewed_sha、user_version=9 却已有 criteria_version 或 base_sha、user_version=10 却已有 ticket_links 或 projects.next_number 或可空的 prd_sha256、user_version=11 却已有 batches 表或 attempts.batch_key 或 tickets.batch_id、user_version=12 却已有 submissions.conflict_preview、user_version=13 却已有 tickets.requester_session_id 或 notices.message_id 或 tickets.owner_session_id、user_version=14 却已有 tickets.owner_session_id、user_version=15 却只有 merges.source / verified_by / verified_at / note 里的部分列）同样拒绝；不自动建库。升级前的快照只能在文件层做（backup 操作对旧库会报错），顺序见「升级旧库的顺序」与「改 schema 的落地顺序」 |
| register_project | project_id, name, root | root 是已存在的绝对项目目录；root 是身份、project_id 是标签，同一路径重复登记复用原记录（回执返回原 project_id 与 name，`reused: true`），不报 CONFLICT、也不改已有标签；把自己的 project_id 指向第二个目录报 CONFLICT |
| list_projects | 空对象 | 只读，列出 project_id / name / root；没有它时项目清单只能读库或靠工单反推 |
| unregister_project | project_id | 撤销错误登记；项目下仍有工单时拒绝（PROJECT_HAS_TICKETS），回执里列出这些工单；撤销后同一路径可重新登记 |
| rename_project | project_id, name, changed_by 可选 | 只改显示名：`projects.name` 换成新名字，`id` / `root` / `next_number` 与工单归属一字不动；回执带 `old_name` 与 `name`（另带 `root` 与自报的 `changed_by`，它可以不给）。名字 1–200 字：空名（含只有空白）与超长报 INVALID_INPUT，project_id 不存在报 NOT_FOUND，被拒时一行不写（只留一条 rejected 事件）。**`name` 是显示名、`project_id` 是身份、改路径（`root`）只能 `unregister_project` + `register_project`**——有工单的项目撤不掉（PROJECT_HAS_TICKETS），所以名字给错了以前只能一直错下去，这条操作就是为那件事存在的；它不重建项目、不动 root、也不碰任何工单。每次改名落一条 `rename_project` 事件，detail 里是这次回执（旧名、新名、`changed_by`），所以「谁把哪个名字改成了什么」事后查得到 |
| create_ticket | project_id, title, deliverable, prd_path, criteria, root 可选, state 可选, blocked_by / related / rides_with 可选, requester_session_id 可选 | **工单号由库里发**：payload 里出现 `ticket_id` 一律报 `INVALID_INPUT` 并写明「号由库里发」，回执里的 `ticket_id` 才是真号（PRD-014 R5）。号是 `<前缀>-<序号>`，前缀取项目 id 的短名（`ai-workspace` → `AW`、`p1` → `P1`），每个项目一条独立序列、在同一个事务里取号并加一。`state` 只收 `planned` 与 `ready`（缺省 `ready`，与以前行为一致）；`planned` 是「还没开工、在库里排着」，不产生尝试、不占 `active_session`、也不接受 `bind_session`。**create 只记 `prd_path`、不记指纹**：`tickets.prd_sha256` 在排队期间是 NULL，依据在 `claim_ticket` 那一刻才定格（PRD-014 R2）。`blocked_by` / `related` / `rides_with` 是三类依赖边（各 0–50 个工单号，出现即按这份覆盖，不出现不动），写边时查一次可达性，成环报 `DEPENDENCY_CYCLE` 并点名环上的单。项目内 PRD 相对路径要存在；创建 revision=1 工单。`criteria` 每一条都要过形状闸门（见下「`criteria` 只收两种写法」），有一条不合格就报 `CRITERIA_NOT_JUDGEABLE` 并点名它是第几条，这张单一行不写；`state=planned` 时 `criteria` 可以是空数组（念头的位置），其余状态至少要一条。root 可选：project_id 未登记时传了就顺手记一行（name 取目录名）再开单，不传报 NOT_FOUND 且 message 写明可以传 root；project_id 已登记而 root 与库里的不一致报 ROOT_MISMATCH，原记录不动 |
| get_ticket | project_id, ticket_id | 返回工单（含 `owner_session_id`：现在谁负责收尾）、所有执行尝试（含 coordinator_session_id：谁派的这条尝试，与 coordinator_workdir）、提交和验收历史，以及合并记录数组 merges、问答历史数组 questions 与 answers、回信记录数组 notices（没有记录时均为空数组），还有依赖边 `links`：`out` 是「它卡在谁身上」、`in` 是「谁在等它」，每边 `{kind, ticket_id, state}`，state 是对方当前的状态；回信结果与协调者工作目录在同一份回执里，验收据此判交接闭环有没有走完。每条 `reviews[*]` 带 `checks`（`{id,status,evidence}`，status 取 `pass`/`unverified`/`fail`，`pass`/`fail` 另带一个 `passed` 布尔镜像）、`checks_summary`（`{total,pass,unverified,fail}`）与一行 `summary`；工单级再带 `review_summary`（最后一条验收的那行摘要，没有验收时为 `null`）与 `unverified_checks`（同一口径的未验条数）——「带未验项的 accept」与「全部已验证」在这两个字段上字面就不同，不必读 `reason` 才分得出（AW-132） |

| list_tickets | project_id 可选, limit 可选, offset 可选, planned 可选 | 默认 50 条，最多 100；按项目过滤。`planned: true`（CLI 糖 `--planned`，等价写法 payload 里写 `{"--planned": true}`）是**池子视图**：只列 `planned` 单，按依赖层数升序（无前置为第 0 层，同层按 `created_at`），`columns` 在原来那八列之后多出 `claimable`、`blocked_by` 与 `criteria_n` 三列，其余视图的表头与以前逐字一致（PRD-014 R6）。`blocked_by` 是**未 `done`** 的前置单号数组（无前置为空数组）；`criteria_n` 是条件条数（`planned` 可为 0，0 就是「念头」，它领不走）；`claimable` = 状态在 planned/ready/rework 之内 **且** 所有 `blocked_by` 已 `done`。**它不代表「现在就能领」**：同项目已有在途单时 `claim_ticket` 仍报 `DISPATCH_BUSY`，那是另一件事，不在这一列里混着答。回执是 `{"columns": […], "items": […], "limit", "offset"}`：`columns` 是这一份输出的表头（`id` / `project_id` / `title` / `state` / `revision` / `criteria_v` / `behind` / `created_at`），表头与每一条的字段名一致，一张单也没有时表头照旧给出。`criteria_v` 是工单当前的判据版本（与 `get_ticket` 的 `criteria_version` 同一个值）；`behind` 是 `git rev-list --count <最新提交的 head_sha>..master`，**算不出来时是字符串 `?` 而不是 `0`**——`head_sha` 为 NULL 的历史提交、分支已被删、目录不是仓库这三种情况都算不了，写 0 就会和「基于最新 master」分不开（PRD-013 R7）。 |
| claim_ticket | project_id, ticket_id, expected_revision, coordinator_session_id, coordinator_workdir 可选, allow_parallel 可选, parallel_reason 可选, parallel_scope 可选 | 原子领取 `planned` / `ready` / `rework` 工单，生成 attempt_id，状态 dispatching。**PRD 指纹在领取这一刻取**：`claim_ticket` 自己读文件算 sha256，写进 attempts 的两列并回写 `tickets.prd_sha256`——排队期间 PRD 升版因此不再让单过期，claim 之后再变才是 `STALE_PRD`。**`blocked_by` 里有未 `done` 的前置时报 `BLOCKED_BY_DEPENDENCY`**，message 点名是哪张单、它当前在什么状态（`cancelled` 也算未满足），一行不写；这道校验排在 `DISPATCH_BUSY` 之前，因为它回答的是「这张单能不能开工」。**条件一条都没有时报 `CRITERIA_NOT_JUDGEABLE`**（`planned` 允许空着，但空着就只是念头），message 指回 `revise_ticket`；它与上面那道依赖校验同类，同样排在 `DISPATCH_BUSY` 之前。coordinator_session_id 是派发它的协调会话，1–500 字符，同一句里就被写成这张单的 `owner_session_id`（当时两者相等，之后 owner 可被 `take_over_ticket` 换掉）；coordinator_workdir 是协调者自己的工作目录（1–1000 字符），不传就是空，不阻止派发；**同项目已有在途工单时报 DISPATCH_BUSY，消息里点名那张单号与它停在哪一步**（在途 = dispatching / running / awaiting_review / blocked，见「同项目一次只跑一张单」；`planned` 不在其中，池子里排着的单不占这个名额）；`allow_parallel` 只收布尔且默认 false，显式传 true 才越过，越过往 events 写一条 `allow_parallel_override` 并在回执里回显 `parallel_with`。**越过必须同时给 `parallel_reason`**（1–200 字符，写清为什么这两张单可以同时跑）：缺了报 `PARALLEL_REASON_REQUIRED`，工单停在原状态、不产生尝试；可选 `parallel_scope` 是 1–20 条字符串，每条是一个项目内相对路径、目录，或一段以 `资源:` 开头的资源标识，形状不对报 INVALID_INPUT。这两项与 `parallel_with` 并列写进同一条事件，回执里也原样回显；`allow_parallel` 为 false 时带上它们报 INVALID_INPUT——不留一条没人读的声明。**带车次的单在这里多看一处**（见「车次：几时几张单算一趟活」）：车次的 `owner_session_id` 为空就把本次 `coordinator_session_id` 写进去，等于本会话就放行，是别的会话报 `BATCH_OWNED_BY_OTHER` 并点名车次号与持有会话（一行不写）；`closed_at` 非空的车次报 `INVALID_STATE`，不再接受新单；`tickets.batch_id` 指向别的项目的车次是脏行，入口就报 `BATCH_PROJECT_MISMATCH`。**车次这一段的三种拒绝都排在 `DISPATCH_BUSY` 之前**：同一现场两个条件都满足时报车次，不报「项目忙」（判序依据见「车次：几时几张单算一趟活」）。回执额外带 `batch_id` 与 `batch_owner_session_id` |
| bind_session | project_id, ticket_id, attempt_id, session_id, project_root | 将预留尝试绑定到会话标识；状态 running；session_id 与 claim 时的 coordinator_session_id 相同则报 SELF_DISPATCH，工单停在 dispatching；回执含 session_id 与 coordinator_session_id；不代表已验证宿主身份 |
| submit_result | project_id, ticket_id, attempt_id, session_id, summary, artifacts, evidence, risks, head_sha, base_sha | 保存 submission_id、成果摘要、自检证据、这次提交的分支 head sha 与它分叉自哪个 commit；状态 awaiting_review。`head_sha` 与 `base_sha` 都必填，各收 40 位十六进制（大小写都收，存小写），缺字段、长度不对或含非十六进制字符报 INVALID_INPUT，被拒时一行不写。`artifacts` / `evidence` / `risks` 三个都是**字符串数组**：`artifacts` 1–20 条、`evidence` 1–100 条、`risks` 0–100 条，每条非空且不超过 4000 字符；传对象数组、传一整段 JSON 字符串、传单个字符串都报 INVALID_INPUT。**首次提交（新仓库只有一次初始提交、没有 `master` 分支）时 `git merge-base HEAD master` 跑不出来，`base_sha` 就填 `git rev-parse HEAD` 的输出（与 `head_sha` 相同），但这一格仍然必填**。形状错时回执只有一条 INVALID_INPUT，`message` 里同时点名五个字段的期望形状与这一次哪几项不合格；三个数组的形状表、最小 payload 与首次提交的取法见下文「`submit_result` 的形状：三个字符串数组与首次提交的 `base_sha`」。`head_sha` 记的是「被验收的那棵树」，合并时 `merge_ticket` 拿它与最新提交比；`base_sha` 是在提交处的工作树里跑 `git merge-base HEAD master` 的输出——**不是「填表那一刻 master 的 sha」**，rebase 之后两者语义不同（PRD-013 R7 / D5）。回执带 `criteria_changed`：这次尝试开始时的 `criteria_version` 低于工单当前值时是 true，说明判据在开工后被改过，验收得按新条件重看一遍。它不回写任何行。**回执与 `get_ticket` 的 `submissions[-1]` 都带 `conflict_preview`**（PRD-015 R2）：提交时对未来的目标分支跑一次 `git merge-tree --write-tree --name-only <head_sha> <目标分支 tip>`，形状是 `{target_branch, target_sha, command, exit_code, conflicts}`，`conflicts` 是按字母序去重的路径数组。无冲突时 `conflicts` 是空数组、`exit_code` 是 0，字段照常出现——不是省略、不是 `null`，验收会话据此分得清「跑过且干净」与「没跑」；有冲突时 `exit_code` 是 1 且回执 `ok` 仍是 true，预演只报告不处置：不拒收提交、不回滚、不自动解冲突、不改分支；算不出来（目录不是仓库、`head_sha` 不在本地、git 不可用）时 `conflict_preview` 是 `null`。落库为 `submissions.conflict_preview`（JSON 文本，可空），`list_tickets` 不加这一列 |
| review_result | project_id, ticket_id, attempt_id, submission_id, reviewer_session_id, verdict, checks, reason | 只接受这八个顶层字段，多传即拒（见「验收 payload 是一张闭集」）；accept→done；reject→rework。验收者不能是该工单任一历史执行会话。`checks` 每条收两种写法：新的 `{id,status,evidence}`（status 取 `pass` / `unverified` / `fail`），或旧的 `{id,passed,evidence}`（归一成 pass / fail）；两个都给时不许自相矛盾，`unverified` 不许带 `passed`。判定规则跟着三档走：**accept 不许有 `fail`（`unverified` 不阻断）**，**reject 至少要有 `fail`**（未验不是失败，不能拿它当打回的理由）。回执带归一后的 `checks`、`checks_summary`（`{total,pass,unverified,fail}`）与一行 `summary` |
| recover_ticket | project_id, ticket_id, attempt_id, expected_revision, reason | 只恢复 dispatching/running/awaiting_review/blocked；attempt_id 必须是当前尝试，旧编号返回 STALE_ATTEMPT；回执含 superseded_attempt_id、discarded_submission_id 与 voided_question_ids；旧尝试失效，它的开放问题转 voided，revision+1，回到 ready |
| revise_ticket | project_id, ticket_id, expected_revision, reason, prd_path, criteria, deliverable 可选, reopen 可选, restart 可选, blocked_by / related / rides_with 可选 | 明确更新需求摘要和验收项。**默认不作废在飞的尝试**：`criteria` 确有变化时 `criteria_version` 加 1（回执回显 `from` / `to`），`revision`、`current_attempt`、`attempts.state` 一个都不动——判据变了不等于这一代作废（PRD-013 R6）。改判据时若有尝试在飞（`dispatching` / `running` / `awaiting_review` / `blocked`），回执带 `must_wake`（`session_id` + `attempt_id` + `criteria_version.from` / `.to`），并写一条 `criteria_revised_in_flight` 事件；带 `must_wake` 只说明「要叫谁、为什么叫」，发送仍由协调会话走 `send_to_session`。要作废就显式传 `restart: true`（只收布尔，缺省 false）：那是三个作废入口之一，旧尝试 `superseded`、开放问题转 `voided`、`revision` 加 1、回到 `ready`，回执没有 `must_wake`。`criteria` 与 create_ticket 同一道形状闸门，**按落点状态判**：`restart` 与「对已完成单 `reopen`」都把单送回 `ready`，所以这两条路不接受空列表（`planned` 的空条件只在不换状态时成立）；不合格报 `CRITERIA_NOT_JUDGEABLE` 且一行不写；deliverable 不传则保留原值；对已完成工单必须传 reopen=true（重开也换代次，`revision` 加 1，回到 ready）；传了依赖边就按那一类覆盖式改写（先清后写，成环报 `DEPENDENCY_CYCLE` 且整个事务回滚）；`planned` 单改路径仍留 `prd_sha256 = NULL`（排队期间不取指纹）；回执含 previous_state、criteria_version、must_wake、prd_path、prd_sha256 与 deliverable；已取消工单不能重开 |
| cancel_ticket | project_id, ticket_id, expected_revision, reason | 取消未终结工单（含 `planned`：池子里排着的单可以直接撤，它本来就没有尝试），旧尝试失效，revision+1；不停止真实会话 |
| archive_ticket | project_id, ticket_id, expected_revision, reason | 把一个已验收（`done`）工单划进第十个终态 `archived`：只从 `done` 进得来，`reason` 必填（缺了报 `INVALID_INPUT`），回执含 ticket_id、state、previous_state、revision、current_attempt 与 reason。**`revision` 不变**——归档不是新的一代，它没作废任何尝试、也没换人，所以 `reviews` / `submissions` / `current_attempt` 一概不动，历史行一条不删。判序是**先状态、后 revision**：同一张单第二次归档报 `INVALID_STATE`（不是 `STALE_REVISION`——那会让人以为重读一次就能成），回执的正文里带着当时的 `revision`，两次相同；`running` / `ready` / `planned` / `cancelled` 的单同样 `INVALID_STATE`，状态一行不动。进去之后：不进 `list_unmerged`、不被 `wait_audit` 的 done 两类命中、不算在途（`ACTIVE_TICKET_STATES` 仍是四态），也不在 `cancel_ticket` 的可取消范围里（终态不可再终止）；作为 `blocked_by` 前置**算满足**（它只能从 `done` 进来，归档承认的是「做过、只是账对不上」，与 `cancelled` 不是一回事）。用 `tools/archive_legacy.py --dry-run / --apply` 一次清存量，别手工改库（AW-128） |
| merge_ticket | project_id, ticket_id, commit, target_branch, tests, reviewed_sha | 给已验收（done）工单写一条合并记录：合并提交、目标分支、合并前跑过的测试结论、被验收的那棵树；不是 done 报 INVALID_STATE；该单 `notices` 为空报 NO_NOTICE（要么补记一条 `sent`、要么记一条 `failed` 并写清原因，`failed` 也算走过回执）；已有合并记录报 ALREADY_MERGED（这一条先判，行为不变）。`reviewed_sha` 必填：与该工单最新一条提交的 `head_sha` 相等就放行并写入该值，不等报 `SHA_MISMATCH` 且一行不写；最新提交的 `head_sha` 为 NULL（schema 8 以前的老提交）时必须传字面量 `unverified`，否则报 INVALID_INPUT，传了就把 `unverified` 原样入库；有 `head_sha` 的单传 `unverified` 同样报 INVALID_INPUT。回执含 ticket_id、commit、target_branch、tests、reviewed_sha、source、verified_by、verified_at、note 与 merge_id。**出处三个取值**（AW-116 / AW-127）：不传 `source` 时为 `auto_merge`（工具跑的正常合并路径：预演、临时工作树测试、真实合并、合并后测试都跑过），`verified_by` 与 `note` 留 NULL，传 `note` 报 INVALID_INPUT；传 `source=manual` 表示人手工 `git merge --no-ff` 合入（例如 `auto_merge` 在该项目上跑不动），**必须**同时传 `note` 写清闸门哪几步跑了、哪几步没跑；传 `source=backfill` 表示记录是事后补的，**必须**写 `note` 说明补记时间与原因。`manual` 与 `backfill` 都要求有可核对的树（`reviewed_sha` 不能是 `unverified`），都会**当场重跑** `git merge-base --is-ancestor <reviewed_sha> <target_branch>`，退出 0 才写行，否则报 `NOT_LANDED` 并点名工单号，一行不写；真跑过的命令由工具写进 `verified_by`，调用方不能自报（传 `verified_by` 报 INVALID_INPUT）。`source` 只收这三个值 |
| repair_backfill_command | project_id, ticket_id | 只改**补记行**（`source=backfill`）`verified_by` 的**路径写法**（反斜杠 → 正斜杠），命令本身一个字不动：把存量值与工具现在的规范写法都按正斜杠归一后比对，相等才更新，不等报 `COMMAND_MISMATCH` 并把原值留给人读；别的 `source`（`auto_merge` / `manual`）报 `INVALID_STATE`（`manual` 行由工具当场写下命令，本来就已是规范写法，没有可修的），还没有合并记录报 `NOT_FOUND`；已经是规范值返回 `changed=false`（幂等，AW-116） |
| list_unmerged | project_id 可选, since_days 可选 | 只读，列出 state=done 但没有合并记录的工单（id / project_id / title / state / revision / created_at，带 has_merge=false）；历史工单直接在 master 上做、没有 merges 行，会照样列出——清单让人看见并判断，不替人隐去。`since_days` 是 1–365 的整数，给了就只列 `created_at` 在这么多天以内的单；**不传时 SQL 与行为与以前逐字一致**，全量清单不变（历史单的密度是这张单的起因，收窄窗口不能变成替人隐去历史）。本操作仍只返回原料；要分「真待合并 / 历史豁免」用 `tools/auto_merge.py --list`，它读 `docs/unmerged-exemptions.json` 分栏（见下文「历史豁免清单」） |
| events | project_id, ticket_id, limit 可选, offset 可选 | 按顺序返回状态事件和原请求回执关联；被拒绝的写操作也各留一行（见「被拒绝的写操作留一行事件」） |
| backup | destination | 在现有目录内创建新的绝对路径快照，不覆盖文件，不记录业务状态变化；只接受当前 schema（16）的库，旧库返回 SCHEMA_MISMATCH 且不落任何文件 |
| ask_ticket | project_id, ticket_id, attempt_id, session_id, question, options 可选 | 就当前尝试提一个可回答的问题，工单进「在等答案」：question 1–2000 字符；options 为 0–6 项 `{"id","text"}`（id ≤20 且不重复、text ≤200），不传或空数组表示开放题；工单与尝试同时进 blocked，revision 不变；回执含 question_id、state、asked_at。工单不在 running 报 INVALID_STATE，attempt_id 不是当前尝试报 STALE_ATTEMPT，session_id 不是该尝试绑定的那个报 SESSION_MISMATCH；一条尝试同时只能有一条开放问题 |
| answer_ticket | project_id, ticket_id, question_id, answer, answered_by, answered_by_session_id 条件必填, prd_sha256 | 把人的结论写进库：answer 1–4000 字符；answered_by 只能为 human / coordinator，coordinator 必须带 answered_by_session_id、human 不许带；**代答的授权读当前 owner**（`tickets.owner_session_id`），与 `record_notice` 同一把尺：`answered_by_session_id` 不等于当前 owner 报 `NOT_COORDINATOR` 且一行不写，是本条尝试的执行会话报 `SELF_ANSWER`（AW-122 之前这里读的是 origin，于是接替之后回执能补、问题却谁都答不了）；prd_sha256 原样抄 get_ticket 的当前值；问题 open → answered、尝试与工单回 running，revision 不变；回执含 answer_id、question_state、ticket_state。工单回到 running 只表示库里不再等答案，不代表那个会话已经在跑（见「`blocked`：工单在等一句话」） |
| review_dispatched | project_id, ticket_id, submission_id, reviewer_session_id 可选, note 可选 | 记下「验收已经派出去了」，**只落一条同名事件，别的一行不动**（AW-133）。`state` 仍是 `awaiting_review`、`revision` 不变、`reviews` 表一行不写——它不是验收结论，是「有人在看」这条事实，所以它有窗口期（`task_center.core.REVIEW_IN_FLIGHT_SECONDS`，30 分钟）。判序按「这张单现在能不能被派验收」排队：状态不是 `awaiting_review` 报 `INVALID_STATE`，`submission_id` 不是最新那条提交报 `STALE_SUBMISSION`（返工后的新提交还没人看过，拿旧提交去标会把新提交压住），那份提交已有 `reviews` 行报 `ALREADY_REVIEWED`（同一棵树不判第二次），四种都一行不写、只留一条带 `rejected` 的拒绝事件。`reviewer_session_id` 与 `note` 可给可不给，给了也只是一条自报的指向，不是身份。回执带 ticket_id、state、revision、submission_id、reviewer_session_id、note 与 `in_flight_until`（派出去那一刻 + 30 分钟）。**消费方只有一个：`tools/next_action.py`**，它靠这条事件不再对同一张单报 `dispatch_review`，超过窗口自动回落 |
| record_notice | project_id, ticket_id, attempt_id, session_id, origin_session_id, status, detail, target_session_id, wake_kind, message_id 可选 | 记一次回信尝试（本操作只落库，不发消息）：status 只收 `sent` / `failed`，其他值报 INVALID_INPUT；session_id 必须等于这张单**当前的 owner**（`tickets.owner_session_id`，claim 时由 `coordinator_session_id` 写下、之后可被 `take_over_ticket` 换掉），否则 NOT_COORDINATOR 且不写行（执行会话自己那次没送到的回执不在这里写，走 `report_notice_failure`）；target_session_id 必填且也必须等于当前 owner，写成执行会话、别的会话或已经归档的原协调会话都报 NOT_TARGET 且不写行；wake_kind 是宿主回执里的原值，不按白名单收：只有 `created`（宿主把它当成新建任务而非投递）**特判**——一律落 `failed`、回执带 `alert: NOTIFY_CREATED_TASK` 并让这次调用**非零退出**（那一行仍然入库，事后查得到）；`queued` / `resumed` 与枚举外的取值（例如 `already-active`）一律原样入库，调用方不得自行映射成近似的三值之一（见「回信落库：一张单最多一条 `sent`，没过回执的单合不进去」）；只校验形状——空串、纯空白、超过 40 字、含内部空白或换行报 INVALID_INPUT 且不写行；同一张单最多一条 `sent`，第二次报 ALREADY_NOTIFIED 且不写行；`failed` 可以多条，措辞在 detail 里，回执带 `alert: NOTIFY_FAILED` 但不改工单状态；回执含 notice_id、ticket_id、attempt_id、origin_session_id、status、detail、target_session_id、wake_kind、ticket_state 与 revision |
| take_over_ticket | project_id, ticket_id, new_owner_session_id, reason | 责任人接替：把 `tickets.owner_session_id` 换成一条新会话，**不动 `revision`**，也不改 `attempts.coordinator_session_id`（origin 是谁派的，历史事实）。四个字段就是全部，不要求 `attempt_id` 或 `expected_revision`：接替不是新的一代。`new_owner_session_id` 已经是当前 owner 报 ALREADY_OWNER；是这条尝试的执行会话报 TAKEOVER_BY_EXECUTOR（那等于自派自做）。工单已取消报 INVALID_STATE；字段缺失、空串或多余字段报 INVALID_INPUT，一行不写。成功事件由 `execute()` 照常写一行 `operation=take_over_ticket`，`detail.result` 里带旧/新 owner、origin 与理由；回执含 ticket_id、previous_owner_session_id、owner_session_id、origin_session_id、reason、state、revision。接替之后 `record_notice`、`answer_ticket` 与 `merge_ticket` 的授权读新 owner（见「协调会话归档之后：owner 与 origin 不是一个字段」）。**日常入口是 `python tools/handover_owner.py --ticket <单号> --to <会话 id> --reason <一句>`**：它先只读核「当前 owner 已不在、目标会话活着、这张单还有收尾动作可做、目标不是执行会话」，四条都过才调本操作；本操作自己仍是四条字段、仍只做声明，不认证 |
| record_pool_report | project_id, ticket_id, session_id 可选, note 可选 | 记下「这张空判据单被点名报过一次」（AW-140）：**只落一条同名事件，别的一行不动**。`state` / `revision` / `criteria_version` / `criteria` 全不变——报过不是新的一代，更不是补了判据。判据只有三条：工单存在、状态可领（`planned` / `ready` / `rework`）、条件确实为空；条件不为空报 `INVALID_STATE` 且一行不写——把有判据的单记成「空判据报过」，等于让读侧替一张真活记账。回执带 `ticket_id`、`state`、`revision`、`session_id`、`note`、`reported_rounds`（记完之后是第几轮，按这张单的同名事件行数算）与 `reported_at`。**消费方只有一个：`tools/next_action.py`**，它按轮次决定「照原样点名 / 按窗口退避 / 报到第 N 轮升级」；阈值、台账与写法见 `docs/无人值守派单.md` §3.2 |
| report_notice_failure | project_id, ticket_id, attempt_id, session_id, target_session_id, detail, wake_kind 可选 | 执行会话记**自己那次没送到的回执**（本操作只落库，不发消息）。它没有 `status` 字段，因此造不出 `sent`——`one_sent_notice` 那条索引在结构上碰不到，`sent` 只有当前的 owner 能宣告。`session_id` 必须是这条尝试绑定的执行会话，否则 SESSION_MISMATCH；`target_session_id` 必须是该尝试的 `coordinator_session_id`（origin：这条回执本来要发给谁），否则 NOT_TARGET；`detail` 写宿主给的原话或错误码（例如「send_to_session 返回 ARCHIVED」）与「送到哪儿失败了」。落库形状与 `record_notice` 的 `failed` 完全一致：`origin_session_id` 自动写执行会话自己，`status` 固定 `failed`，回执带 `alert: NOTIFY_FAILED`、`reported_by_session_id`、notice_id、ticket_state 与 revision。`wake_kind` 这次没有宿主投递回执可读，不给值时落 `unknown`。记完之后 `merge_ticket` 的 `NO_NOTICE` 就过得去（`failed` 也算走过回执），而「回执没送到、原因是什么」在 `notices` 里查得到，记录人写在同一次调用的成功事件里 |


`criteria` 是 1–100 项 `{"id":"C1","text":"明确验收条件"}`，id 不能重复。**唯一例外是 `planned`：它允许 0 项**（AW-109 / PRD-013 R1）——那是「已经知道要补什么、还写不出验收条件」的发现能落库的位置。`ready` / `rework` / 在飞的单必须至少 1 项，`claim_ticket` 任何状态都要求至少 1 项；一条都没有时报 `CRITERIA_NOT_JUDGEABLE`（不是 `INVALID_INPUT`——它不是字段写坏了，是这张单还不可以被验收）。**这条下限按「这次调用之后单子落在哪个状态」算，不是按改之前的状态算**：`revise_ticket` 带 `restart`、或对已完成单带 `reopen`，落点都是 `ready`，所以一张条件空着的 `planned` 单不能靠这条路绕过下限——那正是「`ready` 必须至少 1 项」这句话唯一的漏口（AW-109 返工实测过：按改之前的状态判，会落出一行 `state='ready'` 且 `criteria='[]'`，它谁都领不走，`work_gate --list` 还会说「在等人领走」）。

**`criteria` 只收两种写法**（PRD-013 R1，`create_ticket` 与 `revise_ticket` 同一道闸门）：

1. 文本里含至少一处反引号包裹的命令，且该命令的动词在 `python` / `git` / `grep` / `ls` / `find` / `wc` / `node` / `npm` / `bash` 名单内，例如 `` 含 `python tools/run_tests.py` 且退出码 0 ``；
2. 文本以 `判断类` 开头，且同一段文本里出现「反例」二字，例如 `判断类：两份清单口径是否一致；反例：同一数字两处写成不同值`。

两种都不满足报 `CRITERIA_NOT_JUDGEABLE`，回执点名是第几条并给出改法，一行都不写（这张单不建、`requests` 不落回执）。结构错误排在形状之前：缺字段、空文本、id 重复、超过 100 项仍然报 `INVALID_INPUT`，不会把「字段写坏了」报成「条件写不成样子」。它不判「这条条件合理吗」——那件事机器判不了；闸门只判「可执行」或「你已经明说这是判断类」。

**空条件的单领不走，也派不出去。** `planned` 允许 0 项，但 `claim_ticket` 读到空列表就报 `CRITERIA_NOT_JUDGEABLE` 并指回 `revise_ticket`；`tools/next_action.py` 也不把它算成可领（进 `skipped`，`reason: no_criteria`）；建单超过 `BLANK_CRITERIA_GRACE_SECONDS`（24 小时）还没补上的，另升成一档 `fix_pool` 点名，人读那一行给出两条出路（`revise_ticket` / `cancel_ticket`），`next_action` 自己不写判据也不取消单（`docs/无人值守派单.md` §3.1）；报过之后不是每轮重报（AW-140）：`record_pool_report` 一条事件 = 一次点名，读侧按轮次退避（2h→6h→24h）、报到第 `POOL_REPORT_ESCALATE_AFTER` 轮改喊升级，阈值与台账见 `docs/无人值守派单.md` §3.2）。所以放宽的只是「能不能先把它记下来」，不是「能不能开工」（`pdsa-workflow` 的推进条件本来就写着「验收条件非空」）。池子里那张单在 `work_gate.py --list` 里逐张点名，条件为 0 时标「条件空着」。

**历史不回溯。** 闸门只判调用当下传进来的 `criteria`，库里已有的条件行不重判（PRD-013 D1）。所以着一道闸门上线时，旧单照旧可读可查，只有新开的单与新的 `revise_ticket` 受约束。

`deliverable` 是必填的交付物描述：它要说什么东西、什么形状、给谁看。它和 `criteria` 不能互相替代——`criteria` 管「做得对不对」，`deliverable` 管「做的是不是这件事」。验收条件只约束质量、不约束主题时，成果会合规地跑偏（隔离试点 TEST-003 实际发生过）。

`unregister_project` 的保护条件不是礼貌性的：有工单的项目一旦被撤掉，工单就指向一个不存在的项目，事件与验收记录随之失据。要撤先处理工单（`cancel_ticket` 之后仍需人工清理），不要为了「收拾登记」删工单。

**项目登记不设门禁：列表即范围，登记只是记账。** 能不能在某个目录开单取决于它是 Cindy 列表里的目录，不取决于库里有没有这一行，所以 `create_ticket` 允许带着 `root` 自动记账，不必先单独调 `register_project`。库里只有 `projects` 这一张表，没有白名单也没有镜像表，`root` 是唯一的身份：六个同名的 `bots/*/workspace` 各自一行、互不干扰，`project_id` 撞车不阻断派发——标签给人看，路径才决定工单落在哪。三个字段各自的改法不同：**`name` 是显示名**，写错了用 `rename_project` 改（只动这一列）；**`project_id` 是身份**，换它等于换一个项目，没有原地改名的入口；**`root` 是路径**，改路径只能 `unregister_project` 之后重新登记。`create_ticket` 不会代你改任何一项。

`artifacts` 是 1–20 个业务项目内文件相对路径。禁止绝对路径、项目外路径和解析后重复的路径。程序计算并保存每个文件的 SHA-256 和大小，不信任调用者自报摘要。单文件上限 32 MiB。

### artifact 的 SHA-256 与 git blob 的 sha 不是同一个数

**库里存的是工作树里的字节。** `fingerprint()`（`task_center/artifacts.py`）按 `Path.open("rb")` 顺序读文件算 SHA-256，所以 `submissions.artifacts[].sha256` 永远是「提交那一刻工作树里那个文件的内容」。`review_result` 复核时用同一个函数再算一次，两边同口径，所以自动比对永远不会因为换行而有假阳性。

**两者在换行转换时不同。** `git cat-file blob <rev>:<path>`（或 `git show <rev>:<path>`）读的是库里的 blob。`core.autocrlf=true` 的检出会把 blob 里的 LF 写成工作树里的 CRLF——同一份内容、两种字节、两个 SHA-256。**本仓库从 2026-11-23（AW-137）起把全仓文本钉成 LF**：`.gitattributes` 里 `* text=auto eol=lf`，只有 `skills/**` 与 `.agents/skills/skill-routing/index/**` 保持 `-text`（原字节进出，本来就不转换）。所以新克隆、以及按 `README.md` 的「行尾口径」就地重检出过的树，文本的工作树字节与 blob 相同。**旧检出仍是历史口径**：2026-11-22 在本机（`core.autocrlf=true`）实测 `task_center/core.py`、`docs/protocol.md`、`tests/test_batch.py`、`PRD/PRD-014-计划队列与依赖.md` 的工作树 sha256 与 blob 都不同——那是规则落地之前的事实，没重检出的树今天仍然如此。

**验收用工作树的 SHA-256，不用 blob。** 在 `head_sha` 的检出（工作树）里对文件重算 `sha256`，与 `submissions.artifacts[].sha256` 比；拿分支 blob 去复算会看到系统性差异，那不是成果被改过（`ARTIFACT_CHANGED` 判的是工作树对工作树），也不是提交造了假。要用 blob 核对就先按 git 的换行规则归一（或 `git -c core.autocrlf=false checkout`），但那是另一种口径，不是本库记下的那一个——两处各写一种，事后没人分得清哪条是权威。

`evidence` 为 1–100 条自检说明字符串，`risks` 为 0–100 条字符串数组（没有风险就给空数组，字段不能省）。自检说明是执行者声明，不等于独立测试证据已经被系统验证。

验收条件里说的「提交说明」，指的就是 `submit_result` 的 `summary` 与 `evidence` 两部分：`summary` 说清这次改了什么、边界在哪，`evidence` 逐条给出自检说明。别处不再有第二个位置——验收不得要求执行者额外写进工单描述、聊天回复、单独的文件或某个特定小标题下，也不得以「说明的位置不对」为由判不通过。条件想约束文案时，要写明它落在 `summary` 还是 `evidence`。

### `planned` 与依赖三类（PRD-014 R1/R2/R3）

**`planned` 是「还没开工、已经在库里排着」。** 它和 `ready` 的区别不是语气：`ready` 是「等人领走」（算在途，收尾闸门会拦），`planned` 是「还没轮到」（不算在途）。`create_ticket` 带 `state: "planned"` 建它，之后可以 `cancel_ticket` 撤掉、可以 `revise_ticket` 改判据，但**不产生任何尝试**，也不接受 `bind_session`——没有尝试可绑。

**依据在 claim 那一刻定格。** `create_ticket` 只记 `prd_path`，`tickets.prd_sha256` 在排队期间是 NULL；`claim_ticket` 自己读文件算 sha256、写进 attempts 并回写工单行。所以排着队的单遇到 PRD 升版不算过期，`claim` 之后 PRD 再变才是 `STALE_PRD`（比较对象是那次尝试上记下的那一份）。

**`archived` 是第十个状态，也是第三个终态。** 它只能由 `archive_ticket` 从 `done` 进入（`done` → `archived`，没有第二条路：`create_ticket` 只发 `planned` / `ready`），表达的是「这张单验收过、库里却查不到回执或合并记录——因为它的证据链断在 schema 8 之前」。它和 `cancelled` 不是一回事：取消=没做完（作为 `blocked_by` 前置**不算**满足），归档=做完了但账对不上（**算**满足）。它也不算在途、不进 `list_unmerged`、不被 `wait_audit` 的 done 两类命中，但 `reviews` / `submissions` / `events` 一条不删——「它验收过」是事实，不许为了让清单好看而抹掉。存量清账的判据、命令与 journal 见 `tools/archive_legacy.py`（`state='done'` 且有缺口且 `created_at` 早于 2026-11-21）。

**三类依赖边**（`ticket_links` 表，写入在 `create_ticket` / `revise_ticket`），各自回答「拦不拦、在哪个操作里拦」：

| kind | 用途 | 拦不拦 |
| --- | --- | --- |
| `blocked_by` | 前置单：它没 `done`，下游不许开工 | 拦，`claim_ticket` 报 `BLOCKED_BY_DEPENDENCY` 并点名前置单与它当前的状态 |
| `related` | 相关但可并行：同主题、会互看成果 | 不拦，只进 `get_ticket` 的 `links` 与池子视图 |
| `rides_with` | 同一车次：按车次合并资源与合并动作 | 本操作不单独拦；车次归属由 `claim_ticket` 的运行期校验回答（PRD-014 R4） |

- **只有 `done` 算满足。** `cancelled` 的前置单仍算未满足：取消不是完成，下游要不要撤由人决定，库不替人自动作废下游。
- **成环在写边那一刻就拒。** 写完边、同一条事务里跑一次可达性，成环报 `DEPENDENCY_CYCLE` 并点名环上的单号，整个事务回滚（连刚建的那张单一并回滚）。
- **两个方向都答得出。** `get_ticket` 的 `links.out` 是「它卡在谁身上」，`links.in` 是「谁在等它」，每边带对方的当前状态；`list_tickets --planned` 只显示未 `done` 的 `blocked_by`。

### 车次：几时几张单算一趟活（PRD-014 R4）

**一个车次 = 一次资源占用窗口 + 一次构建 + 一次合并。** 它在库里是一行 `batches`：`id` / `project_id` / `resource_key`（与 `PRD-012` 的 `资源:<类型>:<值>` 同形）/ `owner_session_id` / `planned_at` / `closed_at`。**没有单独的状态列**：`closed_at` 为空就是还在占用，非空就是已关闭——多一列 `status` 就多一处要与它同步的真相。

单与车次的挂法有两列，各回答一个问题：

- `tickets.batch_id`：这张单在哪一趟活里。可空，空即「没有车次」。
- `attempts.batch_key`：这条尝试属于哪个车次。`claim_ticket` 时从 `tickets.batch_id` 抄一份，无车次留 NULL。它只回答「属于哪个车次」，**不承担唯一性**。

**车次内一个会话可以持多张单。** 靠的是索引收窄，不是放宽会话维度：

```sql
CREATE UNIQUE INDEX active_session ON attempts(session_id)
 WHERE batch_key IS NULL AND session_id IS NOT NULL AND state IN ('blocked','running','submitted');
```

所以两件事同时成立：没有车次的单行为一字不变（一个会话一张，第二张报 `CONFLICT`）；有车次的单根本不在这一条索引的范围内，同一会话在同一个车次里可以同时持两张活跃单。

**车次归属只有一个判据：`batches.owner_session_id` 加 `claim_ticket` 的运行期校验**，不是唯一键。唯一键表达不了「同一会话可以持多张、别的会话不能进同一车次」：同会话同车次的两张活跃单会让任何 `(session_id, batch_key)` 或 `(batch_key)` 的唯一索引重键，而放开车次维度只留 `active_session(session_id)` 又会让「两个会话各持同车次一张」偷偷通过。校验在 `claim_ticket` 里完成，**而 claim 阶段能拿到的会话标识只有 `coordinator_session_id`**（执行的会话要到 `bind_session` 才有），所以车次认领的就是 claim 的那条会话；`owner_session_id` 之后不随 `bind_session`（换执行会话）也不随 `recover_ticket` 改变。

**三种释放时机，各自靠什么失效：**

| 情况 | 何时发生 | 靠什么失效 |
| --- | --- | --- |
| 车次关闭 | 车次内最后一张单写合并记录时 `batches.closed_at` 落值 | `closed_at` 一旦非空，`claim_ticket` 报 `INVALID_STATE` 拒绝新单，`owner_session_id` 随车次一起失效、不再拦人。**本版只落了这一列与读它的那道闸**：写 `closed_at` 的合并动作在「车次内一次合并」那张单里做（`PRD-014` R4.5） |
| 单子终结 | `done` / `cancelled`，或验收 `reject` 回 `rework` | 未进车次的单：尝试状态离开 `blocked`/`running`/`submitted`，索引条件失效、槽位自动空出——释放不是一条清理动作，是谓词的自然结果。进了车次的单本来就不占这条索引，终结时车次照旧开着：`closed_at` 仍为空，持有会话仍是它 |
| `recover_ticket` | 协调会话显式作废当前尝试 | 旧尝试转 `superseded`（不在谓词里），无车次的单槽位立刻释放；车次的 `owner_session_id` 不随 recover 改变，同一个会话重新 `claim` 时按新的 `batch_key` 重新进入校验 |

**车次不放宽「同项目一次一张」。** 同项目已有在途单时领第二张照样报 `DISPATCH_BUSY`：要么显式传 `allow_parallel: true` 与 `parallel_reason`（`PRD-015` R1.3 要求同组第二张起这样写），要么等前一张收尾。

**本版没有建车次、也没有搭车的操作。** 建车次与「把一张单放进车次」是 `PRD-015` 的搭车实施单要做的事；`AW-101` 只落 schema 与 `claim_ticket` 那一道校验，所以现在要造一个车次只能直接写库（`INSERT INTO batches…` 与 `UPDATE tickets SET batch_id=…` 两条语句）。这一步是范围，不是遗漏。

`get_ticket` 的工单行带 `batch_id`，`attempts` 每条带 `batch_key`，所以「这张单在哪一趟、这条尝试属于哪趟」都能从一份回执里读出来。

**车次这一段的拒绝排在 `DISPATCH_BUSY` 之前。** 同一现场两个条件都满足时（项目里有在途单，且这张单的车次在别人手里），报的是 `BATCH_OWNED_BY_OTHER`，不是「项目忙」。这不是择一面的偏好，`PRD-014` 两处合起来只指向这一个判序：R4.2 把车次归属校验归成「与 R3 的 `BLOCKED_BY_DEPENDENCY` 同一类显式校验」，而 R3.1 定下的判序是「先回答这张单能不能开工，再回答项目忙不忙」（依赖那道校验因此明写在 `DISPATCH_BUSY` 前面）。还有一条同向的旁证：两条的出口不对称——`DISPATCH_BUSY` 给了 `allow_parallel` 这条出路，车次是别人的这句话没有出路；先报能绕的那个，调用方照它换参数再来一次，下一次仍然过不去。同一段里 `closed_at` 非空（`INVALID_STATE`）与车次行缺失（`INVALID_STATE`，防脏行）也一起排在这里，它们是同一类硬条件。用例 `test_batch_error_before_busy` 固定这一处。

**车次与工单必须同项目。** 一个车次是一次资源占用窗口（`PRD-014` R4.1），它只在一个项目里；`tickets.batch_id` 的外键只保证「车次存在」，说不出「它在同一个项目」。所以一条指向别的项目车次的工单是脏行，按号取单的每个操作都在入口报 `BATCH_PROJECT_MISMATCH`，`message` 点名工单号、工单所属项目、车次号与车次所属项目，一行不改。判据放在 `_ticket`（所有按号取单的操作的唯一入口），不是因为「写的时候不判也一样」：本版没有建车次、也没有搭车的操作，这条脏行只能从库外进来（见下一段），它进入任何业务逻辑的必经点就是那里；将来加了写入侧的操作，复用同一个判据即可，不必在两边各写一遍。

### `blocked`：工单在等一句话

工单跑到一半需要人拍板时，别再拿 `running` 假装还在干、也别用 `rework`（那是「要改」）：走 `ask_ticket` 让它进 `blocked`，问题落库，本轮结束；答案落库后由协调会话发一条消息把人会话叫起来。

- **两处都写，权威在工单级。** `ask_ticket` 同时改 `attempts.state` 与 `tickets.state`；`get_ticket` / `list_tickets` 看工单那一列就知道它在等什么，尝试上的 `blocked` 说明「是这一次尝试卡住了」，所以 `recover_ticket` 之后仍能分出「答完继续」与「尝试作废」。`revision` **不变**：等一句话不是新代次，只有 `recover_ticket` / `cancel_ticket` / `revise_ticket(restart=true 或重开已完成工单)` 才递增它。
- **它占着这个会话。** `active_session` 唯一索引的谓词含 `blocked`，所以同一个 session_id 在等答案期间绑不上第二张单（`CONFLICT`）。不这么写，答案回来把尝试改回 `running` 的那一刻，同一会话上会同时有两条 `running` 尝试，一条合法答案会以 `CONFLICT` 收场。解封的正规手段是 `recover_ticket`，它把这次尝试 `supersede` 掉、释放槽位。
- **答之前可以 recover。** `blocked` 在 `recover_ticket` 的允许状态集合里；recover 之后旧尝试 `superseded`、它的开放问题转 `voided`（回执的 `voided_question_ids` 列出是哪几条），revision+1。此后才到的答案报 `QUESTION_MISMATCH`，不会写进已失效的尝试；也不要把旧问题搬到新尝试上（新尝试 revision 已加一，旧答案依据的需求摘要也可能过期），要接着问就在新尝试上重新 `ask_ticket`。
- **谁能答：人与协调会话。** `answered_by=human` 表示这是人给的原文，此时不许带 `answered_by_session_id`（带了报 `INVALID_INPUT`，虽是 null 也算带了）；`answered_by=coordinator` 表示协调会话代记，`answered_by_session_id` 必填、必须等于该尝试的 `coordinator_session_id`（否则 `NOT_COORDINATOR`），也不能等于执行会话自己的 `session_id`（`SELF_ANSWER`）。这两列是自报字段，不是身份认证：库里验不出「这句真是人说的」，它们只让后来的人看得出这条结论有没有被转述。
- **一条问题一个答案。** `answers.question_id` 是 UNIQUE，本版不提供覆盖或改答案；答错了走 `recover_ticket` 重开。
- **唤醒不是引擎能力。** `answer_ticket` 把工单改回 `running` 只说明库里不再等答案，不代表那个会话已经在跑。叫起来靠协调会话用 `send_to_session`（带 `target_session_id`）发一条消息：只放 `question_id` 与一句指令，**不复制答案正文**（复制就多一份不随修订变的真相）。执行会话醒来读 `get_ticket` 的 `questions` / `answers` 后继续，最后 `submit_result`。投递失败按 `PRD-005` 4.5 第 6 步处置：会话已不在（`ARCHIVED` / `DELETED` / `NOT_FOUND`）只能 `recover_ticket` 重新派发，不许换一个会话接着写。

**为什么是两张表，而不是一张表加可空答案列。** 判据是「一条问题一个答案」这条约束能不能落在库上，以及作废的问题要不要额外字段：`answers.question_id` 的 UNIQUE 让「只接受一个答案」在索引上成立，不靠代码自觉；作废的问题是 `questions.state='voided'` 且没有对应 `answers` 行，而一张表得靠「answer 为 null 且 state='open'」两个字段互相印证才算得出「还没答」，还要额外防「有答案却仍写 open」这种行。两张表也让问答与 `attempts` / `submissions` / `reviews` 的挂法一致（子表引用父行，父行唯一的约束在索引上）。`one_open_question` 是 `questions(attempt_id) WHERE state='open'` 的部分唯一索引：一条尝试同时只有一条开放问题。

**`answer_ticket` 的判定顺序**（依次，先命中先返回）：

| 序 | 判到哪一条 | 码 |
| --- | --- | --- |
| 1 | 问题不存在，或不属于这张工单 | `QUESTION_MISMATCH` |
| 2 | 问题已不是 `open`（`answered` / `voided`，含 recover 之后才到的答案） | `QUESTION_MISMATCH` |
| 3 | 问题所属尝试已不是当前尝试，或它的 revision 与工单当前 revision 不同 | `STALE_ATTEMPT` |
| 4 | `prd_sha256` 与工单当前值不同 | `STALE_PRD` |
| 5 | 工单不是 `blocked`（库里留下了一条 open 问题配非 blocked 工单） | `INVALID_STATE` |
| 6 | `answered_by=coordinator`，但会话就是执行会话自己 | `SELF_ANSWER` |
| 7 | 同上，但会话不是派发这张单的协调会话 | `NOT_COORDINATOR` |

1、2 排在 3 前面是有意的：工单一旦离开 `blocked`，「问题已作废」比「尝试编号不再当前」更接近事实，这也让 recover 之后的迟到答案如 `PRD-005` 4.7 所说报 `QUESTION_MISMATCH`。`ask_ticket` 一侧的 `STALE_ATTEMPT` 与 `SESSION_MISMATCH` 由当前尝试和绑定会话直接判，正常路径全可达。两个兜底码要单独说清：`ALREADY_ASKED` 与第 3 至 5 条一样，正常路径上先被状态检查挡下（`blocked` 再问是 `INVALID_STATE`，作废的问题再答是 `QUESTION_MISMATCH`），它们防的是库里出现「open 问题配 running 工单」这类不一致行；`one_open_question` 索引与状态同步写让这种行写不进去。

### 验收派出去之后：一张单不再派第二个验收人（AW-133）

`awaiting_review` + 没有 `reviews` 行，在验收会话跑的那几分钟里一直是成立的。只看这两个条件的派单器
只能读出「该派验收」，于是一张单连着几轮被派了第二个、第三个验收会话。

补上的是一条可判的事实：**派验收的那一刻先落一条 `review_dispatched` 事件**（见操作表），之后
`tools/next_action.py` 见到它就跳过这张单。窗口 30 分钟，超时还没有 `reviews` 行自动回落成可派——
验收会话可能根本没起来，一直压着反而把这张单锁死。

| 库里的现场 | `next_action` 的输出 | 为什么 |
| --- | --- | --- |
| `awaiting_review`、无 `reviews` 行、无 30 分钟内的 `review_dispatched` | `dispatch_review` | 没人看，该派 |
| `awaiting_review`、无 `reviews` 行、30 分钟内的 `review_dispatched` | 不是 `dispatch_review`；没有别的活时是 `nothing` + `reason_code=review_in_flight` | 已经有人在看，再派一个验收人是重复劳动 |
| 同上，但事件指向的是**旧提交**（返工后的新提交还没人看过） | `dispatch_review` | 新提交没人看，旧事件不成立 |
| 同上，但事件时间读不出来 | `dispatch_review`，并在 `stop` 里写一句 | 宁可多派一次，也不要因为一行脏时间把单押住 |
| 提交已有 `reviews` 行、状态还停在 `awaiting_review` | `nothing` + `reason_code=needs_human` | 状态机慢一步，不是「在飞」；这不是再派的理由 |
| `review_dispatched` 在飞，池子里另有可领单 / 已验收待合的单 | 照旧 `dispatch_execute` / `merge` | 这条事实**只作「已派」提示**，不进 `BLOCKING_STATES`，不改另外三档的顺位 |

两处必须说清，因为它们在库里的字面很近：

- **被拒的调用也写 `events`，但它不是「已派」。** 成功那次的行带 `result`，被拒那次带 `rejected`；
  读侧只认前者。混在一起会让一次失败的标记悄悄压住下一轮派发。
- **它不是状态机的第十一个状态。** 工单还在 `awaiting_review`，`DISPATCH_BUSY` 照旧把它算在途；
  这条事件只是让「该不该再派一个验收人」有一个不靠记忆的答案。

### 回信落库：一张单最多一条 `sent`，没过回执的单合不进去

派发链的收口是「回执发回了原会话」，这件事以前只靠人记得做。`notices` 把它变成一条可查询的记录：

- **写什么。** 每次尝试发一条 `record_notice`：`status` 是 `sent` 或 `failed`，`detail` 是这次尝试的措辞（失败时写原因，例如「目标会话已归档」），`origin_session_id` 是回执的收件会话。`get_ticket` 的 `notices` 数组按写入顺序返回，`created_at` 说明发生在什么时候。
- **发给谁也要落库。** `target_session_id` 必填，而且必须就是 claim 时写下的那条协调会话——写成执行会话自己是这例错误里最常见的一种，那种回执根本到不了人手里，所以它报 `NOT_TARGET` 且一行不写。这个字段就是「事后查这次回执到底发到哪」的答案，不是可以补填的字段。
- **宿主怎么处理的也要落库。** `wake_kind` 记宿主回执里的**原值**（执行会话按本节「回执只发一条」那条走时，协调会话填的是 `unknown`，因为那份原值不再随回执过来），不按本工具的枚举收：已见过的是 `queued`（排在原会话这一轮之后）、`resumed`（直接唤醒了原会话）与 `already-active`（目标会话当时就在跑），将来还可能再来一个。**为什么允许枚举外的值：** 宿主的取值面比这里写下的宽，只收三个值就等于逼调用方挑一个近似的填，库里留下的是推定而不是事实——事后查「这条消息是怎么到的」时，推定与宿主原话看不出区别，这比缺一个字段更难发现。所以除了下面这一个特例，任何非空、无内部空白、不超过 40 字的取值都原样写进 `notices.wake_kind`，调用方**不得**自行映射。唯一被特判的是 `created`：它是失败信号而不是另一种成功方式——宿主没有投递，而是新建了一个任务（那条新会话的标题往往就是回执原文本身），这时这条 notice 一律落 `failed`，回执带 `alert: NOTIFY_CREATED_TASK` 且命令非零退出；那一行仍然留在库里，而不是被回滚掉。工具只报错不停会话：停掉误建的新会话仍由人来做。
- **一张单最多一条 `sent`。** 回信只发一次，重复宣告不是新事实：第二次 `sent` 报 `ALREADY_NOTIFIED`，库里只留第一条。这条约束落在 `one_sent_notice` 部分唯一索引上（`notices(ticket_id) WHERE status='sent'`），不靠代码自觉——并发下两个都会以为自己是第一次。
- **`failed` 可以多条。** 真会失败多次，每次一条，逐条留原因。它**不改工单状态**：失败只说明这次投递没成功，`NOTIFY_FAILED` 不推动任何状态机，也不会自动重派；要重派由人决定，走 `recover_ticket` 重新派发。
- **合并前必须有回信记录。** `merge_ticket` 在该单 `notices` 为空时报 `NO_NOTICE`：要么补记一条 `sent`，要么记一条 `failed` 写清原因。`failed` 也算走过——回执没送达但原因记下来了，这是事实，不是可以拿来越过的门；它拦的是「验收了、合了、回执不知道发没发」这条路。已有合并记录的单行为不变，先报 `ALREADY_MERGED`。
- **回执的收件人就是当前 owner，落库的人也是它。** 两个只读工具打印的 `target_session_id` 都取 `tickets.owner_session_id`（老库没有这一列时回退 `coordinator_session_id`），所以「发给谁」与「谁有权记」是同一条会话；`NOT_TARGET` 只在有人还在按 origin 投递时出现（AW-122）。
- **只有当前的 owner 能记「送到了」。** `session_id` 必须等于这张单的 `owner_session_id`（claim 时等于 `coordinator_session_id`，之后可被 `take_over_ticket` 换掉），否则 `NOT_COORDINATOR` 且一行都不写。与 `answer_ticket` 的同名判定一样，这是把「谁发的回执」与**现在负责收尾的那一条**对齐，不是身份认证——换一个字符串仍然能绕过；而 origin（`attempts.coordinator_session_id`）只用于事后追查「当初是谁派的」。**执行会话自己那次没送到的回执不在这里写**：它没有资格宣告送达，但有资格说清自己发不出去，走 `report_notice_failure`（见「协调会话归档之后」）。
- **两个参数从库里取，不靠人拼。** `python tools/notice_payload.py --ticket <单号>` 打印两段可直接复制的 JSON：`send_to_session` 的 `{message, target_session_id}` 与 `record_notice` 的完整 payload；两处的目标都取 `tickets.owner_session_id`（老库回退 `coordinator_session_id`），两者都取不到时非零退出并说明原因，不猜一个字符串出来。它读的是**最新值**：库旁有非空 `-wal` 时按 `mode=ro` 读（会留 `-shm`/`-wal` 旁文件），没有才用 `immutable=1`（一个旁文件都不留）；每次输出的第二行都写明这次走了哪条路。
- **执行会话那一头也有一个工具。** `python tools/receipt_payload.py --ticket <单号>` 打印 `send_to_session` 的完整 args：`message`（工单号 / 状态 / 提交 sha）与 `target_session_id`（该单的 `tickets.owner_session_id`：现在负责收尾的那条会话；老库回退 `coordinator_session_id`），外加一段说明——`send_to_session` 在工具列表里可能搜不到，要经 `cindy_helper` 的 `call_tool` 调；`target_session_id` 不能省，省了就是新建任务。两者都取不到时它**非零退出、一个 args 都不打印**：打印出来的东西一定会被复制走，没有 target 的那份会被当成「省略 target」发出去。两个脚本共用同一份只读开库与库路径找法（在 `tools/notice_payload.py` 里），所以从 `.worktrees/<工单号>/` 里跑也找得到主检出那份库。
- **回执只发一条，正文不报送原值。** 执行会话交回成果时发**一条**消息：正文只写工单号、状态与提交 sha 或 review_id；`wake_kind` 不写进正文，也不为它另发一条。理由与做法一起换了：那个值只出现在 `send_to_session` 的发送回执里，一条消息写不出自己这一条的发送结果，所以**这条消息本就不承担报送原值的职责**——省掉这一步，也就省掉了「推定一个值写进正文」这条唯一能造假的路径；而以前那条只写原值的第二条，每发一次就要多唤醒一次原会话、多读一遍上下文，收益不抵成本。协调会话收到回执后照常 `record_notice` 落库：`wake_kind` 填 `unknown`（只校验形状、不按白名单收，所以它合规矩也能入库），并在 `detail` 里写明「本次 `wake_kind` 未取到，`unknown` 是占位」——这一列本身记不下「这是推定」，省了 `detail` 就再也分不出这一行是事实还是推定。
- **送达改由一次会话列表核对承担，不看投递回执里的 `wake_kind`。** 协调会话收到回执后跑 `cindy_helper` 的 `list_sessions`（按 `workdir`），确认列表里没有多出标题就是这份回执正文的新任务——**没有多出新任务即算送达**。这条判据只看有操作后果的那一类：`queued` / `resumed` / `already-active` 都只表示消息进了原会话、没有副作用；唯一有后果的是宿主把回执当成新建任务，而它在会话列表里看得见，不必再去读投递回执。2026-11-18 实测同一会话、同样入参与 `target_session_id` 连投三条消息，`wake_kind` 依次是 `already-active`、`created`、`queued`——三值同框，所以「上一条投中了」推不出「这一条也投中了」，能推的只有当场查一遍列表。查见多出的那条就停掉它（`stop_session_turn`，必要时 `archive_sessions`），再带 `target_session_id` 重发。
- **终态只落库不唤醒。** 回执的收尾分两类：`done` / `rework` 这类终态**只落库不唤醒**——提交、验收、`record_notice` 都记进库，不为了通知再发一条消息，否则每张单都要多一次唤醒、多一遍上下文重读；唤醒只留给三种情况：`created` 误建了任务、工单 `blocked` 在等人一句话、`ask_ticket` 已经落了问题。判据按「这件事要不要让别的会话现在改变动作」问，不按「有没有新状态」问。
- **dispatch 完就结束本轮，禁止轮内 sleep 等下游。** 派完就交回这一轮，**唯一续跑点是回执到达时起的新一轮**；回执没到时**由 `python tools/wait_audit.py` 报出**，协调会话**不得在轮内 sleep，也不得 sleep 一阵再查一次库**（轮内轮询是同一个问题的另一种写法）。这样规定的理由不是「等一会儿也没关系」，而是轮内 sleep 把一条活轮押在「回执一定会到」上：睡的时候不能纠错、不能响应队列里的消息，睡过头就是盲等（实测同一条会话日志里 31 次 `sleep`，其中 `sleep 1700` 四次、`sleep 1800` 三次、`sleep 1500` 三次，动作都是「睡完再查一次库」）。而回执本来就不保证送达：`review_result` / `done` / 合并都只落库不外发，唯一的外发是评审会话自己发的一条回执，那是礼貌不是保证；`send_to_session` 还是入队语义，还可能被宿主当成新任务误建。检出面就是 `tools/wait_audit.py`：只读打开工单库与会话库，列出「等在验收超过 4 小时且没有 review 行」「`done` 但 `notices` 为空」「`done` 但 `merges` 无行」「`running` 但执行会话 10 分钟没有活动」四类，阈值出处是 `PRD-010` §4.3 S3（4 小时）与 §4.3 S4（10 分钟）；有命中退出 1，库有问题退出 2，末行固定打四类各几条。**判据：这一轮里搜不到「sleep 一段时间再查库」这种写法；「下游还没回」这个结论来自一次 `wait_audit` 或下一轮到达的回执。**
- **投递之前的三步先做完，再谈回执。** 回执只说明这一条消息怎么被处理，它不替你把前面的判断补上：队列里已有未消费的消息就不叠加（改走 `update` / `steer` / 等），正文里不写会在几小时内失效的标识（只写指向 `get_ticket` 当前状态的说法），同一事项的消息用覆盖式改写合并不拼正文。完整口径见「发消息之前：看队列、只写指针、同类合并」。
- **真伪的边界。** 这里只做落库，不投递：库里记的是「协调会话声明它发了、发给了谁」与它自己填的 `wake_kind`（本条路径上是 `unknown` 占位），不是「宿主确认送达」。自动发信与重试属于之后的适配层。

### 协调会话归档之后：owner 与 origin 不是一个字段

协调会话是宿主的会话，库里只有一个字符串。它不在之后，`record_notice` 的 `NOT_COORDINATOR` 与 `merge_ticket` 的 `NO_NOTICE` 会把一张已经验收完的单锁死：成果在库里，回执没人能补，于是合不进去（AW-106 的现场：`state=done`、review `accept`、`notices` 0 行、`merges` 0 行）。这一节写清可核的事实、两个字段的分工与两条正当出口。

**一、可核的事实：它在哪一刻被归档，能复算。**

- **归档是宿主做的，不是本工具。** `task_center/` 里没有任何归档**会话**的调用（`grep -rn "archive_sessions" task_center/` 无输出），所以「让它别归档」「等它归档完再来」这类设计在本仓库里做不到。注意别把两件事的名字混在一起：`archive_ticket` 归档的是**工单**（`tickets.state='archived'`），跟宿主里那条会话的 `status=archived` 毫无关系，一个归档了工单的会话下一条消息照样收得到。`move_session` 也搬不动已归档的会话（见 `docs/intake.md` 的限制那一段）。
- **看两个字段就够：** 宿主会话库 `sessions` 表的 `status`（或 `cindy_helper` 的 `list_sessions` 回执里的同名字段），以及 `last_turn_ended_at` / `updated_at`。2026-11-22 在 `%APPDATA%\Cindy\cindy-*.db` 实测：调度建的协调会话 `<session-id>`，`last_turn_ended_at` = 08:04:08.244Z、`updated_at` = 08:04:08.266Z（相差 22 ms），`status=archived`；另一条会话同样是「这一轮结束即归档」（相差 26 ms）。
- **不能从「这一轮结束了」推断「它一定归档」。** 同一天同一张表里，另外两条会话也结束了本轮，`status` 仍是 `active`。所以规矩只有一句：**不要假设一轮结束后的协调会话还能收信**，也不要把「还没归档」当成保证。
- **一条命令复算：** `python tools/coordinator_state.py --ticket <单号>`。它只读打开工单库与宿主会话库，打印这张单的 `owner_session_id`（现在谁负责）与 `origin`（谁派的）、宿主里那条会话的 `host_status` / `host_source` / `last_turn_ended_at` / `updated_at` 与结论；退出码 0 = 责任人还在（`OWNER_ACTIVE`），1 = 责任人已归档或宿主里查不到（`TAKEOVER_NEEDED`，并打印接替命令的形状），2 = 查不出来（没有 owner、会话库打不开——**「不知道」不当成「还在」**）。

**二、两个字段：授权读 owner，审计读 origin。**

| 字段 | 说的是什么 | 会不会变 | 谁读它 |
| --- | --- | --- | --- |
| `attempts.coordinator_session_id` | **origin**：谁派的这条尝试 | 不变（历史事实） | 回执的 `target_session_id`、`report_notice_failure` 的目标、`get_ticket` 的审计视图 |
| `tickets.owner_session_id` | **owner**：现在谁负责把它收尾 | 可变，`take_over_ticket` 换它 | `record_notice` 与合并的授权判定 |

`claim_ticket` 同时写下两者（此刻相等）；`take_over_ticket` 只改 owner。以前两者是同一个字符串，于是「派发它的会话被归档」直接等于「这张单再没有人能负责」——那就是 AW-106 的锁。

**三、选定的处置：两条路，各管一件事。**

1. **执行会话记自己那次失败：`report_notice_failure`。** 它只能落 `failed`（操作里没有 `status`，造不出 `sent`），记在它自己那条尝试上，`target_session_id` 仍是 origin。于是「这次回执没送到、宿主回了什么」在 `notices` 里查得到，`merge_ticket` 的 `NO_NOTICE` 也就过得去。记录人写在同一调用的成功事件里（`detail.result.reported_by_session_id`）——不新开列，`notices` 的形状不变。
2. **新会话显式接替责任人：`take_over_ticket`。** 它把 `tickets.owner_session_id` 换掉，之后新 owner 的 `record_notice`（宣告 `sent`）与合并都照原路走。payload 只有四个字段（项目、工单、新 owner、理由），不换 `revision`、不作废尝试、不动提交与验收、也不改 origin。

**AW-106 照这条走一遍**（在副本库与副本仓库上做，正式库与 master 都不动）：

```bash
python tools/coordinator_state.py --ticket AW-106 --db <副本库> --sessions-db <会话库>
# owner 的 host_status: archived / verdict: TAKEOVER_NEEDED
python -m task_center --db <副本库> take_over_ticket --request-id <编号> --input <payload>
# payload：{"project_id":"ai-workspace","ticket_id":"AW-106","new_owner_session_id":"<未归档会话>","reason":"<一句>"}
python -m task_center --db <副本库> record_notice --request-id <编号> --input <payload>
# 回执确实送到过就记 sent；从没送到就记 failed 并写清「原协调会话已归档」
python tools/auto_merge.py --db <副本库> --ticket AW-106
```

**不许让人冒充那条已归档会话的 id 去调 `record_notice`**——那是在库里写一句假话，而这里要的是把「谁接手」这件事本身写下来。

**三点五、无人值守时，接替是派单流程的一步，不是事后救火。** 调度建的那种派单会话 claim 完、这一轮结束就归档，「先 claim、回执来的时候再接替」在无人值守下永远晚一步——回执找不到人时，那张单已经停了几个小时。所以 `PRD-001` v0.12 把顺序定在 claim 之后、发派单消息之前：派单器跑一次 `python tools/handover_owner.py --ticket <单号> --to <常驻会话 id> --reason <一句>`，把 owner 换到一条宿主里活着的常驻会话，然后才把执行会话叫起来。执行会话的收件人因此从第一刻起就是它（`tools/receipt_payload.py` 打印的就是 `tickets.owner_session_id`），接替不再是「回执没送到之后才补的动作」。

这条路的前提是「有人指定了一条常驻会话」。没有的时候不编一个：回执照常发，发不出去就走 `report_notice_failure` 落 `failed`（`detail` 写宿主原话与目标已归档），推进交给下一轮 `next_action`，悬空由 `python tools/wait_audit.py` 报出。**「不知道收件人是谁」与「收件人不在」都落成库里的一行**，不落成一句推测。

**四、代价，写清不藏。**

- **接替是自报声明，不是认证。** `new_owner_session_id` 仍是字符串，`task_center` 不验证宿主里那条会话是否真的死了（它没有宿主访问权）。**`tools/handover_owner.py` 会核**（只读宿主会话库，与 `coordinator_state.py` 同一条读法）：owner 还活着就拒绝接替，目标已归档也拒绝——但那是工具的默认路径，直接调本操作的字符串仍然能绕过。早接替的后果是原 owner 随后的 `record_notice` 变成 `NOT_COORDINATOR`——一次可见的失败，而不是两条同时成立的真相；`events` 里那次接替带着旧 owner、新 owner 与理由，事后可以追。
- **执行会话不能给自己接替**（`TAKEOVER_BY_EXECUTOR`）：那等于自派自做，`SELF_DISPATCH` 那条规矩会从后门失效。
- **`failed` 让合并这道门放行，不证明送达。** 库里因此有两种记录长得不一样：一条 `sent`，或若干条写清原因的 `failed`——判「闭环」时要看是哪一种，不能只看 `notices` 非空。
- **被否掉的另一条路：给调度会话留一个不归档的收件箱。** 归档由宿主决定，本仓库既没有接口阻止也没有接口推迟（见上面第一条事实），所以它在本项目里做不了；即便能做，也会让「一张单最多一条 `sent`」从线性变成「发到哪个收件箱算数」，判据比现在更含糊。

### 被拒绝的写操作留一行事件

写操作被闸门拒掉时，`events` 里补一行，`operation` 就是原操作名，`detail` 只写两件东西：

```json
{"rejected": "STALE_REVISION", "request_id": "claim-t1-2"}
```

- **不写 payload 全文。** 输入里可能有口令、token、完整需求摘要，而拒收行是要长住库里的。所以 `detail` 只有这么两个键，不带着 `input` 或 `summary`。
- **`request_id` 缺失或写入 null 的两种情况：** 调用根本没带编号，或者错在 `REQUEST_CONFLICT`（那个编号已经属于另一次调用，再写进去就分不出是哪一次）。
- **`ticket_id` 取自 payload；工单不存在时为空。** 例如 `create_ticket` 被条件闸门拦下时这张单根本没有，事件行的 `ticket_id` 就是 NULL——「没有这张单」也是事实，但不能凭空造一个工单来挂它。
- **`create_ticket` 成功的那条事件挂在新发的号上。** payload 里没有 `ticket_id`（自报号已被拒），所以这条事件只能挂在回执刚发出去的那个号上；写 NULL 的话「这张单什么时候建的」按单就查不到了。
- **它不影响任何既有判定。** 拒绝行是只读的事实，不推动任何状态机，也不写 `requests`：用同一编号重试仍会重新判定，拿到的是当时的结论而不是旧回执。成功路径一行都不变（本来那条带 `result` 的事件照旧写）。

为什么要有它：`events` 以前只记成功路径，于是「串行保护到底拦过几次」「谁试过用别人的工单背书」这类问题永远查不出来，也判不出某条闸门是有效还是在空转（PRD-013 R3）。

### 会话标识与工单标识

session_id 按原样精确比较，大小写不同即视为不同会话（`EXEC3` 与 `exec3` 不同）。这不是身份认证，真实身份与目录回执由后续 Cindy 适配层承担。

`coordinator_session_id` 同样只是调用方自报的字符串，但它让「谁派的」和「谁做的」分得开：claim 时写进 attempts，bind 时必须换一个会话标识。两者相同就报 `SELF_DISPATCH`，工单停在 dispatching，不产生 session 绑定。这条规则拦的是职责混同，不是防伪——换一个字符串仍然能绕过，所以它配着 `AGENTS.md` 的硬规则五一起用。

工单标识在整个中心内唯一，但它**不再由调用方自报**（PRD-014 R5）：`create_ticket` 从项目自己的 `next_number` 取号并加一，形态是 `<前缀>-<序号>`，前缀取项目 id 的短名（`ai-workspace` → `AW`、`p1` → `P1`、多段取每段首字母）。跨项目不共用一条序列，所以 `AW-001` 与 `KIT-001` 各自独立；prefix 撞在一起时后建的那张会跳过已被占的号——主键是全局的，撞号会覆盖别人的单，跳号不会。

历史号一律不改、不重排、不复用：迁移按每个项目已有的最大号初始化发号盘（`AW-096` → 下一次 `AW-097`），`claim` 不再取号——号在写 `tickets.id` 那一刻就发出去了，它同时是主键、也是 attempts / submissions / merges / notices 引用的那个值，换号就等于换主键。

### 恢复与修订的验证口径

恢复必须写明它核对过的当前尝试编号。拒收返工不递增 revision，一个 revision 内可以有多个尝试；只比版本号会让晚到的旧恢复意图作废正在执行的新尝试。

修订已完成工单必须显式传 `reopen: true`，否则拒绝。修订后旧验收记录保留，已终结（accepted / rejected）的尝试状态不会被改写成 superseded。

`checks` 为逐项验收数组，必须且只能覆盖当前 criteria 的全部 id；每条是 `{id,status,evidence}`，`status` 取 `pass` / `unverified` / `fail`（旧的 `{id,passed,evidence}` 仍收，落库时归一成 pass / fail）：

```json
[{"id":"C1","status":"pass","evidence":"检查了 result.md 的第1至3项，符合给定规则"},
 {"id":"C2","status":"unverified","evidence":"设备签名装不上，真机判据无法执行"}]
```

`verdict` 只能为 accept 或 reject；**accept 不许有失败项**（`unverified` 不阻断——它表示「没能验」，不是「验不过」），**reject 必须有至少一个失败项**（拿未验当理由打回等于对没验的东西下「不合格」）。未验不是绕过验收的出口：它必须写明为什么验不了，并被 `checks_summary` 与摘要行数出来。调用方仍需真正读取和检查成果；数据库只能核对字段与状态，不能证明自然语言证据是真实的。

### 验收 payload 是一张闭集

`review_result` 恰好接受八个顶层字段，写多一个都会被拒：

`project_id`、`ticket_id`、`attempt_id`、`submission_id`、`reviewer_session_id`、`verdict`、`checks`、`reason`。

多传字段（例如给验收补一句 `notes`、`comment` 或 `summary`）得到的是 `INVALID_INPUT: Unknown payload fields`，不是被忽略；报错也不会告诉你是哪几个字段——先照抄上面八个名字核对拼写，再怀疑值。自由文本只有 `reason` 一处可写；逐项证据只能写进 `checks[].evidence`，每一项恰好 `id`、`evidence` 与 `status`（或旧的 `passed`）三个键，多一个键同样报 `INVALID_INPUT`。整张表都是这个口径：未知字段一律拒绝，避免拼错后静默忽略。

## 预留、绑定与提交的顺序

1. 协调会话先调用 claim_ticket，传入自己的会话标识作为 coordinator_session_id，记录 attempt_id。
2. **派发是另一个会话**：协调会话不得自己执行这张工单。默认载体是项目普通会话——`send_to_session` 不传 `target_session_id` 并指定 `project_path`（见下节「派发的载体」）；宿主确实没有新建项目会话的能力时才用一次性独立上下文子任务并标注；两者都没有就停下来告诉人，不要自己动手。适配器负责检查项目与会话回执。
3. 被派发的会话调用 bind_session 保存绑定——它的 session_id 不能等于 claim 时的 coordinator_session_id。真实执行者必须等绑定完成，才能提交；先完成了成果也应保留本地记录并稍后提交，而不是再次创建工单。
4. 执行者保存业务项目中的成果，再调用 submit_result——`head_sha` 填 `git rev-parse HEAD` 的输出（40 位十六进制），它是这次被验收的那棵树。同一张单验收后再提交一次会刷新这个值，所以合并比的是最新一条。
5. 收到回执后通知协调方，**只发一条**：正文写清工单号、状态与提交 sha 或 review_id，不写 `wake_kind`、也不补第二条（为什么一条就够、送达怎么判，见「回信落库：一张单最多一条 `sent`，没过回执的单合不进去」一节的「回执只发一条」「送达改由一次会话列表核对承担」「终态只落库不唤醒」三条）。协调会话收到后 `record_notice` 落库（`wake_kind` 填 `unknown` 并在 `detail` 里注明是占位），再跑一次 `list_sessions` 核对会话列表：没有多出标题等于这份回执的新任务即算送达，多出了就先停掉误建的再带 `target_session_id` 重发。终态（`done` / `rework`）只落库不唤醒，唤醒只留给 `created` 误建、`blocked`、`ask_ticket` 三种。通知丢失时，可根据中心 awaiting_review 状态继续，而不重做执行。
6. 独立验收会话核对成果后调用 review_result。

本版只实现第1、3、4、6步的本机记录和校验，不会自动执行第2或第5步。也正因第2步还没有适配层，「新开一个会话」目前靠调用方自觉；`SELF_DISPATCH` 只能拦住「用同一个标识绑自己的工单」这一种偷懒方式。

### `submit_result` 的形状：三个字符串数组与首次提交的 `base_sha`

演练记录（2026-11-24 03:38，`onboard --host generic` 的最小闭环）里，同一个人为同一个 payload 连撞五次 `INVALID_INPUT`：先 `head_sha`、再 `base_sha`、然后把 `artifacts` 传成对象数组、再把 `artifacts` 传成一整段 JSON 字符串、最后 `evidence` 与 `risks`。五次都是同一个原因——协议只写了字段名，没写形状。所以在这里一次写全：

| 字段 | 形状 | 上下限 | 常见的错法 |
| --- | --- | --- | --- |
| `artifacts` | **字符串数组**，每项是一个项目内相对路径 | 1–20 条 | 传 `[{"path": "result.md"}]`（对象数组）、传 `"[\"result.md\"]"`（一整段 JSON 字符串）、传 `"result.md"`（单个字符串）、把绝对路径写进去 |
| `evidence` | **字符串数组**，每项一条自检说明（跑过什么命令、看到什么） | 1–100 条 | 传空数组、把整段自检写成一个多行字符串 |
| `risks` | **字符串数组**，每项一条已知风险 | 0–100 条（没有就给 `[]`，字段不能省） | 省略字段、写 `""`、写 `null` |
| `head_sha` | 40 位十六进制（大小写都收，存小写） | 恰好 40 字符 | 传短 sha（`git rev-parse --short HEAD`）、传 `HEAD`、传分支名 |
| `base_sha` | 同上，「这棵树从哪分叉」 | 恰好 40 字符 | 传「填表那一刻 master 的 sha」（rebase 之后语义不同，PRD-013 D5） |

三个数组字段的每一项另有一条长度闸门：非空、不超过 4000 字符。

**最小 payload**（把 `<…>` 占位换成真值、`--request-id` 用唯一编号就能跑）：

```json
{
  "project_id": "<项目 id>",
  "ticket_id": "<单号>",
  "attempt_id": "<claim_ticket 回执里的 attempt_id>",
  "session_id": "<这条执行会话的 id>",
  "summary": "改完了什么、边界在哪",
  "artifacts": ["kit/docs/protocol.md"],
  "evidence": ["python tools/run_tests.py → 末行 结果：全部通过"],
  "risks": [],
  "head_sha": "<git rev-parse HEAD 的 40 位十六进制>",
  "base_sha": "<git merge-base HEAD master 的 40 位十六进制>"
}
```

```bash
python -m task_center --db <中心库> submit_result --request-id <唯一编号> --input <JSON文件>
```

**首次提交没有 `master` 可对**：`git worktree add .worktrees/<单号> -b ticket/<单号> master` 出来的新仓库如果只有一次初始提交（没有父提交），`git merge-base HEAD master` 直接失败——`base_sha` 无处可取。这时填 `git rev-parse HEAD` 的输出，它与 `head_sha` 相同：这棵树确实没有从别处分叉。两条都跑得通的取法：

```bash
git rev-parse HEAD                                            # head_sha
git merge-base HEAD master 2>/dev/null || git rev-parse HEAD  # base_sha：没有 master 分支时就与 head 相同
```

`base_sha` 仍然必填、仍然是 40 位十六进制（PRD-013 R7 / AC12）：缺它一样报 `INVALID_INPUT` 且一行不写。这一节给的是「填什么」，不是「可以不填」。

**形状错只报一条，但一次说全**：`artifacts` / `evidence` / `risks` / `head_sha` / `base_sha` 五项的期望形状都写在同一份 `message` 里，后面跟 `Wrong now:` 与这一次不合格的每一项（例如 `artifacts[0] is dict, not a string`）。撞一次就能把五项一起改对，不用逐项试到第五次。

### 派发的载体

第 2 步的「新开一个会话」有明确所指，不是任选一种独立上下文。载体优先级写死：

1. **项目普通会话**（默认）：`cindy_helper` 的 `send_to_session` 不传 `target_session_id` 即新建任务，指定 `project_path` 与 `title`。新会话出现在该项目的会话列表里，可以点开、追问、自己提交成果——2026-11-17 的真实演练用的就是这一种（`tasks/real-session-drill.md`）。
2. **一次性独立上下文子任务**：宿主确实没有新建项目会话的能力时才用，并且必须在 `submit_result` 的 `evidence` 里写明「载体是一次性子任务，无法追问」。
3. **两者都没有**：停下来告诉人，协调会话自己执行仍然不被接受（见「会话标识与工单标识」）。

为什么要单独点名这两种载体：**`cindy_orca` 的 worker 与一次性子任务都不是项目里的 chat session**。Orca worker 活在协同面板里，不是某个项目会话列表中的一条，工单、成果和追问都挂不回项目；一次性子任务没有界面，跑完就消失，出问题时没有对象可追问。两者都能跑起来、也能交回一段文本，所以很容易被当成「已经派发了」，可项目里并没有一条能点开的会话——这就是 0.10 以前两轮派发退化的实际原因。

两个标识来源不同，都不许猜：

1. `coordinator_session_id`：协调会话自己当前任务信息里的 session id（宿主会话信息或工作区回执）。
2. 绑定用的 session_id：协调会话用 `send_to_session` 新建会话后，**创建回执里返回的执行者 id**，原样交给执行会话写进 `bind_session`，不改写、不缩写。

`submit_result` 之前，协调方与执行方各用 `cindy_helper` 的 `list_sessions`（按 `workdir` 过滤）反查这两个 id：两条都在、`title` 与 `workingDir` 符合预期才允许提交；查不到或对不上就按标识不可信处理——先停下，不提交。自身标识不必猜：用 `cindy_helper` 的 `call_tool` 调 `get_current_session_id` 取（它不在 `list_tools` 搜索目录里，按名字搜返回 `UNKNOWN_TOOL`，但 `call_tool` 直接可调），取到什么原样用；反查这一步不能省，它是唯一能当场发现错标识的动作。禁止从临时目录名、运行目录或路径里拼一个字符串当标识：从路径猜出来的 `pi:d00c54ae…` 这类值会原样进库，格式像会话标识、却对应不到任何会话，比明确的自报字符串更难发现（AW-007 就是这一例）。

#### 一张单牵扯到哪些会话、哪条消息

`get_ticket` 回执里的 `sessions` 把五列聚合成一个数组，顺序固定：请求方 → 协调 → 执行 → 验收 → 回执收件人；同一层有多条时按 `rowid` 升序，所以多代次尝试就是多条。

| role | 取自 | 关键口径 |
| --- | --- | --- |
| `requester` | `tickets.requester_session_id` | 由 `create_ticket` 的可选入参写入；不传就是 NULL，**不拿协调会话冒充** |
| `coordinator` | `attempts.coordinator_session_id` | 每代次一条 |
| `executor` | `attempts.session_id` | 每代次一条 |
| `reviewer` | `reviews.reviewer_session_id` | 每条验收一条 |
| `receipt_target` | `notices.target_session_id` | 多一个 `message_id` 键 |

每项八个键：`role` / `session_id` / `session_url` / `device` / `session_state` / `stale` / `at` / `source`。空会话标识不产出条目——不生成 `cindy://session/` 这种点开落不到东西的链接。

- **`session_url`** 是 `cindy://session/<id>?device=<deviceId>`。`device` 取自宿主库 `sessions.agent_device_id`（只读、`mode=ro`）；取不到时 `device` 写 `null`、链接退回 `cindy://session/<id>`，不拼一个假值。本机 1966 条会话的这一列全是 NULL，所以「没有 device」是常态，不是异常。
- **`session_state`** 是宿主的 `sessions.status`；等于 `archived` / `deleted` 时 `stale` 为 `true`（链接照给，但它是旧会话）。宿主库读不到时写 `unknown`、`stale` 为 `false`：未知不等于已失效。
- **消息级只存 id**：`notices.message_id` 是唯一载体，库里不存正文。读法是 `get_chat_history`（按 `session_id` + 该 id）；**不输出 `cindy://...?message=` 这种链接**——这个参数没人验证过能打开任何东西。取不到 id 就留 NULL，不拿时间戳或标题凑。

宿主库的取法：环境变量 `TASK_CENTER_CINDY_DB` 优先，否则取 `%APPDATA%/Cindy/` 下 mtime 最新的 `cindy-*.db`。读不到（不存在、不是 sqlite、没 `sessions` 表）一律当空表，不影响 `get_ticket` 本身。

### 发消息之前：看队列、只写指针、同类合并

`send_to_session` 的入参里没有 steer、合并或优先级——它的完整 `properties` 只有 `message` / `target_session_id` / `title` / `working_dir` / `project_path` / `device` / `model`，没有一样能表达「先看看目标会话在干什么」。所以发之前那三个判断只能在调用方做；不做，代价就留在目标的队列里：`ed91af11`（CHM-042）的队列堆了 4–5 条，其中一条让绑定的 attempt 在入队两分钟后就 superseded；`bc5c116f`（CHM-030）一条消息在队列里躺了 18 小时没被消费，最后靠另开新会话绕过。

三步，每一步都能当场判一次：

1. **看队列。** `list_session_queue({session_id})` 返回 `{ok, session_id, queued_count, queue[]}`，每条带队列位置、来源、入队时间、正文摘要与 `consuming`；`list_sessions` 每条也带 `queuedCount`。`queued_count` 大于 0 就不再叠加——`send_to_session` 再发一条只是把它排到后面，它这一轮读到的仍是旧口径。于是三选一：`update_session_queued_message` 原地改写、`steer_session_queued_message` 提进当前轮，或等它消费完。判据：投递之前查过一次队列，查到有未消费消息时队列里没有多出新的一条。
2. **正文只写指针。** 判一个字符串能不能进消息正文，只看它会不会在几小时内失效：`attempt_id`、commit sha、分支 head、`revision` 号、行号，以及已被 supersede 的旧编号，这些只留在库里；正文改成指向说法，例如「以 `get_ticket` 的当前 attempt 为准」。判据：正文里搜不到这六类会过期的字符串，而且指令在目标会话重新读一次 `get_ticket` 之后仍然成立。
3. **同类消息合并成一条。** 只有三条同时成立才合并：同一事项（同一张工单或同一个待办，不是同一个项目）、该条尚未开始投递（`consuming=false`）、这条是你自己用 `send_to_session` 投递的。合并是覆盖式改写：`update_session_queued_message({session_id, queued_message_id, message})` 把整段正文换成最新那一版，保留编号与队列位置；不是把新话拼在旧话后面，那样一条消息里会有两份互相矛盾的口径。三条缺一条就不合并，宁可另发一条，或者等它消费完再发。

工具与权限（宿主原文，2026-11-21 取回）：

| 工具 | 作用 | 能操作哪一条 |
| --- | --- | --- |
| `update_session_queued_message` | 原地替换一条尚未消费的排队消息，保留编号与队列位置 | 任务只能改自己投递的；声明 control 的插件仅可编辑任务界面允许编辑的待发消息，其余类型返回 `UNSUPPORTED_CAPABILITY` |
| `cancel_session_queued_message` | 撤回一条尚未消费的排队消息 | 任务只能撤回自己投递的；声明 control 的插件可撤回所有来源 |
| `move_session_queued_message` | 移到指定位置（0 是队首，超出队列长度放到队尾），返回实际最终位置 | 自己投递的，或自己任务队列里由其它任务、协同成员发来的机器消息 |
| `steer_session_queued_message` | 转成插话，插进目标会话当前 turn | 同上 |
| `list_session_queue` | 只读列出任意 session 当前尚未消费的队列 | 只读，不做限制 |

- **任务不能操作的消息**：用户手打、伙伴委派、插件、定时任务这几类来源的消息，任务一律改不动；要去动它们，得由声明 control 的插件来做。所以「看到队列里有一条不对就改掉」这件事并不成立，先看来源。
- **`MESSAGE_CONSUMING`**：这一条 `consuming=true`，正在投递，改不动。它不是「没有这条」，等它投完再看队列。
- **`QUEUED_MESSAGE_NOT_FOUND`**：可能已经投递出去、也可能已被撤回或过期，宿主只报「找不到」不分这几种。**不能当成撤回成功**——要确认结果就重新 `list_session_queue` 看一眼。
- **`delivery=steered`** 才算真插进当前轮；回 `delivery=queued` 说明这一轮已经收尾，插不进去了。

### 验收记录的三档：`pass` / `unverified` / `fail`

**为什么要有第三档。** 验收条件里有一类判据在某些现场根本跑不了——真机装了不签名安装不上的包、需要另一个账号或另一个网络才能复现的路径。库里以前只有 `passed` 布尔，而 `accept` 要求全部为 true，于是验收方只剩两条路：填 true 撒谎，或者填 false 把一个其实没验的东西判成不合格。CH-096 的验收就落在第一条路上：`checks` 全是 true、真话写在 `reason` 里，于是「accept」与「整体通过」在库面上长得一模一样（AW-132）。

现在每条 check 有一个三档 `status`：

| `status` | 含义 | 对 `verdict` 的影响 |
| --- | --- | --- |
| `pass` | 验过，通过 | 不阻断 accept |
| `unverified` | 没能验（写清为什么），既不算通过也不算失败 | 不阻断 accept，但会被数出来 |
| `fail` | 验过，不通过 | accept 必报 `INVALID_INPUT`；是 reject 的必要条件 |

**怎么统计。** `review_result` 回执与 `get_ticket` 的每条 `reviews[*]` 都带 `checks_summary = {"total","pass","unverified","fail"}` 与一行 `summary`；`summary` 只写非零的档位，所以「全部已验证」是 `accept：3 条已验证`，带未验的是 `accept：2 条已验证、1 条未验`——字面不同，不需要读 `reason`。工单级还有 `review_summary`（最后一条验收的那行）与 `unverified_checks`（同一口径的未验条数），没有验收时是 `null` 与 `0`。

**归档口径不动。** `unverified` 不影响 `done` 与合并：`review_result` 的 verdict 集合仍是 accept / reject，`merge_ticket` 只看 `done` 与回执记录。是否愿意让带未验项的单进 master 是人的判断，库只负责把它数清楚。

## 验收通过不等于已合并

`done` 只说明验收通过，不说明结果进了目标分支。没有合并记录时，「验收了但没合」只能靠人记得——`tasks/plan-005-merge-after-accept.md` 的起因就是功能验收了、代码还留在分支上。`merges` 表把这件事变成一条可查询的记录：

- 写入用 `merge_ticket`，只接受 `state=done` 的工单；同一工单只允许一条，第二次返回 `ALREADY_MERGED`。`commit` 是合并提交（`--no-ff` 生成的那个），`target_branch` 是它进入的分支，`tests` 是合并前跑过的测试结论。三项都是自报字符串，数据库不能验证它们真的发生过。
- 读取用 `list_unmerged`：列出已验收但没有合并记录的工单。**历史工单 AW-004~AW-016 是直接在 master 上做的，没有 `merges` 行，因此会出现在清单里**——这是事实，不是待修的缺陷；要处理的是「现在这张到底合没合」，不是替历史补记录。
- 读取用 `list_unmerged`：列出已验收但没有合并记录的工单。**历史工单 AW-004~AW-016 是直接在 master 上做的，没有 `merges` 行，因此会出现在清单里**——这是事实，不是待修的缺陷；要处理的是「现在这张到底合没合」，不是替历史补记录。

**这条记录是谁写的、当时闸门跑了哪几步，必须一眼分得开（AW-116 / AW-127）。** `merges` 自 schema 16 起多四列：`source`、`verified_by`（`manual` / `backfill` 两档真跑过的核对命令，路径一律正斜杠，例如 `git -C D:/cindy-harmony-app merge-base --is-ancestor 8885cdc7 master`——反斜杠在 Git Bash 里会被当转义吃掉）、`verified_at` 与 `note`。存量行的四列是 NULL，那是「那时没有这一项」。

| `source` | 谁写的 | 当时闸门跑了哪几步 | `note` | `verified_by` |
| --- | --- | --- | --- | --- |
| `auto_merge` | `tools/auto_merge.py` | 冲突预演、临时工作树测试、真实 `--no-ff` 合并、合并后测试都跑过 | 不收（不是人写的记录，不需要辩解） | NULL（工具自己就是证据） |
| `manual` | 人手工 `git merge --no-ff` | 工具那套没跑；跑了哪些由写记录的人写进 `note`，落点由工具当场核一次 | 必填，写清哪几步跑了、哪几步没跑 | 当场跑过的 `merge-base --is-ancestor` |
| `backfill` | 人事后补记 | 当时没有记录，只有今天能核的落点；补记时间与原因写在 `note` | 必填，写清补记时间与原因 | 当场跑过的 `merge-base --is-ancestor` |

`manual` 与 `backfill` 都会当场重跑 `merge-base --is-ancestor`，不在目标分支里就报 `NOT_LANDED` 且一行不写；两档都要有可核对的树，`unverified` 在这里不算数。**为什么单列 `manual` 而不是让 `auto_merge` 带上 note**：`cindy-harmony` 这个项目在 `auto_merge` 跑不动（缺口是 AW-126）的那段时间，合入是手工做的；若把手工合并记成 `auto_merge`，读的人会以为预演、临时工作树测试、合并后测试都跑过，于是只能把「手工合并、工具为何中止」塞进 `tests` 那一列——一列硬塞三件事。**存量两行不改**：`CH-093` 与 `CH-094` 落库时还没有 `manual` 这一项，它们仍是 `source=auto_merge`、`tests` 仍是那句手写说明；按今天的口径去回填历史行，是拿今天的口径改过去的记录。

**补记的 A/B/C 三档，判据是两个现场事实**（与上面 `source` 的取值不是同一件事：那是一列能写什么，这是补记工具怎么分桶）。最新提交有没有 `head_sha`、它在不在目标分支里。

| 档 | 判据 | 处置 |
| --- | --- | --- |
| A | 有 `head_sha`，且是目标的祖先 | `tools/backfill_merges.py --apply` 补一条 `source=backfill` 的记录；本来没有回执的先补一条 `failed`（不是 `sent`） |
| B | 最新提交没有 `head_sha` | 一行不写，落进 `docs/unmerged-exemptions.json` / `.md` 并写清理由 |
| C | 有 `head_sha`，但不在目标分支里 | 拒绝并点名，不补记——它到底落到哪里要先查清 |

```bash
python tools/backfill_merges.py --dry-run   # 打印 A/B/C 三档与每张单的判据，什么都不写
python tools/backfill_merges.py --apply     # 只给 A 档写记录，并刷新历史豁免清单
```

**历史豁免清单不是免检证。** `auto_merge.py --list` 按它分两栏；豁免条目若现场已经有 `head_sha`，就说明事实变了，照样列进「真待合并」并告警。B 档之所以被豁免而不是补记录：没有可核对的树，补一条就是「没人记过」冒充「已经合了」。C 档不豁免：它恰恰证明这张单**没有**进目标分支。

**清单是快照，空清单也是一等结果（AW-130）。** `--apply` 现在无论如何都重写这两个文件：B 档为空、乃至一张待补记的单都没有，都落一份空清单——不写就等于让上一次的快照继续冒充现状。文件头（`docs/unmerged-exemptions.md`）与 `empty_means` 字段（`.json`）写明空是什么意思：此刻没有「`done` 但没有 `merges` 行、又没有 `head_sha`」的单，以前列在这里的历史单只有两种去向——证据补齐走 A 档，或已被 `archived` 终态覆盖；`archived` 不算 `done`，既不需要也不允许补合并记录。`auto_merge.py --list` 的「历史豁免」一栏也因此不再只在有在途单时才打印：没有待合并的单时它照打「历史豁免（0 张 …）」并说明这 0 张是怎么来的，清单若是旧快照（还列着 N 张而现场 0 张）则点明「多半已被 archived 终态覆盖」并给出刷新命令，两处永远说同一件事。这一栏之所以要单独修：以前 `list_unmerged` 为空时 `list_unmerged_rows` 整段提前返回，那份列着 51 张旧名字的清单没有任何一处被命令输出纠正，而它描述的单在 AW-128 收口时已经全部成为 `archived`。
- `get_ticket` 的 `merges` 与 `attempts` / `submissions` / `reviews` 并列，空数组表示还没有合并记录。

具体动作由 `tools/auto_merge.py` 执行，不靠人记得：

```bash
python tools/auto_merge.py --list                    # 每轮开工先看：done 但没有合并记录的工单（分「真待合并 / 历史豁免」两栏）
python tools/auto_merge.py --ticket AW-xxx           # 预演 → 测试 → --no-ff 合入 master → merge_ticket
python tools/auto_merge.py --ticket AW-xxx --dry-run # 只预演与跑测试，不碰目标分支、不写记录
python tools/auto_merge.py --ticket AW-xxx --record-only # 只用于「合并已成功、只差记录」：测试 → 补记录 → 清理
```

正常合并的第 1 步就判回执（`notices` 为空报 `NO_NOTICE`），而且判在任何写入之前；
`--dry-run` 只把这道判降成一条警告，好让预演真的能预演到底（冲突预演 + 临时 worktree
里那场全量测试），它不建回退点、不碰目标分支、不写合并记录。**预演能过不代表真合并
能过**：零回执的单预演会成功、真合并仍被挡在第 1 步。

约定：**每个工单一个 worktree，主检出永远留在 master**。执行会话开工时在仓库根建工作树，然后只在里面改：

```bash
cd {{WORKSPACE}}
git worktree add .worktrees/AW-0xx -b ticket/AW-0xx master   # 开工
# ... 在 .worktrees/AW-0xx/ 里改、提交、跑测试 ...
```

**先合并、后清理，顺序不能颠倒。** 删分支与工作树放在验收通过、协调会话合并成功（`merge_ticket` 写回记录）之后：

```bash
git worktree remove .worktrees/AW-0xx                         # 合并成功之后才做
git branch -d ticket/AW-0xx                                    # 同样在合并之后
```

`git branch -d` 只删已被合并的分支；分支还没合进 master 时 git 会拒绝（`error: the branch 'ticket/AW-0xx' is not fully merged`）。被拒就是提醒你合并还没成——不要改用 `-D` 硬删，那等于把这张单的成果从分支上抹掉，`tools/auto_merge.py` 之后连源分支都找不到。

主检出不切分支是有原因的：主检出停在 `ticket/AW-021` 时跑合并，`git merge --no-ff <源分支>` 会把 master 合进那条工单分支——方向反了，两个分支都被改，看起来还像一次成功的合并。所以 `tools/auto_merge.py` 的第 2 步先查「当前检出是不是目标分支」，不是就拒绝、打印 `git switch master`，并保证 `master` 与当前分支的 commit 都没变。

验收通过后由协调会话跑合并，执行会话不自己合，也不手动 `git merge`：

```bash
python tools/auto_merge.py --ticket AW-xxx
```

工具在临时 worktree 里先合一次并跑**这个项目的测试命令**，绿了才 `git merge --no-ff`；红了一律中止，此时目标分支一动未动。真实合并后还会再跑一次同一个命令，再红就 `git reset --hard pre-merge-<工单号>` 回滚——回退点在合并前就打好了。回退点已存在时看它指向哪里：指向**当前目标分支 head**（上一轮停在第 5 步测试红或第 6 步回滚）就沿用，直接重跑；指向别处才拒，不覆盖旧回退点（`AW-131`）。合并成功后的清理按主检出根算 `.worktrees/<工单号>`，`--repo` 指向工作树时也不拼成 `<工作树>/.worktrees/<工单号>`。

### 发布类工单的提交落在哪里

**一句话：先在 `ticket/<工单号>` 上提交，由 `auto_merge` 合入 master，由合并那一步推，最后才打 tag。**

发布单（发版、打包、改 `VERSION` 与 `CHANGELOG.md` 的那一类）跟普通单走同一条路，一步不少：

```bash
cd {{WORKSPACE}}
git worktree add .worktrees/AWK-0xx -b ticket/AWK-0xx master   # 开工
git -C .worktrees/AWK-0xx commit -m "AWK-0xx：发布 0.5.3"       # 提交落在工单分支上
python tools/auto_merge.py --ticket AWK-0xx                     # 预演 → 测试 → --no-ff 合入 master → 写合并记录
git -C {{WORKSPACE}} push origin master                         # 由合并那一步推，推进的是带合并提交的 master
git tag v0.5.3 && git push origin v0.5.3                        # 合并记录写成功之后才打 tag
```

**直接落 master 会让两条记录路径同时失效。** 把提交直接 `git commit` 在主检出上再推，看起来更快，代价是「这张单到底合过没有」在库里永远补不上：

1. **`auto_merge --ticket` 停在第 2 步。** 它要「源分支领先目标」，而改动根本没进分支，`rev-list --count master..ticket/AWK-0xx` 是 0，命令还没跑到测试就退出。
2. **`--record-only` 也拒。** 它要「源分支 tip == 被验收的 sha」，而分支停在上一个版本的提交上，这条等式不成立。2026-11-23 的 AWK-115 就是这个现场：分支停在 `d849bc9960f89711b75daa01c94fa295780d8b34`（0.5.1），被验收的是 `83e22f0b1be7e4bbb9f6bc5d136fbed200597f37`（0.5.2，直接提交在 master 上），两个 sha 都不是同一棵树（`git rev-parse d849bc9 83e22f0` 可核），收口因此一条记录也写不了。0.5.0 / 0.5.1 / 0.5.2 三张单都是这么补记的：只能由人在 `merge_ticket` 的 `manual` 档里写清「哪几步跑了、哪几步没跑」，而这一档每次都要人现场判断「这到底算不算合过」。

工具现在会当面说这件事：源分支不领先、而目标分支上已经有看着像这张单的提交时，`auto_merge` 第 2 步会打出「你可能把提交直接落 master 了」并点名那几个提交、指向本节；`--record-only` 则会点名「分支 tip」与「被验收的 sha」这两个完整 sha。看到哪一种都不是重试能过去的，要做的是把改动重新落在 `ticket/<工单号>` 上再合一次。

**打 tag 之前必须已经有一条 `merges` 行。** tag 与 Release 一旦推出去就收不回来，而 `merges` 是唯一能查「这一版是哪次合并进来的」的记录；先打 tag 再补记录，等于把「没合过」印在版本号上，事后只能靠 tag 的时间戳去反推。`git tag` 之后别急着推：先 `echo '{"project_id":"<项目>","ticket_id":"AWK-0xx"}' | python -m task_center --db {{WORKSPACE}}/.data/task-center.sqlite3 get_ticket`，看一眼 `merges` 非空。

**分支与工作树留到合并记录写成功之后再删。** 这是「先合并、后清理」在发布单上的样子：删早了 `auto_merge` 连源分支都找不到，那时能做的只有 `--record-only`，而它要求的等式已经被删掉的分支带走了。

### 别的项目怎么告诉闸门「我的测试怎么跑」

缺省是 `python tools/run_tests.py`（本工作区的约定，`ai-workspace` 与 `cindy-dashboard` 都有这一份）。没有这一份的项目在 `tools/merge-projects.json` 里按 `project_id` 登记一条，`cindy-harmony` 就是这么登记的：

- `test_command`：一条命令。字符串按当前平台默认 shell 跑（要写循环用这种），数组按 argv 直接执行。
- `test_glob` + `test_argv`：一个 glob 里的每个文件都用 `test_argv` + 文件跑一遍，第一个非零就停。

三种跑不起来分三句话，都不是「测试红」：`NO_TEST_COMMAND`（没登记，仓库里也没有 `tools/run_tests.py`，要人去配一条）、`TEST_COMMAND_MISSING`（登记了但第一段命令在本机没有，去查 PATH）、`TEST_GLOB_EMPTY`（glob 一个文件都没匹配到——空匹配退出码是 0，那等于没跑还说全绿）。

不改各项目自己的测试脚本，也不把缺省改成空跑：为了「支持别的项目」让缺省什么都不跑，等于闸门形同虚设。

### 被验收的树与要合的树

`done` 说的是「那次提交的成果通过了验收」，不说「现在要合的这一支还是它」。两者之间隔着一次「再提交一次」的机会：验收之后往分支上补一个提交，合并照样通过，而库里查不出来——R-008 那次合并两棵树刚好相同，是因为验收后没人再提交，不是因为闸门查过。所以现在两棵树各有一个可查的值：

- **被验收的那棵树**：`submit_result` 的 `head_sha` 写进 `submissions.head_sha`。验收会话拿它跟 `git rev-parse HEAD`（或分支 tip）核一次：这是它判「我验收的是不是这一支」的依据，核不上就不该 accept。
- **要合的那棵树**：`merge_ticket` 的 `reviewed_sha`。它与该工单**最新一条提交**的 `head_sha` 相等才写合并记录，不等报 `SHA_MISMATCH` 且一行不写。
- **git 一侧的比对在 `tools/auto_merge.py`，不在 `task_center`。** 中心库不跑 git，所以 `merge_ticket` 只能比库里的两个值；把分支 tip 读出来与 `head_sha` 比这件事，由合并工具在第 2 步做：`git rev-parse refs/heads/<源分支>` 与提交的 `head_sha` 不等就非零退出，输出里同时打出「被验收的 <sha>」与「现在要合的 <sha>」。这一步在打回退点、写 journal、建临时工作树之前，所以被拒时目标分支、回退点、journal、临时工作树与合并记录一样都没动。
- **两种处理方式。** 要么把源分支退回被验收的那个提交再合，要么让独立验收会话对现在的 tip 重新验收一次（提交一次带 `head_sha` 的成果，再 `review_result`）。不要在合并时换一个 sha 蒙过去：那个值是要留档的事实。

**老提交（`head_sha` 为 NULL）怎么合。** schema 9 之前写下的提交没有这一列，库里有 140 多条这样的提交与几十张已验收未合并的老单。它们不能静默放行（那等于把「没核对过」写成「核对过」），也不能全堵死（那样老单永远合不了），所以留了一个明文出口：`reviewed_sha` 传字面量 `unverified`，它按原样写进 `merges.reviewed_sha`，于是「这单没记录过 sha」在库里查得到。`tools/auto_merge.py` 遇到这种单会自己这么传，并在输出里打一条警告；有 `head_sha` 的单传 `unverified` 会被拒（INVALID_INPUT）——那是把一个可核对的事自称没核对。历史合并记录不追溯：已有的 `merges` 行 `reviewed_sha` 就是 NULL，它如实表示那时还没有这一列。

`--record-only` 也做同一道比对，它比的是那次合并合进来的那一支（`parent2` 指向的源分支 tip）：不等同样拒绝收口，由人决定这张单怎么算。它的拒绝理由会点名「分支 tip」与「被验收的 sha」并把两个完整 sha 都打出来——这种现场多半是落点错了（提交直接落在目标分支上），处置见上面的「发布类工单的提交落在哪里」。

### 合并动了 schema 的工单：先对齐库，再写记录

`auto_merge` 的顺序是「合并 → 跑测试 → 写记录」，而写记录用的是**合并后的代码**。碰到改 `SCHEMA_VERSION` 的工单，那一刻磁盘上的代码已经是新一代、中心库还停在上一代：`merge_ticket` 直接 `SCHEMA_MISMATCH`，迁移只能人工补。更隐的一种是同一步看起来成功了——进程里那份 `task_center` 是 `auto_merge` 启动时载入的旧模块，它按旧版本给自己和旧库都放行，而磁盘上的代码已经换了代。同一个动作两次不同结果：失败会喊，看起来成功只会把不一致推给下一步。

所以第 7 步写记录之前多一道**schema 闸门**，它是 `auto_merge` 的一部分，不是新的库操作：

1. 读合并后仓库里 `task_center/db.py` 的 `SCHEMA_VERSION`，与库的 `PRAGMA user_version` 比一次。两边一致就往下走。
2. 代码比库新：先在**文件层**做快照（库文件旁边一份 `<库名>.pre-migrate-<旧>-to-<新>-<时间戳>`；`backup` 操作对旧库本身就会报 `SCHEMA_MISMATCH`，所以只能这么做），再用合并后的代码在短命子进程里对正式库跑 `migrate`。
3. 迁移成功后把 `task_center` 按磁盘重新载入，写记录用新的那份；迁移失败就停在这里。
4. 之后才是 `merge_ticket`。

闸门报的状态名（`python tools/auto_merge.py` 输出里单独一行「状态：…」）：

| 状态 | 含义 | 已写合并记录？ | 怎么办 |
| --- | --- | --- | --- |
| `SCHEMA_AHEAD` | 代码领先库，正在迁移（不是失败） | 还没有 | 正常进行；迁移成功后输出快照路径 |
| `SCHEMA_MIGRATE_FAILED` | `migrate` 没把库升到目标版本 | 没有 | 按 migrate 输出修迁移，再用 `--record-only` 收口（同一道闸门会再跑一遍）；代码不回滚 |
| `SCHEMA_SNAPSHOT_FAILED` | 快照写不进去，于是不动库 | 没有 | 先让库文件所在目录可写，再 `--record-only` |
| `SCHEMA_DB_AHEAD` | 库比这次合并进来的代码还新 | 没有 | 多半合错了分支；核对 `git log` 与 `git show HEAD:task_center/db.py`，库不会为合并降级 |
| `SCHEMA_RELOAD_MISMATCH` | 重新载入的 `task_center` 与合并后代码版本不一致 | 没有 | `TASK_CENTER_CODE` 与仓库里的代码统一后 `--record-only` |

迁移失败那一档现场是「合并已在目标分支上、库还是旧代、`merges` 一行都没有」，所以恢复手段是 `--record-only`，不是重跑整个合并（重跑会被第 2 步的「源分支领先」拦住）。此时要保留的是那次合并提交，要做的是修迁移——工具不会替你把合并撤掉。

预演与 `--dry-run` 不碰这一步：它们本来就不写合并记录，也就不该动库。库落后于代码时
`--dry-run` 会直接停住并指向 `python -m task_center --db <库> migrate`，而不是带着旧库跑预演。

同一道闸门在**读库之前**还会先跑一次预查：`--record-only` 收口那种现场（代码已升、库还在
旧代、`merges` 没有行）里，按新代码打开中心库是 `SCHEMA_MISMATCH`，连工单都读不出来。
所以它先快照、`migrate`、重载模块，再去读工单；这一步在取锁之前，因为「按代码读库」
（项目归属反查）比锁还早。两个并发合并一起预查也不会撞：`migrate` 在一个事务里复检
`user_version`，第二个看到的是「已是最新」。

### 合并断在半路：`--record-only`

一次真实合并在「合并提交已经落到工作区」与「记录已经写进库」之间隔着两场全量测试，进程被杀就停在那一段。**这种现场默认路径一步都走不动**：第 2 步要「源分支领先目标」，而源分支已经被合进去了，领先数正好是 0——命令连测试都跑不到，人只能手工去比对 git log 和库。

**「`done` 但没有 `merges` 行」有两种，不要用同一条命令。** ①合并已经成功、只差记录：分支已全在目标里，用 `--record-only`；②合并根本没发生（真失败：分支还领先目标，或冲突预演 / 测试红过）：走正常路径 `--ticket`，先在分支上把问题修好再合。`--list` 的结尾会把这两句打出来。判据就是下面那条反向判据：它成立是①，不成立是②。

```bash
python tools/auto_merge.py --ticket AW-058 --record-only
```

它和正常合并共用第 0 步的锁、第 1 步的工单门（必须 `done`、没有合并记录，否则报 `ALREADY_MERGED`）与第 2 步的检出检查（当前检出在目标分支上、已跟踪文件无改动、目标与源分支都在；收尾包那三个路径对「无改动」是唯一的例外，见 `docs/closing.md` 的「合并放行收尾包，不代它提交」），只把「源分支领先目标」换成**反向判据**：`git merge-base --is-ancestor <源> <目标>` 成立，且 `rev-list --count <目标>..<源>` 为 0。这一步不预检未跟踪文件：这次不往工作区写任何路径。

接着三件事：

1. **找到那次合并提交。** 在目标的祖先里按 `parent2 == 源分支 tip` 逐行比对 `git rev-list --parents <目标>`。找不到（当时是快进合并，根本没有合并提交）就非零退出、不写记录——记录里的那个 `commit` 必须是真合过的那一个，不能拿 HEAD 顶替；输出里同时给出要找的分支 tip 与目标分支当前的提交。
2. **在当前检出跑一遍全量测试**，绿了才写记录，`tests` 就是这次实跑的汇总行。红就非零退出，不写记录、不清理工作树与分支；回退点写在输出里：`pre-merge-<工单号>`，没有这个 tag 时就是合并提交的第一个父提交。
3. **记录写成功之后清理**：`git worktree remove .worktrees/<工单号>`（存在才删）、`git branch -d ticket/<工单号>`。清理失败只报告、不改退出码——那时该做的是人工收拾，不是重跑（重跑会被第 1 步的 `ALREADY_MERGED` 挡回来）。工具不会 `--force` 删工作树：里面还有未提交改动时，该保留的正是那部分。

正常合并路径还会写一份 `.data/merge-journal/<工单号>.json`（`commit` / `target_branch` / `source_branch` / `tag` / `started_at` / `step`）：合并提交落到工作区之后、合并后测试之前写一次（`step: merged`），测试绿了更新成 `tests-passed`，`merge_ticket` 成功删掉；测试红回滚之后也删掉，删前把 `step` 改成 `rolled-back`；schema 闸门迁移失败时改成 `migrate-failed`。它只解决一件事：进程被杀之后，「这次合并做到第几步」不用再从锁文件和 git log 推。

还有一类阻塞**预演看不见**：主检出里存在与源分支入站文件同名的未跟踪文件。`merge-tree` 预演只算两棵树能不能合，这是工作区的事，所以预演会照常通过，只有真合那一步才失败，报的是 `error: The following untracked working tree files would be overwritten by merge:`，末尾一行 `Merge with strategy ort failed.`。遇到它不要删别人的文件，按下面「修订文档时的自查」第 ③ 条先比对两份清单再挪开。

### 同一时刻只允许一次合并

两个协调会话同时跑合并时，两边都会走过「目标分支没动 → 打回退点 → 真合」，后一个的回退点就指向已经被改过的 master，于是「红了回滚」会把前一次的合并一起抹掉。所以合并本身也要互斥：

- 锁的位置是 **git 公共目录**（`git rev-parse --path-format=absolute --git-common-dir` 指到的那个目录下的 `auto-merge.lock`），也就是**共享仓库根**：主检出（`.git` 是目录）与任一工作树（`.git` 是文件）解析到同一个绝对路径，**共用一把**。按检出根各持一把时，工作树里放着活锁也拦不住主检出的合并，两把互不拦（AW-029 实测）。锁里记 pid、开始时间（UTC）、工单号；按仓库互斥，不同仓库的合并不互相排队。拿不到锁的处置照旧：第 1 步之前非零退出、点名占用者，不要手工删锁。
- 拿不到锁时 `auto_merge` 在第 1 步之前就以非零退出，输出的是占用者：**工单号 / pid / 开始时间 / 已持续多久**。看到它别接着试，先按占用者去查——它可能正在跑两次全量测试，也可能正要写合并记录。
- **一个顺序上的例外**：取锁并不是 CLI 做的第一件事。第 1 步的项目归属校验（`open_center_for_project`）先跑，之后才轮到第 0 步取锁。所以拿一个非 git 且未登记的路径跑合并，先报的是「不属于任何已注册项目」，还走不到锁那一步；上面「第 1 步之前非零退出」因此要读成「第 1 步的工单状态校验之前」——归属校验已经过了，锁还没拿到。
- **不要手工删锁。** 删掉一把活着的锁，等于允许两次合并同时改目标分支，而这正是锁要防的事。持有者正常结束或报错都会自己释放（释放放在 `finally` 里）。
- 锁不永久残留：pid 已不在进程表里，或已持续超过 2 小时，下一次运行会把旧锁当残留接管，并在输出里写明接的是哪一把（工单、pid、开始时间、判为残留的原因）。所以进程被强杀、机器重启都不会让合并永久停摆。
- 被接管过的旧持有者不会删掉新持有者的锁：释放前核对文件里的 pid 是不是自己。

```text
合并中止 · 第 0 步 · 合并互斥锁
原因：另一次合并正在跑：工单 AW-026 / pid 43120 / 开始于 2026-11-17T09:12:03Z（已持续 3 分 12 秒）。
怎么补：等它结束再跑（正常结束或出错都会自己释放）……
```

### 合并提交靠 MERGE_HEAD 过钩子

合并发生在验收之后，所以这一次提交时工单已经是 `done`，而 `tools/work_gate.py` 的普通规则只放行 `running`——只按 running 判，每一次合并都会被自己的钩子拦下。合并提交的识别方式是**仓库里存在未结束的合并**（`.git/MERGE_HEAD`；worktree 里 `.git` 是个文件，要沿 `gitdir:` 指到的目录找，工具已经这么做了）：

- 放行：`MERGE_HEAD` 存在 + 工单 `state=done` + 该工单**没有**合并记录 → 输出「合并提交：<工单号>」。
- 其余一律照旧：普通提交仍需 `running` + 本会话已绑定；`ready` / `dispatching` / `rework` / `awaiting_review` 即便检出停在半合状态也拒绝；已记过合并的工单再出现合并提交报 `ALREADY_MERGED`。

「刚跑过一次 `git merge` 卡在那里」不能当成通行证：判定不看人怎么说，只看这三条同时成立。

合并动作本身（在临时 worktree 里预演、跑测试、红了回滚）不属于这张表；记录只说明「谁在什么时候报告了哪次合并」。

## 只读观察面：`work_gate --list` 与 `project_doctor`

这两条命令**只读**：不建库、不写工单、不改任何仓库的 `git config`。它们回答的是两个在库里看不出来的问题——「谁在等谁、等了多久」和「哪个项目其实没装钩子」。

### `python tools/work_gate.py --list`

表格是六列，按制表符分隔：

```text
工单号\t项目\t尝试状态\t执行会话\t尝试开始时间\t最后事件时间
```

后两列的时间来自 `attempts.created_at` 与这张单 `events` 的最大 `created_at`（一条事件都没有时用尝试开始时间兜底），都是带时区的 ISO 8601，能直接算间隔。stdout 只有这张表：汇总行「共 N 张在途，其中 M 张最后落库超过 45 分钟」与停住判红都走 stderr，所以按行解析不会被最后一行绊倒。

- `--json`：同一份清单打成一整块 JSON（`result.items` 每条都带 `started_at` 与 `last_event_at`，另有 `stalled_count` / `stalled_minutes` / `stalled`）。stdout 上不会再有表格行。
- `--stalled-minutes N`：给定额外阈值并按它判红。有在途单最后事件时间超过 N 分钟时打印「疑似停住」并以退出码 1 结束；没超过退 0。不传这个参数时只统计（汇总行照旧按 45 分钟报数）而不判红：这条命令同时是「开工前查有没有人在做」，它一旦因为别人在跑就恒为 1，钩子和日常流程会把它当噪声跳过。

### `python tools/project_doctor.py`

对每个已登记项目查三项：本仓库的 `core.hooksPath` 是否指向该仓库自己的 `.githooks`、`.worktrees/` 是否被 git 忽略、仓库根有没有 `AGENTS.md` 或 `CLAUDE.md`。任何一项不成立就退出码 1，并在那一行后面跟一句补法；全齐退出 0。非 git 项目只报「不是仓库，不检查钩子」与 `.worktrees` 不检查，不算失败（`AGENTS.md` 照旧要查）。

```bash
python tools/project_doctor.py                  # 查所有已登记项目（按 center.json / TASK_CENTER_DB 找库）
python tools/project_doctor.py --db <库路径>     # 指定中心库
python tools/project_doctor.py --root <目录>     # 只查这一个目录，不需要库
python tools/project_doctor.py --json           # 机器可读：result.projects 每项带 problems 与 fix
```

补法是**打印**出来的，脚本不替你执行：改别人的仓库配置与「体检」是两件事，混在一起就没人敢在业务项目上跑它。所以它跑完之后，缺项的项目的 `core.hooksPath`、`.gitignore` 一字未动。

## 恢复、版本与竞争

`revision` 是工单执行代次，不是 PRD 文档里的人工版本号，也不是判据版本。只有 `recover_ticket`、`cancel_ticket`、`revise_ticket` 传 `restart: true`（或重开已完成工单）这三条路递增；**普通 `revise_ticket` 改判据不递增**；领取与提交同样不递增，通过当前 attempt_id 排除旧尝试。**`archive_ticket` 也不递增**：归档不换代次、不作废尝试、不换人，`revision` 原地不动（AW-128）——所以一张单“第三次被归档”这种写法在库里不可能成立，第二次就已经是 `INVALID_STATE`。

`criteria_version` 是另一个数，只管一件事：验收条件改过几次。`revise_ticket` 每次真的改了 `criteria` 就加 1，工单与当次尝试各有一列——尝试上的那一列记的是「这一次尝试是按哪一版判据派发的」，`revise_ticket` 不追改它，所以 `submit_result` 的 `criteria_changed` 才判得出「判据在开工后变过」。两个数分开的理由见 PRD-013 R6：以前改一次判据就把在飞的尝试 supersede 掉，执行会话拿着过期标准继续干，而验收只看得到 `revision` 跳了。

PRD 内容在领取、绑定、提交和验收时重新核对。变更返回 STALE_PRD；使用 revise_ticket 明确更新，不能只改标题后继续提交旧结果。每次领取会把当时的 PRD 路径与摘要快照写入 attempts，因此"哪次尝试依据哪份需求"可直接查表。

同一 session 不能同时绑定多个 running/submitted/blocked 尝试（`blocked` 算活跃：等答案的会话不能再领第二张单）。验收拒收后释放旧尝试，可为同一会话创建新的尝试。失败记录和旧验收保留。

### 同项目一次只跑一张单

`claim_ticket` 在状态检查之后查同项目其他工单：只要有 `dispatching` / `running` / `awaiting_review` / `blocked` 之一，就报 `DISPATCH_BUSY`，消息里写出那张单的编号与状态。理由是节奏：并行派发时「谁在等谁」只看会话列表看不出来，而 2026-11-18 核对时更早的 55 张单里已有 76 对时间窗重叠（AW-046 与 AW-047 相隔 2 秒领取）；具体条数以库里 `events` 为准，这里只当当日的事实举例。这条是库里的默认路径，不是让人记得的叮嘱；撞上就会当场拒绝，并告诉你是谁占着。

四个在途状态的口径：

- `dispatching`（已领取、还没绑定）与 `running` 显然算在途。
- `awaiting_review` 也算：验收没结束之前这张单的成果还在改，此时开下一张会让同一批文件被两处修改。代价是「验收完、合并前」也不能开新单；合并本来就是验收后的下一步，不该横插一张单。
- `blocked`（在等一句话）也算：它还占着一条活跃尝试，答案回来就要接着跑。

不算在途的：`done`、`cancelled`、以及还没被领取的 `ready` 与 `rework`。**`rework` 不是免排队通道**——一张单被拒收回到 `rework` 之后释放了项目，别人可以领；但它自己复工时同样排在别人的在途单后面。

要并行就显式开口：`allow_parallel: true`（只收布尔，`1` 与 `"true"` 都报 `INVALID_INPUT`）。越过时 `claim_ticket` 往 events 写一条带 `allow_parallel_override` 的记录，回执里的 `parallel_with` 列出被越过的那几张单——事后看事件表，能分清这是故意的还是漏了检查。串行的范围是项目：两个项目各自推进，互不排队。

`recover_ticket` 只能在核对旧会话、成果和副作用后由协调者明确调用。它不提供对外操作的 exactly-once 保证，也不证明旧进程已停止。真实派发回执未知、失联、任务超时的自动恢复属于后续阶段。

### 改工单自己引用的 PRD 要显式 revise_ticket

工单的 PRD 摘要在领取、绑定和提交三处都会重新核对：`prd_path` 指向的那份文件改了、单据却没改，提交的成果就与快照对不上，必然 `STALE_PRD`。所以问题不在「能不能改自己引用的 PRD」，而在必须同时把工单的摘要换成新文件的摘要。两条合法路径：

1. **先改文件，再 `revise_ticket`**：`revise_ticket` 会用当前文件重新指纹 `prd_path`、改写 `tickets.prd_sha256`，**同时换掉在飞尝试上的那一份**（`attempts.prd_path` / `prd_sha256`）——`STALE_PRD` 比的就是尝试上那一份，只改工单行等于没改；要换一代人做就同时传 `restart: true`（它才让旧尝试失效、`revision` 加 1），然后在新的尝试上重新领取、绑定、提交。不传 `restart` 时旧尝试照旧在飞，接着提交即可。
2. **由协调会话先把 PRD 写成新版本号**：再把新版本引用进工单，`deliverable` 里只写这张工单真正要改的其他文件。适合需求本身变了的情况，旧版本引用的工单随之失效。

禁止的只有一条：**改了 PRD 却不动工单就去提交**。

三条真实路径：

- **AW-005（路径 1，成功能过）**：`deliverable` 要求改 `PRD-001` 里的错字，做法是先 `revise_ticket` 重取摘要（revision 1 → 2，reason「改字前先落到工单：PRD 内容随之变化，按 STALE_PRD 口径显式修订并重新快照」），再领取、绑定、提交，验收通过。
- **AW-010（只能删要求）**：`deliverable` 写了「PRD 同步一句」，而它的 `prd_path` 就是 `PRD/PRD-001-Cindy会话工作体系.md`；那张单的派发指令明确禁止执行会话碰 PRD，于是没有合法改法，只能用 `revise_ticket` 删掉这句才提交（revision 2 → 3）。
- **AW-008（路径 2）**：要改的是 `AGENTS.md` 与本文档，PRD 0.10 由协调会话先写成需求、与同一张工单一起提交（C3），执行会话从头到尾没碰 PRD。

两条历史记录留在原处、不追溯：AW-004 的 C3 与 AW-005 的 `deliverable` 都在要求里点到了 `PRD-001`。它们已按当时的口径走完（两者都在 `revise_ticket` 里重取过摘要，AW-004 的 `prd_sha256` 从 `c6978886…` 变成 `0ebc3d57…`），而本条规则只约束此后新开的工单；存量的正确处理是留着并在这里点名，不是回头删改历史。

写条件时当场自查一次：要求里出现的文件名（含 `PRD-001` 这类编号简写），和这张工单的 `prd_path` 是不是同一个文件。是的话，要么在工单里明确写上先 `revise_ticket` 这一步，要么把这条要求挪到协调会话的需求变更里。

### 升级旧库的顺序

快照 → `migrate` → 校验 → 删旧快照，顺序不能颠倒。`backup` 与读取共用同一套 schema 校验，在 schema 3 的库上直接返回 `SCHEMA_MISMATCH`，所以升级前的那一份只能在文件层做：用 SQLite backup API（库开着也安全），或停写后复制文件连同旁边的 WAL/SHM。具体命令：`sqlite3 <源库> ".backup <目标文件>"`（`sqlite3` 的 `.backup` 就是 SQLite backup API 的命令行形式）；或停写后把库文件连同旁边的 `-wal` / `-shm` 一起 `copy` 走——只拷主库会丢掉还没落盘的写入。

校验包括三件：`migrate` 回执的 `from_version` 是升级前的版本、`migrated: true`、`schema_version` 是当前版本；`list_tickets` / `list_projects` 还能看到原有记录；升级后 `backup` 能成功（它只接受最新 schema，成功即证明库已经是最新）。全部通过后再删旧快照。升级链是逐级执行的（3 → 4 → 5 → 6 → 7 → 8 → 9 → 10），从 v3 直接升到 v10 也一样，不需要先单独升到 v4 或 v5。

### 改 schema 的落地顺序

下面这个顺序是 2026-11-17 一次真死锁换来的，不是偏好。少一步或者把两步并成一步，都会停在一个两边都对不上的状态里：

1. **副本上验证升降级。** 用 SQLite backup API 做一份副本，在副本上跑 `migrate`，看回执的 `from_version` / `migrated` / `schema_version`，再 `list_tickets` / `list_projects` 确认原有记录都在。**真实库在这一步一步不动**：升级链第一次跑在真实库上，等于把唯一的现场当试验品。
2. **代码先合进 master。** 升级链写在代码里，代码没进 master 就升真实库，master 上那份 `tools/work_gate.py` 会拿旧版本去读一个新版本的库。闸门跑的是代码所在的分支，不是你脑子里的分支。
3. **合完在 master 上跑 `migrate`。** 用合入后的 master 代码对真实库执行，记下回执的 `from_version`。**这一步是合并之后协调会话的半步，不属于某张执行工单的交付物**：执行尝试跑在 `ticket/<工单号>` 分支上，它既不该碰真实库，也证明不了合入后的代码与它测过的那份一致。
4. **校验。** `migrate` 回执 `migrated: true` 且 `schema_version` 是当前版本；`list_projects` / `list_tickets` 看得到原有记录；升级后 `backup` 能成功（它只接受最新 schema，成功即证明库已是最新）；`python tools/work_gate.py --list` 能正常列出，不报读库失败。
5. **删快照。** 前四步全过才删升级前那份副本。没校验就删，等于把唯一的退路换成一句「应该没问题」。

**两个方向都不宽，所以中间的空窗必须当轮关掉。** schema 校验是等号，不是「大于等于」：v7 代码读 v6 库报 `SCHEMA_MISMATCH`，v6 代码读 v7 库同样报 `SCHEMA_MISMATCH`。于是「代码合了、库还没升」这段时间里，`work_gate.py`、`list_projects`、`backup` 全读不了库——而 `--list` 正是「开工前先查有没有人在做」那条命令。所以第 2 步和第 3 步要在同一个合并窗口里做完：合并之后立刻在 master 上升级，不留着过夜，也不在这中间派新的执行会话。快照是为了能退，不是为了让空窗留着。

**升级中途读不了库时，只允许一种绕法。** 合并进行中主检出的钩子读不了库，唯一允许的做法是显式把钩子指到某个能读库的工作树：

```bash
git -c core.hooksPath=<某工作树>/.githooks commit -m "..."
```

并且要在提交说明里写清为什么。**不允许临场发明别的绕法**：`--no-verify`、临时把 `TASK_CENTER_DB` 指到另一个库、把工单状态改成能放行的样子，都是把「闸门当场失败」换成「闸门永远不知道」，比绕过去更贵。这条只是那一瞬间的例外，不是常态。

**两张单同时把版本推到同一级时，上面五步不变，只多一条让路规则。** 后落地的那张顺延一级：版本号 +1、迁移键 +1、冻结形状与迁移用例名跟着 +1；先进 master 的那张一个字都不改。判断要不要顺延只有一条命令：读 master 上 `task_center/db.py` 里那一行版本定义，数等于你分支上写的数就是它（命令与三种读法在 `docs/schema-shift.md` 第一节）；做法的完整版也在那张卡，现场演练在 `tasks/drill-AW-118-schema-shift.md`。没有这条让路规则时，`auto_merge` 会停在「第 3 步 · 冲突预演」——它不替你决定两个键该留谁。

**`claim_ticket` 时传 `coordinator_workdir`。** 不传不报错、也不阻止派发，所以很容易漏；漏了之后 `attempts.coordinator_workdir` 是空值，`get_ticket` 只剩一个 `coordinator_session_id` 能看出「谁派的」（AW-051 就是空着交的）。同一类漏法还有回信落库：没人调 `record_notice`，那张单的 `notices` 就一直是个空数组，交接闭环走没走完事后无法从库里判断。协调会话在 `claim_ticket` 里把自己的工作目录原样填进去，发完回执再补一条 `record_notice`。

文件摘要核对是读取时刻的检查，源文件仍可被其他进程修改。第一版面向可信协作，不把项目路径或会话字符串检查宣传成防恶意操作的安全边界。

## 常见错误

- DB_NOT_INITIALIZED：数据库不存在，核对唯一中心位置，不自动新建备用库。
- PROJECT_MISMATCH / SESSION_MISMATCH：核对项目或会话绑定。
- ROOT_MISMATCH：给的 root 与已登记的不一致（同一个 project_id 指向第二个目录，或 root 已属于另一个 project_id）。root 是身份，改不靠开单，先 `unregister_project` 再重登；这次调用不改任何已有记录。（只是显示名写错了不走这条路：用 `rename_project`，有工单的项目撤不掉。）
- STALE_REVISION / STALE_ATTEMPT：读取当前状态，不能提交旧执行结果。
- STALE_SUBMISSION / ALREADY_REVIEWED：`review_dispatched` 只认最新那条提交，且已经判过的提交不再派第二个验收人；按 `get_ticket` 的 `submissions[-1]` 与 `reviews` 重读一遍再标。
- STALE_PRD / ARTIFACT_CHANGED：需求或成果改变（成果被删除或移出项目也统一报 ARTIFACT_CHANGED）；明确修订或核对后恢复再提交。
- REQUEST_CONFLICT：请求编号被用于不同参数，查清原请求，不随意换编号重做副作用。
- CONFLICT / INVALID_STATE：任务已被领取、已提交、已结束，或存在唯一性冲突。
- SELF_REVIEW：换用没有执行过该工单的独立验收会话。
- SELF_DISPATCH：协调会话不能执行自己派发的工单，新开一个会话来做。工单仍停在 dispatching，换个 session_id 重新 bind_session 即可。查一条会话到底绑在哪些单上（没绑会打印 `NOT_BOUND` 并非零退出）：`python tools/session_roster.py --session <id>`。
- STORAGE_ERROR：保留原请求编号和参数，查明数据库问题后再重试。
- NOT_TARGET：`target_session_id` 不是该尝试 claim 时写下的协调会话。回执发错了地方比没发更难发现，所以写成执行会话自己或其他会话都不允许，且一行不写。
- NOTIFY_CREATED_TASK：回执的 `wake_kind` 是 `created`——宿主把它当成了新建任务，这条回执没有到达任何人。命令非零退出，但库里已经留了一条 `failed`；先停掉那条误建的任务，再带上 `target_session_id` 重发。
- NO_NOTICE：这张单没有任何回信记录，合并不进去。补记一条 `sent`，或补一条 `failed` 写清原因，再合并。协调会话已经归档（`python tools/coordinator_state.py --ticket <单号>` 报 `TAKEOVER_NEEDED`）时按「协调会话归档之后」那节走：执行会话记 `report_notice_failure`，或新会话先 `take_over_ticket` 再补记。`tools/auto_merge.py` 在正常合并路径上把这道判**提到了任何写入之前**（第 1 步收尾），所以忘补回执时是「一步都不动」地被拒，而不是 `git merge` 已经落进目标分支才在写记录那步失败；那种现场才需要 `--record-only` 收口。
- SHA_MISMATCH：要合的那棵树与被验收的不是同一个提交——`merge_ticket` 收到了不等于最新提交 `head_sha` 的 `reviewed_sha`。验收之后分支又变动过了，要么退回被验收的提交，要么重新验收现在的 tip；不要换一个 sha 蒙过去。`tools/auto_merge.py` 在真合并之前自己会先比一次，被拒时输出两个 sha。
- ALREADY_MERGED：这张工单已有合并记录，读取它而不是再写一条；要改记录内容得先由人核对实际合并情况。
- NOT_COORDINATOR：只有派发这张单的协调会话能代记结论或回信结果（`answer_ticket` 的代答、`record_notice`）。换一个会话不能替代；请协调会话自己来写。协调会话已经不在时不要冒用它的 id，走 `take_over_ticket` 显式接替（见「协调会话归档之后」）。
- ALREADY_OWNER：`take_over_ticket` 的 `new_owner_session_id` 就是这张单当前的 owner，没有东西可接替；一行不写。
- TAKEOVER_BY_EXECUTOR：`take_over_ticket` 的 `new_owner_session_id` 是这条尝试的执行会话。执行会话不能给自己接替责任人（那等于自派自做），换一个会话来做；一行不写。
- ALREADY_NOTIFIED：这张单已经有一条 `sent` 回信记录，读它的 `notices` 而不是再写一条；`failed` 不受此限，可以逐次记录。
- DISPATCH_BUSY：同项目还有在途工单（`dispatching` / `running` / `awaiting_review` / `blocked`），消息里点名了是哪张单、停在哪一步。它先做完再领下一张；确实要并行就显式传 `allow_parallel: true`，并写清 `parallel_reason`，越过会在 events 里留一条带事由与范围的记录。
- BATCH_OWNED_BY_OTHER：这张单的车次在别的会话手里（`batches.owner_session_id` 不是本次 `coordinator_session_id`），消息点名车次号与持有会话，一行不写。交回持有那个车次的会话，或另开车次。**它排在 `DISPATCH_BUSY` 之前**：同一现场两个条件都满足时报这一条。
- BATCH_PROJECT_MISMATCH：`tickets.batch_id` 指向的车次属于别的项目（脏行），`message` 点名工单、工单项目、车次与车次项目。逐列核对那一行，把 `batch_id` 改对或清空，不要在原地「先用着」。
- SCHEMA_MISMATCH：目标文件不是本工具的数据库，或版本不在已知升级路径上。init、migrate、读取与 backup 做同一套表/列校验，任意外部库不会被当成有效快照；也因此 backup 不能给旧库做快照——升级前需要一份快照时，先在文件层做（SQLite backup API，或停写后复制文件），再 migrate，旧库升级不自动迁移。`tools/work_gate.py --list` 读不了库时也打这个码并非零退出，不再打印一张空清单冒充「没有人在做」。
- BACKUP_EXISTS / INVALID_INPUT：备份目标已存在或不是可写绝对目录。目标目录必须支持硬链接（FAT32、部分 SMB/NFS 不支持）。
- CRITERIA_NOT_JUDGEABLE：`criteria` 里有一条既没有引号包裹的可执行命令（动词在 `python` / `git` / `grep` / `ls` / `find` / `wc` / `node` / `npm` / `bash` 名单内），也不以 `判断类` 开头并写出反例。回执点名是第几条；把条件改成这两种写法之一，这张单一行都不会落库。
- PARALLEL_REASON_REQUIRED：`allow_parallel: true` 却没有 `parallel_reason`（或它不是 1–200 字的非空字符串）。工单停在原状态，没产生尝试；补上「为什么这两张单可以同时跑」再领。

## 修订文档时的自查

改 `AGENTS.md`、`PRD/`、本文档、`tasks/` 下的计划，或者写返工消息与派发消息之前，先跑下面三条。它们不是原则，是三个当场能判的动作：每条都写成「什么时候用 / 怎么做 / 半修长什么样」，例子全部来自 2026-11-17 一天里被验收拦下的实例。

### 一、改一处旧话必须全文搜同类说法

**什么时候用。** 你要动的是一个说法里的某个词、某个范围、某个结论——「1–7 节一字未改」「不动第八节」「合并记录 9 条」「28 个节点」这一类。这种说法在一份文件里很少只出现一次：开头摘要、范围自述、变更记录、验证命令、另一份文件对它的引用，各有一份。改完一处，其余几处不会自己跟着变。

**怎么做。** 改之前和改之后各跑一次全文搜，搜的是**被改掉的那个说法本身**，不是你这次动的那个文件：

```bash
cd {{WORKSPACE}}
grep -rn "被改掉的那个说法" docs/ AGENTS.md PRD/ tasks/ README.md
# 拿不准时就整个仓库搜，只留文档类
grep -rn "<说法>" . --include=*.md
```

判据只有一条：**命中位置必须和你改过的地方对得上**。搜出 3 处只改了 1 处，剩下 2 处就是新的假话。范围宁可放大到整个仓库也不要只搜「我改的那个文件」——假话往往就活在你没打算搜的另一份文件里，而且它挨着一句刚改对的陈述，比原文更难发现。

**半修长什么样。** 只改被点名的那一句。今天有两次：

- **AW-031 的范围句。** 变更记录写「1–8 节一字未改」，而同一版改了 §5 里的 AC01 与 AC08，§5 就在 1–8 里面，句子当场自相矛盾。第一次只把 `8` 改成 `7`（`a77a1ff`），§5 照样在改动里，句子照样假；第二轮回炉（`b094aaf`）才换成能对着 diff 逐条数的写法：「本次共 6 行新增 / 5 行删除，其中表头版本行 1 行、§5 AC01 与 AC08 共 2 行、§8 两行、本节新增 1 行；1–4、6–7 节与附录 A 零改动」。核这一条：`git show a77a1ff -- PRD/PRD-005-决策通道.md | grep -n "一字未改"` 打出的是「1–7 节、附录 A……一字未改」。
- **AW-037 的归因句。** `tasks/plan-008` 里写「写这段时 11 条，AW-035/036 合进来后 13 条」，可 AW-036 当时并没有合并记录，那个因果是补出来的。回炉（`e72d3dd`）后换成「数字 + 核对时间 + 核对命令 + 逐行拆解」。核它：`grep -n "13 条" tasks/plan-008-过期表述清零.md`。

### 二、数字、条数、哈希、标识必须带核对时间与核对命令

**什么时候用。** 要往文档、计划或消息里写一个数字（条数、行数、节点数、测试数）、一个提交哈希、一个 session id / attempt id / submission id / review id，或者一句「现在是什么状态」。

**怎么做。** 每一个都写成「数字 + 核对时间 + 核对命令」，命令要能让下一个人原样跑出同一个值：

```bash
cd {{WORKSPACE}}
# 条数：merges / tickets 是活表，现在的行数不是当时的行数
python -c "import sqlite3;c=sqlite3.connect('{{WORKSPACE}}/.data/task-center.sqlite3');print(c.execute('select count(*) from merges').fetchone()[0])"
# 某张工单有没有合并记录（询因果之前先问这个）
python -c "import sqlite3;c=sqlite3.connect('{{WORKSPACE}}/.data/task-center.sqlite3');print(c.execute(\"select count(*) from merges where ticket_id='AW-036'\").fetchone()[0])"
# 哈希与分支
git rev-parse --short HEAD; git rev-parse --abbrev-ref HEAD
# 行号与说法在全文的出现位置
grep -n "<说法>" docs/protocol.md
```

写不出核对命令的数字就是「未核」，不是近似值。写「未核」比写一个裸数诚实，也便宜得多：裸数以真值的样子进库、进消息，下游会拿它推因果，改起来要两三轮。同理，会话标识只有两个合法来源——自己用 `cindy_helper` 的 `call_tool` 调 `get_current_session_id` 现取，或从协调会话的创建回执里原样照抄——两者都不允许凭印象写。

**时间和输出必须来自同一次执行，而且要从最终文本上取。** 接到一个数字，就要同时接到算它的那次输出：把 `date -u` 与产生这个数的命令写进同一条命令（或同一个 bash 调用），两块输出原样贴回。判据四条，缺一条就算没核：① 只写了时间、没贴那次执行的输出；② 时间晚于你把它写进文档或提交的时刻——那说明这个时间不是跑出来的；③ 时间与数字取自不同的两次执行；④ **跑完之后又改过这份文件**——插行、加词，或者命令的范围包含本文件自己——**没重跑就算没核**，证据必须取自最终文本。本单 2026-11-17 栽过这几种：把当时还没到的未来时刻写成核对时间（提交时刻早于它）；示例的命中数随文档自身的修改而漂移（命令范围把本文件自己也算进去了）；按上文取的行号贴出来之后又在它上方插了两行，没重跑，终稿里那些行号已经指错地方——三条判据逐条对照都能过，数还是过期的。前两种是「时间和数不是一起拿到的」，最后一种是「数取自终稿之前的执行」。所以第 ① 条那句「改之前与改之后各跑一次」在这里同样管用，而且要把「之后」理解为最后一次改动之后。合格的样子就在本文档第 ③ 条：`date -u` 与 `grep` 写在同一次执行，输出原样贴底下。

**半修长什么样。** 两种。一种是**凭空写标识**：2026-11-17 的派发消息里写过 `c1c2be32…` 当 session id，返工消息里写过 `483b…` 当 commit 前缀，还有一次把 36 位 UUID 打错；`list_sessions` 里查不到这几条，事后只能整单 recover 或补发纠正，而消息本身看不出它们有问题。另一种是**没有归因的数字**：「13 条」这种裸数被下游当成既定事实，于是被补了一句假因果（`plan-008` 那两句），纠正花了两轮。自查动作是同一个：这个值我这轮跑过命令没有？没跑就写「未核」。

### 三、合并前检查主检出有没有同名未跟踪文件

**什么时候用。** 成果已经验收、准备让协调会话跑 `python tools/auto_merge.py --ticket AW-xxx` 之前。典型成因是你的工作树新增过一个文件，而主检出里也有同名文件且未被 git 跟踪——多半是同一份计划文件的副本（本单就是从主检出把 `tasks/plan-011-…` 拷进工作树的）。

**怎么做。** 取两边的交集，交集就是会挡住合并的那几个文件。两侧都要用 `-z` 去掉八进制转义，再交给 `comm` 比：

```bash
cd {{WORKSPACE}}
# 主检出里未跟踪的（?? 开头），去引号与八进制转义
git status --porcelain -z | tr '\0' '\n' | grep '^?? ' | cut -c4- | sort
# 相对 master 真正变动的路径（只要入站全量就换成 git ls-tree -r -z --name-only ticket/AW-xxx）
git diff --name-only -z master ticket/AW-xxx | tr '\0' '\n' | grep -v '^$' | sort
# 交集
comm -12 \
  <(git status --porcelain -z | tr '\0' '\n' | grep '^?? ' | cut -c4- | sort) \
  <(git diff --name-only -z master ticket/AW-xxx | tr '\0' '\n' | grep -v '^$' | sort)
```

`comm -12` 有输出就先解决再合：让协调会话确认那个文件归谁（**不要自己删别人的未提交成果**），挪开或改名后再跑合并。

**只改一侧比不写更危险。** `git status --porcelain` 对含非 ASCII 的文件名会加引号并转义成八进制（`"tasks/plan-011-\346\226\207…"`），不带 `-z` 时 `awk` 取到的就是这个转义串。只给 status 一侧加 `-z`、树那一侧留着转义，两边就再也不一样了——交集从 1 行变成 0 行，**把「有阻塞」报成「没阻塞」**。2026-11-17 本单初稿正是这么写的（只写了一句「改用 `git status --porcelain -z | tr '\0' '\n'`」），验收会话实测原写法 1 行、改一侧后 0 行，整单因这一句被拒收。同一场景三种写法的实测对比：

```text
两侧都带转义（原写法）         → 1 行（命中的是主检出那份未跟踪的 plan-011）
只有 status 一侧 -z（初稿）    → 0 行（假阴性，把阻塞报成无阻塞）
两侧都 -z（上面这份）          → 1 行：tasks/plan-011-文档修订的三条自查.md
```

判据：**跑出来的行数要与预期一致**。你觉得「应该会被挡」而结果是 0 行，先怀疑命令而不是仓库。拿不准就别拼管道，人眼比对上面两份清单——删掉一句不可靠的补救命令，比留着一句只说对一半的强。

**为什么要单独查一遍。** 因为 `auto_merge` 的 `merge-tree` 预演**看不见这一类**：预演只算两棵树能不能合，无冲突就过；同名未跟踪文件是主检出工作区的事，不在任何一棵树里。所以预演绿了不代表能合，失败只在真合那一步出现，原文是：

```text
error: The following untracked working tree files would be overwritten by merge:
	<主检出里的同名文件>
Please move or remove them before you merge.
Aborting
Merge with strategy ort failed.
```

（上面是 2026-11-17 在临时仓库里复现的原文，只有文件路径那一行随实际情况变。）

**半修长什么样。** 预演绿了就宣布「可以合」，把工作区阻塞当成合并冲突去找。今天和它打过交道的写法能查到，但**命令要把本文件排除在外**：

```bash
cd {{WORKSPACE}}
date -u +%Y-%m-%dT%H:%M:%SZ                       # 时间与结果必须在同一次执行里取
grep -rln "拷进工作树" tasks/plan-00[678]*.md      # 故意不写 tasks/*.md，理由见下
```

输出（`2026-11-17T11:18:37Z`，主检出与分支工作树各跑一次，两边都是这三份；改完这一节后在 `2026-11-17T11:19:02Z` 又各跑一次，仍是这三份）：

```text
tasks/plan-006-并行冲突与决策通道.md
tasks/plan-007-锁作用域与消息通道三档.md
tasks/plan-008-过期表述清零.md
```

这三张单都在用「先把计划文件拷进工作树、让它在分支上受跟踪」这个办法；`plan-009`/`plan-010` 换的是另一种写法（在计划里先声明「合并前检查主检出无未跟踪同名文件」），本例不给它们计数。

**为什么范围写成 `plan-00[678]*` 而不是 `tasks/*.md`：** 因为这一节自己就写着「拷进工作树」这个词。用 `tasks/*.md` 会把它算进去，于是这个本来用来演示的计数会随本文件每改一次而变——写死一个数，改一次就变成假话；而本节第 ① 条要的恰好是「命中位置与你改过的地方对得上」。同理，**同一组命令在主检出与分支工作树里可能得出不同结果**：未入库的新文件只存在于一个检出里。本节实测：宽写法在主检出得 3 份、在带 `plan-011` 副本的工作树得 4 份，收窄写法两边都是 3 份。所以示例要选在两个检出下都稳定的范围；要贴计数，就按第 ② 条把 `date -u` 与 `grep` 写进同一次执行，把两块输出原样贴回来。

## 尚未实现

真实会话身份和目录回执适配、外部 worktree 根目录绑定、依赖图、验收任务排队、模型配置、租约心跳、预算控制、看板和定时自动推进。
