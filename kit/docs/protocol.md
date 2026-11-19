# 本机工单协议 v0.1

## 调用约定

Python：`Center(absolute_db_path).execute(operation, payload, request_id=None)`。
CLI：`python -m task_center --db <绝对路径> <operation> --request-id <id> --input <UTF-8 JSON文件或->`。

所有业务写入必须使用 request_id。读取、init、migrate 和 backup 不要求它。request_id 在整个中心唯一；重试必须保持 operation 和完整 payload 不变。重试返回的是**当时的提交回执**，不是当前状态；需要最新进度时调用 get_ticket。

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

## 操作与字段

下表中的字段除明确标注外均为必填。未知字段拒绝，避免拼错后静默忽略。

| 操作 | 输入字段 | 结果与边界 |
| --- | --- | --- |
| init | 空对象 | 显式建库并写入当前 schema 版本（8）；已有同版本数据库不重复建表，旧版本和其他库拒绝 |
| migrate | 空对象 | 只做已知升级链（3 → 4：attempts 加 coordinator_session_id；4 → 5：新建 merges 表；5 → 6：新建 questions / answers 两张表，并按「blocked 也算活跃尝试」重建 active_session 索引；6 → 7：attempts 加 coordinator_workdir 列、新建 notices 表与一张单一条 sent 的部分唯一索引；7 → 8：notices 加 target_session_id 与 wake_kind 两列，存量行留空串表示「历史未记」）；保留全部工单、尝试、提交、验收、事件与既有回信记录；已是 8 返回「已是最新」且不改任何行；其他版本号或不是本工具的库报 SCHEMA_MISMATCH 且不改文件；旧版本头配新形状（例如 user_version=5 却已有 questions 表、user_version=6 却已有 notices 表、user_version=7 却已有 target_session_id 或 wake_kind）同样拒绝；不自动建库。升级前的快照只能在文件层做（backup 操作对旧库会报错），顺序见「升级旧库的顺序」与「改 schema 的落地顺序」 |
| register_project | project_id, name, root | root 是已存在的绝对项目目录；root 是身份、project_id 是标签，同一路径重复登记复用原记录（回执返回原 project_id 与 name，`reused: true`），不报 CONFLICT、也不改已有标签；把自己的 project_id 指向第二个目录报 CONFLICT |
| list_projects | 空对象 | 只读，列出 project_id / name / root；没有它时项目清单只能读库或靠工单反推 |
| unregister_project | project_id | 撤销错误登记；项目下仍有工单时拒绝（PROJECT_HAS_TICKETS），回执里列出这些工单；撤销后同一路径可重新登记 |
| create_ticket | project_id, ticket_id, title, deliverable, prd_path, criteria, root 可选 | 项目内 PRD 相对路径；保存内容摘要，创建 revision=1、ready 工单。root 可选：project_id 未登记时传了就顺手记一行（name 取目录名）再开单，不传报 NOT_FOUND 且 message 写明可以传 root；project_id 已登记而 root 与库里的不一致报 ROOT_MISMATCH，原记录不动 |
| get_ticket | project_id, ticket_id | 返回工单、所有执行尝试（含 coordinator_session_id 与 coordinator_workdir）、提交和验收历史，以及合并记录数组 merges、问答历史数组 questions 与 answers、回信记录数组 notices（没有记录时均为空数组）；回信结果与协调者工作目录在同一份回执里，验收据此判交接闭环有没有走完 |
| list_tickets | project_id 可选, limit 可选, offset 可选 | 默认 50 条，最多 100；按项目过滤 |
| claim_ticket | project_id, ticket_id, expected_revision, coordinator_session_id, coordinator_workdir 可选, allow_parallel 可选 | 原子领取 ready/rework 工单，生成 attempt_id，状态 dispatching；coordinator_session_id 是派发它的协调会话，1–500 字符；coordinator_workdir 是协调者自己的工作目录（1–1000 字符），不传就是空，不阻止派发；**同项目已有在途工单时报 DISPATCH_BUSY，消息里点名那张单号与它停在哪一步**（在途 = dispatching / running / awaiting_review / blocked，见「同项目一次只跑一张单」）；`allow_parallel` 只收布尔且默认 false，显式传 true 才越过，越过往 events 写一条 `allow_parallel_override` 并在回执里回显 `parallel_with`；回执含 coordinator_session_id、coordinator_workdir、allow_parallel 与 parallel_with |
| bind_session | project_id, ticket_id, attempt_id, session_id, project_root | 将预留尝试绑定到会话标识；状态 running；session_id 与 claim 时的 coordinator_session_id 相同则报 SELF_DISPATCH，工单停在 dispatching；回执含 session_id 与 coordinator_session_id；不代表已验证宿主身份 |
| submit_result | project_id, ticket_id, attempt_id, session_id, summary, artifacts, evidence, risks | 保存 submission_id、成果摘要和自检证据；状态 awaiting_review |
| review_result | project_id, ticket_id, attempt_id, submission_id, reviewer_session_id, verdict, checks, reason | 只接受这八个顶层字段，多传即拒（见「验收 payload 是一张闭集」）；accept→done；reject→rework。验收者不能是该工单任一历史执行会话 |
| recover_ticket | project_id, ticket_id, attempt_id, expected_revision, reason | 只恢复 dispatching/running/awaiting_review/blocked；attempt_id 必须是当前尝试，旧编号返回 STALE_ATTEMPT；回执含 superseded_attempt_id、discarded_submission_id 与 voided_question_ids；旧尝试失效，它的开放问题转 voided，revision+1，回到 ready |
| revise_ticket | project_id, ticket_id, expected_revision, reason, prd_path, criteria, deliverable 可选, reopen 可选 | 明确更新需求摘要和验收项，旧尝试失效，revision+1；deliverable 不传则保留原值；对已完成工单必须传 reopen=true；回执含 previous_state、prd_path 与 deliverable；已取消工单不能重开 |
| cancel_ticket | project_id, ticket_id, expected_revision, reason | 取消未终结工单，旧尝试失效，revision+1；不停止真实会话 |
| merge_ticket | project_id, ticket_id, commit, target_branch, tests | 给已验收（done）工单写一条合并记录：合并提交、目标分支、合并前跑过的测试结论；不是 done 报 INVALID_STATE；该单 `notices` 为空报 NO_NOTICE（要么补记一条 `sent`、要么记一条 `failed` 并写清原因，`failed` 也算走过回执）；已有合并记录报 ALREADY_MERGED（这一条先判，行为不变）；回执含 ticket_id、commit、target_branch、tests 与 merge_id |
| list_unmerged | project_id 可选 | 只读，列出 state=done 但没有合并记录的工单（id / project_id / title / state / revision / created_at，带 has_merge=false）；历史工单直接在 master 上做、没有 merges 行，会照样列出——清单让人看见并判断，不替人隐去 |
| events | project_id, ticket_id, limit 可选, offset 可选 | 按顺序返回状态事件和原请求回执关联 |
| backup | destination | 在现有目录内创建新的绝对路径快照，不覆盖文件，不记录业务状态变化；只接受当前 schema（7）的库，旧库返回 SCHEMA_MISMATCH 且不落任何文件 |
| ask_ticket | project_id, ticket_id, attempt_id, session_id, question, options 可选 | 就当前尝试提一个可回答的问题，工单进「在等答案」：question 1–2000 字符；options 为 0–6 项 `{"id","text"}`（id ≤20 且不重复、text ≤200），不传或空数组表示开放题；工单与尝试同时进 blocked，revision 不变；回执含 question_id、state、asked_at。工单不在 running 报 INVALID_STATE，attempt_id 不是当前尝试报 STALE_ATTEMPT，session_id 不是该尝试绑定的那个报 SESSION_MISMATCH；一条尝试同时只能有一条开放问题 |
| answer_ticket | project_id, ticket_id, question_id, answer, answered_by, answered_by_session_id 条件必填, prd_sha256 | 把人的结论写进库：answer 1–4000 字符；answered_by 只能为 human / coordinator，coordinator 必须带 answered_by_session_id、human 不许带；prd_sha256 原样抄 get_ticket 的当前值；问题 open → answered、尝试与工单回 running，revision 不变；回执含 answer_id、question_state、ticket_state。工单回到 running 只表示库里不再等答案，不代表那个会话已经在跑（见「`blocked`：工单在等一句话」） |
| record_notice | project_id, ticket_id, attempt_id, session_id, origin_session_id, status, detail, target_session_id, wake_kind | 记一次回信尝试（本操作只落库，不发消息）：status 只收 `sent` / `failed`，其他值报 INVALID_INPUT；session_id 必须等于该尝试 claim 时写下的 coordinator_session_id，否则 NOT_COORDINATOR 且不写行；target_session_id 必填且也必须等于那条协调会话，写成执行会话或别的会话报 NOT_TARGET 且不写行；wake_kind 是宿主回执里的原值，不按白名单收：只有 `created`（宿主把它当成新建任务而非投递）**特判**——一律落 `failed`、回执带 `alert: NOTIFY_CREATED_TASK` 并让这次调用**非零退出**（那一行仍然入库，事后查得到）；`queued` / `resumed` 与枚举外的取值（例如 `already-active`）一律原样入库，调用方不得自行映射成近似的三值之一（见「回信落库：一张单最多一条 `sent`，没过回执的单合不进去」）；只校验形状——空串、纯空白、超过 40 字、含内部空白或换行报 INVALID_INPUT 且不写行；同一张单最多一条 `sent`，第二次报 ALREADY_NOTIFIED 且不写行；`failed` 可以多条，措辞在 detail 里，回执带 `alert: NOTIFY_FAILED` 但不改工单状态；回执含 notice_id、ticket_id、attempt_id、origin_session_id、status、detail、target_session_id、wake_kind、ticket_state 与 revision |

