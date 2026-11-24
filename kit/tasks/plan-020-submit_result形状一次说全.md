# 计划 020：`submit_result` 的形状一次说全，首次提交的 `base_sha` 有着落

## 依据

- `PRD-013-工单机制的执行面.md` **v0.3**：R7（`submissions.base_sha` 必填、`list_tickets` 的 `behind` 列）、AC12（不传 `base_sha` → `INVALID_INPUT` 且不写行）、D5（`base_sha` 取 `git merge-base HEAD <目标分支>`，不取「填表那一刻目标分支的 sha」）。
- `PRD-015-多主任务派单.md` **v0.2**：R2 的 `conflict_preview`（`submit_result` 回执里的那一节，本单不动）。
- 工单 AWK-118 的五条判据（C1–C5），取自中心库 `get_ticket`；`revision=1`、`criteria_version=1`。
- 现场依据（2026-11-24 本次开工，命令自己跑过）：
  - master `f9841d7` 上 `python tools/run_tests.py` 末行 `结果：全部通过`，合计 1050 项。
  - `kit/task_center/core.py` 的 `_submit_result` 按 `head_sha` → `base_sha` → `artifacts` → `evidence` → `risks` 的次序逐项 `require`，每一处各自 `INVALID_INPUT`，所以形状全错的人要试五次（`text()` / `string_list()` 每处只报第一个不合格字段）。
  - `kit/docs/protocol.md` 第 85 行是 `submit_result` 的整行说明：只写了「`head_sha` 与 `base_sha` 都必填、各收 40 位十六进制」，**没写** `artifacts` / `evidence` / `risks` 是字符串数组、也没写各自的上下限（1–20 / 1–100 / 0–100）。
  - 首次提交无处取 `base_sha`：`onboard.py` 的 generic 计划第 5 步把 `base_sha` 写成「`<git merge-base HEAD master 的 40 位十六进制>`」，而 `git worktree add -b ticket/X master` 出来的新仓库若只有一次初始提交、没有 `master` 分支，这条命令直接失败。

## 所用 skill

- `plan-gate`：它规定了这份计划必须有六节、`## 依据` 要带 PRD 编号与版本、点名的 skill 后面要写清「它改变了什么」，并把「有没有按规矩规划」变成一条会失败的脚本检查。
- `writing-plans`：它把每个任务压成「一个有独立测试环的最小单位」，每一步只留一个动作加一句可判的期望输出；这份计划按它的粒度切，不抄代码正文。
- `planning-and-task-breakdown`：它要求先画依赖再排序（引擎校验 → 文档口径 → 计划层提示 → 全量验收），并让每个任务自己留一条可验证的产出。
- `test-driven-development`：它把「先看见红再变绿」定为硬规则；任务 1 先落会失败的用例、跑一次红、再落实现、跑同一批绿。
- `executing-plans`：它要求逐任务执行、每个任务留一条带命令与结果的记录、偏离计划必须写成一条明账；本单按它的节奏做。
- `ticket-dispatch`：它规定提交要带 `head_sha` / `base_sha`、回执只发一条、成果路径要写实；本单的 `submit_result` 按它写。
- `verification-before-completion`：它禁止用「应该绿了」代替输出；下面每个任务都挂一条真能跑的命令与它的期望行。

## 交付物

**主题**：让按 `onboard --host generic` 计划走的人在 `submit_result` 上不再连撞五次——三个数组字段的形状一次写清、形状错误合成一条报错、首次提交的 `base_sha` 有一处说得清也填得出的说法。

