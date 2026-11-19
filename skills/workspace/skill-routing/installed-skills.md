# 已装 skill 处置表

**处置只有两种，缺一种都算错**：`接入` 某个节点，或 `明确不接` 并写明它属于哪个项目面。
本表是反向覆盖的依据——一次「装了但没接、也没说不接」的漏项，就是一次静默失效。

## 已装清单的来源

| 来源 | 路径 | 数量 |
| --- | --- | --- |
| 本仓库 | `.agents/skills/` | 4 |
| 本机通用 | `~/.agents/skills/` | 33 |
| Cindy 托管 | 会话提示里列出的 `cindy-managed-skills`（路径随会话变化，名字固化为清单） | 5 |

合计 42 个已装 skill。处置表另有 1 行是入口自身（`skill-routing`），因为脚本把入口也当成已装 skill，不处置会报 `undispositioned-skill`——所以处置表一共 43 行。托管清单的 5 个名字写在 `scripts/installed-manifest.txt`，脚本会把它们和磁盘上扫到的名字合并，所以运行目录消失后校验仍然有效。

## 处置表

| 已装 skill | 来源 | 处置 | 节点 / 项目面 | 说明 |
| --- | --- | --- | --- | --- |
| prd-authoring | 本仓库 | 接入 | N04 N05 N07 | 需求条目、验收条件、变更影响面的唯一写法出处 |
| plan-gate | 本仓库 | 接入 | N08 N10 | 规划闸门：该读的先读、该调的 skill 真调、计划过 `doc_gate` |
| pdsa-workflow | 本仓库 | 接入 | N21 | 偏差归类与暂停/返工的判断依据 |
| ticket-dispatch | 本仓库 | 接入 | N11 N13 N17 N18 | 领取、绑定、提交、验收的调用顺序与报错码 |
| project-routing | 本机通用 | 接入 | N01 | 项目归属与「该不该问人」 |
| handoff-to-workspace | 本机通用 | 接入 | N01 | 用户点名「找 AI 工作区」「交给工作区」时的直接入口：只管「要不要交给工作区」，`project-routing` 管「这次属于哪个项目」 |
| planning-and-task-breakdown | 本机通用 | 接入 | N08 | 任务依赖与顺序 |
| writing-plans | 本机通用 | 接入 | N08 | 每步的完成证据、计划结构 |
| brainstorming | 本机通用 | 接入 | N09 | 目标不清、多方案难选时先问再落笔 |
| test-driven-development | 本机通用 | 接入 | N14 | 先红后绿的实现顺序 |
| executing-plans | 本机通用 | 接入 | N14 | 按计划实施时的执行步序 |
| systematic-debugging | 本机通用 | 接入 | N15 | 红的、行为不对时的排查顺序（原先装了没接） |
| verification-before-completion | 本机通用 | 接入 | N16 | 说「做完了」之前的取证要求 |
| code-review-and-quality | 本机通用 | 接入 | N19 | 多轴审查的维度划分 |
| requesting-code-review | 本机通用 | 接入 | N19 | 发起审查时要提供的材料 |
| receiving-code-review | 本机通用 | 接入 | N20 | 收到意见先验证再答复 |
| merge-latest-branches | 本机通用 | 接入 | N22 | 验收通过后合并的顺序与前置检查（原先装了没接） |
| writing-skills | 本机通用 | 接入 | N25 | 新建或修改 skill 的写法与验证 |
| shuo-ren-hua | 本机通用 | 接入 | N26 | 文档与回复的语言质量（原先装了没接） |
| dispatching-parallel-agents | 本机通用 | 接入 | N27 | 多个独立任务并行时的分派判据（原先装了没接） |
| subagent-driven-development | 本机通用 | 接入 | N27 | 子代理执行计划时的上下文与回收（原先装了没接） |
| resumable-implementation-contracts | 本机通用 | 接入 | N27 | 长任务断点续接的稳定任务 ID 与续跑点（原先装了没接） |
| using-git-worktrees | 本机通用 | 接入 | N28 | 开工前的隔离工作区 |
| doubt-driven-development | 本机通用 | 接入 | N19 | 高风险改动的对抗性复核，挂在审查节点下 |
| cindy-skill-creator | Cindy 托管 | 接入 | N25 | 发布到 Cindy SkillHub 时才用 |
| learn | Cindy 托管 | 接入 | N25 | 从一次任务蒸馏出可复用 skill 的流程 |
| context-engineering | 本机通用 | 明确不接 | 工具环境面：本机 agent 的上下文与规则文件配置，不属于本工作区任何一个判断节点 | 与工单流程无关，接了会误导 |
| first-principles-explain | 本机通用 | 明确不接 | 通用问答面：概念解释，且已设 `disable-model-invocation` | 不参与工程判断 |
| community_crawl | 本机通用 | 明确不接 | 社区运营项目面：多平台数据抓取（TapTap、B 站、Reddit 等） | 属于内容与运营项目的执行手段 |
| content-heat-tracker | 本机通用 | 明确不接 | 内容生产项目面：心动小镇各平台官号热度追踪表 | 业务表操作，不是本工作区判断节点 |
| kb-context | 本机通用 | 明确不接 | 内容生产项目面：心动小镇内容与个人履历的既有判断 | 动手前查旧判断用，不参与工单流程 |
| feedback-coding | 本机通用 | 明确不接 | 内容研究项目面：访谈/试玩等口述材料的编码分析 | 产出是问题点报告，不做工程交付 |
| notice-cards | 本机通用 | 明确不接 | 内容生产项目面：3:4 通知卡与图文出图 | 出图产出，不是判断节点 |
| preview-script-review | 本机通用 | 明确不接 | 内容生产项目面：版本前瞻台词脚本评审 | 专项文案评审 |
| square4 | 本机通用 | 明确不接 | 内容生产项目面：四字方图排版出图 | 出图产出 |
| video-app-links | 本机通用 | 明确不接 | 内容生产项目面：视频外链的 App/网页分流跳转 | 页面实现细节 |
| feishu-doc-publish | 本机通用 | 明确不接 | 文档发布面：往飞书放文档（新建文档、定好知识库位置、表格列宽） | 它是产出物的落地手段，只决定交出去的东西放哪，不参与工单归属、派发与验收的判断 |
| reverse-everything | 本机通用 | 明确不接 | 第三方工程面：无源码软件/游戏的逆向与解包 | 与本工作区工单流程无关 |
| taptap-suite | 本机通用 | 明确不接 | TapTap 业务平台面：开发者后台资料、包体、审核、社区管理 | 平台业务，不是本工作区节点 |
| xdoa-skill | 本机通用 | 明确不接 | 公司事务面：OA、VPN、账号、会议室、审批流 | 公司行政事务 |
| cindy-plugin-creator | Cindy 托管 | 明确不接 | 工具环境面：Cindy 插件（ghost）的开发与发布 | 插件不是 skill，本工作区 29 节点无对应 |
| cindy-theme-creator | Cindy 托管 | 明确不接 | 工具环境面：Cindy 主题包与外观 | 纯外观配置 |
| pr | Cindy 托管 | 明确不接 | 代码托管面：GitHub PR 从分支到合并的流程 | 本工作区不做 push 与 PR，合并走 N22 |
| skill-routing | 本仓库 | 接入 | N01 N02 N03 N04 N05 N06 N07 N08 N09 N10 N11 N12 N13 N14 N15 N16 N17 N18 N19 N20 N21 N22 N23 N24 N25 N26 N27 N28 N29 | 入口本身：所有节点的归口都要先经它查出来；它不替代任何节点的 skill |

## 维护规则

新增一个已装 skill 后，**本表加一行**，否则 `scripts/check_routing.py` 会以「已装未处置」非零退出。
把某个 `明确不接` 改成 `接入` 时，同时更新 `nodes.md` 的归口——两处不一致时以 `nodes.md` 为准，本表只负责「有没有被处置」。