`criteria` 是 1–100 项 `{"id":"C1","text":"明确验收条件"}`，id 不能重复。

`deliverable` 是必填的交付物描述：它要说什么东西、什么形状、给谁看。它和 `criteria` 不能互相替代——`criteria` 管「做得对不对」，`deliverable` 管「做的是不是这件事」。验收条件只约束质量、不约束主题时，成果会合规地跑偏（隔离试点 TEST-003 实际发生过）。

`unregister_project` 的保护条件不是礼貌性的：有工单的项目一旦被撤掉，工单就指向一个不存在的项目，事件与验收记录随之失据。要撤先处理工单（`cancel_ticket` 之后仍需人工清理），不要为了「收拾登记」删工单。

**项目登记不设门禁：列表即范围，登记只是记账。** 能不能在某个目录开单取决于它是 Cindy 列表里的目录，不取决于库里有没有这一行，所以 `create_ticket` 允许带着 `root` 自动记账，不必先单独调 `register_project`。库里只有 `projects` 这一张表，没有白名单也没有镜像表，`root` 是唯一的身份：六个同名的 `bots/*/workspace` 各自一行、互不干扰，`project_id` 撞车不阻断派发——标签给人看，路径才决定工单落在哪。标签给错了先 `unregister_project` 再重登，`create_ticket` 不会代你改标签。

