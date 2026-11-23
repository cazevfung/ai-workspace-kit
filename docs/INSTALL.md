# 安装与验证

## 前置条件

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.10+ | 核心只用标准库，不需要 pip install |
| Git | 建议 | 没有也能用，但少了「没工单不许提交」这道默认路径 |
| Agent | 读 `AGENTS.md` 与 `~/.agents/skills` 的宿主 | Cindy、Claude Code、Codex 等 |

## 安装

```bash
git clone https://github.com/cazevfung/ai-workspace-kit ai-workspace-kit
cd ai-workspace-kit
python install.py
```

或：

```bash
bash scripts/install.sh                    # macOS / Linux
powershell -ExecutionPolicy Bypass -File scripts/install.ps1   # Windows
```

### 参数

| 参数 | 作用 |
| --- | --- |
| `--workspace PATH` | 工作区目录，默认 `~/ai-workspace` |
| `--skills-dir PATH` | 技能目录，默认 `~/.agents/skills` |
| `--force` | 目标目录非空时覆盖 |
| `--no-skills` | 只装工作区，不动技能目录 |
| `--overwrite-skills` | 用套件版本覆盖同名技能（默认跳过已存在的） |
| `--link-agents` | 为 `~/.claude/skills` 与 `~/.codex/skills` 建链接 |
| `--dry-run` | 只打印计划，不写任何文件 |

## 安装器做了什么

1. 复制 `kit/` 到工作区，并把 `{{WORKSPACE}}` 替换成真实路径；
2. 把 26 个技能复制到 `~/.agents/skills`（已存在的默认跳过）；
3. 写 `~/.agents/center.json`（中心库指针）与 `~/.agents/intake/`（交接协议）；
   同时写 `<工作区>/.agents/center.json`，记住这次安装用的是哪个用户级目录
   （`--agents-home` 装到别处时，闸门按它找库，不去读 `~/.agents`）；
4. 初始化 `.data/task-center.sqlite3` 并登记项目 `ai-workspace`；
5. `git init` 并做首次提交（**在启用钩子之前**，否则钩子会拦住这次提交）；
6. 配置 `core.hooksPath = .githooks`。

## 验证

```bash
cd ~/ai-workspace
python tools/work_gate.py --list     # 开工第一条命令
python tools/run_tests.py            # 应全绿
python tools/doc_gate.py --all       # 应退出 0
```

`run_tests.py` 会分别跑 `tests/` 与 `tools/test_*.py` 两处，并打印各自用例数。
只看到「全部通过」而没有两处的数字，说明你跑的不是这个脚本。

## 在 agent 里使用

- **Cindy**：把工作区目录加为项目；技能已装在 `~/.agents/skills`，会话能直接读到。
  派发执行会话用 `send_to_session`，顺序见 `.agents/skills/ticket-dispatch/SKILL.md`。
- **Claude Code**：安装时加 `--link-agents`，或手工把技能链到 `~/.claude/skills`。
- **Codex**：同上，链到 `~/.codex/skills`。

## 卸载

```bash
python uninstall.py --workspace ~/ai-workspace
python uninstall.py --skills
python uninstall.py --all            # 连 center.json 与 intake/ 一起删
python uninstall.py --purge-data     # 连 .data/ 进度库一起删
```

只删安装清单 `~/.agents/.ai-workspace-kit.json` 里记过的技能，不动你原有的同名技能。

## 常见问题

**`work_gate.py` 报读不了库。** 它报的码是 `DB_NOT_FOUND`，并且会打出**它读的是哪个
文件**。发现顺序写在 `docs/closing.md` 的「中心库怎么被发现」：`--db` →
`TASK_CENTER_DB` → `TASK_CENTER_AGENTS_HOME` → 工作区指针 `.agents/center.json` 记的
`agents-home` → 本工作区 `.data/task-center.sqlite3` → `~/.agents/center.json`。
用 `--agents-home` 装过、而 `<工作区>/.agents/center.json` 里记的那个目录已经被挪走或
删掉时，它不会偷偷回落到 `~/.agents`（那是另一套库）：重新跑一次 `install.py --force`，
或显式 `--db` / `TASK_CENTER_DB` 指过去。不要另建一个备用库。

**`git commit` 被拒。** 这是提交闸门在工作。先 `python tools/work_gate.py --list`
看有没有活跃尝试；没有就开工单。确实要提交无工单的改动，用 `--no-verify`，
或者显式 `git config --unset core.hooksPath`（要知道自己在关什么）。

**技能在 agent 里看不到。** 确认技能装在 `~/.agents/skills/<名字>/SKILL.md`；
Cindy 每次以 `--no-approve` 启动，项目级技能目录不参与启动，用户级目录才可靠。

**测试很慢。** 590 项用例大约 1–2 分钟；第一次跑会创建临时库，之后更快。
