# 计划 016：公开仓库里剩下的本机路径

工单 AWK-112。工作树 `.worktrees/AWK-112`，分支 `ticket/AWK-112`（基线 `7f99910`）。

## 依据

- **业务依据：暂无。** 这一单不新增也不改任何业务判断（不碰 `task_center/`、不碰工单库、不动判据），它只处理「随包发布的文本里还留着这台机器的路径」这一件卫生问题。
- `PRD/PRD-001-工作区使用与工单体系.md` **v0.1**：过程依据。本单成果照旧走「提交 → 独立验收 → 合并」同一闭环，`submit_result` 之后不自己验收。
- `PRD/PRD-013-工单机制的执行面.md` **v0.3**（R10：合并只走 `auto_merge` 一个入口）：所以本单不写合并记录、不打 tag、不改已发布内容，历史只增不改。
- 既有事实（开工前在 `.worktrees/AWK-112` 里实测，不是引自工单）：
  - `grep -rn -F "D:/AI工作区" --exclude-dir=.git .` 命中 **18 处、7 个文件**：`CHANGELOG.md` 7、`README.md` 2、`README.en.md` 1、`kit/tasks/plan-012` 1、`plan-013` 1、`plan-015` 1、`maintainer/sync_from_origin.py` 5。
  - 工单里那份「22 处、7 个文件」是 2026-11-23 对 v0.5.0 的 `source.zip` 量的，名单里还有 `kit/PRD/PRD-010` 与 `kit/tasks/plan-010`。**这两份现在已经是 0 处**（AWK-114 前后已清），顶上来的是 `plan-013`、`plan-015`。所以清单按本单开工前的实测为准，工单里那份数字作为「当时的事实」记进变更记录，不改它。
  - `grep -rn -F "D:/AI工作区" README.md README.en.md docs/ kit/PRD/` 当前命中 3 处，全在 `README.md`（2）与 `README.en.md`（1）；`docs/` 与 `kit/PRD/` 已经是 0。
  - 另外两种写法：`/d/AI工作区` 2 处（`CHANGELOG.md:128` 与 `design/` 下那份已作废的项目接入面 PRD 第 181 行），`d:\\ai工作区` 1 处（`docs/probe.md:19` 的示例 JSON）。
  - `python tools/run_tests.py` 基线：末行 `结果：全部通过`，汇总 `tests/ 354 OK；tools/ 658 OK | 合计 1012 项`（跳过 3 项）。
  - `install.py` 的机制：双花括号包住大写的 workspace 单词那种占位符在安装时被替换成真实绝对路径。仓库里放占位符而不是某台机器的路径，是这套工具本来的做法（`kit/tools/idle_watch.py` 已按这个改过）。

## 所用 skill

- `writing-plans`：把结论落成每条带「命令 + 期望输出」的任务块，按「先量、再改、最后交清单」排，而不是按文件排——它让我把「每一处命中的去向」当成交付物，而不是把「grep 变空」当成终点。
- `planning-and-task-breakdown`：按「能不能单独被验收」切块：重测与清单一块、三个要改的文件各自一块、变更记录与全量核查一块；不做「顺手把 README 的用例数也改了」这种把两件事塞进一单的扩散。
- `verification-before-completion`：说「做完了」之前必须贴 `python tools/run_tests.py` 的末行与两条 grep 的原样输出；「应该干净了」不算证据，这条单最容易犯的正是这个错——它看上去只是删几个字符。
- `ticket-dispatch`：提交成果只发一条回执（工单号 + 状态 + 提交 sha），成果清单带 `.worktrees/AWK-112/` 前缀，不自己验收。

## 交付物

**主题**：把公开仓库里「作为默认路径或用法示例出现」的本机路径换成占位符或中性说法；把不可能中性化的每一处逐条归到 **已改 / 历史记录保留 / 上游专名** 三档之一，写成一份可逐行核对的清单。

**形状**：三份文件的小改（`README.md`、`README.en.md`、`maintainer/sync_from_origin.py`）+ `CHANGELOG.md`「未发布」下的一条新条目（含 18 处逐行清单与归宿）。三档清单是这张单的主要产出，代码改动是它的后果。

