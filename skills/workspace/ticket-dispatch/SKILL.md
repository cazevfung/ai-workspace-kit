---
name: ticket-dispatch
description: 在本工作区领取工单、派发执行会话、绑定会话、提交成果、发起独立验收、处理报错码时使用。用户说「派发这个工单」「领取」「提交成果」「找人验收」「报错了看不懂」时使用。查需求写法用 prd-authoring，判断下一步用 pdsa-workflow。
---

# 工单派发与回报

顺序不能变，也不能合并成一次调用。**这不是繁琐，是设计**：每一步各自一个事务，中间的外部副作用（创建会话）不可回滚，所以必须落库之后再发生。

```
claim_ticket      → 落库派发意图，工单进 dispatching，返回 attempt_id
   ↓（此处才调用 Cindy 创建会话）
bind_session      → 核对回执目录，绑上 session_id，工单进 running
   ↓
submit_result     → 成果落库 + 摘要，工单进 awaiting_review
   ↓
review_result     → 独立会话逐项判定，进 done 或 rework
```

## 一、命令形状

```bash
# 在仓库根 {{WORKSPACE}} 直接跑，代码已合入 master，不用先 cd
python -m task_center --db <绝对路径> <操作> --request-id <唯一编号> --input <JSON文件>
```

`--request-id` 是**幂等键**：同编号同参数重试返回原回执；同编号不同参数返回 `REQUEST_CONFLICT`。网络抖动、超时、看不懂结果时的正确动作都是「用同一个编号再发一次」，不是换新编号重做。

## 二、每一步的必填字段

| 操作 | 字段 |
| --- | --- |
| `claim_ticket` | `project_id` `ticket_id` `expected_revision` |
| `bind_session` | `project_id` `ticket_id` `attempt_id` `session_id` `project_root` |
| `submit_result` | 上述 + `session_id` `summary` `artifacts` `evidence` `risks` `head_sha`（`git rev-parse HEAD` 的输出，40 位十六进制；它是这次**被验收的那棵树**，合并时拿它比分枝 tip） |
| `review_result` | `project_id` `ticket_id` `attempt_id` `submission_id` `reviewer_session_id` `verdict` `checks` `reason` |

未知字段会被直接拒绝（`INVALID_INPUT`，拼错不会静默忽略——这是好事，别绕过）。

**派发执行会话时必须显式指定模型，不再依赖继承。** `send_to_session` 的 create 模式（不传 `target_session_id`）里显式传 `model: "deepseek/deepseek-v4.1-flash"` 与 `effort: "high"`；这两个参数只在 create 模式生效，jump 模式（带 `target_session_id` 投递）忽略，不改既有会话。旧的「派发不指定模型、一律继承当前会话模型」口径作废：协调会话现在常驻 k3，它的模型会随讨论阶段变化，继承会把 k3 带进执行会话，而执行会话要的是稳定、便宜、跑出过基线的 flash。模型 id 先用 `list_available_models` 核对（本机经 `xd` provider 提供的精确 id 就是这个），与 provider / Agent 不匹配时创建前就失败，不会静默降级。这一段只定执行会话用哪个模型，派发顺序一字不变（claim → 创建会话 → bind_session → submit_result → review_result）。

### 派单消息由命令生成骨架、由人写现场

claim 之后跑 `python tools/dispatch_payload.py --ticket <单号> --context <现场段文件>`。输出分两段，**整条消息不交给机器**：库里只有标题、交付物、判据，生成不出这一单的现场；把整条交给机器，等于把执行会话的上下文抽干。

