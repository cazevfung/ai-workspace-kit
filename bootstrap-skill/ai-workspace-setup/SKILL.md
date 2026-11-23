---
name: ai-workspace-setup
description: 在用户想安装、部署或分享「AI 工作区 / AI workspace / 工单体系」时使用：确认环境、获取套件仓库、运行一键安装器、验证装出来的工作区能跑。用户说「装一个 AI 工作区」「把这套工单流程装到我电脑上」「帮我部署这套东西」时触发。只负责安装与验证，不替用户写业务成果。
---

# 安装 AI 工作区

这套工作区的本体是「文件 + 一个本地 SQLite + 提交钩子 + 一批技能」，不是一段提示词。
所以安装动作必须落到磁盘上，不能只在对话里描述。

## 一、先确认三件事

1. **Python 3.10+**：`python --version`（或 `py --version`、`python3 --version`）。
2. **Git**：`git --version`。没有也能装，但会少掉「没工单不许提交」这道默认路径，要告诉用户。
3. **装到哪里**：默认 `~/ai-workspace`。用户没说就用默认值，并在开始前报一句；
   用户给了路径就用他的。**不要**装进正在用的业务项目目录。

## 二、拿到套件仓库

套件是一个 GitHub 仓库（含 `install.py`、`kit/`、`skills/`）。
按顺序找它：

1. 用户给了仓库地址或本地路径 → 直接用。
2. 当前目录里就有 `install.py` 与 `kit/` → 就在当前目录，不用再拿。
3. 都没有 → 问用户要仓库地址。**不要**凭空编一个 URL，也不要用别的仓库冒充。

```bash
git clone <仓库地址> ai-workspace-kit
cd ai-workspace-kit
```

## 三、先预演，再真装

```bash
python install.py --workspace <路径> --dry-run   # 只打印计划，不动文件
python install.py --workspace <路径>             # 真装
```

装之前把预演输出给用户看一眼，尤其是：
- 工作区目录；
- 技能会装到 `~/.agents/skills`，同名技能默认**跳过不覆盖**；
- 会不会写用户级 `~/.agents/center.json` 与 `~/.agents/intake/`：`--agents-home` 不是本机真实用户目录时默认**不写**（一个字节都不写），要写得加 `--allow-user-config`；
- 如果 `~/.agents/center.json` 已经指向**别的**库，默认安装会停在那里并以码 2 退出——那是保护，不是报错。

用户已有同名技能、或 `~/.agents/center.json` 指向别的工作区时，先停下说明，
让用户决定用 `--overwrite-skills`、换一个 `--agents-home`，还是由他自己确认重指
（重指才用 `--allow-user-config`）。**不要**替用户覆盖配置，也不要为了让命令跑完
自己加那个开关。

## 四、验证（拿输出，不要只说成功）

```bash
cd <工作区>
python tools/work_gate.py --list
python tools/run_tests.py
python tools/doc_gate.py --all
```

三件事都要看到：

1. `run_tests.py` 退出 0，并打印 `tests/` 与 `tools/` **两处**的用例数；
2. `doc_gate.py --all` 退出 0；
3. `work_gate.py --list` 能读到库（空列表也算通过）。

任一条非零，把原始输出贴给用户，并按下面对照处理，不要略过。

| 现象 | 处理 |
| --- | --- |
| `work_gate` 说读不了库 | 确认 `~/.agents/center.json` 存在且指向 `<工作区>/.data/task-center.sqlite3` |
| `run_tests` 报目录缺失 | 安装不完整，用 `--force` 重装到空目录 |
| `doc_gate` 报 skill 不存在 | 技能没装到 `~/.agents/skills`；检查是否用了 `--no-skills` |
| 首次提交失败 | git 缺 `user.name` / `user.email`；配置后在工作区里手工提交一次 |

## 五、告诉用户下一步

装完给三条可执行动作，不要写「请自行探索」：

1. 把工作区目录加为 Cindy 项目（或对应宿主的项目）；
2. 在工作区会话里先读 `AGENTS.md`；
3. 要开工单时按 `.agents/skills/ticket-dispatch/SKILL.md` 的顺序走。

## 不要做

- 不把工作区装进业务项目目录，也不改业务项目里的文件。
- 不在用户没确认时覆盖 `~/.agents/center.json` 或同名技能。
- 不把「安装脚本跑完」说成「工作区已经能用」——验证三条命令的输出才算。