`artifacts` 是 1–20 个业务项目内文件相对路径。禁止绝对路径、项目外路径和解析后重复的路径。程序计算并保存每个文件的 SHA-256 和大小，不信任调用者自报摘要。单文件上限 32 MiB。

`evidence` 为至少一条自检说明字符串，`risks` 为字符串数组，可为空。自检说明是执行者声明，不等于独立测试证据已经被系统验证。

验收条件里说的「提交说明」，指的就是 `submit_result` 的 `summary` 与 `evidence` 两部分：`summary` 说清这次改了什么、边界在哪，`evidence` 逐条给出自检说明。别处不再有第二个位置——验收不得要求执行者额外写进工单描述、聊天回复、单独的文件或某个特定小标题下，也不得以「说明的位置不对」为由判不通过。条件想约束文案时，要写明它落在 `summary` 还是 `evidence`。

### `blocked`：工单在等一句话

工单跑到一半需要人拍板时，别再拿 `running` 假装还在干、也别用 `rework`（那是「要改」）：走 `ask_ticket` 让它进 `blocked`，问题落库，本轮结束；答案落库后由协调会话发一条消息把人会话叫起来。

- **两处都写，权威在工单级。** `ask_ticket` 同时改 `attempts.state` 与 `tickets.state`；`get_ticket` / `list_tickets` 看工单那一列就知道它在等什么，尝试上的 `blocked` 说明「是这一次尝试卡住了」，所以 `recover_ticket` 之后仍能分出「答完继续」与「尝试作废」。`revision` **不变**：等一句话不是新代次，只有 `revise_ticket` 才递增它。
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