- **骨架段（机器写）**：工单与项目根 / 协调会话 id（`tickets.owner_session_id`）/ 开工第一步 / 开工方式（git 或 non_git）/ 回执怎么发。别手写、也别改输出里的值——这五行以前散在三处散文模板里，抄漏一行当场没有反应，事后查 `notices` 才发现（NC-007：派单消息只剩一句「做完回我一声」，执行会话 158 次工具调用里 `send_to_session` 出现 0 次）。
- **现场段（人写）**：黄金圈 **Why**（这一单为什么这么做，1–2 句，必须能改变执行中的判断）/ **How**（已定的路径与不许重新决定的事）/ **What**（交付物）+ **最容易做歪的地方**（派单前三条质询的落点），写在文件里用 `--context` 传，工具原样接在分隔线后面，一个字不改。

**工单的 Why 在派单时定格**：之后意图演化走 `revise_ticket` 或 `steer`，执行会话不追新版。

**机器只判三件事：在不在、空不空、是不是纯指针。** 缺 `--context`、文件不存在、内容为空、缺四节里任一节、某节正文为空、Why 整段只写「见 PRD」这类指向，都非零退出、stdout 一个字节都不打印、stderr 点名缺哪一节——半条派单消息被贴出去，比没有更贵。**Why 写得好不好机器判不了**，那是验收会话的事，所以别把这条写成「生成器保证 Why 有效」。

取数与回执方向同一套——同一个库、同一张单、收件人取同一列 `tickets.owner_session_id`（`record_notice` 的授权与 target 读的也是它），不读环境变量、不猜字符串。取不到同样一行都不给：owner 为空、这张单没有尝试、状态不在 `dispatching` / `running` 之内时非零退出。`--json` 给机器读。

## 三、回执要核对什么

派发会话之后，宿主回执里至少看三样：

1. **实际项目目录**：与 `projects.root` 一致，作为 `bind_session` 的 `project_root` 传进去。不一致就是 `PROJECT_MISMATCH`，走 `recover_ticket`，不许硬绑。
2. **session_id**：真正落到那条会话上的标识，不是请求编号。
3. **投递成功 ≠ 已开始执行**。状态是 `dispatching` 就还是没开始；要等绑定。
4. **这张单牵扯到的会话链接**：`get_ticket` 回执里的 `sessions` 给出请求方 / 协调 / 执行 / 验收 / 回执收件人五项，每项带 `session_url`（`cindy://session/<id>?device=<deviceId>`）。派发与验收的消息里带上这几条，接收方不用你手抄 session id；`stale` 为 true 的注明是旧会话，`device` 为 null 时链接不带 device 参数，不自行补。口径见 `docs/protocol.md` 的「一张单牵扯到哪些会话、哪条消息」。

**收尾方向的回执只有一条。** 执行会话交回成果时只发一条：工单号 + 状态 + 提交 sha 或 review_id，正文里不写 `wake_kind`，也不再为它另发一条消息——那个值只出现在 `send_to_session` 的发送回执里，一条消息写不出自己这一条的结果，所以这条消息本就不承担报送原值的职责。**这条消息的参数不用自己找：`python tools/receipt_payload.py --ticket <单号>` 直接打印 `send_to_session` 的完整 args（`message` + `target_session_id`）。** `send_to_session` 在工具列表里可能搜不到这个名字（按名字搜会返回空或 `UNKNOWN_TOOL`），经 `cindy_helper` 的 `call_tool` 调即可；`target_session_id` 不能省——省了就是新建任务。那张单的尝试上没有 `coordinator_session_id` 时这条命令会非零退出、一个 args 都不打印，那是「没有可发的收件人」，不是可以绕过去的提示。协调会话收到后 `record_notice` 落库，`wake_kind` 填 `unknown`（只校验形状、不按白名单收），并在 `detail` 里写明「本次 `wake_kind` 未取到，`unknown` 是占位」——这一列留不下「这是推定」的标记，不写 `detail` 就分不出库里的值是事实还是推定。

**收件人是当前 owner，不是「谁派的」。** `python tools/receipt_payload.py` 打印的 `target_session_id` 取 `tickets.owner_session_id`（现在负责收尾的那条会话），`attempts.coordinator_session_id` 只作 origin 审计。无人值守时派单器 `claim` 之后要先把它接到一条活着的常驻会话（`python tools/handover_owner.py --ticket <单号> --to <会话 id> --reason <一句>`），别让回执天生就没有收件人。

