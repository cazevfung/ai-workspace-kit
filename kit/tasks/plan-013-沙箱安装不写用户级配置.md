# 计划 013：装到沙箱时不许改写真实 ~/.agents（AWK-113）

## 依据

- `kit/PRD/PRD-001-工作区使用与工单体系.md` v0.1（工单闭环、验收条件要能跑、执行者不自验）
- `docs/INSTALL.md` §安装器做了什么（当前口径：无条件写 `<agents-home>/center.json`、`skills/`、`intake/`）与 `kit/docs/closing.md` 的「中心库怎么被发现」（第 4 步：工作区指针 → `<agents_home>/center.json` 的 `db`，指丢即 `DB_NOT_FOUND`，不回落）
- `install.py` 当前 master 46ce67d：第 283–284 行把 `--agents-home` 默认成 `~/.agents`，第 339–367 行无条件 `write_text(<agents_home>/center.json)`、`copy_tree(kit/docs, <agents_home>/intake)`、写 `<agents_home>/.ai-workspace-kit.json`，第 399 行起把 27 个技能复制进 `<skills_root>`；`kit/tools/center_config.py` 的发现链第 4 步与被写的指针互相咬着
- 事件现场（2026-11-23 22:10，本单来源）：一个验收会话把工作区装到 `D:\awk109-verify\ws`，没传 `--agents-home`，于是 `<ws>/.agents/center.json` 记成真实的 `C:\Users\202108-107\.agents`，真实 `center.json` 被改指到沙箱空库；同一台机器上所有闸门一起读到空库，报 0 张在途
- 本机实测（2026-11-23，只读）：真实 `~/.agents` 有 3156 个文件、511 MB，其中 `skills/` 501 MB；按「≤256 KB 的文件算内容哈希、其余算大小与 mtime」做一次树快照约 0.5 秒——快照对比这件事在用例里负担得起，不需要真写进去再恢复
- 本轮目标见 `kit/tasks/round.md` R-002

## 所用 skill

- `plan-gate`：它改变了什么——先读 PRD 与 `round.md` 记下编号与版本号，计划落成六节文件并过 `doc_gate.py`，不过闸不开工。
- `test-driven-development`：它改变了什么——先在 `kit/tools/test_install_guard.py` 写会红的用例（沙箱默认不写、显式开关才写、真装重指拒绝、快照能识别被改），跑红之后再写 `install_guard.py` 与 `install.py` 的改动把它变绿。
- `planning-and-task-breakdown`：它改变了什么——把「判定逻辑 / 安装器接线 / 端到端用例 / 自检脚本 / 文档」切成五个互不重叠的任务块，判据模块排在最前，因为它决定后续所有任务的口径。
- `executing-plans`：它改变了什么——在本工作树里按任务块顺序逐条做，每块拿到证据再进下一块，中途不回主检出、不推远端。
- `verification-before-completion`：它改变了什么——说「跑绿了」之前必须贴出 `python tools/run_tests.py` 的汇总行与 `python verify/verify_install.py` 的通过计数，不贴输出就不算完。

## 交付物

主题：安装器在「装到非真实用户目录」与「真装但已有 center.json 指向别的库」这两种情况下，默认一个字节都不写用户级配置；要写必须显式 `--allow-user-config`。

形状：

- `kit/tools/install_guard.py`：一个纯函数模块（只依赖标准库、不写盘），回答「这次安装允不允许写用户级配置」与「为什么」，并提供一个树快照函数 `tree_digest(root)` 供用例与自检脚本做前后对比。判定输入只有四样：`agents_home`、本次工作区的库路径、`--allow-user-config` 是否给了、本机真实用户目录（可注入，便于用例用假 HOME 覆盖）。
- `install.py`：新增 `--allow-user-config`；开跑先算判定，判定为拒绝时**在复制任何文件之前**退出（退出码 2）并把已有的 `center.json` 原样打印；判定为跳过时第 3、4 步逐项打印「跳过了什么、要写加什么参数」；`--dry-run` 把这次会写什么、会拒什么一起预报。工作区指针 `<工作区>/.agents/center.json` 只在真的写了 `<agents-home>/center.json` 时才写——否则它指向一个不存在的文件，闸门会按「第 4 步指丢」报 `DB_NOT_FOUND`，而工作区本来就有自己那份库。
- `kit/tools/test_install_guard.py`：模块级用例（判定矩阵 + 快照能识别改写）与端到端用例（真跑 `install.py`，全部用临时 `--agents-home` 与临时 HOME，另加「真实 `~/.agents` 前后快照一致」）。
- `verify/verify_install.py`：安装命令补 `--allow-user-config`，并新增两项现场检查——真实 `~/.agents` 装前装后快照一致；不带开关的沙箱安装不写 `center.json` / `skills/` / `intake/` / 清单。
- 文档：`install.py` 顶部用法、`docs/INSTALL.md` 参数表与「安装器做了什么」、`README.md` / `README.en.md` 的安装段与演练命令、`CHANGELOG.md` 一条未发布记录、`bootstrap-skill/ai-workspace-setup/SKILL.md` 的预演口径、`kit/docs/closing.md` 补一句「沙箱默认安装不写指针，走第 5 步」。