### 回信落库：一张单最多一条 `sent`，没过回执的单合不进去

派发链的收口是「回执发回了原会话」，这件事以前只靠人记得做。`notices` 把它变成一条可查询的记录：

- **写什么。** 每次尝试发一条 `record_notice`：`status` 是 `sent` 或 `failed`，`detail` 是这次尝试的措辞（失败时写原因，例如「目标会话已归档」），`origin_session_id` 是回执的收件会话。`get_ticket` 的 `notices` 数组按写入顺序返回，`created_at` 说明发生在什么时候。
- **发给谁也要落库。** `target_session_id` 必填，而且必须就是 claim 时写下的那条协调会话——写成执行会话自己是这例错误里最常见的一种，那种回执根本到不了人手里，所以它报 `NOT_TARGET` 且一行不写。这个字段就是「事后查这次回执到底发到哪」的答案，不是可以补填的字段。
- **宿主怎么处理的也要落库。** `wake_kind` 记宿主回执里的**原值**（执行会话按本节「回执只发一条」那条走时，协调会话填的是 `unknown`，因为那份原值不再随回执过来），不按本工具的枚举收：已见过的是 `queued`（排在原会话这一轮之后）、`resumed`（直接唤醒了原会话）与 `already-active`（目标会话当时就在跑），将来还可能再来一个。**为什么允许枚举外的值：** 宿主的取值面比这里写下的宽，只收三个值就等于逼调用方挑一个近似的填，库里留下的是推定而不是事实——事后查「这条消息是怎么到的」时，推定与宿主原话看不出区别，这比缺一个字段更难发现。所以除了下面这一个特例，任何非空、无内部空白、不超过 40 字的取值都原样写进 `notices.wake_kind`，调用方**不得**自行映射。唯一被特判的是 `created`：它是失败信号而不是另一种成功方式——宿主没有投递，而是新建了一个任务（那条新会话的标题往往就是回执原文本身），这时这条 notice 一律落 `failed`，回执带 `alert: NOTIFY_CREATED_TASK` 且命令非零退出；那一行仍然留在库里，而不是被回滚掉。工具只报错不停会话：停掉误建的新会话仍由人来做。
- **一张单最多一条 `sent`。** 回信只发一次，重复宣告不是新事实：第二次 `sent` 报 `ALREADY_NOTIFIED`，库里只留第一条。这条约束落在 `one_sent_notice` 部分唯一索引上（`notices(ticket_id) WHERE status='sent'`），不靠代码自觉——并发下两个都会以为自己是第一次。
- **`failed` 可以多条。** 真会失败多次，每次一条，逐条留原因。它**不改工单状态**：失败只说明这次投递没成功，`NOTIFY_FAILED` 不推动任何状态机，也不会自动重派；要重派由人决定，走 `recover_ticket` 重新派发。
- **合并前必须有回信记录。** `merge_ticket` 在该单 `notices` 为空时报 `NO_NOTICE`：要么补记一条 `sent`，要么记一条 `failed` 写清原因。`failed` 也算走过——回执没送达但原因记下来了，这是事实，不是可以拿来越过的门；它拦的是「验收了、合了、回执不知道发没发」这条路。已有合并记录的单行为不变，先报 `ALREADY_MERGED`。
- **只有协调会话能记。** `session_id` 必须等于该尝试的 `coordinator_session_id`，否则 `NOT_COORDINATOR` 且一行都不写。与 `answer_ticket` 的同名判定一样，这是把「谁发的回执」与 claim 时写下的那一条对齐，不是身份认证——换一个字符串仍然能绕过。
- **两个参数从库里取，不靠人拼。** `python tools/notice_payload.py --ticket <单号>` 打印两段可直接复制的 JSON：`send_to_session` 的 `{message, target_session_id}` 与 `record_notice` 的完整 payload；那张单没有协调会话标识时非零退出并说明原因，不猜一个字符串出来。它读的是**最新值**：库旁有非空 `-wal` 时按 `mode=ro` 读（会留 `-shm`/`-wal` 旁文件），没有才用 `immutable=1`（一个旁文件都不留）；每次输出的第二行都写明这次走了哪条路。
- **回执只发一条，正文不报送原值。** 执行会话交回成果时发**一条**消息：正文只写工单号、状态与提交 sha 或 review_id；`wake_kind` 不写进正文，也不为它另发一条。理由与做法一起换了：那个值只出现在 `send_to_session` 的发送回执里，一条消息写不出自己这一条的发送结果，所以**这条消息本就不承担报送原值的职责**——省掉这一步，也就省掉了「推定一个值写进正文」这条唯一能造假的路径；而以前那条只写原值的第二条，每发一次就要多唤醒一次原会话、多读一遍上下文，收益不抵成本。协调会话收到回执后照常 `record_notice` 落库：`wake_kind` 填 `unknown`（只校验形状、不按白名单收，所以它合规矩也能入库），并在 `detail` 里写明「本次 `wake_kind` 未取到，`unknown` 是占位」——这一列本身记不下「这是推定」，省了 `detail` 就再也分不出这一行是事实还是推定。
- **送达改由一次会话列表核对承担，不看投递回执里的 `wake_kind`。** 协调会话收到回执后跑 `cindy_helper` 的 `list_sessions`（按 `workdir`），确认列表里没有多出标题就是这份回执正文的新任务——**没有多出新任务即算送达**。这条判据只看有操作后果的那一类：`queued` / `resumed` / `already-active` 都只表示消息进了原会话、没有副作用；唯一有后果的是宿主把回执当成新建任务，而它在会话列表里看得见，不必再去读投递回执。2026-11-18 实测同一会话、同样入参与 `target_session_id` 连投三条消息，`wake_kind` 依次是 `already-active`、`created`、`queued`——三值同框，所以「上一条投中了」推不出「这一条也投中了」，能推的只有当场查一遍列表。查见多出的那条就停掉它（`stop_session_turn`，必要时 `archive_sessions`），再带 `target_session_id` 重发。
- **终态只落库不唤醒。** 回执的收尾分两类：`done` / `rework` 这类终态**只落库不唤醒**——提交、验收、`record_notice` 都记进库，不为了通知再发一条消息，否则每张单都要多一次唤醒、多一遍上下文重读；唤醒只留给三种情况：`created` 误建了任务、工单 `blocked` 在等人一句话、`ask_ticket` 已经落了问题。判据按「这件事要不要让别的会话现在改变动作」问，不按「有没有新状态」问。
- **真伪的边界。** 这里只做落库，不投递：库里记的是「协调会话声明它发了、发给了谁」与它自己填的 `wake_kind`（本条路径上是 `unknown` 占位），不是「宿主确认送达」。自动发信与重试属于之后的适配层。

