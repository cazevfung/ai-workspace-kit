# 计划 021：沙箱安装时「用户级配置被跳过」要看得见

## 依据

- `PRD-001-工作区使用与工单体系.md` **v0.1**：它管的是「成果可追、每张单记下依据」，本单按它记账；**业务依据：暂无**——安装器打到屏幕上的那份总结没有任何 PRD 描述过它，本单的判据只来自工单 AWK-119 的 C1–C4 与一次真演练。
- 工单 AWK-119 的四条判据（C1–C4），取自中心库 `get_ticket`，`revision=1`、`criteria_version=1`。
- 现场依据（2026-11-24 本次开工自己跑出来的）：
  - 工作树建在 `a8ef1e7`（master 最新），`python tools/run_tests.py` 末行 `结果：全部通过`，`汇总：tests/ 356 OK；tools/ 696 OK  |  合计 1052 项`。
  - `install.py` 第 306–320 行已经把「沙箱 + 没给开关」判出来了（`decision.sandbox` / `decision.allowed`），第 359–364 行与第 375–383 行各自用 `say("   跳过：…")` 说了一遍——**但这两句夹在 12 个步骤中间，而末尾的 `安装完成。下一步：` 一个字都不提**。陌生人看最后一段就以为整套装好了。
  - `--allow-user-config` 的判据只有一处：`kit/tools/install_guard.py` 的 `decide_user_config`，返回 `allowed` 与 `reason`。本单读它、不改它。
  - 现有端到端用例在 `kit/tools/test_install_guard.py` 的 `InstallRunTest`（真跑 `install.py`，全部落在临时目录），只断言「`跳过` 与 `--allow-user-config` 出现过」——出现一次也算过，所以「看得见、数得清」没有被锁住。

## 所用 skill

- `plan-gate`：它规定规划前先读 `AGENTS.md` / `tasks/round.md` / PRD 并真的打开要用的 skill，产出的计划必须过 `python tools/doc_gate.py`；这份计划按它的六节写，并且真的跑了一遍闸门。
- `writing-plans`：它要求每个任务只留一个动作加一条可判的期望输出、接口写死在任务里而不是让人猜；本单的四个任务按它的粒度切，每一块都有独立的验证命令。
- `planning-and-task-breakdown`：它要求先画依赖再排序（先落会红的用例 → 再改安装器 → 再补回执层的检查 → 最后全量验收），本单照这个顺序排，避免「实现先写好、用例照着实现写」。
- `test-driven-development`：它把「先看见红再变绿」定为硬规则；任务 1 先落两条会红的用例、任务 2 跑同一批变绿，红与绿都留命令与输出。
- `executing-plans`：它要求逐任务执行、每个任务留一条带命令与结果的记录、偏离计划必须写成一条明账；本单按它的节奏做。
- `ticket-dispatch`：它规定提交要带 `head_sha` / `base_sha`、回执只发一条、成果路径要写实；本单的 `submit_result` 与回执按它写。
- `verification-before-completion`：它禁止用「应该绿了」代替输出；下面每个任务都挂一条真能跑的命令与它的期望行。

## 交付物

**主题**：沙箱安装（`--agents-home` 不是本机真实用户目录）默认不写用户级配置，这件事必须在装完的总结里一眼看见——逐条带路径与原因，再加一句能对上数的合计；给了 `--allow-user-config` 时这些行一个字都不出现。

**形状**：
- `install.py`：新增一处记账（常量 `SKIP_PREFIX` / `SKIP_REASON_USER_CONFIG` + 一个 `Summary` 小类），装了几项、没写哪几项都记在同一本账上；末尾多一节 `== 安装总结`，逐条打 `未写：<路径>（默认不写用户级配置，加 --allow-user-config）`，最后一句 `本次装了 N 项、跳过 M 项`。各步原来那两句散落的 `say("   跳过：…")` 收进这本账，屏幕上不再有两套口径。
- `kit/tools/test_install_guard.py`：`InstallRunTest` 新增两条用例——不带开关时总结里数得出的跳过项与合计（且 `未写` 行数等于合计里的 M）、带开关时用户级那几项一条都不出现。
- `verify/verify_install.py`：沙箱那段真装完之后多一条 `check()`，把「总结里说了什么」变成验收入口能看见的一行。
- 屏幕之外不动：`kit/tools/install_guard.py` 一个字节不改，默认仍然不写。

**给谁看**：第一次把套件装到临时目录试一下的陌生人（他只看最后那几行），以及验收这张单的独立会话（它靠 C1–C4 的命令复核，不靠我的自述）。