**给谁看**：第一次把仓库 clone 下来的人（`README` 是他们唯一会读的东西）；以及验收会话——它会拿这份清单逐行对 `grep` 的实际命中。

**不覆盖什么**：
- 不改 `CHANGELOG.md` 与 `kit/tasks/` 里已有的任何一行：那是当时发生的事，改掉等于篡改记录。新的清单只以「未发布」下的新条目形式出现。
- 不改 `design/PRD/`、`panel/docs/`、`kit/docs/probe.md` 之外的示例文本，`design/PRD/PRD-009` 的 `/d/AI工作区` 是已作废条目的历史证据，只登记不改。
- 不动 git 历史（不 rebase、不 amend、不 force push），不回写已发布的 Release/tag，不升 `VERSION`、不打 tag、不推送。
- 不改 `task_center/`、不改任何闸门脚本的判断逻辑、不写库、不改工单状态与判据。
- 不顺手改 `README` 里 `合计 1004 项` 那类用例数——它在本单开工前已经是 1012（属 AWK-114 的读数，见「未决」）。

**服务本轮目标第 6 条**（`tasks/round.md`「文档与工具剩余增量」里「文档」那一半）：本单让随包文档不再把某台机器的目录当成默认路径。

## 任务

### 任务 1：重测并把每一处命中归到三档

在 `.worktrees/AWK-112` 里把 `## 依据` 那两条扫描各重跑一次，再补两条变体扫描：`grep -rn -F "/d/AI工作区" --exclude-dir=.git .` 与 `grep -n -F 'd:\\ai工作区' kit/docs/probe.md`。结果逐行写进临时台账，后面直接搬进 `CHANGELOG`。

三档的判定口径：能被读者当成「这台机器上的默认目录」的 → 已改；写明是当时发生的事的记录（`CHANGELOG`、`kit/tasks/plan-*`）→ 历史记录保留；指的是这台机器上那个具体项目的名字与身份、换掉会让人读不懂（`design/PRD/PRD-009` 里已作废条目的现场证据）→ 上游专名。

完成证据：`grep -rn -F "/d/AI工作区" --exclude-dir=.git .` 输出 2 行（`CHANGELOG` 与 `design/` 下那份作废 PRD）；`grep -n -F 'd:\\ai工作区' kit/docs/probe.md` 输出 1 行；改前那条全仓扫描数行数输出 18（与工单里 22 的差异在台账里写一行说明）；三档清单初稿 18 行，每行含「路径:行号 + 原样片段 + 归宿 + 一句理由」。反例：清单只写「CHANGELOG 不改」而不给行号，或出现第四种归宿。

### 任务 2：`README.md` 的两处（小节标题 + PRD 那句）

`README.md:300` 的小节标题「与工作区（那台机器的目录）」换成中性叫法（「与上游工作区的已知差异」），正文里指代同一件事的句子跟着改。

`README.md:304` 第 1 条里「把那串路径当搜索串、`grep` 得到空」这句话是「拿本机路径当用法示例」，改成不出现该路径的等价说法：`kit/PRD/` 下搜不到原工作区的本机路径，逐行清单在 `CHANGELOG` 的新条目里。

只动这两处所在的句子，不重写整节；同节里的 schema 16、迁移编号、刻意留着的五条差异一律不碰。

完成证据：`grep -n "D:/AI" README.md` 无输出、退出码 1（只查「盘符 + 这个项目名」的形状，所以 `D_AI工作区` 那种拒绝样例不受影响）；`python tools/ref_gate.py --paths README.md --kinds line` 末行 `闸门通过`、退出码 0（这一节里有 `文件:N` 引用，改句子时容易改断）。反例：`grep` 仍有输出，或 `ref_gate` 报某行指不到东西。

### 任务 3：`README.en.md` 的同一处

`README.en.md:273` 里「`grep -rl` 那串路径、结果为空」那个括号换成同一件事的英文中性说法（"the upstream machine path no longer appears under `kit/PRD/`"），与中文版口径一致：不是「换了个搜索串」，而是「套件里不再出现上游那台机器的路径」。

