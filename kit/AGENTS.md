# AI 工作区 · 会话入口说明

这个目录是**协调项目**，不是业务工作的执行目录。业务执行与验收发生在对应业务项目的会话里，成果留在那个项目里。

本文件是这套工作区的行为契约。规则写得具体，是因为每一条都对应一次真实踩过的坑；照着做不会更慢，绕过去才会。

## 进来先做什么

**动手先开工单。** 要改任何东西（代码、文档、skill）先跑 `python tools/work_gate.py --list` 看自己有没有活跃尝试；没有就 `create_ticket` → `claim_ticket` → `bind_session`。只读问答不需要工单。两个仓库的 commit 钩子都会调 `work_gate.py`，没工单的提交会被当场拒绝。

1. 读 `README.md` 了解当前阶段，再读与本任务相关的 PRD（`PRD/PRD-00N-*.md`）。
2. 动手改工单状态之前，读 `docs/protocol.md`——调用顺序与错误码在那里，不在本文件。
3. 不确定某件事属于哪个项目时，跑 `python -m task_center.probe --roots <目录> --db <库> --explain`。它只给候选，不给你答案。
4. 任何会话开始、以及做到需要判断的节点时，先读 `.agents/skills/skill-routing/SKILL.md`：它把 29 个判断节点各归到一个 skill 或一条确定性动作；归口缺失或引用了不存在的 skill，会在 `.agents/skills/skill-routing/scripts/check_routing.py` 那里当场失败。

## 做规划之前

写计划、拆任务、定方案之前先读 `.agents/skills/plan-gate/SKILL.md`。它要求四件事，缺一件就算没做规划：

1. 先读 `AGENTS.md` + 相关 PRD，**记下 PRD 编号与版本号**。
2. 按任务类型挑 skill，并**真的打开 `SKILL.md`** 读，不是只看描述。
3. 计划写到 `tasks/plan-00N-名称.md`，必须含 `依据 / 所用 skill / 交付物 / 任务 / 不做什么 / 未决` 六个节；`交付物` 要写清**主题**与**不覆盖什么**。
4. 跑 `python tools/doc_gate.py tasks/plan-00N-名称.md`，不过就改，不许先开工。

skill 只有名字和描述会进提示，模型可能该用而没用；脚本的退出码是确定性的，所以约束放在产物上。

## 六条硬规则

**一，项目归属不许猜。** 宿主回执里有 `project_root` 就用它；没有就列候选问人。禁止从聊天语义推断「你说的应该是 X 项目」。绑定错项目的代价比多问一句高一个数量级。

**二，写需求必须报版本。** 任何工单、计划、验收结论都要写清依据的是哪份 PRD 的哪个版本。PRD 内容一改，旧成果自动失效（`STALE_PRD`），这不是意外。

**三，验收条件不许在执行中改。** 觉得验收条件不合理，走 `revise_ticket` 显式修订并说明影响哪些工单；不许在实施过程中悄悄降低标准，也不许由执行者自己验收。

**四，没有工单不许改文件。** 规则本身不拦人，所以拦在落盘那一步：`tools/work_gate.py` 核对「工单存在、属于本仓库所属项目、当前尝试已绑定且 `running`」，commit 钩子会调它。没设置 `TASK_CENTER_TICKET`，或者工单还停在 `ready`/`dispatching`，提交直接被拒。它不是安全边界（`--no-verify` 能绕过），作用是把「没有工单」变成一个当场失败的默认路径。

**合并提交是唯一例外**：合并发生在验收之后，那一刻工单已经是 `done`。所以当仓库里存在未结束的合并（`MERGE_HEAD`）且工单是 `done`、还没有合并记录时，闸门放行并输出「合并提交：<工单号>」。普通提交的规则一字不变；已记过合并的工单再出现合并提交会被拦。

**五，派发就是新开一个会话。** 协调会话 claim 之后，执行这张工单的必须是另一个会话：`claim_ticket` 时把自己的标识写成 `coordinator_session_id`，再用同一个标识 `bind_session` 会被拒（`SELF_DISPATCH`），工单停在 `dispatching`。载体按优先级选：