**不覆盖**：
- 不改 `install_guard.py` 的任何判定：什么算沙箱、什么算「指着别的库」、默认写不写，一条都不动；本单只加「说出来」的那一层。
- 不改默认行为：不带 `--allow-user-config` 仍然一个用户级文件都不写。
- 不改退出码语义：沙箱跳过仍是 0，拒绝改写仍是 2。
- 不改 `--no-skills` / `--skills-dir` / `--dry-run` 的语义，只是把它们也让同一本账记上（跳过就是跳过，合计要对得上）。
- 不动已经装好的工作区、不改库结构、不改工单状态机、不碰 `kit/task_center/`。
- 不发版：不动 `VERSION` / `CHANGELOG.md` / `plugin/` / `dist/` / `panel/`，不打 tag、不推送、不自己合并。

**服务本轮目标第 4 条**：`tasks/round.md` 第 4 条「陌生人演练排在回执可用之后单独成单：真装、真导插件、真建项目」——本单就是那次演练真跑出来的一处：退出码 0、屏幕安静，27 个技能一个都没装。补的是「装完看得清」，不新开方向。

## 任务

### 任务 1：两条用例先红（锁住「数得清」）

**文件**：`kit/tools/test_install_guard.py`（`InstallRunTest` 里新增两条）

**接口**：新增一个模块级辅助 `summary_counts(output) -> (装有几项, 跳过几项, 未写行列表)`，正则为 `本次(?:装了|会装) (\d+) 项、跳过 (\d+) 项`；`未写行` 取所有以 `未写：` 开头的行。两条用例都用它，所以「合计」与「逐条」是同一份账。

**步骤**：
1. 落 `test_sandbox_install_summary_shows_skips_and_total`：临时 `--agents-home` + `--no-git` 真装一遍，断言
   - `未写：` 行数等于合计里的 M，且 M ≥ 5；
   - `<agents>/skills`、`<agents>/center.json`、`<ws>/.agents/center.json`、`<agents>/intake`、`<agents>/.ai-workspace-kit.json` 这五个路径各出现在某一条 `未写：` 行里；
   - 每条 `未写：` 行都带原因 `默认不写用户级配置，加 --allow-user-config`；
   - 合计里的 N 等于同一份输出里「已装 N 项：」后面用 `、` 分开的名字个数。
2. 落 `test_explicit_flag_summary_has_no_skip_lines`：同一套临时目录带 `--allow-user-config` 真装一遍，断言
   - `未写：` 一行都没有（用户级那几项不再被当成跳过）；
   - 那句合计仍在，且 M = 0；
   - 用户级那四个路径确实被写了（`center.json` / `skills/` / `intake/` / 安装清单都存在）。
3. 跑 `python -m unittest tools.test_install_guard -v`（工作目录 `.worktrees/AWK-119/kit`）→ 两条新用例红在「找不到 `本次装了 … 项、跳过 … 项`」。

**验证**：`python -m unittest tools.test_install_guard.InstallRunTest.test_sandbox_install_summary_shows_skips_and_total -v` 输出 `FAIL`，失败信息里有 `本次装了`（说明是缺总结，不是别的错）。

### 任务 2：安装器把跳过记成一本账，末尾给合计（`install.py`）

**为什么不是「再加两句 print」**：判据 C2 要的是统计口径，不是散落的 `print`。散着打就会有两种口径——某一步新加了跳过、合计没跟上，数字照样对不上。所以装了几项、没写哪几项各是 `Summary` 上的一个列表，合计直接 `len()` 出来。

**文件**：`install.py`（`say` / `step` / `warn` / `fail` 旁边新增常量与 `Summary` 类；`main()` 的 3/7、4/7、6/7、7/7 与「附：浮窗面板」每一步各记一笔；末尾 `安装完成` 之前多一节总结）

**接口**：
- `SKIP_PREFIX = "未写："`、`SKIP_REASON_USER_CONFIG = "默认不写用户级配置，加 --allow-user-config"`。
- `class Summary`：`did(name)` 记一个装了的项名；`skip(path, reason=SKIP_REASON_USER_CONFIG, note="")` 记一个没写的路径；`report(dry=False)` 打这一节（已装项逐个名字、每条 `未写：` 行、最后 `本次装了 N 项、跳过 M 项`）。
- 项名固定十二个：工作区本体、verify/ 校验脚本、项目内技能、用户级技能、center.json、工作区指针、intake/、安装清单、中心库、git 仓库、提交闸门、浮窗面板。每个项要么进 `did`，要么进 `skip`，只有一处。

