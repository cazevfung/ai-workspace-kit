# 入口 skill：把「装一套 AI 工作区」变成一句话

`ai-workspace-setup/` 是一个**薄入口 skill**：它不复制工作区内容，只把 agent 引导到仓库与安装器。
适合放到 SkillHub 或本机 `~/.agents/skills`，给「只想先试试」的人一个低门槛入口。

## 它做什么

1. 确认 Python / Git / 安装目录；
2. 找到套件仓库（用户给地址、或当前目录已有、否则问用户要）；
3. 先 `--dry-run` 预演，再真装；
4. 跑三条验证命令并贴输出；
5. 告诉用户下一步。

## 它不做什么

- 不把 25 个技能与工作区代码塞进 skill 包（那会让技能更新变成大包更新）；
- 不替用户覆盖 `~/.agents/center.json` 或同名技能；
- 不在没有仓库地址时编一个 URL。

## 怎么发布

- **本机试用**：把 `ai-workspace-setup/` 复制到 `~/.agents/skills/` 即可。
- **SkillHub**：按 `cindy-skill-creator` 的流程上传这个目录。
- **随仓库分发**：安装器不会自动装它（它是入口，不是工作区技能）；需要的话手工复制。

完整选型分析见 [`../docs/SHARING.md`](../docs/SHARING.md)。