### 会话标识与工单标识

session_id 按原样精确比较，大小写不同即视为不同会话（`EXEC3` 与 `exec3` 不同）。这不是身份认证，真实身份与目录回执由后续 Cindy 适配层承担。

`coordinator_session_id` 同样只是调用方自报的字符串，但它让「谁派的」和「谁做的」分得开：claim 时写进 attempts，bind 时必须换一个会话标识。两者相同就报 `SELF_DISPATCH`，工单停在 dispatching，不产生 session 绑定。这条规则拦的是职责混同，不是防伪——换一个字符串仍然能绕过，所以它配着 `AGENTS.md` 的硬规则五一起用。

工单标识在整个中心内唯一，不按项目隔离：项目乙再建 `T-1` 会得到 CONFLICT。需要分区时在标识里带项目前缀。

### 恢复与修订的验证口径

恢复必须写明它核对过的当前尝试编号。拒收返工不递增 revision，一个 revision 内可以有多个尝试；只比版本号会让晚到的旧恢复意图作废正在执行的新尝试。

修订已完成工单必须显式传 `reopen: true`，否则拒绝。修订后旧验收记录保留，已终结（accepted / rejected）的尝试状态不会被改写成 superseded。

`checks` 为逐项验收数组，必须且只能覆盖当前 criteria 的全部 id：

```json
[{"id":"C1","passed":true,"evidence":"检查了 result.md 的第1至3项，符合给定规则"}]
```

`verdict` 只能为 accept 或 reject；accept 必须全部通过，reject 必须有至少一个失败项。调用方仍需真正读取和检查成果；数据库只能核对字段与状态，不能证明自然语言证据是真实的。

### 验收 payload 是一张闭集

`review_result` 恰好接受八个顶层字段，写多一个都会被拒：

