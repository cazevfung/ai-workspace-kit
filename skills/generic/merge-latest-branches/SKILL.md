---
name: merge-latest-branches
description: 验收通过之后把工单分支合进 master。用户问「验收完了要不要合」「合进 master」「冲突了怎么办」时使用；合并前工作区要干净、用例要全绿，有冲突先停下问人，不许自动解完直接合。
---

# 合并最新分支并发布

把还没进 master 的最新改动合进来，同时保住 master 和工作区里已经生效的行为。冲突不能整文件选用旧分支。

## 先固定现状

合并前记录 HEAD、`git status`、产品文件的未提交 diff、`git worktree list`、`git branch --no-merged HEAD`，以及 `refs/cindy/savepoints` 里还不是 HEAD 祖先、且改动了 `frontend/src`、`pipeline/*.py` 或 `plans/` 的存档。忽略 `node_modules`、`dist`、`__pycache__` 和临时脚本。

工作区里已有的产品改动先提交到 master，再合并。没提交的修改不会进入合并，也不能在发布时丢掉。不要提交 `.cindy-worktrees/`。

## 只合并最新的

- 分支尖端已在 master 里的，跳过。
- 一个候选是另一个候选的祖先时，只合并较新的那个。
- worktree 目录和分支名不等于最新内容。以分支尖端、存档相对其父提交的产品 diff 为准。
- 存档里的产品改动如果 master 还没有，按同一套冲突规则补上。已经在 master 里的不重复合。

一次只合一个候选。合完并确认没有冲突标记后，再合下一个。

## 冲突怎么留

不要对整个文件使用 `--ours` 或 `--theirs`。

- 只有一边新增的函数、字段、界面区块，留下。
- 同一处用户可见行为不一致时，留合并前 master（含刚提交的工作区改动），不让较旧的分支把旧控件、旧文案、旧筛选放回来。
- 同一函数两边都改过时，保留 master 上较晚的修复，同时收下分支独有的新逻辑。
- 两边改的是同一行为，又无法判断哪边是合并前的现状时，停下来问。不要猜，也不要发布。

## 合完再看有没有被盖掉

对照合并前的 master 和工作区 diff：

- 合并前才有的界面行为必须还在。
- 新分支独有、且不和较新 master 冲突的行为必须进来。
- 被旧分支盖掉的差异要说明；不确定就不要发布。

改过前端就在 `frontend` 执行构建。改过 Python 就 `python -m py_compile` 相关文件。

## 发布

只有用户要求发布时才做。站点是 `heartopia-content-dashboard`。用已提交的代码构建，`base` 为 `/s/r0gz9bxhsaxy/`。

按 XD Sites 更新已有站：`sites_publish_plan` 使用 `intent=update_existing`、`publishNow=true`。先把返回的完整 `summaryMarkdown` 发到对话里，再按该计划的执行参数调用 `sites_static_deploy`。不要在同一次执行里既规划又部署。

