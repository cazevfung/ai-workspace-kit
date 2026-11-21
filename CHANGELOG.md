# 更新记录

## 未发布（KIT-005）

主题：装上就能开工。

- 新增 `onboard.py`：打印「`create_project` → 开协调会话 → 第一条指令」的调用计划，`--json` 给每步完整参数，`--dry-run` 零副作用；`dispatch_ticket` 计划复用同一份 `send_to_session` 参数。
- 新增 `skills/workspace/bootstrap-workspace`：写清经 `cindy_mcp_call_tool` 调 `cindy_helper` 的确切参数、预演与事后 `list_sessions` 反查。
- `plugin/draft/` 升级为可安装清单：`ghost.json` 声明 `bootstrap` 与 `dispatch_ticket`，`main.js` 返回与 `onboard.py --json` 同源的步骤与失败停点，补 `locales/`。图标待发布前用真实素材补。
- 新增 `tools/add_project.py`：为业务仓库写 `.agents/ticket-config.json`（`test_cmd` / `target_branch`）并把 `core.hooksPath` 指向套件 `.githooks`。
- `tools/auto_merge.py` 改读项目配置：缺配置在第 2 步非零退出并提示跑 `add_project.py`，不再回落 `tools/run_tests.py`；目标分支默认取配置值。
- `tools/doc_gate.py` 补源码树的技能发现路径（`skills/workspace` 与 `skills/generic`），修掉 0.3.0 里「在源码目录直接跑测试有 7 项 `test_doc_gate` 失败」的已知问题；判定条件未改。
- 新增 `verify/verify_onboard.py`：临时 HOME 里跑 install → add-project → create_ticket → claim → bind → submit → review → merge → close 九步。
- 用例数 453 → 481。

## 0.3.0

同步到原工作区 2026-11-20 13:32（master `6bc6d32`），对应上游 R-007 到 R-009 三轮。

- 中心库 schema 8 → 9：提交时记下被验收那棵树的 head sha，合并前与被合并的树比对，合错树会被挡下。
- 新增 `tools/claims_gate.py` 与 `docs/claims-format.md`：把文档里写的数（计数、合计、比例、摘要、字节数、行号锚点、清单条数）变成一次能失败的复算。`--coverage` 只列没有 claim 覆盖的活数位置，退出码恒为 0。
- 新增 `tools/ref_gate.py`：行号引用与「见 某文件 的「某标题」」锤点，指向不存在就非零退出。
- `tools/auto_merge.py` 随 schema 变化更新；`ticket-dispatch` 技能补上新的验收与合并口径。
- 用例数 381 → 453。
- 已知：在侢架目录直接跑 `python tools/run_tests.py` 会有 7 项 `test_doc_gate` 失败，因为那些用例要求项目里存在 `.agents/skills`。安装之后跑是全绿的，`verify_install.py` 验的就是安装后的状态。

## 0.2.1

- 修安装器：技能与交接协议里的 `{{WORKSPACE}}` 以前会原样留在用户机器上，现在复制时就换成真实路径。注入统一用正斜杠，因为 `.json` 里的反斜杠要转义，直接塞 Windows 路径会把 JSON 弄坏。
- `verify_install.py` 加一条检查：装完之后 `skills` 与 `intake` 下不能残留占位，现在 14 项全过才算通过。
- README 补「从需求到合并：它管的是整条线」：PRD 与版本、两次评审、工单派到不同项目、别的项目请工作区接管，以及交接链在包里的四个件。

## 0.2.0

同步到原工作区 2026-11-19 12:02（master `688ae7f`）的状态。

- `tools/doc_gate.py`：新增「完成证据必须可判定」判据（任务块里要有可跑命令 + 期望输出，或写成判断类并给出反例）。
- `tools/evidence_baseline.txt`：历史计划豁免清单。原工作区里的条目不会随套件分发，装出来是空基线。
- `skills/workspace/skill-routing/`：新增 `scripts/`（路由检查、索引检查、上游漂移检查、技能重新链接）与 `index/` 快照，路由表可直接自检。
- `docs/skill-loading.md`：技能内容入库与加载验证的判定方式。
- `AGENTS.md`：回执口径改为「一条回执 + 一次落库 + 一次会话列表核对」；终态只落库不唤醒。
- `skills/workspace/prd-authoring`、`ticket-dispatch`、`installed-skills.md` 同步更新。
- 用例数 376 → 381。
- 新增 `maintainer/redact_for_public.py`：同步之后清掉真实用户名与会话 id（`tests/fixtures` 是原样复制的，光靠同步脚本清不掉）。

## 0.1.0

首个可分发版本。

- 工作区骨架：`task_center/` 工单核心、`tools/` 四道闸门与 `auto_merge`、`tests/` 全量用例。
- 25 个技能：5 个工作区技能 + 20 个通用技能，随仓库分发。
- 一键安装：`install.py`（跨平台）+ `install.ps1` / `install.sh`。
- 卸载：`uninstall.py`，按安装清单只删本套件装进去的东西。
- 文档：`docs/SHARING.md`（载体选型分析）、`docs/INSTALL.md`、`docs/ARCHITECTURE.md`。
- 设计史：`design/` 保留原始 PRD 与 AGENTS 记录。