**步骤**：
1. 落常量与 `Summary`；`report()` 里合计那行由两个列表的长度算出来（`f"本次{verb} {len(done)} 项、跳过 {len(skipped)} 项"`，`verb` 在预演时是「会装」）。
2. 把各步的跳过改成记一笔：3/7（沙箱不给开关、`--no-skills`）、4/7（`center.json` / 工作区指针 / `intake/` / 安装清单）、6/7 与 7/7（`--no-git`）、面板（`panel/` 不存在），并在沙箱那一笔的 `note` 里带上「0/27 个技能一个都没装」这类数字，读的人不用自己去数。
3. 各步原来那两句 `say("   跳过：…")` 删掉，改成一句指路：`   见末尾「安装总结」：默认不写用户级配置`——屏幕上只留一套口径（C2）。
4. 末尾 `print(f"""\n{'预演完成' if dry else '安装完成'}…` 之前调 `summary.report(dry=dry)`。

**验证**：`python install.py --workspace <临时ws> --agents-home <临时agents> --no-git` 的末段期望看到 `== 安装总结`、五行 `未写：`、`本次装了 5 项、跳过 7 项`（`--no-git` 时两个 git 项也算跳过）；`grep -n "跳过" install.py | head -10` 的输出里要有 `Summary.report` 那一处合计（C2 要的统计口径），不是一摞 `say`。

### 任务 3：验收回执上也能看见（`verify/verify_install.py`）

**为什么动它**：`verify_install.py` 的沙箱那段已经在真装一遍，却只判「退出码 0 且没写文件」——正是这次告警要补的那种「安静地成功」。

**文件**：`verify/verify_install.py`（「沙箱默认安装不写用户级配置」那条 `check()` 之后新增一条）

**步骤**：
1. 新增 `check("沙箱安装的总结列了未写项与合计", …)`：从那次 install 的输出里数 `未写：` 行数与合计里的 M，要求相等且 ≥ 5、且五个路径各出现一次。
2. 跑 `python verify/verify_install.py`。

**验证**：`python verify/verify_install.py` 里那一行输出 `[ok] 沙箱安装的总结列了未写项与合计`，退出码 0（其余检查与改动前一样全 ok）。

### 任务 4：全量验收与四条判据逐条对

**为什么单独成一档**：任务 1 改了用例、任务 2 改了安装器、任务 3 改了回执脚本，三处各自绿不等于整棵树绿。

**步骤**：
1. 跑 `python tools/run_tests.py`。
2. 逐条跑 C1–C4 的命令与判断。
3. `git diff --name-only master...HEAD` 核「没碰发版、没碰库结构」。

**验证**：
- `python tools/run_tests.py` 末行 `结果：全部通过`，合计 ≥ 1052（新增两条，期望 1054）。
- C1（判断类）：临时 agents-home 上不带开关装一遍，总结里能数出跳过项与路径；反例是只打一行小字、或路径与合计对不上。
- C2：`grep -n "跳过" install.py | head -10` 能看到 `Summary.report` 的合计口径，反例是散落的一摞 `say("   跳过：…")`。
- C3（判断类）：带 `--allow-user-config` 装一遍，`未写：` 一行都没有；反例是两种情形打同一套话。
- C4：`python tools/run_tests.py` 末行 `结果：全部通过`。
- `git diff --name-only master...HEAD` 输出里不出现 `VERSION` / `CHANGELOG.md` / `plugin/` / `dist/` / `panel/` / `kit/task_center/`。

## 不做什么

- 不改 `kit/tools/install_guard.py` 的判定，不改默认行为（默认仍然一个用户级文件都不写），不改退出码语义。
- 不改 `--no-skills` / `--skills-dir` / `--dry-run` / `--force` 的语义：本单只让它们也进同一本账。
- 不动 `kit/task_center/`：不改 schema、不改工单状态机、不发版、不打 tag、不推送、不自己合并（合并由协调会话在验收后走 `auto_merge`）。
- 不重写安装器的步骤编号与既有输出措辞（`1/7`…`7/7` 原样保留，陌生人书签过的位置不挪）。
- 不把「跳过」做成一个可关闭的开关：这次要的就是默认说清，而不是多一个 `--quiet`。
- 不拿真实的 `C:\Users\202108-107\.agents` 做实验：所有安装都带临时 `--agents-home`。

## 未决

无。