**送达不看投递回执，查一次会话列表。** 协调会话收到回执后跑 `cindy_helper` 的 `list_sessions`（按 `workdir`），确认列表里没有多出标题就是这份回执正文的新任务——没有多出新任务即算送达。`queued` / `resumed` / `already-active` 都只表示消息进了原会话、没有操作后果，唯一有后果的是宿主把回执当成新建任务，而它在会话列表里看得见；查见多出的那条就停掉（`stop_session_turn`，必要时 `archive_sessions`），再带 `target_session_id` 重发。**无人值守时这条换成可判的替代：** 那时没有活着的会话可以核列表、可以插话，「`wake_kind` 必须是 `queued` / `already-active`」「`delivery=steered`」都做不到——要求的是「`notices` 里有一行写清去向」：发得出去就一条 `sent`，发不出去就 `report_notice_failure` 落 `failed` 并写清目标已归档（宿主原话照抄），推进交给下一轮 `next_action`。

**终态只落库不唤醒。** `done` / `rework` 只落库不唤醒，不为通知再发消息；唤醒只留给 `created` 误建、`blocked`、`ask_ticket` 三种。三份文档说的是同一套做法，完整口径以 `docs/protocol.md` 的「回信落库：一张单最多一条 `sent`，没过回执的单合不进去」一节为准。

**dispatch 完就结束本轮，禁止轮内 sleep 等下游。** 派完就交回这一轮，**唯一续跑点是回执到达时起的新一轮**；回执没到时**由 `python tools/wait_audit.py` 报出**，协调会话**不得在轮内 sleep，也不得 sleep 一阵再查一次库**。为什么会踩进轮内 sleep：终态只落库不唤醒，评审会话那条回执是礼貌不是保证，`send_to_session` 又是入队语义（还可能被宿主当成新任务误建），于是「等回执」在本轮里没有任何可观测的信号——一急就写成 `sleep` 加查库（实测同一条会话日志 31 次，最长 1800 秒）。而轮内 sleep 把一条活轮押在「回执一定会到」上：睡的时候不能纠错、不能响应排队消息。所以等下游的结论只从两处来：下一轮到达的回执，或一次只读巡检 `python tools/wait_audit.py`（五类：等在验收超 4 小时没 review 行、待在验收且提交超过 20 分钟而 `notices` 一行都没有（S3 的分钟级前哨，20 = 巡检 10 分钟 × 2）、`done` 但 `notices` 为空、`done` 但 `merges` 无行、`running` 但执行会话 10 分钟没活动；阈值出处 `PRD-010` §4.3 S3a / S3 / S4；有命中退出 1、库有问题退出 2）。

## 派发前的深度质询（功能类工单必做）

claim 之前对着 PRD 与判据自问三条，有一条答不出来就回去补 PRD（走 prd-authoring），不许派：

1. 这份规格哪里最可能被照字面做歪？指出具体一节或一条。
2. 一个有想法的工程师拿到它会先问什么？写出一个问题——写不出来说明规格已足够具体，或你还没读进去。
3. 哪条判据在成果很浅的情况下依然会通过？有就补一条结果级判断类。

把三个答案写进派发消息，让执行会话按「意图」而不只是「指令」干活。

派发消息里带上工单的三行圈（Why/How 为内容、向上追溯用指针），执行会话按意图干活。工单的 Why 在派发时定格；主任务里意图有演化，走 revise 或 steer，执行会话不追新版。验收会话在逐条判据之外多判一句：成果是否服务工单 Why、有没有越过 How 里「不许重新决定」的线——越线即 fail。

## 四、派发顺序上最容易犯的四个错

