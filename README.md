# AI 工作区套件（AI Workspace Kit）

把「AI 干的活有工单、有证据、有独立验收、事后可回查」这件事，打包成别人装一次就能用的东西。

它不是提示词合集，也不是又一个看板。它把一套已经在真实项目里跑了几十张工单的流程，
连同代码、闸门、技能和文档一起交给你：**你只需要有 Python 和一个会读 `AGENTS.md` 的 agent**
（Cindy、Claude Code、Codex 等都可以）。

---

## 它解决什么

用 AI 连续做十步，最容易丢的不是代码，是这三样：

1. **范围**——这次到底要交付什么，验收条件是什么；
2. **归属**——这条改动该记在哪个项目、哪张单上；
3. **结论**——谁说它做完了，依据是哪条证据。

这套工作区把三样都落进一个本地 SQLite：工单记范围与验收条件，尝试记哪条会话在做，
提交与验收记成果、证据和结论。再加四道会**真的非零退出**的闸门：

| 闸门 | 拦住什么 | 命令 |
| --- | --- | --- |
| `work_gate` | 没有工单就想提交 | `python tools/work_gate.py --list` |
| `doc_gate` | 计划没读 PRD、没写清证据 | `python tools/doc_gate.py --all` |
| `close_gate` | 一轮没做完就说「做完了」 | `python tools/close_gate.py --check` |
| `intake_gate` | 交接包缺节就往下传 | `python tools/intake_gate.py <包>` |

---

## 一键安装

要求：**Python 3.10+**；**Git** 建议装（没有也能用，只是少了「没工单不许提交」这道默认路径）。

```bash
git clone <本仓库地址> ai-workspace-kit
cd ai-workspace-kit
python install.py
```

默认装到 `~/ai-workspace`，并把 25 个技能装到 `~/.agents/skills`。常用参数：

```bash
python install.py --workspace D:/ai-ws        # 指定工作区目录
python install.py --dry-run                   # 只打印计划，不动任何文件
python install.py --overwrite-skills          # 用套件版本覆盖同名技能（默认跳过已存在的）
python install.py --no-skills                 # 只装工作区，不动技能目录
python install.py --link-agents               # 顺带为 ~/.claude 与 ~/.codex 建技能链接
```

也可以用包装脚本：

```bash
powershell -ExecutionPolicy Bypass -File scripts/install.ps1   # Windows
bash scripts/install.sh                                        # macOS / Linux
```

安装器做六件事：复制工作区 → 装技能 → 写 `~/.agents/center.json` 与交接协议 →
初始化中心库并登记项目 → `git init` 并做首次提交（在启用钩子之前）→ 启用提交闸门。
装完照它打印的三条命令跑一遍即可。

### 安装后验证

```bash
cd ~/ai-workspace
python tools/work_gate.py --list     # 开工第一条命令
python tools/run_tests.py            # 应全绿（约 3–5 分钟）
python tools/doc_gate.py --all       # 应退出 0
```

---

## 装了什么

```
ai-workspace/
├── AGENTS.md              # 行为契约：六条硬规则、消息通道、收尾、分支与合并
├── README.md
├── PRD/                   # 需求；每个成果都要报 PRD 编号与版本
├── tasks/                 # round.md（本轮目标）、rounds.md（历史）、plan-*.md
├── task_center/           # 工单核心（纯标准库，SQLite）
├── tools/                 # 四道闸门 + auto_merge + run_tests
├── tests/                 # 376 项用例，证明装出来的东西是对的
├── docs/                  # protocol / closing / probe / intake / skill-loading
├── examples/
├── .agents/skills/        # 5 个工作区技能
└── .githooks/             # commit 钩子
```

技能同时装在 `~/.agents/skills`（用户级），所以在**任何项目**里都能被 agent 读到：

- **工作区技能（5）**：`prd-authoring`、`pdsa-workflow`、`ticket-dispatch`、`plan-gate`、`skill-routing`
- **通用技能（20）**：`brainstorming`、`writing-plans`、`executing-plans`、`test-driven-development`、
  `systematic-debugging`、`verification-before-completion`、`code-review-and-quality`、
  `requesting-code-review`、`receiving-code-review`、`merge-latest-branches`、`writing-skills`、
  `shuo-ren-hua`、`dispatching-parallel-agents`、`subagent-driven-development`、
  `resumable-implementation-contracts`、`using-git-worktrees`、`doubt-driven-development`、
  `planning-and-task-breakdown`、`project-routing`、`handoff-to-workspace`

---

## 三种分发方式，为什么主推仓库

| 方式 | 适合谁 | 能装完整工作区吗 | 结论 |
| --- | --- | --- | --- |
| **GitHub 仓库 + `install.py`** | 所有人 | 能：代码、闸门、钩子、技能一次装齐 | **主渠道** |
| **技能包（SkillHub）** | 只想先试试的人 | 不能：技能只是说明，装不了 Python 包与钩子 | 入口：装完指向仓库 |
| **Cindy 插件** | 只用 Cindy 的人 | 不能：插件运行在沙箱里，改不了用户目录与 git 钩子 | 可选包装，非主渠道 |

完整分析见 [`docs/SHARING.md`](docs/SHARING.md)。一句话：这套东西的价值有一半在**文件系统**
（工作树、提交钩子、本地库）上，插件与技能都碰不到那一半，所以仓库是唯一能装全的载体；
另两种做成薄入口，负责把人带到仓库。

---

## 卸载

```bash
python uninstall.py --workspace ~/ai-workspace          # 删工作区（会二次确认）
python uninstall.py --skills                            # 只从 ~/.agents/skills 移除本套件的技能
```

默认**不删** `~/.agents/center.json` 和 `~/.agents/intake/`（可能还有别的项目在用），
要一起删加 `--all`。工作区里的 `.data/` 是你的进度数据，卸载时默认保留。

---

## 来源与许可

本套件的框架代码、闸门、技能与文档来自一个已稳定运行的个人 AI 工作区，
经过去掉项目专属内容和本机绝对路径后整理而成。设计史（原始 PRD 与 AGENTS 记录）
放在 `design/` 下，供想理解「为什么规则长这样」的人查阅。

MIT License，见 [`LICENSE`](LICENSE)。