**形状**：
- `kit/docs/protocol.md`：`submit_result` 那一行补上三个数组字段的形状与上下限，新增一节「`submit_result` 的形状」，里面有一张三行字段表和一段**能直接跑通的最小 payload**；`base_sha` 的首次提交口径写进这一行（供 `grep -n "base_sha" | head -5` 一次看到）和这一节。
- `kit/task_center/core.py`：`submit_result` 的五个形状字段（`head_sha` / `base_sha` / `artifacts` / `evidence` / `risks`）合并成一次校验，一条 `INVALID_INPUT` 同时点名每个字段的期望形状与这一次哪里不对；上下限、非空、40 位十六进制一条不松。
- `kit/onboard.py`：generic 计划第 5 步的 payload 提示与 `INVALID_INPUT` 处置那一行，改成「首次提交填 `git rev-parse HEAD`」，不再让人拿一条跑不通的命令去推。
- 用例：`kit/tests/test_center.py` 新增两条（一条锁「一条报错说全」，一条锁「该拒的仍然拒、合法的仍然一次通过」）。

**给谁看**：第一次按 `onboard --host generic` 派单的人（他手上只有这份协议与那份计划），以及验收这张单的独立会话（它靠 `## 任务` 里的命令复核，不靠我的自述）。

**不覆盖**：
- 不改 `submit_result` 的校验语义：三个数组的上下限（1–20 / 1–100 / 0–100）、每条非空且 ≤4000 字符、两个 sha 必填且 40 位十六进制，一条都不松；只是错误信息一次说全。
- 不把 `base_sha` 改成可选、不改成缺省填 `""`（PRD-013 AC12 的反例就是这两条）——首次提交给的是**能跑出来的取法**，不是把必填项放空。
- 不改库结构、不改 `schema`、不改工单状态机、不动 `conflict_preview`、不动 `review_result`。
- 不发版：不动 `VERSION` / `CHANGELOG.md` / `plugin/` / `dist/` / `panel/`，不打 tag、不推送、不自己合并。

**服务本轮目标第 4、6 条**：`tasks/round.md` 第 4 条「陌生人演练排在回执可用之后单独成单」与第 6 条「文档与工具剩余增量」——本单就是那次演练真跑出来的一处卡点（它的现场记录是 2026-11-24 03:38 的五次 `INVALID_INPUT`），补的是文档与提示层，不新开方向。

## 任务

### 任务 1：五个形状字段一次校完、一条说全（引擎那一端）

**为什么不是「把三处 `require` 拼一起」**：`text()` 与 `string_list()` 是通用的形状函数，别处（`claim_ticket` 的 `parallel_scope`、`review_result` 的 `reviewed_sha`）也在用，改它们的报错口径会牵连无关操作。所以新增一个只服务 `submit_result` 的收集器：把每个字段「哪里不对」收进一个列表，最后一条 `require` 带出**完整规则 + 这一次的错**。

**文件**：
- 测试：`kit/tests/test_center.py`（新增两条，放在现有 `submit_result` 那批旁边）
- 实现：`kit/task_center/core.py`

**接口**：
- 新常量 `SUBMIT_ARRAYS = (("artifacts", 1, 20), ("evidence", 1, 100), ("risks", 0, 100))` 与 `SUBMIT_SHAPE_HINT`（一段英文规则串，含 `arrays of strings`、`artifacts 1-20`、`evidence 1-100`、`risks 0-100`、`40 hexadecimal characters`）。
- 新函数 `submit_shape(data)` → `(head_sha, base_sha, paths, evidence, risks)`：全部合格才返回，否则抛一条 `INVALID_INPUT`，`message` = `SUBMIT_SHAPE_HINT` + `Wrong now: <逐项>`。
- `_submit_result` 用它一次取齐五个值，再照旧做 `fingerprint`、重复路径、`conflict_preview`、落库。

**步骤**：
1. 在 `kit/tests/test_center.py` 写两条失败用例：
   - `test_submit_shape_error_names_every_field_once`：`artifacts` 传对象数组 → 回执 `INVALID_INPUT`，`message` 里同时出现 `artifacts 1-20`、`evidence 1-100`、`risks 0-100`、`arrays of strings` 与 `artifacts[0]`；再来一次 `artifacts` 传字符串、`evidence` 传空数组、`risks` 传数字数组 → 同一条 `message` 里三项都被点名（`artifacts is`、`evidence has 0 entries`、`risks[0]`）；两次之后工单仍是 `running`、`submissions` 一条不写。
   - `test_submit_still_rejects_what_it_used_to`：缺 `base_sha`、`base_sha` 长度不对、`evidence` 101 条、`risks` 101 条、`artifacts` 21 条各自仍报 `INVALID_INPUT`；合法的「最小 payload」（`artifacts` 1 条、`evidence` 1 条、`risks` 空数组）仍然一次成功并落一条提交。
