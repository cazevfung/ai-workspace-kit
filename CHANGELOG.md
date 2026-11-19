# 更新记录

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
