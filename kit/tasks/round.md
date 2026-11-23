# 本轮：装完之后能被核验

轮次：R-002
状态：进行中

## 本轮目标

1. **开工回执**：`kit/onboard.py` 的 bootstrap 计划多一步「本地写回执」，回执落到 `<workspace>/.data/onboard-receipt.json`，字段四个（`project_path` / `target_session_id` / `working_dir` / `created_at`）；这一步标成 `local`，不冒充 `cindy_helper` 的工具。
2. **回执校验**：新增 `verify/verify_receipt.py`，缺文件、路径不是绝对路径、`target_session_id` 为空这三类各报各的，一律非零退出；随四条样例用例进 `python tools/run_tests.py` 的计数。
3. **文档口径**：`skills/workspace/bootstrap-workspace/SKILL.md` 写明回执写什么、写在哪、缺回执时不许说「已开工」。
4. **陌生人演练**排在回执可用之后单独成单：真装、真导插件、真建项目，其中插件导入那一下要人点，本轮不做。

5. **引擎同步**：把 `kit/task_center` 与 `kit/tools` 同步到工作区当前状态（schema 13、迁移 9–12、`planned` 与发号、依赖边、车次、冲突预演、回执与问答），让套件自带的 `auto_merge` 能读同一个共享库并完成一次真实合并。

6. **文档与工具剩余增量**：`PRD-010`~`PRD-015` 正文、技能文本、`idle_watch.py` 与 `project_doctor.py` 同步进套件；同时补两处实测出来的盲点——`wait_audit` 要报「`awaiting_review` 但 15 分钟没有回执」，`auto_merge` 的 schema 闸门要认得 `kit/task_center/db.py`。

## 结束条件

- `python kit/tools/auto_merge.py --list` 退出码 0，读的是同一个共享库（当前库是 schema 13）。
- `python tools/run_tests.py` 退出码 0，两处用例数都打印，合计不少于 501 项（当前 497 加 4 条回执用例）。
- `python verify/verify_receipt.py --workspace .` 在一个没有回执的空目录里退出码非零并输出 `receipt missing`；在写好回执的目录里退出码 0。
- `python kit/onboard.py --json` 的 bootstrap 计划共 5 步，第 5 步 `kind` 是 `local`，`cindy_helper_tools` 仍是四个。
- `python tools/doc_gate.py --all` 退出码 0。
- 独立验收会话给 `accept`，判词逐条带它自己跑出来的输出。

## 不做什么

- 不声称回执能证明宿主里项目真的打开了：回执只是一份声称，真实反查留在演练单。
- 不改 `task_center/` 的 schema，不动闸门脚本的判断逻辑，不发版，不打 tag，不推送。
- 不做陌生人真机演练与插件导入（要一次人工点击），排下一张单。
- 不把 `PRD-010`~`PRD-015` 的 PRD 正文与技能文本同步进来，也不同步 `idle_watch.py` / `project_doctor.py`——那是第二张单。
- 不把工作区自己的库迁移或备份当成这张单的成果。
- 不动上游工作区的主检出。
