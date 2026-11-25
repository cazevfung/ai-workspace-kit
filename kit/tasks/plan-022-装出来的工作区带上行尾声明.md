# 计划 022：装出来的工作区里必须带上行尾声明

## 依据

- `PRD-001-工作区使用与工单体系.md` **v0.1**：它管的是「成果可追、每张单记下依据」，本单按它记账；**业务依据：暂无**——安装器该装哪些文件没有任何 PRD 描述过，本单的判据只来自工单 AWK-120 的 C1–C5 与一次真装实测。
- 工单 AWK-120 的五条判据（C1–C5），取自中心库 `get_ticket`，`revision=1`、`criteria_version=1`。
- 现场依据（2026-11-24 本次开工自己跑出来的）：
  - 工作树建在 `b56077e`（master 最新）。装到临时目录（`--no-skills`，临时 `--agents-home`）之后，在装出来的工作区 `tools/` 下跑 `test_eol_check.FreshCloneTest` 与 `test_eol_check.MissingRuleTest`：`Ran 4 tests`，`FAILED (failures=2, errors=1)`——正是发布单 AWK-121 要声称「陌生人装完全绿」时挡住它的那 3 项。
  - 原因只有一条：`install.py` 的「1/7 复制工作区本体」只 `copy_tree(KIT, workspace, …)`，装的是 `kit/` 那棵树；仓库根的 `.gitattributes`（1015 字节）没跟着走。装出来的工作区里 `git check-attr eol -- tools/work_gate.py` 读出来是 `unspecified`，`core.autocrlf=true` 的检出于是全是 CRLF。
  - `MissingRuleTest` 在装出来的工作区里不是「断言失败」而是 error：它读 `clone/.gitattributes` 就直接 `FileNotFoundError`——文件根本没有。
  - 装出来的工作区是个 git 仓库（第 6 步 `git init` + 首次提交），所以这份声明必须在首次提交之前落盘，否则它进了工作区也进不了克隆。
  - 现有端到端用例 `kit/tools/test_install_guard.py` 的 `InstallRunTest` 都带 `--no-git` 真装，没有一条断言过「装出来的工作区里行尾规则生效」。

## 所用 skill

- `plan-gate`：规定规划前先读 `AGENTS.md` 与相关 PRD、真的打开要用的 skill，计划必须含六节并过 `python tools/doc_gate.py`；这份计划按它的六节写，闸门真跑过。
- `writing-plans`：要求每个任务一个动作加一条可判的期望输出；本单三个任务各挂一条能跑的命令。
- `test-driven-development`：先让用例在缺文件时红，再改安装器让它绿；红与绿都留了输出。
- `executing-plans`：逐任务执行，偏离写成明账（见「未决」）。
- `verification-before-completion`：不拿「应该绿了」代替输出，全量测试与反例演练都贴实测行。
- `ticket-dispatch`：提交要带 `head_sha` / `base_sha`、回执只发一条。

## 交付物

**主题**：任何人把套件装到一个干净目录里，装出来的工作区自己就带行尾声明——`python tools/run_tests.py` 在那里必须全绿，而不是靠把 eol 用例 skip 掉来凑绿。

**形状**：
- `install.py`：新增模块常量 `ROOT_GITATTRIBUTES = HERE / ".gitattributes"` 与函数 `copy_gitattributes(workspace, dry)`，按字节 `shutil.copy2` 到 `<工作区>/.gitattributes`；调用点在「1/7 复制工作区本体」里、`copy_tree(KIT, …)` 之后——这一步早于第 6 步的首次提交，所以它进得了第一次提交、也进得了任何一次克隆。屏幕上一行说清装了什么；套件根没有这份文件时进「安装总结」的 `未写：` 账，不假装装过。
- `kit/tools/test_install_guard.py`：新增 `GitattributesInstallTest`（类级只装一次，两条用例）：① 文件在、与仓库根同一 sha256、屏幕上说得出来；② 在装出来的工作区 `tools/` 下跑 `FreshCloneTest` 与 `MissingRuleTest`，要求 `Ran 4 tests` + `OK` 且输出里没有 `skipped`。
- `kit/README.md`：行尾口径那节补一句「这份声明由 `install.py` 从套件仓库根装过来，漏装会怎样」——README 本身就是装进工作区的文件，说清前提。
- 不动：`kit/tools/check_eol.py` 的判据与 `.gitattributes` 的内容，一个字都没改。

**给谁看**：下一个从源码包装一遍、然后照着 README 跑 `run_tests` 的人（他应当看到全绿），以及验收这张单的独立会话（它按 C1–C5 的命令自己跑）。

**不覆盖**：
- 不改行尾规则本身：`.gitattributes` 一个字节不动，`* text=auto eol=lf` 与 `skills/** -text` 的写法与顺序原样。
- 不放宽判据、不 skip 用例：`check_eol.scan` 的判据、`test_eol_check.py` 里的断言一条都不删、不加跳过条件。
- 不动已经装好的工作区、不追发补丁包、不改库结构、不碰 `kit/task_center/`。
- 不发版：不动 `VERSION` / `CHANGELOG.md` / `plugin/` / `dist/` / `panel/`，不打 tag、不推送、不自己合并。
- 不「顺手优化」`.gitattributes`（例如把 `skills/** -text` 并进 `* text=auto eol=lf`）：那是另一个话题，改了就是 C5 的反例。

