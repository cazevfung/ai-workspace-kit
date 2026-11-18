# 项目定位探针（read-only）

`probe.py` 回答两件事：**这个会话在哪个项目里**、**中心库在哪里**。它不做匹配、不缓存、不写数据库，也不判断某句话指哪个项目。

从 2026-11-17 起它是一个全局能力，不再属于 AI 工作区项目：安装位置 `~/.agents/skills/project-routing/`，任意目录、任意会话可用。仓库里这份是可测试的源文件。

## 零参数就是默认用法

```powershell
python task_center/probe.py
```

它只做三次廉价查询：当前目录链上找 git 根或标记目录、读 `~/.agents/center.json`、按注册表核对归属。**默认不扫描磁盘。** 输出 486 字节，约 150 token。

```json
{"ok":true,"result":{
  "here":{"cwd":"{{WORKSPACE}}\\.worktrees\\ticket-core","root":"{{WORKSPACE}}\\.worktrees\\ticket-core",
          "basis":"git root","name":"ticket-core","activity":"active",
          "registered":{"project_id":"ai-workspace","name":"AI工作区","root":"d:\\ai工作区"}},
  "center":{"available":true,"path":"{{WORKSPACE}}\\.data\\task-center.sqlite3","source":"config"},
  "near":[],"broken":[]}}
```

`here.root` 与 `here.registered` 可能不同，这是对的：worktree 的 git 根比它的项目注册更深，两个事实都保留。

## 需要看别处时才给参数

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--db` | 读配置 | 中心库；高于配置文件。只读打开 |
| `--config` | `~/.agents/center.json` | 一行 `{"db": "<绝对路径>"}` |
| `--here` | 当前目录 | 换成别的目录做定位 |
| `--roots` | 无 | 给才扫描这些父目录；不给则完全不扫 |
| `--depth` | 2 | 扫描深度 0–4 |
| `--active-days` | 14 | 活跃窗口 |
| `--limit` | 8 | 排名条数，省略数记在 `counts.omitted` |
| `--jobs` | 8 | 并行 git 调用数 |
| `--coordinator` | 无 | 该注册项目只列出、不排名，也不抑制别的条目 |
| `--full` | 关 | 打印完整报告；`--explain` 同时包含扫描详情 |

## 判断规则（固定，不随项目变化）

- 有 git 就用提交时间；没有 git 才做有限深度文件遍历（深度上限 2，文件上限 2000）。
- 不使用目录自身 mtime：改子目录里的文件不会更新父目录时间戳，用它会反向误判。
- git 调用超时 5 秒；超时记为 `unobserved`，不记为 `active`。
- 时间只用于排序，带内的项目永远排在活跃项目之后。
- 空壳判 dormant，已注册但根目录消失判 broken，两者都必须出现在输出里。
- 协调入口不排名，也不能抑制别的条目——一个从不被提供的条目没有资格挤掉别的。
- 嵌套候选（祖先已是候选）不参与排名；但注册项目永不被抑制。

## 中心库发现

`--db` → `~/.agents/center.json` 的 `db` → 都没有就报原因。**绝不新建库**：一个会话自己开一份库，会把记录悄悄拆成两份。

## 测试

```powershell
python -m unittest tests.test_probe -v     # 26 项
```

覆盖：无标记目录、空壳、嵌套排除、深度上限、截断与省略计数、库缺失不建文件、注册根消失、协调入口排除与抑制、git 超时、数据库只读、git 优先于新鲜文件、新提交判活跃、git 根优先于标记子目录、非 git 的标记祖先、不在项目内、注册祖先胜过更深的 git 根、配置发现与优先级、配置不可读、摘要体积。
