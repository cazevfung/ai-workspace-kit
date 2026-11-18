# 本轮：把工作区跑起来，收下第一张工单

轮次：R-001
状态：进行中

## 本轮目标

1. 确认环境可用：`python tools/run_tests.py` 全绿。
2. 在中心库里留下第一张走完「创建 → 领取 → 绑定 → 提交 → 验收」的工单。
3. 让这一轮的收尾经过一次 `close_gate`，而不是口头说「做完了」。

## 结束条件

- `python tools/run_tests.py` 退出 0，且 `tests/` 与 `tools/` 两处的用例数都打印出来。
- `python tools/doc_gate.py --all` 退出 0。
- `python tools/work_gate.py --list` 在开工时能读到这张单。

## 不做什么

- 不修改 `task_center/` 的 schema，不改 `docs/protocol.md` 的调用口径。
- 不引入第三方依赖；核心只依赖 Python 标准库与 git。