`project_id`、`ticket_id`、`attempt_id`、`submission_id`、`reviewer_session_id`、`verdict`、`checks`、`reason`。

多传字段（例如给验收补一句 `notes`、`comment` 或 `summary`）得到的是 `INVALID_INPUT: Unknown payload fields`，不是被忽略；报错也不会告诉你是哪几个字段——先照抄上面八个名字核对拼写，再怀疑值。自由文本只有 `reason` 一处可写；逐项证据只能写进 `checks[].evidence`，每一项恰好 `id`、`passed`、`evidence` 三个键。整张表都是这个口径：未知字段一律拒绝，避免拼错后静默忽略。

## 预留、绑定与提交的顺序

1. 协调会话先调用 claim_ticket，传入自己的会话标识作为 coordinator_session_id，记录 attempt_id。
2. **派发是另一个会话**：协调会话不得自己执行这张工单。默认载体是项目普通会话——`send_to_session` 不传 `target_session_id` 并指定 `project_path`（见下节「派发的载体」）；宿主确实没有新建项目会话的能力时才用一次性独立上下文子任务并标注；两者都没有就停下来告诉人，不要自己动手。适配器负责检查项目与会话回执。
3. 被派发的会话调用 bind_session 保存绑定——它的 session_id 不能等于 claim 时的 coordinator_session_id。真实执行者必须等绑定完成，才能提交；先完成了成果也应保留本地记录并稍后提交，而不是再次创建工单。
4. 执行者保存业务项目中的成果，再调用 submit_result。
5. 收到回执后通知协调方，**只发一条**：正文写清工单号、状态与提交 sha 或 review_id，不写 `wake_kind`、也不补第二条（为什么一条就够、送达怎么判，见「回信落库：一张单最多一条 `sent`，没过回执的单合不进去」一节的「回执只发一条」「送达改由一次会话列表核对承担」「终态只落库不唤醒」三条）。协调会话收到后 `record_notice` 落库（`wake_kind` 填 `unknown` 并在 `detail` 里注明是占位），再跑一次 `list_sessions` 核对会话列表：没有多出标题等于这份回执的新任务即算送达，多出了就先停掉误建的再带 `target_session_id` 重发。终态（`done` / `rework`）只落库不唤醒，唤醒只留给 `created` 误建、`blocked`、`ask_ticket` 三种。通知丢失时，可根据中心 awaiting_review 状态继续，而不重做执行。
6. 独立验收会话核对成果后调用 review_result。

本版只实现第1、3、4、6步的本机记录和校验，不会自动执行第2或第5步。也正因第2步还没有适配层，「新开一个会话」目前靠调用方自觉；`SELF_DISPATCH` 只能拦住「用同一个标识绑自己的工单」这一种偷懒方式。

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

## 验收通过不等于已合并

`done` 只说明验收通过，不说明结果进了目标分支。没有合并记录时，「验收了但没合」只能靠人记得——`tasks/plan-005-merge-after-accept.md` 的起因就是功能验收了、代码还留在分支上。`merges` 表把这件事变成一条可查询的记录：

- 写入用 `merge_ticket`，只接受 `state=done` 的工单；同一工单只允许一条，第二次返回 `ALREADY_MERGED`。`commit` 是合并提交（`--no-ff` 生成的那个），`target_branch` 是它进入的分支，`tests` 是合并前跑过的测试结论。三项都是自报字符串，数据库不能验证它们真的发生过。
- 读取用 `list_unmerged`：列出已验收但没有合并记录的工单。**历史工单 AW-004~AW-016 是直接在 master 上做的，没有 `merges` 行，因此会出现在清单里**——这是事实，不是待修的缺陷；要处理的是「现在这张到底合没合」，不是替历史补记录。
- `get_ticket` 的 `merges` 与 `attempts` / `submissions` / `reviews` 并列，空数组表示还没有合并记录。

具体动作由 `tools/auto_merge.py` 执行，不靠人记得：

