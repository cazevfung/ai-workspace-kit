# Cindy 插件入口（可装，发布前还差图标）

`draft/` 现在是一份可安装的 Cindy 插件源码：`ghost.json` 是 `schemaVersion: 3`，
`main.js` 暴露两个工具，`locales/` 提供中英文案。它仍然**不是主渠道**——插件运行在
沙箱里，改不了用户主目录、`git config core.hooksPath` 与 `~/.agents/skills`，完整工作区
只能由仓库里的 `install.py` 装。

## 它做什么

只把工作区自己的计划原样交给当前 Agent，不执行任何宿主写操作：

| 工具 | 返回什么 | 与谁同源 |
| --- | --- | --- |
| `bootstrap` | `list_projects → create_project → send_to_session → list_sessions` 的完整参数 | `<工作区>/onboard.py --json` 的 `bootstrap` 计划 |
| `dispatch_ticket` | `get_ticket → claim_ticket → send_to_session → list_sessions` 的完整参数 | `<工作区>/onboard.py --json` 的 `dispatch_ticket` 计划 |

两个工具都会回传同一步骤的失败停点（读不到项目清单、目录不存在、会话 `NOT_FOUND` /
`ARCHIVED` / `DELETED`、`DISPATCH_BUSY`、`SELF_DISPATCH`、`STALE_ATTEMPT` 等），
不会自己换一套顺序。`kit/tools/test_plugin_plan.py` 会比对 `main.js` 里的 PLANS
与 `onboard.build_plans()`，两边不一致就红。

## 为什么插件不是主渠道

- **沙箱**：写用户主目录、`git config core.hooksPath`、往 `~/.agents/skills` 铺技能，
  这些动作要么被挡住，要么需要逐项授权；
- **覆盖面**：只有 Cindy 用户能用；Claude Code / Codex 用户拿不到同一套东西；
- **更新成本**：每改一次工作区规则都要重新打包、发版、过审，而仓库只需 `git pull`；
- **职责错位**：插件擅长「工具与连接器」，不擅长「往磁盘上铺一个项目骨架」。

## 发布前还差什么

- **图标**：按规范需要用户提供或同意后生成的真实图片，不能放纯色占位图；本轮先不声明
  `icon` 字段（清单允许省略），客户端会显示默认符号。
- **打包与实测**：用当前版本的 `ghost_forge_pack` 打包，并在目标 Cindy 版本上实测安装、
  调用两个工具、预览与卸载路径。打包不等于安装或发布。
- **版本下限**：`minCindyVersion` 目前是 `0.1.68`；如果实测发现某能力需要更高版本，
  按实际依赖改，不要为了兼容旧版虚报。

在补齐之前，可以把 `draft/` 当本地开发源码安装试用，但不要声称已经上架。