1. **项目普通会话**（默认）：`cindy_helper` 的 `send_to_session` **不传** `target_session_id` 即新建任务，指定 `project_path` 与 `title`。新会话能点开、能追问、能自己 `submit_result`。
2. **一次性独立上下文子任务**：只有宿主确实没有「新建项目会话」的能力时才用，并在 `submit_result` 的 `evidence` 里写明「载体是一次性子任务，无法追问」。
3. **两者都没有**：停下来告诉人。不许协调会话自己动手写成果、再自己提交。

自己的会话标识**必须向宿主取**：

- **协调会话**：用自己当前任务信息里的 session id。
- **执行会话**：用协调会话新建会话时**创建回执里返回的 id**，原样写进 `bind_session`。

提交前两边都要用 `cindy_helper` 的 `list_sessions`（按 `workdir`）反查这两个 id：两条都在、`title` 与 `workingDir` 符合预期，才可以继续。查不到就当标识不可信——不许绑定、不许提交，先修来源。自身标识用 `cindy_helper` 的 `call_tool` 调 `get_current_session_id` 取（它不在 `list_tools` 的搜索目录里，按名字搜返回空或 `UNKNOWN_TOOL`，但 `call_tool` 直接可调）。**禁止从临时目录名、运行目录或任何路径拆一个字符串当标识。**

这条的机制只能拦住「同一个标识替自己绑定」，换个字符串照旧能绕过，所以它是一个默认路径，不是认证。

**库里默认一次只跑一张单。** 同项目已有在途工单（`dispatching` / `running` / `awaiting_review` / `blocked`）时，`claim_ticket` 报 `DISPATCH_BUSY` 并点名那张单号与它停在哪一步，`ready`/`rework` 也领不走。节奏是「前一张单验收、合并完，再领下一张」；确实要并行（例如两张单互不碰文件）就显式传 `allow_parallel: true`。走过一轮 `rework` 的单不是免排队通道。

**六，消息通道不许乱用。** 「发出去」和「它这一轮看到」是两件事，入队成功不代表纠错生效。

固定三句模板：

- **我自己是谁，自取。** 执行会话用 `cindy_helper` 的 `call_tool` 调 `get_current_session_id`，取到什么写什么。
- **协调会话是谁，只能由它在派发消息里给。** 拿到后先用 `list_sessions` 反查 title 与 workingDir，对不上就不用。
- **做完要回一条，不是两条。** 一条消息写工单号 + 状态 + 提交 sha 或 review_id，正文里不写 `wake_kind`，也不为它另发一条消息。协调会话收到后 `record_notice` 落库（`wake_kind` 填 `unknown`，并在 `detail` 里注明这是占位），再跑一次 `list_sessions` 核对：没有多出新任务即算送达。

几条可判定动作：

1. **派发消息里不写死 session id。** 消息先于会话存在，写进去的任何 id 都是猜的。执行会话自己取，取到后照样 `list_sessions` 反查一次。
2. **纠错或补充之后必须核一遍队列。** `send_to_session` 只是入队：目标在跑就把你的话排在这一轮之后。发完用 `list_session_queue(session_id)` 看自己那条还在不在 `queue` 里；**目标已经过了一轮而它还在 = 「该插没插」**，立刻 `steer_session_queued_message({session_id, queued_message_id})`，回执要 `delivery=steered`。要改当前这一轮，也可以 `steer_session({session_id, message})` 直接注入。
3. **送达看会话列表，不看投递回执。** 协调会话收到回执后跑 `cindy_helper` 的 `list_sessions`（按 `workdir`），看是否多出一条标题就是回执正文的新任务——没有多出即算送达。`queued` / `resumed` / `already-active` 都只表示消息进了原会话、没有副作用；唯一有后果的是宿主把回执当成新建任务，而这件事在会话列表里看得见。查见多出的那条就停掉它（`stop_session_turn`，必要时 `archive_sessions`），再带 `target_session_id` 重发。派发时别忘带 `target_session_id`：不传时 `send_to_session` 的语义是「新建任务并立刻跑一轮」，不是「投递到某个会话」。
4. **终态只落库不唤醒。** `done` / `rework` 只落库：提交、验收、`record_notice` 都记进库，不为通知再发一条消息。唤醒只留给三种情况——`created` 误建了任务、工单 `blocked` 在等人一句话、`ask_ticket` 已经落了问题。
5. **工作区流程内的决策不用同步提问卡片。** 卡片占住本轮，别的会话连排队都进不去。要人拍板就走 `ask_ticket` 落库 + 结束本轮，靠新消息唤起。

