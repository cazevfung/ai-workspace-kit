# 已装 skill 处置表

**处置只有两种，缺一种都算错**：`接入` 某个节点，或 `明确不接` 并写明它属于哪个项目面。
本表是反向覆盖的依据——一次「装了但没接、也没说不接」的漏项，就是一次静默失效。

## 套件自带的 skill

套件安装后，这些 skill 会出现在 `~/.agents/skills/` 下（工作区技能同时在项目的 `.agents/skills/` 里）。下表把它们逐个接到 29 个节点之一；`skill-routing` 是入口自身，单列一行。

| 来源 | 路径 | 数量 |
| --- | --- | --- |
| 工作区技能 | 项目的 `.agents/skills/` | 5 |
| 通用技能 | `~/.agents/skills/` | 20 |

合计 25 个技能，加入口自身共 26 行。

## 处置表

| 已装 skill | 来源 | 处置 | 节点 / 项目面 | 说明 |
| --- | --- | --- | --- | --- |
| prd-authoring | 工作区 | 接入 | N04 N05 N07 | 需求条目、验收条件、变更影响面的唯一写法出处 |
| plan-gate | 工作区 | 接入 | N08 N10 | 规划闸门：该读的先读、该调的 skill 真调、计划过 `doc_gate` |
| pdsa-workflow | 工作区 | 接入 | N21 | 偏差归类与暂停/返工的判断依据 |
| ticket-dispatch | 工作区 | 接入 | N11 N13 N17 N18 | 领取、绑定、提交、验收的调用顺序与报错码 |
| project-routing | 通用 | 接入 | N01 | 项目归属与「该不该问人」 |
| handoff-to-workspace | 通用 | 接入 | N01 | 用户点名「找 AI 工作区」「交给工作区」时的直接入口：只管「要不要交给工作区」，`project-routing` 管「这次属于哪个项目」 |
| planning-and-task-breakdown | 通用 | 接入 | N08 | 任务依赖与顺序 |
| writing-plans | 通用 | 接入 | N08 | 每步的完成证据、计划结构 |
| brainstorming | 通用 | 接入 | N09 | 目标不清、多方案难选时先问再落笔 |
| test-driven-development | 通用 | 接入 | N14 | 先红后绿的实现顺序 |
| executing-plans | 通用 | 接入 | N14 | 按计划实施时的执行步序 |
| systematic-debugging | 通用 | 接入 | N15 | 红的、行为不对时的排查顺序（原先装了没接） |
| verification-before-completion | 通用 | 接入 | N16 | 说「做完了」之前的取证要求 |
| code-review-and-quality | 通用 | 接入 | N19 | 多轴审查的维度划分 |
| requesting-code-review | 通用 | 接入 | N19 | 发起审查时要提供的材料 |
| receiving-code-review | 通用 | 接入 | N20 | 收到意见先验证再答复 |
| merge-latest-branches | 通用 | 接入 | N22 | 验收通过后合并的顺序与前置检查（原先装了没接） |
| writing-skills | 通用 | 接入 | N25 | 新建或修改 skill 的写法与验证 |
| shuo-ren-hua | 通用 | 接入 | N26 | 文档与回复的语言质量（原先装了没接） |
| dispatching-parallel-agents | 通用 | 接入 | N27 | 多个独立任务并行时的分派判据（原先装了没接） |
| subagent-driven-development | 通用 | 接入 | N27 | 子代理执行计划时的上下文与回收（原先装了没接） |
| resumable-implementation-contracts | 通用 | 接入 | N27 | 长任务断点续接的稳定任务 ID 与续跑点（原先装了没接） |
| using-git-worktrees | 通用 | 接入 | N28 | 开工前的隔离工作区 |
| doubt-driven-development | 通用 | 接入 | N19 | 高风险改动的对抗性复核，挂在审查节点下 |
| skill-routing | 工作区 | 接入 | N01 N02 N03 N04 N05 N06 N07 N08 N09 N10 N11 N12 N13 N14 N15 N16 N17 N18 N19 N20 N21 N22 N23 N24 N25 N26 N27 N28 N29 | 入口本身：所有节点的归口都要先经它查出来；它不替代任何节点的 skill |

## 维护规则

新增一个 skill 后，**本表加一行**并接到某个节点（或写明「明确不接」+ 项目面），同时在 `nodes.md` 补归口——两处不一致时以 `nodes.md` 为准，本表只负责「有没有被处置」。
