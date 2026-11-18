# 29 节点总表（N01–N29）

每个节点是**一次判断**，不是一个步骤。判断发生时才需要它，所以表按「你正在判断什么」组织，不按流程顺序。

## 列的解释

| 列 | 含义 | 机器校验 |
| --- | --- | --- |
| 编号 | `N01`–`N29`，连续、唯一 | 必须齐 N01..N29，不许重号空号 |
| 节点 | 要判断的问题 | —— |
| 归口 | 读哪个 skill，或执行哪条确定性动作 | 必须非空；`skill:` 前缀的名字必须能在 `.agents/skills/`、`~/.agents/skills/` 或 cindy-managed 清单里找到 `SKILL.md` |
| 状态 | 已接 / 半接 / 无 skill | —— |
| 兜底动作 | 归口 skill 不存在、读不到或没给出结论时做什么 | 必须非空 |

**归口单元格格式**：一条或多条，用 ` + ` 分隔，每条以 `skill:` 或 `action:` 开头。`action:` 后面必须是可执行的命令，或一句可判定的规则（能当场答「做到了没有」）。

## 总表

| 编号 | 节点 | 归口 | 状态 | 兜底动作 |
| --- | --- | --- | --- | --- |
| N01 | 判断当前对话属于哪个项目、该不该问人 | `skill:project-routing` | 已接 | 跑 `python -m task_center.probe --roots D:/ --depth 2 --explain`，它只给候选不给答案；仍不能确定就列候选问人，不许从聊天语义推断归属 |
| N02 | 动手前查有没有活跃尝试 | `action:python tools/work_gate.py --list` | 无 skill | 输出为空才允许自己新建尝试；有活跃尝试就先问那个会话，不并写同一批文件 |
| N03 | 这件事要不要开工单、切多细 | `action:规则：一次可独立验收的改动等于一张工单，验收条件写不出证据命令就说明切得还太粗` | 无 skill | 想不清拆分维度时，按「交付物能不能被另一个会话单独判定」切；切完仍跨两个项目，就开两张单 |
| N04 | 写 PRD、需求条目、验收条件 | `skill:prd-authoring` | 已接 | 落笔前先读 `PRD/` 下最近一份当体例参照；写不出验收条件说明需求还没成形，回去问人而不是先写正文 |
| N05 | 需求变更的影响面：哪些在途工单失效 | `skill:prd-authoring` | 半接 | 跑 `python -m task_center --db {{WORKSPACE}}/.data/task-center.sqlite3 list_tickets --input -`（`project_id` 传项目名）列出在途工单，逐张对 `prd_sha256`；对不上的都按 `STALE_PRD` 处理并 `revise_ticket` |
| N06 | 把需求落成工单：`deliverable` 的主题与形状、`criteria` 拆分 | `action:规则：deliverable 写清主题、形状与明确不覆盖什么；criteria 每条附一条可跑的证据命令` | 无 skill | 写不出证据命令的条件一律不进 `criteria`，改写进 `deliverable` 的「不覆盖」，或先退回 PRD |
| N07 | 验收条件自检：可测、有证据形态、能举反例 | `skill:prd-authoring` | 已接 | 给每条条件编一个「故意做错」的版本，跑一遍能不能被判出来；判不出来就说明条件不可测 |
| N08 | 拆任务、排顺序、选方法 | `skill:plan-gate` | 已接 | 先 `skill:planning-and-task-breakdown` 排依赖，再 `skill:writing-plans` 定每步的完成证据；两个都读不到就按「能被别人独立验证的最小一步」切 |
| N09 | 目标本身不清楚、多方案难选 | `skill:brainstorming` | 已接 | 先把候选方案各写一句「它赌的是什么」，赌不清的方案直接删；仍然两个都成立就把选择推给人 |
| N10 | 计划落盘并过闸 | `skill:plan-gate` | 已接 | 跑 `python tools/doc_gate.py tasks/plan-00N-名称.md`，非零就不许开工，改计划而不是改闸门 |
| N11 | 领取工单并选派发载体 | `skill:ticket-dispatch` | 已接 | 没有新建项目会话能力时才降级为一次性子任务，并在 `evidence` 里写明「载体是一次性子任务，无法追问」；两者都没有就停下问人 |
| N12 | 会话标识取证与 `list_sessions` 反查 | `action:规则：自己的 session_id 只从宿主取（get_current_session_id 或创建回执），两个 id 都要在 list_sessions 里查到才允许绑定或提交` | 无 skill | 查不到、标题或工作目录对不上，一律当标识不可信，先修来源；禁止从临时目录名或任何路径拼一个字符串顶上 |
| N13 | 绑定前核对目录与回执 | `skill:ticket-dispatch` | 已接 | 回执里的 `project_root` 与本次实际目录不一致就不绑，先发消息问派发方 |
| N14 | 写实现代码（先红后绿） | `skill:test-driven-development` | 已接 | 先写会失败的用例并真跑一次记下输出，再写实现；`executing-plans` 读不到时按同一顺序手工做，不许先写实现后补测试 |
| N15 | 调试、测试红了、行为不对 | `skill:systematic-debugging` | 已接 | 复现→读全栈与输入→缩小到最小失败单元；连复现都做不到时，先补一条能复现的用例，再谈修 |
| N16 | 提交前自检 | `skill:verification-before-completion` | 已接 | 每条验收条件跑一次并复制真实输出；跑不了的条件在 `evidence` 里写明跑不了的原因，不许写「已验证」 |
| N17 | `submit_result` 的 artifacts / evidence 写法与 `STALE_PRD` | `skill:ticket-dispatch` | 半接 | `artifacts` 只写相对 `{{WORKSPACE}}` 的路径；报 `STALE_PRD` 时先 `revise_ticket` 重取摘要再提交，不许改标题绕过 |
| N18 | 独立验收逐项判定 | `skill:ticket-dispatch` | 半接 | 逐条对照条件写命令与真实输出，打勾或打叉并给理由；判不出来的条件按不通过处理，不许「整体感觉没问题」 |
| N19 | 多轴审查别人的产出 | `skill:code-review-and-quality` | 已接 | 按维度分块看（正确性、证据、边界、可维护性），每块至少给一条具体位置；`requesting-code-review` 用于发起，不是替代审查 |
| N20 | 收到审查意见怎么回应 | `skill:receiving-code-review` | 已接 | 每条意见先独立验证再答复，写明「照改」或「不改+理由」；不许嘴上同意、代码不动 |
| N21 | 偏差归类：实现错 / 计划错 / 外部条件 | `skill:pdsa-workflow` | 已接 | 分不清就先问「换个会话按原计划重做，能过吗」：能过是实现错，不能过是计划错，缺人或缺权限是外部条件 |
| N22 | 验收通过后合并进 master | `skill:merge-latest-branches` | 已接 | 合并前 `git status` 必须干净、用例全绿；有冲突先停下报人，不许自动解冲突后直接合 |
| N23 | 进度记录与收口 | `action:python -m task_center --db {{WORKSPACE}}/.data/task-center.sqlite3 events --input -`（`project_id` 与 `ticket_id` 必填） | 无 skill | 中心库是唯一进度来源；读不到事件就据实说明读不到，不许另写一份 Markdown 进度当依据 |
| N24 | 写记忆（user / feedback / project / reference） | `action:规则：走 cindy_memory 的 memory_write，一条 description 写不出来就不写` | 无 skill | 写不出「以后什么情况下会用到它」就不写；临时进度、代码结构、git 历史不写进记忆 |
| N25 | 新建或修改 skill 本身 | `skill:writing-skills` | 已接 | 改 skill 前先跑一遍它的触发场景，确认 description 能被命中；`cindy-skill-creator` 只在发布到 Cindy SkillHub 时用 |
| N26 | 文档与回复的语言质量 | `skill:shuo-ren-hua` | 已接 | 写完读出声：读起来像标签堆叠、省主语、倒装的，改回完整句子再交 |
| N27 | 多任务并行与长任务断点续接 | `skill:subagent-driven-development` | 已接 | 并行前先确认各任务无共享文件；有共享状态就串行。长任务按 `skill:resumable-implementation-contracts` 落稳定任务 ID 与续跑点，`skill:dispatching-parallel-agents` 管分派 |
| N28 | 开工前隔离工作区 | `skill:using-git-worktrees` | 已接 | 同一工作树已有别的活跃尝试时，先建 worktree 再动手；建不出来就停下告诉人，不许在共享工作树上并写 |
| N29 | 我要把一条消息送进一个正在跑的会话：现在投还是插进这一轮，发完怎么确认打中了原目标 | `action:先 list_session_queue 看目标在不在跑；在跑且这条消息要改的是它当前这一轮的产出，就 steer_session 直接注入（或先 send_to_session 拿到 queued_message_id 再 steer_session_queued_message），否则 send_to_session 并带 target_session_id；发完读回执的 wake_kind：created 就是发错了目标，其余任何非空取值（queued / resumed / already-active 等）都算打中 | 无 skill | 目标不在跑、或 steer 回执是 delivery=queued（这一轮已收尾，插不进去）时，按 `PRD/PRD-005-决策通道.md` §8.2 的四条判据重新判断该走哪一档，改投递并明确告诉人这一轮的产出已经按旧指令做完；回执出现 `wake_kind=created` 时先停掉新建的那条任务，再带 `target_session_id` 重发 |

## 与本表配套的文件

| 文件 | 内容 |
| --- | --- |
| `roles.md` | 协调 / 执行 / 验收三类会话的最小入口表 |
| `installed-skills.md` | 本机已装 skill 的处置表（接入某节点，或明确不接 + 项目面） |
| `gaps.md` | 没有 skill 可归口的节点、兜底动作、这轮不新建 skill 的理由 |
| `scripts/check_routing.py` | 反向校验：节点齐否、归口存在否、已装 skill 处置否、角色表引用有效否 |