2. 跑 `python -m unittest tests.test_center -v`（工作目录 `.worktrees/AWK-118/kit`）→ 红在「message 里没有 `artifacts 1-20`」与「一条 message 里只有第一项」。
3. 落 `SUBMIT_ARRAYS` / `SUBMIT_SHAPE_HINT` / `submit_shape`，`_submit_result` 改用它；`string_list` 的唯一调用点就是这里，一并从 `core.py` 移除（`sha_text` 仍服务 `reviewed_sha`，保留）。
4. 重跑同一批 → 绿。
   **实施时的一处偏离（记账）**：收集器一开始写在 `string_list` 原位，结果 `ref_gate` 红在 `PRD-014` 的两处 `task_center/core.py:454` / `:865`——在文件中间插函数会把行号引用整体推走。改成照本文件已有的做法（`review_checks` 那一批）放到文件末尾，`string_list` 原地不动（它不再被 `submit_result` 调用），`_submit_result` 那一段替换成同样的行数。代价：多一个当前没被调用的通用形状函数，与 `text()` / `integer()` 同层。

**验证**：`python -m unittest tests.test_center -v` 末行 `OK`；`python -c "import sys; sys.path.insert(0,'.'); from task_center.core import submit_shape"` 退出码 0（名字真的落在模块上）。

### 任务 2：协议里写清三个数组与首次提交的 `base_sha`（文档那一端）

**为什么不能只写「三个字段」**：演练里第 3 次和第 4 次报错分别是「`artifacts` 要字符串」与「要 1–20 个」，同一个人对同一个字段撞了两次——只写「数组」或只写「字符串」都不够，得把**元素类型 + 条数上下限 + 每条的长度上限**写在一起，并给一段原样能跑的 payload。

**文件**：`kit/docs/protocol.md`（第 85 行 `submit_result` 那一行 + 新增一节，放在「预留、绑定与提交的顺序」的第 4 步之后）

**步骤**：
1. 改第 85 行那一行：在 `head_sha` / `base_sha` 之后补「`artifacts` / `evidence` / `risks` 三个都是字符串数组（1–20 / 1–100 / 0–100 条，每条非空且 ≤4000 字符），传对象、传 JSON 字符串、传单个字符串都报 `INVALID_INPUT`」，并补一句首次提交口径：「新仓库只有一次初始提交、没有 `master` 分支时，`git merge-base HEAD master` 跑不出来，`base_sha` 就填 `git rev-parse HEAD` 的输出（与 `head_sha` 相同）」。
2. 新增一节 `### `submit_result` 的形状：三个字符串数组与首次提交的 base_sha`：
   - 一张四列表格（字段 / 形状 / 上下限 / 常见的错法）；
   - 一段 `python -m task_center` 的最小 payload，`artifacts` / `evidence` / `risks` 都按字符串数组写，`risks` 给空数组，两个 sha 用真值占位说明；
   - 一句「一条报错说全」：形状错时 `message` 一次点名五个字段的期望形状与这一次哪几项不合格，不用逐条试。
3. 跑闸门与判据命令。

**验证**：`python tools/doc_gate.py --all` 退出码 0；`grep -n "字符串数组" docs/protocol.md` 有输出；`grep -n "base_sha" docs/protocol.md | head -5` 里能看到首次提交那句（它就在第 85 行那一行里）。

### 任务 3：计划层的提示改成一条跑得通的命令（`onboard.py`）

**为什么要动它**：陌生人拿到的第一手材料是 `onboard --host generic` 打印的计划，不是协议全文。那里第 5 步的 `base_sha` 提示写着 `git merge-base HEAD master`，而这正是新仓库跑不出来的那条。

