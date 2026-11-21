# Cindy 插件入口（可装，已打成包）

`draft/` 是一份可安装的 Cindy 插件源码：`ghost.json` 是 `schemaVersion: 3`，`main.js` 暴露两个工具，
`locales/` 提供中英文案，`assets/icon.png` 是 1024×1024 的方形图标（由 Cindy 的图片能力生成一次，
不是占位色块）。打好的包在仓库根的 `dist/ai-workspace-kit-0.4.0.cindy`。

它仍然**不是主渠道**——插件运行在沙箱里，改不了用户主目录、`git config core.hooksPath` 与
`~/.agents/skills`，完整工作区只能由仓库里的 `install.py` 装。

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

## 装插件 → 启动 → 它报出四步计划

1. **装。** 在 Cindy 侧边栏「插件」里导入本地包，选中 `dist/ai-workspace-kit-0.4.0.cindy`，确认权限清单。
   **这一步必须由人在客户端点确认**：导入会写进用户插件目录并启用，Agent 不代点，也不该在你没看权限
   清单时替你点。装上是本地开发副本（`namespace: local`），不经过市场审核。
2. **启动。** 在任意会话里敲 `$ai-workspace <工作区绝对路径>`，或者直接说「把这条路径加成项目、开一个
   协调会话」。命中的是 `bootstrap`。
3. **它报出四步计划**，只给参数、不替你执行：
   - `list_projects`：先看清单里有没有这个目录；读不到就停，不要猜路径。
   - `create_project`：回执的 `project_path` 必须等于你给的那条路径。
   - `send_to_session`：不传 `target_session_id` 才是新建任务，回执要带回 `target_session_id` 与
     `working_dir`；返回 `NOT_FOUND` / `ARCHIVED` / `DELETED` 就停下修会话。
   - `list_sessions`：按工作目录反查，`title` 与 `workingDir` 都对得上才算开出来了。

`dispatch_ticket` 同一形状，换成 `get_ticket → claim_ticket → send_to_session → list_sessions`。

## 打包与自检

```bash
python tools/test_plugin_pack.py     # 拆包验清单、main.js、图标与源码是否一致
```

`dist/` 下只留 `.cindy` 入库（`.gitignore` 里 `dist/*` 之外仍忽略）。改源码后要重新打包：
用 Cindy 的插件打包能力对 `plugin/draft/` 打一次，把产物放回 `dist/`，再跑上面那条命令。

## 为什么插件不是主渠道

- **沙箱**：写用户主目录、`git config core.hooksPath`、往 `~/.agents/skills` 铺技能，
  这些动作要么被挡住，要么需要逐项授权；
- **覆盖面**：只有 Cindy 用户能用；Claude Code / Codex 用户拿不到同一套东西；
- **更新成本**：每改一次工作区规则都要重新打包、发版、过审，而仓库只需 `git pull`；
- **职责错位**：插件擅长「工具与连接器」，不擅长「往磁盘上铺一个项目骨架」。

## 发布状态

- **没上架。** 打包只产生产物；没有提审、没有发布到公开市场或任何组织。`releaseNotes` 也没写，
  因为那是发给「已上架版本的更新」，本轮不是。
- **装过没有。** 仓库里这一轮只做到「打包 + 拆包自检」；真要装进 Cindy，走上面第 1 步，由人点。
- **版本下限**：`minCindyVersion` 是 `0.1.68`；实测发现某能力需要更高版本时按实际依赖改，不为了兼容旧版虚报。
