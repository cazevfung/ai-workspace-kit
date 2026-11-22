# 本轮：装完之后能被核验

轮次：R-002
状态：进行中

## 本轮目标

1. **开工回执**：`kit/onboard.py` 的 bootstrap 计划多一步「本地写回执」，回执落到 `<workspace>/.data/onboard-receipt.json`，字段四个（`project_path` / `target_session_id` / `working_dir` / `created_at`）；这一步标成 `local`，不冒充 `cindy_helper` 的工具。
2. **回执校验**：新增 `verify/verify_receipt.py`，缺文件、路径不是绝对路径、`target_session_id` 为空这三类各报各的，一律非零退出；随四条样例用例进 `python tools/run_tests.py` 的计数。
3. **文档口径**：`skills/workspace/bootstrap-workspace/SKILL.md` 写明回执写什么、写在哪、缺回执时不许说「已开工」。
4. **陌生人演练**排在回执可用之后单独成单：真装、真导插件、真建项目，其中插件导入那一下要人点，本轮不做。

## 结束条件

- `python tools/run_tests.py` 退出码 0，两处用例数都打印，合计不少于 501 项（当前 497 加 4 条回执用例）。
- `python verify/verify_receipt.py --workspace .` 在一个没有回执的空目录里退出码非零并输出 `receipt missing`；在写好回执的目录里退出码 0。
- `python kit/onboard.py --json` 的 bootstrap 计划共 5 步，第 5 步 `kind` 是 `local`，`cindy_helper_tools` 仍是四个。
- `python tools/doc_gate.py --all` 退出码 0。
- 独立验收会话给 `accept`，判词逐条带它自己跑出来的输出。

## 不做什么

- 不声称回执能证明宿主里项目真的打开了：回执只是一份声称，真实反查留在演练单。
- 不改 `task_center/` 的 schema，不动闸门脚本的判断逻辑，不发版，不打 tag，不推送。
- 不做陌生人真机演练与插件导入（要一次人工点击），排下一张单。
- 不把 `schema` 10–13 与 `PRD-010`~`PRD-015` 的增量同步进来，那是另一类事。
- 不动 `D:/AI工作区` 主检出。
