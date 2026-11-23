# 计划 005：开机回执与可核验

## 依据

- `kit/PRD/PRD-001-工作区使用与工单体系.md` v0.1：§3「归属不猜」「执行者不自验」「收尾可判定」，AC2（一张单能从创建走到验收）。
- 既有事实（2026-11-21 本机实测）：`python tools/run_tests.py` 当前 497 项全绿；`verify/verify_onboard.py` 九步全在临时目录里跑，一次宿主调用都没有；`python kit/onboard.py --json` 返回 `side_effects: []`；`~/.agents/.ai-workspace-kit.json` 只有 `version` / `workspace` / `skills_dir` / `installed_skills`，没有宿主机字段。
- 0.4.0 已发布：远端 `master` 与 `refs/tags/v0.4.0` 都指向 `bdd043f`。
- 业务依据：暂无 PRD 专门描述「装机后如何核验宿主侧开工」。

## 所用 skill

- `plan-gate`：它规定规划前必须先读 `round.md` 与 PRD 版本并写进计划，所以这份计划有依据节与服务目标编号，不再是先说方案再补出处。
- `writing-plans`：它要求每个任务写明改哪个文件、接口是什么、先写失败用例再跑一次，所以任务块里给了文件路径与「先红后绿」的两步。
- `prd-authoring`：它要求验收条件必须能举反例、命令要自己先跑一遍，所以「回执能不能证明开工」这条写成判断类并点明反例。
- `ticket-dispatch`：它规定验收条件只收「命令加期望输出」或「判断类加反例」，所以本单每条条件都带一条能跑的命令。

## 交付物

**主题**：让「装完 → 开工」这一步留下一个能被第三方事后核对的落盘物，并给它一个会失败的校验器。

**形状**：
- `kit/onboard.py` 的 bootstrap 计划多一步 `local` 步（写 `onboard-receipt.json`），不进 `cindy_helper_tools`；
- 新增 `verify/verify_receipt.py`，只读、三类失败各报各的；
- `skills/workspace/bootstrap-workspace/SKILL.md` 加一节：回执四个字段、写在哪、缺回执时的判据；
- 四条样例用例随 `tools/run_tests.py` 跑。

**给谁看**：拿这个包在别的机器上装的人，和事后做验收的会话。

**明确不覆盖**：不证明宿主里项目真的打开了（那要靠下一张演练单里的 `list_sessions` 反查）；不改 schema；不发版；不推送；不碰 `tool_center` 的闸门判断逻辑。

**服务本轮目标第 1、2、3 条**（回执、校验器、文档口径）；第 4 条演练明确不在本单。

## 任务

### 任务 1：定下回执的字段与落点，写进技能文档

改 `skills/workspace/bootstrap-workspace/SKILL.md`：新增「五、写回执」一节，写明四个字段、文件位置 `<workspace>/.data/onboard-receipt.json`、以及「没有回执就不许说已开工」。

验证：`grep -c "onboard-receipt" skills/workspace/bootstrap-workspace/SKILL.md` 输出大于等于 1；`grep -n "target_session_id" skills/workspace/bootstrap-workspace/SKILL.md` 输出里能看到四个字段名。

### 任务 2：`onboard.py` 的计划加回执步（先红后绿）

先写会失败的用例 `tools/test_onboard.py::test_bootstrap_plan_has_local_receipt_step`，跑一次确认它红，再改 `build_plans`。

验证：`python kit/onboard.py --json` 的输出里 `plans.bootstrap.steps` 为 5 步，第 5 步 `kind` 是 `local`、其 args 里的路径以 `/onboard-receipt.json` 结尾，且 `cindy_helper_tools` 仍是四个宿主工具。输出贴进 evidence。

### 任务 3：`verify/verify_receipt.py` 与四条用例

三类失败各报各的：文件不存在（`receipt missing`）、`project_path` 不是绝对路径、`target_session_id` 为空。四条用例覆盖「缺失 / 路径不合格 / 标识为空 / 全部合格」。

验证：在一个空目录里跑 `python verify/verify_receipt.py --workspace .`，退出码非零且输出含 `receipt missing`；`python tools/run_tests.py` 退出码 0，两处用例数都打印，合计不少于 501 项。

### 任务 4：与插件计划保持同源

插件 `bootstrap` 工具返回的计划不得与 `onboard.py` 的计划分叉：回执步只出现在工作区侧，或两侧同时出现但都不进宿主工具清单。

验证：`python tools/test_plugin_plan.py` 退出码 0；`python kit/onboard.py --json` 的 `cindy_helper_tools` 仍是四个。

### 任务 5：闸门与提交前自检

验证：`python tools/doc_gate.py --all` 退出码 0；`git status --short` 输出为空。

## 不做什么

- 不声称回执能证明宿主里项目打开了；回执只是一份声称，真实反查由演练单做。
- 不改 `task_center/` schema、不动 `tools/` 下闸门与合并的判断逻辑、不发版、不打 tag、不推送。
- 不做真机演练与插件导入（要一次人工点击）。
- 不把 `schema` 10–13 与 `PRD-010`~`PRD-015` 的增量同步进来。
- 不动上游工作区的主检出。

## 未决

- [待澄清：插件 `bootstrap` 工具要不要也返回回执步] 默认要，因为两条入口同源，差一步就会给出不同计划；恢复条件：插件通道无法返回非宿主步时，在插件说明里明写「回执由会话按 SKILL.md 写」。
- 判断类风险：回执可被伪造（格式合法的假 `target_session_id`）。默认：脚本只校验形状并在输出里打印「形状通过，未验证会话存在」。恢复条件：宿主能离线导出会话清单时，再把它接进脚本。