完成证据：`grep -n "D:/AI" README.en.md` 无输出、退出码 1；`python tools/ref_gate.py --paths README.en.md --kinds line` 退出码 0。反例：两版说法不一致（一处说「已清」、另一处说「暂时没搜」）。

### 任务 4：`maintainer/sync_from_origin.py` 的五处

这五处不是文档，是「用法示例 + 默认值 + 替换规则」：`--origin` 的默认值写死成本机目录，等于把这个脚本锁在某台机器上。改法：

- `--origin` 改成必填来源：命令行没给就看环境变量，两者都没有就退出码 2 并打一行用法（不猜一个默认目录）。
- 替换规则不再写死常量，改成由实参 `origin` 派生出三种写法（`/`、`\`、`\\`）再替换成占位符——同一份代码对任何原目录都成立，也顺手把「换一台机器就要改脚本」这件事去掉。
- docstring 的用途与用法各改一句，指代原工作区时用中性说法。

完成证据：`grep -n -i "AI工作区" maintainer/sync_from_origin.py` 无输出、退出码 1（连专名一起清，不留“半句”）；`grep -n "origin" maintainer/sync_from_origin.py | head -20` 的输出里默认值是「必填或环境变量」而不是某个具体目录（判据 C4）；`python maintainer/sync_from_origin.py` 不带参数时退出码 2 且打出一行用法；`python maintainer/sync_from_origin.py --origin /nonexistent-dir` 退出码 2 并打「原工作区不完整或不正确」。反例：不带参数时它照样跑起来、往 `kit/` 里写东西。

### 任务 5：变更记录 + 全量核查

在 `CHANGELOG.md` 的 `## 未发布` 下**新增**一条（放在最上面，不动下面任何一条），内容包括：这张单为什么做、18 处逐行清单与三档归宿、工单里那份 22 处的数字为什么与今天的 18 处不同（当时含 `PRD-010`、`plan-010`，这两份现在已 0 处）、以及刻意没做的事（不改历史、不改判据、不回写 Release）。

完成证据：`grep -n -A 40 "公开仓库里剩下的本机路径" CHANGELOG.md | head -60` 能看到 18 行清单与三档标签；`python kit/tools/doc_gate.py kit/tasks/plan-016-公开仓库本机路径清理.md` 输出 `闸门通过：1 个文件`、退出码 0；`python tools/run_tests.py` 末行 `结果：全部通过`、合计仍是 1012 项（本单不加用例；数字变了说明改文档时打断了某个引用闸门）；`git archive HEAD | python -c "import sys;print(len(sys.stdin.buffer.read()))"` 退出码 0（判据 C2 的打包那半）。反例：清单里出现没有归宿的行，或合计项数从 1012 变了。

## 不做什么

- 不改 git 历史、不 `amend`、不 `rebase`、不 `force push`、不重打 tag、不回写已发布 Release。
- 不改 `CHANGELOG.md`「未发布」以外的任何历史条目、不改 `kit/tasks/plan-0*` 里的任何一行。
- 不改 `kit/PRD/`（已是 0 处）、不改 `design/PRD/PRD-009` 的历史证据行、不改 `panel/docs/` 的样张。
- 不改 `task_center/`、不改闸门脚本判断逻辑、不加新用例、不升 `VERSION`、不打 tag、不推送。
- 不顺手改 `README` 里的用例数（1004），不顺手清理 `kit/docs/probe.md` 的示例 JSON（只登记，见 `未决`）。

## 未决

- [待澄清：`docs/probe.md:19` 的示例 JSON 写着 `d:\\ai工作区`，它不是判据 C1 的范围（`kit/docs` 不在 C1 的路径里，那份文件在 `kit/` 下、相对路径是 `docs/probe.md`），本单只登记不改；要改的话另开一张单，因为它同时改写示例输出与 `probe` 的可读性。]
- [待澄清：`README` 与 `README.en` 里的 `合计 1004 项` 在本单开工前就已经是 1012（AWK-114 加了 8 项用例），属另一张单的读数，本单不动；数字口径以 `python tools/run_tests.py` 的汇总行为准。]