```bash
python tools/auto_merge.py --list                    # 每轮开工先看：done 但没有合并记录的工单
python tools/auto_merge.py --ticket AW-xxx           # 预演 → 测试 → --no-ff 合入 master → merge_ticket
python tools/auto_merge.py --ticket AW-xxx --dry-run # 只预演与跑测试，不碰目标分支、不写记录
python tools/auto_merge.py --ticket AW-xxx --record-only # 已合并且无记录时收口：测试 → 补记录 → 清理
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

工具在临时 worktree 里先合一次并跑 `python tools/run_tests.py`，绿了才 `git merge --no-ff`；红了一律中止，此时目标分支一动未动。真实合并后还会再跑一次全量测试，再红就 `git reset --hard pre-merge-<工单号>` 回滚——回退点在合并前就打好了。回退点已存在时工具拒绝合并，不覆盖旧回退点。

### 合并断在半路：`--record-only`

一次真实合并在「合并提交已经落到工作区」与「记录已经写进库」之间隔着两场全量测试，进程被杀就停在那一段。**这种现场默认路径一步都走不动**：第 2 步要「源分支领先目标」，而源分支已经被合进去了，领先数正好是 0——命令连测试都跑不到，人只能手工去比对 git log 和库。

```bash
python tools/auto_merge.py --ticket AW-058 --record-only
```

它和正常合并共用第 0 步的锁、第 1 步的工单门（必须 `done`、没有合并记录，否则报 `ALREADY_MERGED`）与第 2 步的检出检查（当前检出在目标分支上、已跟踪文件无改动、目标与源分支都在），只把「源分支领先目标」换成**反向判据**：`git merge-base --is-ancestor <源> <目标>` 成立，且 `rev-list --count <目标>..<源>` 为 0。这一步不预检未跟踪文件：这次不往工作区写任何路径。

接着三件事：

1. **找到那次合并提交。** 在目标的祖先里按 `parent2 == 源分支 tip` 逐行比对 `git rev-list --parents <目标>`。找不到（当时是快进合并，根本没有合并提交）就非零退出、不写记录——记录里的那个 `commit` 必须是真合过的那一个，不能拿 HEAD 顶替。
2. **在当前检出跑一遍全量测试**，绿了才写记录，`tests` 就是这次实跑的汇总行。红就非零退出，不写记录、不清理工作树与分支；回退点写在输出里：`pre-merge-<工单号>`，没有这个 tag 时就是合并提交的第一个父提交。
3. **记录写成功之后清理**：`git worktree remove .worktrees/<工单号>`（存在才删）、`git branch -d ticket/<工单号>`。清理失败只报告、不改退出码——那时该做的是人工收拾，不是重跑（重跑会被第 1 步的 `ALREADY_MERGED` 挡回来）。工具不会 `--force` 删工作树：里面还有未提交改动时，该保留的正是那部分。

正常合并路径还会写一份 `.data/merge-journal/<工单号>.json`（`commit` / `target_branch` / `source_branch` / `tag` / `started_at` / `step`）：合并提交落到工作区之后、合并后测试之前写一次（`step: merged`），测试绿了更新成 `tests-passed`，`merge_ticket` 成功删掉；测试红回滚之后也删掉，删前把 `step` 改成 `rolled-back`。它只解决一件事：进程被杀之后，「这次合并做到第几步」不用再从锁文件和 git log 推。

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

## 恢复、版本与竞争

`revision` 是工单执行代次，不是 PRD 文档里的人工版本号。显式恢复、修订或取消会递增；领取和提交不递增，通过当前 attempt_id 排除旧尝试。

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

1. **先改文件，再 `revise_ticket`**：`revise_ticket` 会用当前文件重新指纹 `prd_path`、改写 `prd_sha256`（同时 revision 递增、旧尝试失效），然后按新 revision 重新领取、绑定、提交即可。
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

校验包括三件：`migrate` 回执的 `from_version` 是升级前的版本、`migrated: true`、`schema_version` 是当前版本；`list_tickets` / `list_projects` 还能看到原有记录；升级后 `backup` 能成功（它只接受最新 schema，成功即证明库已经是最新）。全部通过后再删旧快照。升级链是逐级执行的（3 → 4 → 5 → 6 → 7 → 8），从 v3 直接升到 v8 也一样，不需要先单独升到 v4 或 v5。

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

**`claim_ticket` 时传 `coordinator_workdir`。** 不传不报错、也不阻止派发，所以很容易漏；漏了之后 `attempts.coordinator_workdir` 是空值，`get_ticket` 只剩一个 `coordinator_session_id` 能看出「谁派的」（AW-051 就是空着交的）。同一类漏法还有回信落库：没人调 `record_notice`，那张单的 `notices` 就一直是个空数组，交接闭环走没走完事后无法从库里判断。协调会话在 `claim_ticket` 里把自己的工作目录原样填进去，发完回执再补一条 `record_notice`。

文件摘要核对是读取时刻的检查，源文件仍可被其他进程修改。第一版面向可信协作，不把项目路径或会话字符串检查宣传成防恶意操作的安全边界。

## 常见错误

- DB_NOT_INITIALIZED：数据库不存在，核对唯一中心位置，不自动新建备用库。
- PROJECT_MISMATCH / SESSION_MISMATCH：核对项目或会话绑定。
- ROOT_MISMATCH：给的 root 与已登记的不一致（同一个 project_id 指向第二个目录，或 root 已属于另一个 project_id）。root 是身份，改不靠开单，先 `unregister_project` 再重登；这次调用不改任何已有记录。
- STALE_REVISION / STALE_ATTEMPT：读取当前状态，不能提交旧执行结果。
- STALE_PRD / ARTIFACT_CHANGED：需求或成果改变（成果被删除或移出项目也统一报 ARTIFACT_CHANGED）；明确修订或核对后恢复再提交。
- REQUEST_CONFLICT：请求编号被用于不同参数，查清原请求，不随意换编号重做副作用。
- CONFLICT / INVALID_STATE：任务已被领取、已提交、已结束，或存在唯一性冲突。
- SELF_REVIEW：换用没有执行过该工单的独立验收会话。
- SELF_DISPATCH：协调会话不能执行自己派发的工单，新开一个会话来做。工单仍停在 dispatching，换个 session_id 重新 bind_session 即可。查一条会话到底绑在哪些单上（没绑会打印 `NOT_BOUND` 并非零退出）：`python tools/session_roster.py --session <id>`。
- STORAGE_ERROR：保留原请求编号和参数，查明数据库问题后再重试。
- NOT_TARGET：`target_session_id` 不是该尝试 claim 时写下的协调会话。回执发错了地方比没发更难发现，所以写成执行会话自己或其他会话都不允许，且一行不写。
- NOTIFY_CREATED_TASK：回执的 `wake_kind` 是 `created`——宿主把它当成了新建任务，这条回执没有到达任何人。命令非零退出，但库里已经留了一条 `failed`；先停掉那条误建的任务，再带上 `target_session_id` 重发。
- NO_NOTICE：这张单没有任何回信记录，合并不进去。补记一条 `sent`，或补一条 `failed` 写清原因，再合并。`tools/auto_merge.py` 在正常合并路径上把这道判**提到了任何写入之前**（第 1 步收尾），所以忘补回执时是「一步都不动」地被拒，而不是 `git merge` 已经落进目标分支才在写记录那步失败；那种现场才需要 `--record-only` 收口。
- ALREADY_MERGED：这张工单已有合并记录，读取它而不是再写一条；要改记录内容得先由人核对实际合并情况。
- NOT_COORDINATOR：只有派发这张单的协调会话能代记结论或回信结果（`answer_ticket` 的代答、`record_notice`）。换一个会话不能替代；请协调会话自己来写。
- ALREADY_NOTIFIED：这张单已经有一条 `sent` 回信记录，读它的 `notices` 而不是再写一条；`failed` 不受此限，可以逐次记录。
- DISPATCH_BUSY：同项目还有在途工单（`dispatching` / `running` / `awaiting_review` / `blocked`），消息里点名了是哪张单、停在哪一步。它先做完再领下一张；确实要并行就显式传 `allow_parallel: true`，越过会在 events 里留一条记录。
- SCHEMA_MISMATCH：目标文件不是本工具的数据库，或版本不在已知升级路径上。init、migrate、读取与 backup 做同一套表/列校验，任意外部库不会被当成有效快照；也因此 backup 不能给旧库做快照——升级前需要一份快照时，先在文件层做（SQLite backup API，或停写后复制文件），再 migrate，旧库升级不自动迁移。`tools/work_gate.py --list` 读不了库时也打这个码并非零退出，不再打印一张空清单冒充「没有人在做」。
- BACKUP_EXISTS / INVALID_INPUT：备份目标已存在或不是可写绝对目录。目标目录必须支持硬链接（FAT32、部分 SMB/NFS 不支持）。

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