## 任务

### 任务 1：先让缺文件这件事变红

**文件**：`kit/tools/test_install_guard.py`（新增 `GitattributesInstallTest`）

**接口**：`setUpClass` 里用 `TemporaryDirectory` 真装一次（`--no-skills`，临时 `--agents-home`；有 git 时用一份临时 `GIT_CONFIG_GLOBAL` 提供 `user.name` / `user.email`，让第 6 步的首次提交成立，真实 `~/.gitconfig` 不碰），两条用例共用这一次安装。

**步骤**：
1. `test_installed_file_is_byte_identical`：断言 `<ws>/.gitattributes` 存在、sha256 等于 `REPO/.gitattributes`、屏幕上有「行尾声明」。
2. `test_installed_workspace_eol_checks_are_green`：在 `<ws>/tools` 下 `python -m unittest test_eol_check.FreshCloneTest test_eol_check.MissingRuleTest`，断言返回码 0、`Ran 4 tests`、`OK`、不含 `skipped`。
3. 跑 `python -m unittest test_install_guard.GitattributesInstallTest -v`（工作目录 `kit/tools`）→ 两条红：一条在「文件没有」，一条在装出来的工作区里 `FAILED (failures=2, errors=1)`。

**验证**：上面那条命令输出 `FAILED (failures=2)`，红的原因分别是「`装出来的工作区里没有 .gitattributes`」与 `test_eol_check` 的 eol 报错，不是别的错。

### 任务 2：安装器把这份声明装过去

**文件**：`install.py`（模块常量与 `copy_gitattributes`；`main()` 的 1/7 里调用一次）

**步骤**：
1. 落 `ROOT_GITATTRIBUTES` 与 `copy_gitattributes()`：源文件不在返回 `None`，预演不写盘，正常按字节 `shutil.copy2`（`copy_tree` 的模板替换对这份文件没有意义——里面没有工作区占位符）。
2. 在 `copy_tree(KIT, …)` 之后调用，`say` 一行「已复制行尾声明：.gitattributes（与套件根逐字节相同）」；返回 `None` 时 `warn` + 记一笔 `未写：`，不把「没装成」说成「装了」。
3. 跑任务 1 那两条用例 → 绿。

**验证**：`python -m unittest test_install_guard.GitattributesInstallTest -v` → `Ran 2 tests … OK`；临时目录里 `sha256sum <ws>/.gitattributes .gitattributes` 两行相同（`0a79aae6…`）。

### 任务 3：反例演练 + 全量验收 + 文档

**为什么单独成一档**：用例绿了只说明「有文件时能过」，得再看一遍「没文件时确实红」——否则可能写出一条永远绿的用例。

**步骤**：
1. 反例：把安装器那一行临时改成 `attrs = None`，重跑任务 1 的两条用例 → 两条都红（`FAILED (failures=2)`）；再把那一行改回来，`grep -n "attrs = copy_gitattributes" install.py` 确认还原。
2. `kit/README.md` 行尾口径那节补一句「这份声明由 `install.py` 装过来，漏装的后果是 `test_eol_check` 必红」。
3. 跑 `python tools/run_tests.py`；逐条对 C1–C5。

**验证**：
- 反例那一步的输出是 `FAILED (failures=2)`，改回后同一命令是 `OK`。
- `python tools/run_tests.py` 末行 `结果：全部通过`，合计 1056 项（原来 1054，新增两条）；`tests/ 356 OK；tools/ 700 OK`。
- C1：`grep -n "gitattributes" install.py` 有输出，且安装步骤里那句 `已复制行尾声明` 与调用点都在。
- C2（判断类）：装到临时目录后文件在、sha256 与仓库根相同。
- C3（判断类）：装出来的工作区里 `FreshCloneTest` 与 `MissingRuleTest` 绿，且 `Ran 4 tests`（没有被 skip 掉的那一条）。
- C4：`python tools/run_tests.py` 末行 `结果：全部通过`，合计 1056 ≥ 1054。
- C5（判断类）：`git diff --name-only master...HEAD` 里不出现 `kit/tools/check_eol.py` 与 `.gitattributes`。

## 不做什么

- 不改 `.gitattributes` 的内容与顺序，不改 `kit/tools/check_eol.py` 的判据——要绿只能靠「把文件装过去」。
- 不给 eol 用例加 skip 条件、不在装出来的工作区里删断言。
- 不改安装器的步骤编号与既有输出措辞（`1/7`…`7/7` 原样），只多一行说明。
- 不动 `kit/task_center/`、不发改版文件、不打 tag、不推送、不自己合并。
- 不拿真实的 `C:\Users\202108-107\.agents` 做实验：所有安装都带临时 `--agents-home`，git 身份用临时 `GIT_CONFIG_GLOBAL`。

## 未决

无。
