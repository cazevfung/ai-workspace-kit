# 计划 001：把工作区跑起来并收下第一张工单

## 依据

- `PRD/PRD-001-工作区使用与工单体系.md` v0.1
- `AGENTS.md` 的六条硬规则与「收尾」一节

## 所用 skill

- `plan-gate`：它要求计划先读 PRD、记下版本号、按任务类型挑技能，并过 `doc_gate`；这份计划按它的六节结构写。
- `ticket-dispatch`：它规定领取、绑定、提交、验收的顺序与必填字段；第 2 个任务照它的调用顺序做。

## 交付物

- 主题：一个能跑通的空工单闭环，以及一份可以照着执行的上手路径。
- 不覆盖：不修改 `task_center/` 的代码与 schema，不新增第三方依赖，不改 `docs/protocol.md` 的调用口径。

## 任务

### 1. 确认环境

跑一遍仓库自带的两处测试，确认代码与本机 Python 版本匹配。

- 验证：`python tools/run_tests.py` 期望输出末行「结果：全部通过」，退出码 0。

### 2. 建第一张工单并走完闭环

按 `ticket-dispatch` 的顺序建单、领取、绑定一个执行会话、提交成果，再由另一个会话验收。

- 证据（判断类）：`create_ticket` 回执里有 `ticket_id`，提交后 `get_ticket` 返回 `state=awaiting_review`。反例：提交后状态仍是 `running`，或验收会话与领单的是同一个会话。

### 3. 收尾并留下记录

按 `docs/closing.md` 写七节收尾包，把 `tasks/round.md` 的状态位改成结论之一。

- 验证：`python tools/close_gate.py` 期望输出无 FAIL、退出码 0；工作区干净、库里无在途单时 `python tools/close_gate.py --check` 同样退出码 0。

## 不做什么

- 不改 `task_center/` 与 schema：这一轮只验证环境与流程，不改机制。
- 不引入定时任务、服务或外部依赖。
- 不在没有用户确认时推送远端。

## 未决

- 无。
