# 可选的 Cindy 插件入口（未打包，仅设计稿）

本目录**不是**一个可直接安装的 `.cindy` 包。它是一个入口设计稿，说明如果要在 Cindy 插件市场里
放一个入口，应该长什么样、以及为什么它不该承担完整安装。

## 为什么插件不是主渠道

- **沙箱**：插件运行在 Cindy 沙箱里，写用户主目录、`git config core.hooksPath`、
  往 `~/.agents/skills` 铺 25 个目录，这些动作要么被挡住，要么需要逐项授权；
- **覆盖面**：只有 Cindy 用户能用；Claude Code / Codex 用户拿不到同一套东西；
- **更新成本**：每改一次工作区规则都要重新打包、发版、过审，而仓库只需 `git pull`；
- **职责错位**：插件擅长「工具与连接器」，不擅长「往磁盘上铺一个项目骨架」。

## 插件应该做什么

一个薄入口就够：

1. 展示一句话介绍与前置条件（Python 3.10+、git）；
2. 让用户选安装目录；
3. 引导用户执行 `git clone` + `python install.py`（或调用宿主提供的「运行命令」能力，如果有）；
4. 展示 `install.py` 的输出，并把三条验证命令交给当前会话执行；
5. 不自己改 `~/.agents/center.json`、不覆盖同名技能。

如果要真正做成插件，按 `cindy-plugin-creator` 的流程走；`draft/ghost.json` 是一个
`schemaVersion: 3` 的起点，字段含义以该 skill 的参考文档为准。打包前必须补：

- 一个真实图标（不允许占位图）；
- `main.js` 与工具实现；
- `locales/` 文案；
- 在目标 Cindy 版本上实测安装、预览与卸载路径。

在补齐之前，请不要把 `draft/` 当成可发布产物。

## 建议的发布顺序

1. 仓库先公开、README 与 `install.py` 能跑通（已有）；
2. 放一个 SkillHub 入口 skill（`bootstrap-skill/`）；
3. 有稳定用户后，再做插件入口，并复用同一个 `install.py`。