给谁看：第一次在自己机器上试装的人，以及让 agent 代办安装的人——他跑 `--dry-run` 应当能看出「这次会写哪些用户级文件」，跑真装时若目标不是自己的 `~/.agents` 或那份 `center.json` 已经指着别的库，应当被拦住而不是事后才发现。

明确不覆盖：不改 `task_center/`（schema、状态机、操作面一行不动）；不改 `kit/tools/center_config.py` 的发现顺序；不动已装好的工作区与真实 `~/.agents`；不迁移历史安装、不给旧 `center.json` 做备份轮转；不改技能链接方式（`--link-agents` 只受「显式传了才做」这条既有规则约束）；不升 `VERSION`、不打 tag、不推送、不发版。

**服务本轮目标第 4 条**（陌生人真装演练的安全前提：演练要真装，就得先保证真装不会弄坏本机用户级配置）。

## 任务

### 一、工作树与尝试绑定

`git worktree add .worktrees/AWK-113 -b ticket/AWK-113 master`；会话标识用 `cindy_helper` 的 `call_tool` 调 `get_current_session_id` 现取，原样写进 `bind_session`（attempt 取 `get_ticket` 的 `current_attempt`）。

证据：`git worktree list` 输出里出现 `.worktrees/AWK-113 ... [ticket/AWK-113]`；`get_ticket` 回执里 `state` 是 `running` 且当次 attempt 的 `session_id` 等于本会话标识；两条命令退出码都是 0。

### 二、判据模块与它的用例（先红后绿）

新建 `kit/tools/install_guard.py`，提供：

- `real_agents_home(home=None) -> Path`：`(home or Path.home()) / ".agents"`；
- `decide_user_config(agents_home, workspace_db, allow_user_config=False, home=None) -> UserConfigDecision`：返回 `allowed` / `sandbox` / `refusal`（空串表示不拒绝）/ `reason`（`sandbox-skipped` / `explicit-allow` / `first-install` / `same-db` / `would-repoint`）/ `existing_db` / `existing_text` / `agents_home` / `workspace_db` / `real_agents_home`；
- `read_center(path) -> tuple[str, str]`：返回 `(db, 原文)`，读不到、不是 JSON、没有 `db` 时 `db` 为空串、原文照抄；
- `tree_digest(root) -> dict`：`relpath → sha256`（≤256 KB 的文件算内容哈希，更大的只记 `size:mtime_ns`），另带 `""` 键记目录项本身，用来抓「新增了一个文件」这类改动。

判定顺序（写进注释与用例名）：沙箱且没给开关 → 跳过；否则 `center.json` 存在且 `db` 与本工作区库不同 → 没给开关就拒绝；其余允许。

证据：在 `kit/` 下先跑 `python -m unittest tools.test_install_guard -v` 期望输出 `ModuleNotFoundError` 或断言失败（红）；实现后同一条命令期望 `OK`、退出码 0。

### 三、安装器接线

`install.py`：加 `--allow-user-config`（默认 False，help 写清两种触发场景）；开跑算一次判定；拒绝时在「复制工作区本体」之前打印已有的 `center.json` 原文与做法并 `return 2`（`--dry-run` 只预报、仍返回 0）；跳过时第 3 步技能与第 4 步用户级文件逐项打印跳过了什么；工作区指针只在写了 `center.json` 时写；`--dry-run` 逐条列出会写的文件（`center.json` / `intake/` / 清单 / 技能目录），跳过时列出会跳过什么。

证据：在 `kit/` 下跑 `python -m unittest tools.test_install_guard -v` 期望新加的端到端用例全部 `OK`；`python install.py --workspace <临时>/ws --agents-home <临时>/agents --dry-run --no-git` 期望输出里有「跳过」与 `--allow-user-config` 且 `<临时>/agents` 不存在（退出码 0）。

