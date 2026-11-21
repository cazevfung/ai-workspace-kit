# AI 工作区

本工作区统一通过 Cindy（或其他支持 `AGENTS.md` + skill 的 agent）普通对话框协调、执行和验收。
它要解决的不是「让 AI 写代码」，而是「让 AI 的改动有工单、有证据、有独立验收、有可回查的记录」。

## 进来先读

1. [`AGENTS.md`](AGENTS.md)——项目归属、六条硬规则、已知未实现清单。
2. [`PRD/`](PRD/)——需求依据；每个成果都要写清依据的是哪份 PRD 的哪个版本。
3. [`docs/protocol.md`](docs/protocol.md)——调用顺序与错误码。
4. [`docs/decisions.md`](docs/decisions.md)——随包口径：消息通道三档、回执一条、送达判据、项目接入面；本文与 `AGENTS.md` 里引到这几件事的地方都指向它。

## 一分钟上手

```bash
python tools/work_gate.py --list        # 开工第一条命令：现在有没有活跃尝试
python tools/run_tests.py               # 验证环境，应全绿
python -m task_center --db "<工作区>/.data/task-center.sqlite3" list_projects
```

第一张工单的完整流程：

```bash
# 1. 建单（request-id 是幂等键，重试时沿用同一个）
echo '{"project_id":"ai-workspace","ticket_id":"T-001","title":"第一张工单","deliverable":"...","prd_path":"PRD/PRD-001-工作区使用与工单体系.md","criteria":"...","root":"<工作区>"}' \
  | python -m task_center --db "<工作区>/.data/task-center.sqlite3" create_ticket --request-id c1

# 2. 领取 → 3. 绑定会话 → 4. 提交成果 → 5. 另一个会话发起验收
```

完整字段与错误码见 [`docs/protocol.md`](docs/protocol.md)，派发顺序见
`.agents/skills/ticket-dispatch/SKILL.md`。

## 这套东西由什么组成

| 部分 | 作用 |
| --- | --- |
| `onboard.py` | 启动层：打印「create_project → 开协调会话 → 第一条指令」的调用计划，`--json` 给完整参数 |
| `task_center/` | 工单核心：项目、工单、尝试、提交、验收、事件，全部落 SQLite |
| `tools/` | 闸门：`work_gate`（没工单不许提交）、`doc_gate`（计划格式）、`close_gate`（一轮怎么算完）、`auto_merge`（验收后合并）、`intake_gate`（交接包）、`claims_gate`（文档里的数复算）、`ref_gate`（引用与锚点）、`run_tests`（唯一验证入口）、`add_project`（把业务仓库写配置并指向 `.githooks`） |
| `.agents/skills/` | 工作区技能：`prd-authoring` / `pdsa-workflow` / `ticket-dispatch` / `plan-gate` / `skill-routing` / `bootstrap-workspace` |
| `docs/` | 协议与判据：`protocol`、`closing`、`probe`、`intake`、`skill-loading` |
| `PRD/` `tasks/` | 需求与本轮目标；进度以中心库为准，不另维护 Markdown 进度 |

`.agents/skills/` 里的技能同时装在用户级目录 `~/.agents/skills/`，因此在任何项目里都能被 agent 读到。

## 不要把它当成

- 不是全自动流水线：`claim` 之后调用宿主创建会话那一步仍是手工的。
- 不是安全边界：闸门拦的是「忘了开工单」，不是恶意改动；`--no-verify` 能绕过。
- 不是项目管理系统：它只记「这一张单做到哪一步、证据是什么、谁验收的」。
