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
| `submit_result` | 上述 + `session_id` `summary` `artifacts` `evidence` `risks` |
| `review_result` | `project_id` `ticket_id` `attempt_id` `submission_id` `reviewer_session_id` `verdict` `checks` `reason` |

未知字段会被直接拒绝（`INVALID_INPUT`），拼错不会静默忽略——这是好事，别绕过。

## 三、回执要核对什么

派发会话之后，宿主回执里至少看三样：

1. **实际项目目录**：与 `projects.root` 一致，作为 `bind_session` 的 `project_root` 传进去。不一致就是 `PROJECT_MISMATCH`，走 `recover_ticket`，不许硬绑。
2. **session_id**：真正落到那条会话上的标识，不是请求编号。
3. **投递成功 ≠ 已开始执行**。状态是 `dispatching` 就还是没开始；要等绑定。

**收尾方向的回执只有一条。** 执行会话交回成果时只发一条：工单号 + 状态 + 提交 sha 或 review_id，正文里不写 `wake_kind`，也不再为它另发一条消息——那个值只出现在 `send_to_session` 的发送回执里，一条消息写不出自己这一条的结果，所以这条消息本就不承担报送原值的职责。协调会话收到后 `record_notice` 落库，`wake_kind` 填 `unknown`（只校验形状、不按白名单收），并在 `detail` 里写明「本次 `wake_kind` 未取到，`unknown` 是占位」——这一列留不下「这是推定」的标记，不写 `detail` 就分不出库里的值是事实还是推定。

**送达不看投递回执，查一次会话列表。** 协调会话收到回执后跑 `cindy_helper` 的 `list_sessions`（按 `workdir`），确认列表里没有多出标题就是这份回执正文的新任务——没有多出新任务即算送达。`queued` / `resumed` / `already-active` 都只表示消息进了原会话、没有操作后果，唯一有后果的是宿主把回执当成新建任务，而它在会话列表里看得见；查见多出的那条就停掉（`stop_session_turn`，必要时 `archive_sessions`），再带 `target_session_id` 重发。

**终态只落库不唤醒。** `done` / `rework` 只落库不唤醒，不为通知再发消息；唤醒只留给 `created` 误建、`blocked`、`ask_ticket` 三种。三份文档说的是同一套做法，完整口径以 `docs/protocol.md` 的「回信落库：一张单最多一条 `sent`，没过回执的单合不进去」一节为准。

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