## 收尾（一轮怎么算说完）

**一轮的收尾是一件能被判定的事，不是一句「做完了」。** `tasks/round.md` 顶部两行写 `轮次：R-00N` 与 `状态：…`（进行中 / 等一句答复 / 已收尾 / 需重开），历史轮次在 `tasks/rounds.md` 各占一行；要说这一轮完了，就按 `docs/closing.md` 写七节的包到 `tasks/closing-<轮次>.md`。

说「这一轮结束」之前先跑一次闸门，两条命令各管一层：

```bash
python tools/close_gate.py            # 七节齐全、第 3 节不是裸「无」、结论是三档之一、状态位与收尾包配套
python tools/close_gate.py --check    # 再加三条现场事实：工作区干净、无在途工单、状态与结论一致
```

**非零就不许说「这一轮结束」**：照着输出补齐再跑一次，不许绕过。`doc_gate` 管计划与轮的格式，`close_gate` 管收尾，两者不互相替代。

收尾包单独提交就行，不需要工单：暂存区只含 `tasks/closing-*.md`、`tasks/round.md` 的状态行、`tasks/rounds.md` 的历史行，且本项目没有在途工单时，commit 钩子把它认成「收尾提交：R-00N」放行。

`python tools/work_gate.py --list` 报「本轮 R-00N 未收尾」时，先收尾再开新单：它意味着库里已经没有在途单、这一轮也有已验收并合并的成果，但 `tasks/round.md` 还写着 `状态：进行中`。

**回复的第一行写结论**：`本轮：已收尾` / `本轮：等你一句答复——<那一句>` / `本轮：需重开`，与收尾包第 7 节的三档一一对应；欠人的那条写成可执行动作（打开哪个文件改哪一行、跑哪条命令），写「需要你确认」不算。

## 分支与合并

**每个工单一个 worktree，主检出永远留在 master。** 开工时建一条分支和它自己的工作树，之后只在这棵树里改：

```bash
git worktree add .worktrees/<工单号> -b ticket/<工单号> master   # 开工
# 在 .worktrees/<工单号>/ 里改、提交、跑测试
```

**顺序是先合并、后清理，两步不能颠倒。** 删分支与工作树放在验收通过、协调会话合并成功（`merge_ticket` 写回记录）之后：

```bash
git worktree remove .worktrees/<工单号>    # 合并成功之后才做
git branch -d ticket/<工单号>               # 同样在合并之后
```

`git branch -d` 只删已被合并的分支；分支还没合进 master 时 git 会拒绝（`not fully merged`）。被拒就是提醒你合并还没成——不要改用 `-D` 硬删。

不要切主检出的分支：主检出停在 `ticket/<工单号>` 时跑合并，`git merge --no-ff` 会把 master 合进那条工单分支，方向反了。`tools/auto_merge.py` 第 2 步会拒绝并让你 `git switch master`。

**验收通过后由协调会话合并，执行会话不自己合。** 合并只用这一条命令：

```bash
python tools/auto_merge.py --list                    # 已验收但没合并的工单
python tools/auto_merge.py --ticket <工单号>          # 合并（默认源分支 ticket/<工单号>、目标 master）
python tools/auto_merge.py --ticket <工单号> --dry-run # 只预演与跑测试，不碰目标分支、不写记录
python tools/auto_merge.py --ticket <工单号> --record-only # 已合并且无记录时收口
```

它按顺序做八件事：工单必须 `done` 且没有合并记录 → 必须已走过回执 → 当前检出必须在目标分支上、干净、源分支领先目标、要新增的路径不与未跟踪文件同名 → `merge-tree` 预演无冲突 → 打回退点 `pre-merge-<工单号>` → 临时 worktree 里先合一次并跑测试 → 绿了才 `git merge --no-ff` → 合并后再跑一次测试，通过才调 `merge_ticket` 写回记录。任何一步不满足都以非零退出并写明怎么补。