1. **先开会话再 claim。** 结果：会话已经在干活，但库里没有这次意图；中断后无法追溯，也无法判断该不该重发。
2. **先写完成果再 claim。** 成果的 PRD 摘要与尝试记录对不上，提交时 `STALE_PRD`。
3. **一个会话连领两个工单。** `attempts.session_id` 上有唯一索引（活跃态），第二个会被拒。要并行就开两个会话。
4. **回执不确定时重开会话。** 正确做法是核对已有会话与执行记录，确认没在跑再决定；重复创建会造出两个都在改同一批文件的会话。

## 五、报错码怎么处置

| 码 | 含义 | 正确动作 |
| --- | --- | --- |
| `DB_NOT_INITIALIZED` | 库不存在 | 核对唯一中心路径；**不要**顺手新建一个备用库 |
| `PROJECT_MISMATCH` | 目录/项目对不上 | 核对回执；`recover_ticket` 后重来 |
| `SESSION_MISMATCH` | 会话与尝试不匹配 | 查清楚是哪条会话提交的 |
| `STALE_REVISION` | 读到的工单已变 | 重新读当前状态再操作 |
| `STALE_ATTEMPT` | 旧尝试迟到的动作 | 放弃这次动作，不要换编号重放 |
| `STALE_PRD` | PRD 改过，成果基于旧版 | 显式 `revise_ticket` 或重新规划，不能直接验收 |
| `ARTIFACT_CHANGED` | 成果被改/删/移出项目 | 重新提交；不要试图让验收绕过 |
| `CONFLICT` / `INVALID_STATE` | 已被领取/已提交/已终结 | 读状态，走正确入口，别硬改 |
| `SELF_REVIEW` | 执行会话想验收自己 | 换一个没执行过该工单的会话 |
| `REQUEST_CONFLICT` | 同编号换了参数 | 查清原请求，不要随意换号重做副作用 |
| `STORAGE_ERROR` | 数据库层问题 | 保留原编号与参数，查清后重试 |
| `SCHEMA_MISMATCH` | 目标文件不是本工具的库 | 停手，确认路径 |

## 六、验收会话的规矩

- 必须是**另一个**会话，且没参与执行。可以换模型，但换模型不代替真实检查。
- 逐项给 `checks`，每项写明通过与否和理由；`verdict=accept` 时不允许有任何失败项。
- 不许只看执行者的自述总结下结论，要对着成果文件本身看。
- 拒收必须写出具体缺陷，让返工有靶子。

## 七、不确定的时候

- 不确定项目归属 → 跑 `python -m task_center.probe --db <库> --roots <目录> --explain`，把候选摊给人选。**禁止**从聊天内容推断项目。
- 不确定工单当前状态 → `get_ticket`，不要凭记忆。
- 不确定刚才那次调用成没成 → 用同 `request_id` 重发，或 `events` 看有没有落库。

## 八、criteria 只收两种写法

开关单时写的 `criteria` 会被验收会话逐条对；写不可判定的条件，验收就靠嘴上过。所以 `criteria` 只收两种：

1. **命令 + 期望输出。** 写清跑哪条命令、看到什么算过（退出码、行数、某个词出现几次）。例如：`python tools/run_tests.py` 退出 0 且合计不少于 376 项，Tests 与 tools 两处的用例数都打印出来。
2. **`判断类` + 反例。** 这条靠人看，那就必须写出「什么情况算不过」。例如：判断类：两份清单口径是否一致；反例：同一数字在两处写成不同值。

只写「检查一下文档准确」「看是否合理」这类句子，两条都不算——这类条件在验收时既无法判红也无法判绿，最后只能按执行者的自述收下。

## 不要做

- 不手工改数据库、不写 SQL 修状态。
- 不把模拟会话的 id 当成真实 Cindy session 写进库。
- 不在执行者与验收者是同一条会话的情况下走完闭环。
- 不在文档或汇报里把「已提交」说成「已完成」——完成只由验收判定。
