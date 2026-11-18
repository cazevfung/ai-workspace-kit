# 架构：这套东西是怎么咬合的

## 1. 三个平面

```
┌─────────────────────────────────────────────────────────────┐
│ 指令面   AGENTS.md · skills/*/SKILL.md · docs/*              │  agent 读
├─────────────────────────────────────────────────────────────┤
│ 状态面   .data/task-center.sqlite3 · tasks/ · PRD/            │  唯一真相
├─────────────────────────────────────────────────────────────┤
│ 执行面   tools/*.py · .githooks/pre-commit · git worktree    │  会非零退出
└─────────────────────────────────────────────────────────────┘
```

分开的意义：**指令面**告诉模型该怎么做，但模型可能没做；**执行面**把「该做而没做」
变成一个确定性的失败；**状态面**让下一个会话能查到发生了什么。三者缺一，系统就退化成聊天记录。

## 2. 数据模型

```
projects ──< tickets ──< attempts ──< submissions ──< reviews
                 │            │
                 └── events   └── notices
```

| 表 | 一行代表什么 |
| --- | --- |
| `projects` | 一个已登记的目录（`root` 是身份，`project_id` 是标签） |
| `tickets` | 一张工单：交付物、验收条件、PRD 摘要、当前状态与 revision |
| `attempts` | 一次领取：哪个协调会话、哪个执行会话、工作目录、状态 |
| `submissions` | 一次提交：摘要、成果文件指纹、证据、风险 |
| `reviews` | 一次验收：逐项 checks、结论（accept / rework）、理由 |
| `events` | 状态迁移的追加日志 |
| `notices` | 执行会话给协调会话的回执（含 `wake_kind`） |
| `questions` / `answers` | `ask_ticket` 的待决策问题 |
| `merges` | 验收后合并的记录 |

所有写操作带 `request_id` 幂等键；同编号同参数重试返回原回执，换参数报 `REQUEST_CONFLICT`。

## 3. 一张工单的生命周期

```
ready ──claim_ticket──> dispatching ──bind_session──> running
  ^                                                      │
  │                                              submit_result
  │                                                      v
  └──────rework──── reviews <──review_result── awaiting_review
                          │
                          └──accept──> done ──merge_ticket──> (merges 记录)
```

- `blocked` 由 `ask_ticket` / `recover_ticket` 产生，解除后回到原状态。
- 同项目默认只允许一张在途单（`dispatching` / `running` / `awaiting_review` / `blocked`）。
- `ready` / `rework` 可领取；`done` / `cancelled` 终结。

## 4. 四道闸门各管什么

| 闸门 | 触发时机 | 判据 | 失败时 |
| --- | --- | --- | --- |
| `work_gate` | 每次 `git commit` | 有工单、属于本项目、当前尝试已绑定且 `running` | 拒绝提交并打印怎么补 |
| `doc_gate` | 写计划 / 本轮目标后 | 六节齐全、依据带 PRD 版本、skill 真被点名并说明改了什么、任务有证据 | 列出缺哪节 |
| `close_gate` | 宣告一轮结束前 | 七节收尾包、结论三档之一、状态位与结论一致；`--check` 再加工作区干净、无在途单 | 非零并点名 |
| `intake_gate` | 把长线任务交给工作区前 | 六节交接包齐全、每节满足必填判据 | 拒绝接收 |

闸门不是安全边界：它们拦「忘了」，不拦「故意」。`--no-verify` 与
`git config --unset core.hooksPath` 都能绕过，前提是你知道自己在关什么。

## 5. 技能怎么参与

- `skill-routing` 是入口：29 个判断节点，每个归到一个 skill 或一条确定性动作。
- `plan-gate` 在写计划前强制「读 PRD、挑 skill、真读、过 doc_gate」。
- `prd-authoring` / `ticket-dispatch` / `pdsa-workflow` 分别管需求写法、派发顺序、偏差归类。
- 20 个通用技能提供实现、调试、审查、验证、合并等具体方法。

技能装在 `~/.agents/skills`，因此对**所有项目**可见；工作区里的 `.agents/skills`
是同一批内容的项目内副本，方便版本化。

## 6. 合并与工作树

- 每张工单一个 `.worktrees/<工单号>`，检出 `ticket/<工单号>`；主检出永远在 `master`。
- 验收通过后由协调会话跑 `tools/auto_merge.py`：预演 → 临时工作树里合并并跑测试 →
  绿了才真合并 → 再跑一次测试 → 写合并记录。
- 断在半路（合并成功但没记录）用 `--record-only` 收口，不手工补记录。

## 7. 已知边界

- 派发那一步（`claim` 之后新建执行会话）依赖宿主能力，不是本仓库代码。
- 九个概念态只有七个能在库里表达；`待规划` / `验收中` / `受阻或待决策` 用 reason 与 events 表达。
- 项目级事件写得出、读不出（`ticket_id` 为 NULL，而 `events` 操作要求工单号）。
- 没有定时任务、看板与自动推进。