### 四、端到端用例：沙箱、开关、真装重指、预演

`kit/tools/test_install_guard.py` 的端到端部分（`install.py` 不在时整组 `skipTest`，装出来的工作区里这些用例不假绿也不假红）：

1. 沙箱安装（临时 `--agents-home`、`--no-git`）退出 0，临时 agents 目录里没有 `center.json` / `skills/` / `intake/` / 清单，工作区里 `.data/task-center.sqlite3` 在、指针不在；真实 `~/.agents` 的 `tree_digest` 前后相等（不等时把差异路径打进断言信息）。
2. 同一场景加 `--allow-user-config`：`center.json` 的 `db` 等于本工作区库、指针记下这次 `agents_home`、`skills/` 与 `intake/` 与清单都在；真实 `~/.agents` 快照仍相等。
3. 假 HOME（`USERPROFILE` 与 `HOME` 都指向临时目录，先跑子进程确认 `Path.home()` 真是它，否则 `skipTest`）：预置 `<假 HOME>/.agents/center.json` 指向另一个库 → 不加开关退出 2、输出里有原 `db`、`center.json`、`--allow-user-config`，且 `center.json` 字节不变、工作区没被建出来；加开关退出 0、`center.json` 改指本工作区库。
4. `--dry-run` 两种判定各一条：沙箱默认输出「跳过」与开关名且不建任何目录；真装重指输出拒绝预警、退出 0、`center.json` 不变。

证据：在 `kit/` 下跑 `python -m unittest tools.test_install_guard -v` 期望 15 条以上全 `OK`、`skipped=0`（源码树布局），退出码 0。

### 五、自检脚本加两项现场检查

`verify/verify_install.py`：安装命令补 `--allow-user-config`；跑之前对真实 `~/.agents` 取一次 `tree_digest`，装完再取一次并比对（不一致就列差异路径）；再跑一次**不带开关**的沙箱安装（另一套临时目录），断言它一个用户级文件都没写。README / README.en 里「14 项全过」这句跟着改成新计数。

证据：`python verify/verify_install.py` 期望末行是 `<总数>/<总数> 项通过`、退出码 0，且输出里含「真实 ~/.agents 未被写」与「沙箱默认安装不写用户级配置」两条 PASS。

### 六、文档口径

`install.py` 顶部用法加一行 `--allow-user-config`；`docs/INSTALL.md` 参数表加一行、并在「安装器做了什么」与常见问题里写清「沙箱默认跳过什么、真装重指被拒怎么处理、指针为什么只在写了 center.json 时才写」；`README.md` / `README.en.md` 安装段加一句与演练命令补开关；`CHANGELOG.md` 顶部加一条未发布记录（写清现场、判据、用例数变化、刻意没做的）；`bootstrap-skill/ai-workspace-setup/SKILL.md` 的预演段写清 agent 该把「会不会写用户级配置」念给用户听；`kit/docs/closing.md` 的发现顺序表补一句。

证据：`python kit/tools/doc_gate.py --all` 退出码 0；`grep -rn "allow-user-config" docs/INSTALL.md README.md README.en.md CHANGELOG.md bootstrap-skill install.py` 期望每个文件至少命中一行、退出码 0。

### 七、全量验证与提交

跑全套：`python tools/run_tests.py`（源码树入口）；再看一次 `git status`，把改动全部提交到 `ticket/AWK-113`。

证据：`python tools/run_tests.py` 末行 `结果：全部通过`、退出码 0，汇总行两处用例数都打印且合计大于本单开头的 967；`git log --oneline -1` 是本单提交、`git status --porcelain` 为空（退出码 0 且无输出）。

## 不做什么

- 不改 `task_center/` 的 schema 与操作面，不改 `kit/tools/center_config.py` 的发现顺序。
- 不拿真实的 `C:\Users\202108-107\.agents` 做实验：所有安装都在临时 `--agents-home` 或假 HOME 下跑，「真实目录没被写」只用前后快照证明，绝不先写进去再恢复。
- 不动已装好的工作区（含 `D:/AI工作区`）、不动主检出、不推远端、不打 tag、不发版。
- 不迁移历史安装、不自动备份或轮转旧 `center.json`、不替用户决定「该不该重指」——只拒绝并给出参数。
- 不改 `--link-agents` 的语义（仍是显式传了才建链接）。

## 未决

无。