**文件**：`kit/onboard.py`（`GUIDANCE["generic_dispatch"]["submit_result"]` 那一行、generic 计划第 5 步的 `args` / `check`）

**步骤**：
1. `GUIDANCE` 里 `submit_result` 的处置句改成：`head_sha` 用 `git rev-parse HEAD`；`base_sha` 用 `git merge-base HEAD master`，**没有 `master` 分支（首次提交）就用 `git rev-parse HEAD`，两者相同**；三个数组字段是字符串数组（1–20 / 1–100 / 0–100）。
2. 第 5 步 `args` 里三个数组字段的占位改成显式的字符串数组写法，`base_sha` 的占位带上首次提交那半句；`check` 补一句「形状错时一条报错会同时点名五个字段，不必逐条试」。
3. 跑 `python tools/test_onboard.py`。

**验证**：`python tools/test_onboard.py` 末行 `OK`；`python onboard.py --host generic --json` 输出里 `merge-base` 那一处紧跟 `git rev-parse HEAD` 的兜底（`grep -c "rev-parse HEAD"` 输出大于改前，命令在 `kit/` 下跑）。

### 任务 4：全量验收与五条判据逐条对

**为什么单独成一档**：任务 1 改了 `core.py`、任务 2 改了协议、任务 3 改了计划层，三处各自的用例都绿，不等于整棵树绿。

**步骤**：
1. 跑 `python tools/run_tests.py`。
2. 逐条跑 C1–C5 的命令与判断。
3. `git diff --name-only master...HEAD` 核「没碰发版、没碰库结构」（不出现 `VERSION` / `CHANGELOG.md` / `plugin/` / `dist/` / `panel/` / `task_center/db.py`）。

**验证**：
- `python tools/run_tests.py` 末行 `结果：全部通过`，合计 ≥ 1050（新增两条，期望 1052）。
- `grep -n "字符串数组" docs/protocol.md` 有输出（C1）；`grep -n "base_sha" docs/protocol.md | head -5` 里能看到首次提交那句（C2）。
- 判断类：拿一个 `artifacts` 为对象数组的 payload 真调一次 `submit_result`（临时库），回执 `error.message` 同时点名 `artifacts` 的期望形状与 `evidence` / `risks`，反例是仍然一次只报一条（C3）；同一个合法 payload 一次成功、`get_ticket` 的 `submissions` 多一条，反例是为了报错更全而把正常的也拒了（C4）。
- `git diff --name-only master...HEAD` 输出里不出现上面那六个路径。

## 不做什么

- 不把 `base_sha` 改成可选、不默认填 `""`、不在代码里静默用 `head_sha` 顶替——`PRD-013` AC12 的判据就是「不传 → `INVALID_INPUT`」，本单只把它「填什么」写成一条跑得出的命令并说清名字。要改这条语义得先 `revise_ticket` 改 PRD，不在本单。
- 不改 `text()` / `sha_text()` 的通用报错口径（`claim_ticket`、`review_result` 共用它们）。
- 不改 schema、不改 `db.py`、不新增列、不改工单状态机、不动 `conflict_preview` 与 `review_result`。
- 不改 `VERSION` / `CHANGELOG.md` / `plugin/` / `dist/` / `panel/`；不打 tag、不推送、不自己合并（合并由协调会话在验收后走 `auto_merge`）。
- 不拿中心库做写入实验：任务 1、4 的 payload 一律用临时库。

## 未决

无。[待澄清：交付物第 (3) 项说「必要时给默认值」——本单按「不给静默默认、给一条跑得通的取法」办]：理由是 PRD-013 AC12 把「`base_sha` 可空」写成了反例，静默用 `head_sha` 顶替还会在**非**首次提交时把分叉点记成一句没人验证过的话（`behind` 会跟着算错）。这条取舍如果验收会话认为不够，改法是先 `revise_ticket` 改 PRD-013 AC12，再在本单的 `submit_shape` 里加缺省。
