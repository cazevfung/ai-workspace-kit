# 31 节点总表（N01–N31）

每个节点是**一次判断**，不是一个步骤。判断发生时才需要它，所以表按「你正在判断什么」组织，不按流程顺序。

## 列的解释

| 列 | 含义 | 机器校验 |
| --- | --- | --- |
| 编号 | `N01`–`N31`，连续、唯一 | 必须齐 N01..N31，不许重号空号 |
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
| N03 | 这件事要不要开工单、切多细 | `action:规则：先分两类——价值在「想清楚」的活（新功能设计、PRD、方法论）留在当前会话做深，产出是讨论透的方案文档，不开工单；价值在「做对」的活（改文件、加字段、迁移、脚本）才进工单，一次可独立验收的改动等于一张工单，验收条件写不出证据命令就说明切得还太粗` | 无 skill | 想不清拆分维度时，按「交付物能不能被另一个会话单独判定」切；切完仍跨两个项目，就开两张单；分不清是哪类时问一句「交付物的价值主要在写出来之前的想清楚，还是写出来的结果本身」；答不上来按设计类处理 |
| N04 | 写 PRD、需求条目、验收条件 | `skill:prd-authoring` | 已接 | 落笔前先读 `PRD/` 下最近一份当体例参照；写不出验收条件说明需求还没成形，回去问人而不是先写正文 |
| N05 | 需求变更的影响面：哪些在途工单失效 | `skill:prd-authoring` | 半接 | 跑 `python -m task_center --db {{WORKSPACE}}/.data/task-center.sqlite3 list_tickets --input -`（`project_id` 传项目名）列出在途工单，逐张对 `prd_sha256`；对不上的都按 `STALE_PRD` 处理并 `revise_ticket` |
| N06 | 把需求落成工单：`deliverable` 的主题与形状、`criteria` 拆分 | `action:规则：deliverable 写清主题、形状、明确不覆盖什么，以及「做成之后用户/系统能做什么」（结果，不是动作）；criteria 每条附一条可跑的证据命令，功能类工单至少一条结果级「判断类+反例」（在 X 场景下符合 PRD §Y 的意图；反例：字面命令全过但场景下不成立）；功能类工单的 deliverable 带「三行圈」：Why（1–2 句内容，这句必须能改变执行中的判断；改不了任何决定的就是装饰，重写；可追溯的指针附在句后，不替代内容）、How（1–2 句，已定路径约束与不许重新决定的决定）、What（交付物）；主任务没有 PRD 时，Why 由协调会话从对话里原样誊入，不许让执行会话自己翻记录；三行写不像话说明还没想清楚，退回不开单` | 无 skill | 写不出证据命令的条件一律不进 `criteria`，改写进 `deliverable` 的「不覆盖」，或先退回 PRD |
| N07 | 验收条件自检：可测、有证据形态、能举反例 | `skill:prd-authoring` | 已接 | 给每条条件编一个「故意做错」的版本，跑一遍能不能被判出来；判不出来就说明条件不可测 |
| N08 | 拆任务、排顺序、选方法 | `skill:plan-gate` | 已接 | 先 `skill:planning-and-task-breakdown` 排依赖，再 `skill:writing-plans` 定每步的完成证据；两个都读不到就按「能被别人独立验证的最小一步」切 |
| N09 | 目标本身不清楚、多方案难选 | `skill:brainstorming` | 已接 | 先把候选方案各写一句「它赌的是什么」，赌不清的方案直接删；仍然两个都成立就把选择推给人 |
| N10 | 计划落盘并过闸 | `skill:plan-gate` | 已接 | 跑 `python tools/doc_gate.py <项目根>/tasks/plan-00N-名称.md`（先落进服务对象项目的 `tasks/`），非零就不许开工，改计划而不是改闸门 |
| N11 | 领取工单并选派发载体 | `skill:ticket-dispatch` | 已接 | 没有新建项目会话能力时才降级为一次性子任务，并在 `evidence` 里写明「载体是一次性子任务，无法追问」；两者都没有就停下问人 |
| N12 | 会话标识取证与 `list_sessions` 反查 | `action:规则：自己的 session_id 只从宿主取（get_current_session_id 或创建回执），两个 id 都要在 list_sessions 里查到才允许绑定或提交` | 无 skill | 查不到、标题或工作目录对不上，一律当标识不可信，先修来源；禁止从临时目录名或任何路径拼一个字符串顶上 |
| N13 | 绑定前核对目录与回执 | `skill:ticket-dispatch` | 已接 | 回执里的 `project_root` 与本次实际目录不一致就不绑，先发消息问派发方 |
| N14 | 写实现代码（先红后绿） | `skill:test-driven-development + skill:ui-design` | 已接 | 先写会失败的用例并真跑一次记下输出，再写实现；`executing-plans` 读不到时按同一顺序手工做，不许先写实现后补测试 |
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
| N29 | 我要把一条消息送进一个正在跑的会话：发之前该不该发、发什么，还是改成插进这一轮，发完怎么确认打中了原目标 | `action:发之前先 list_session_queue 看目标队列（或用 list_sessions 的 queuedCount）：queued_count 大于 0 就不再叠加，改走 update_session_queued_message 原地改写（同一事项、consuming=false、自己投递三条同时成立时就是合并成一条，覆盖式改写不拼正文）或 cancel_session_queued_message 撤掉已经过时的那条（只撤自己投递的）、或 steer_session_queued_message 提进当前轮，或等它消费完；队列空时才 send_to_session 并带 target_session_id，正文只写指向 get_ticket 当前状态的指令，不写 attempt_id / commit sha / 分支 head / revision / 行号；在跑且这条要改的是它当前这一轮的产出，就 steer_session 直接注入（或先 send_to_session 拿到 queued_message_id 再 steer_session_queued_message）；发完读回执的 wake_kind：created 就是发错了目标，其余任何非空取值（queued / resumed / already-active 等）都算打中；无人值守时没有活会话可核队列、可插话，判据换成「notices 里有一行写清去向」——发不出去就走 report_notice_failure 落 failed 并写清目标已归档，推进交给下一轮 next_action` | 无 skill | 目标不在跑、或 steer 回执是 delivery=queued（这一轮已收尾，插不进去）时，按 `PRD/PRD-005-决策通道.md` §8.2 的四条判据重新判断该走哪一档，改投递并明确告诉人这一轮的产出已经按旧指令做完；回执是 MESSAGE_CONSUMING 说明那条正在投递、等它投完再改，回执是 QUEUED_MESSAGE_NOT_FOUND 不能当撤回成功、重查一次队列；回执出现 `wake_kind=created` 时先停掉新建的那条任务，再带 `target_session_id` 重发 |
| N30 | 共用可变资源的身份：在真机、部署环境、共享数据库、共享目录上取证之前该先读什么，读完发现不是自己那一份怎么办，用完怎么还回去 | `skill:shared-resource-identity` | 已接 | 读不出身份就写「本次身份未取到」并把该条证据标未验证，不许用「应该是那台」补位；采集前后两次读到的不一样就是身份变过，那条作废重采、留在记录里不删；设备上是别人的包先停手，重装会毁掉别人手工做的登录态时先 `ask_ticket` 落库问人，不许直接 `uninstall` |
| N31 | 干活时发现了一个不属于本单的问题：它落在哪个项目的哪张单 | `action:当场 create_ticket（project_id 用 N01 定的那个、state=planned、deliverable 一句话写清问题与形状），回执里的 ticket_id 就是落点；不 claim、不派发、不 bind | 无 skill | 定不了属于哪个项目就先跑 `python -m task_center.probe --roots D:/ --depth 2 --explain` 拿候选问人；`create_ticket` 报 `CRITERIA_NOT_JUDGEABLE` 就把 deliverable 收窄成一句话问题再开；不值一张单的写进本次收尾包第 4 节给原因，也算处置 |

**N03 的分类自问：** 分不清一件活属于「想清楚」还是「做对」时，按兜底列那句话问一遍；答不上来按设计类处理。

## 与本表配套的文件

| 文件 | 内容 |
| --- | --- |
| `roles.md` | 协调 / 执行 / 验收三类会话的最小入口表 |
| `installed-skills.md` | 本机已装 skill 的处置表（接入某节点，或明确不接 + 项目面） |
| `gaps.md` | 没有 skill 可归口的节点、兜底动作、这轮不新建 skill 的理由 |
| `scripts/check_routing.py` | 反向校验：节点齐否、归口存在否、已装 skill 处置否、角色表引用有效否 |
