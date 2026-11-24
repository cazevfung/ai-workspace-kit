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
# 1. 建单（request-id 是幂等键，重试时沿用同一个；不传 ticket_id，号由发号盘发，回执里的 id 才是真号）
echo '{"project_id":"ai-workspace","title":"第一张工单","deliverable":"...","prd_path":"PRD/PRD-001-工作区使用与工单体系.md","criteria":[{"id":"C1","text":"含 `python tools/run_tests.py`，退出码 0"}],"root":"<工作区>"}' \
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
| `tools/` | 闸门：`work_gate`（没工单不许提交）、`doc_gate`（计划格式）、`close_gate`（一轮怎么算完）、`auto_merge`（验收后合并）、`intake_gate`（交接包）、`claims_gate`（文档里的数复算）、`ref_gate`（引用与锚点）、`check_eol`（行尾自检）、`pool_report`（给空判据单记一笔「已经点名报过」）、`run_tests`（唯一验证入口）、`add_project`（把业务仓库写配置并指向 `.githooks`） |
| `.agents/skills/` | 工作区技能：`prd-authoring` / `pdsa-workflow` / `ticket-dispatch` / `plan-gate` / `skill-routing` / `bootstrap-workspace` |
| `docs/` | 协议与判据：`protocol`、`closing`、`probe`、`intake`、`skill-loading` |
| `PRD/` `tasks/` | 需求与本轮目标；进度以中心库为准，不另维护 Markdown 进度 |

`.agents/skills/` 里的技能同时装在用户级目录 `~/.agents/skills/`，因此在任何项目里都能被 agent 读到。

## 行尾口径：文本一律 LF

`.gitattributes` 把全仓文本钉成 LF（`* text=auto eol=lf`）。只有 `skills/**` 保持 `-text`
（连「按 LF 存」都不做，一律原字节进出，因为它要与 live 技能目录逐字节一致）。二进制文件由
`text=auto` 按内容判定，不受转换影响。手工安装的工作区里还多一处 `.agents/skills/skill-routing/index/**`
快照，同样标 `-text`。

这么钉是为了让「检出的字节」等于「库里的 blob」：`task_center/artifacts.py` 记的 sha256
是**工作树字节**算出来的，同一份内容在 `core.autocrlf=true` 与 `false` 的机器上一个是
CRLF、一个是 LF，指纹就成了两个数，换台机器复核只能报 `ARTIFACT_CHANGED`。

`python tools/check_eol.py` 就是核这件事的：声明面看 `git check-attr` 的 `eol`，落地面看工作树
字节里有没有 CRLF。**它核的是「某一次检出」而不是「仓库」**，所以规则合入之后，每个已有的检出
都要就地重检出一次，否则工作区停在半新半旧的 CRLF 上（`git add` 会一直警告
`CRLF will be replaced by LF`）：

```bash
git rm --cached -r -q . && git reset --hard
```

**这一步会丢未提交的改动**：暂存区与工作区里没提交的内容、以及挡在受管路径上的未跟踪
文件都会被覆盖。先把它们提交，或挪到 `.data/` 下再跑。新克隆不需要这一步——检出即 LF。

## 不要把它当成

- 不是全自动流水线：`claim` 之后调用宿主创建会话那一步仍是手工的。
- 不是安全边界：闸门拦的是「忘了开工单」，不是恶意改动；`--no-verify` 能绕过。
- 不是项目管理系统：它只记「这一张单做到哪一步、证据是什么、谁验收的」。