**未跟踪文件撞上这次合并要新增的路径时，第 2 步就停下并指名文件，不进 merge。** 判断按 Windows 口径归一（`/` 与 `\` 不分、大小写不分），内容与分支逐字节相同也一样。补救是把文件挪到 `.data/untracked/<工单号>/`、路径写进 `submit_result` 的 `evidence`，不要挪去系统 Temp。

**回滚**：真实合并后测试再红，工具会自己 `git reset --hard pre-merge-<工单号>`。人工回滚用同一条命令。

## 目录约定

| 路径 | 内容 |
| --- | --- |
| `PRD/` | 需求文档，`PRD-00N-名称.md`，历史版本不得无记录覆盖 |
| `tasks/` | 计划与执行记录；`tasks/round.md` 是本轮目标，历史在 `tasks/rounds.md` |
| `.agents/skills/` | 本工作区专用 skill |
| `task_center/` `tests/` `docs/` `examples/` | 工单核心代码与测试，仓库根就能跑 |
| `.worktrees/<工单号>/` | 当前工单的工作树，检出 `ticket/<工单号>`；主检出不切分支 |
| `.githooks/` | commit 钩子；`core.hooksPath` 指向它 |
| `.data/` | 本地数据库与临时证据，不入库 |

## 常用命令

```bash
git worktree add .worktrees/<工单号> -b ticket/<工单号> master   # 开工：一单一树
python tools/run_tests.py                       # 全部测试：tests/ 与 tools/test_*.py 两处一起跑
python tools/run_tests.py -v                    # 同上，逐条打印用例名
python -m task_center --db "{{WORKSPACE}}/.data/task-center.sqlite3" list_projects
python -m task_center --db <绝对路径> <操作> --request-id <唯一编号> --input <JSON文件>
python -m task_center.probe --roots D:/ --depth 2 --explain   # 只读项目探测
python tools/auto_merge.py --list                 # 已验收但还没合并的工单
python tools/auto_merge.py --ticket <工单号>       # 预演 → 测试 → --no-ff 合入 master；失败回滚
python onboard.py --json                          # 装完在宿主里开工：create_project → 开会话 → 第一条指令
python tools/add_project.py <业务仓库>             # 按项目配置：写 ticket-config.json 并指向套件 .githooks
python tools/work_gate.py --list                  # 现在有哪些活跃尝试
python tools/work_gate.py --ticket <工单号>        # 这张工单能否为本次改动背书
TASK_CENTER_TICKET=<工单号> git commit -m "..."    # 钩子会再核对一次
```

测试分在两处：`tests/` 放工单核心用例，`tools/test_*.py` 放闸门脚本自己的用例。只跑一处等于把另一处当不存在，所以验证一律用 `python tools/run_tests.py`。

## 不要做

- 不启用定时自动化，不发布服务，不改其他业务项目。
- 不让各会话自行写 SQL 或各自维护一份 JSON/Markdown 进度——中心数据库是唯一进度来源。
- 不把模拟会话的测试结果说成「真实会话已接通」。
- 不在没有用户确认时推送。本机没有远端时，合并只走 `tools/auto_merge.py`：验收通过、测试绿、无冲突才合，且只在目标分支的检出上做，合并后留一条可查询的记录。
- 分支按工单走，**先合并、后清理**；没合就删分支 git 会拒绝，被拒不要用 `-D` 硬删。

## 已知未实现（别当成已具备）

- **九个概念态里只有七个能在库里表达**：`待规划`、`验收中`、`受阻或待决策` 没有对应状态，阻塞原因与解除条件只能写在 reason 与事件里。
- **没有 `workflow_runs` 表，且已裁定不建**：PDSA 由工单状态、尝试与验收记录表达，暂停原因写 `recover_ticket` 的 reason。
- **项目登记面已具备** `list_projects` 与 `unregister_project`（有工单时拒绝撤销）；`projects.root` 仍不能改，修正路径走「撤销 → 重新登记」。
- **项目级事件写得出读不出**：`register_project` 写的事件 `ticket_id` 为 NULL，`events` 操作要求工单号。
- **真实派发适配层不存在**：`claim` 之后调用 Cindy 创建会话这一步仍是手工的。
