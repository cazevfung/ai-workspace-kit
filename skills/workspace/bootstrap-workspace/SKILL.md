---
name: bootstrap-workspace
description: 在 AI 工作区装好之后，把它加成宿主项目、开协调会话、发出第一条指令，或把一张已领取的工单派给执行会话时使用。用户说「装完了怎么开工」「把这个目录加成项目」「开个协调会话」「把这单派出去」时触发。只负责调用计划与事后反查，不代替执行会话 bind_session，也不改中心库。
---

# 装完在宿主里开工

安装器只把文件铺到磁盘上；「加成项目、开会话、发指令」是宿主操作，必须显示在会话里、
失败要能看见。所以本技能不猜参数，也不自己造一套顺序：先让工作区自己的
`onboard.py` 打印计划，再照计划逐条调 `cindy_helper`。

## 一、先拿计划

在工作区根目录：

```bash
python onboard.py --json            # 两条计划：bootstrap 与 dispatch_ticket
python onboard.py --dry-run         # 人读版预演，零副作用
```

`bootstrap` 是五步：`list_projects → create_project → send_to_session → list_sessions → write_receipt`；
`dispatch_ticket` 是四步：`get_ticket → claim_ticket → send_to_session → list_sessions`。
后者复用同一份 `send_to_session` 参数。前四步的工具名必须都在 `cindy_helper` 清单里，
出现清单外的名字说明版本对不上，停下来核对，不要硬调；`write_receipt` 是 `local` 步，
不进 `cindy_helper` 清单（见第四节）。

## 二、调用形状

宿主工具经 `cindy_mcp_call_tool` 调用，参数是两层信封：

```json
{"server": "cindy_helper", "tool": "call_tool",
 "args": {"name": "<工具名>", "args": {"<参数>": "<值>"}}}
```

下面每一步的「参数」都填进内层 `args`。

### 1. list_projects

```json
{"include_hidden": true, "offset": 0, "limit": 100}
```

清单里已有 `project_path` 等于工作区绝对路径的项目，就复用它，跳过 `create_project`。
读不到清单就停下：不要凭显示名猜路径。

### 2. create_project

```json
{"name": "<工作区目录名或用户给的名字>", "working_dir": "<工作区绝对路径>"}
```

回执的 `project_path` 必须等于工作区绝对路径。目录不存在或权限被拒时停下说明，
不要为了继续而换一个目录。同名项目已存在时复用，不要重复登记。

### 3. send_to_session（新建协调会话）

```json
{"project_path": "<上一步的 project_path>",
 "title": "AI 工作区 · 协调会话",
 "message": "你是这个 AI 工作区的协调会话。先读 AGENTS.md，再跑 python tools/work_gate.py --list 确认没有在途工单……"}
```

**不要传 `target_session_id`**：不传才是新建任务；传了就是投递给已有会话。可选的
`agent_kind`（`claude-code` / `codex` / `pi`）与 `model` 只在用户明确指定时传。
回执里的 `target_session_id` 与 `working_dir` 原样记下，不要改写或缩写。

### 4. list_sessions（事后反查）

```json
{"workdir": "<工作区绝对路径>", "device": "local",
 "include_deleted": false, "limit": 100, "order": "desc"}
```

看返回里有没有 `title` 与 `workingDir` 都对得上的那一条。对不上就是会话没建出来：
不要说已经开工，重发一次 `send_to_session`；仍失败就停下问人。

### 5. write_receipt（本地写回执）

这一步**不是宿主工具**：没有 `cindy_helper` 里的对应工具，也不许自己给它编一个名字。
它由会话在本地写一个文件，内容与位置见第四节。前三步的回执没有反查到
`target_session_id` 之前不要写——拿猜的 id 写出来的回执比没有更坏，因为它看起来像证据。

## 三、派发一张工单

协调会话先 `claim_ticket`（`coordinator_session_id` 用自己的，从
`get_current_session_id` 取），然后照 `dispatch_ticket` 计划：

1. `get_ticket` 确认 `state` 是 `ready` / `rework`；
2. `claim_ticket` 传 `expected_revision` 与协调会话标识，记下 `attempt_id`；
3. `send_to_session` 用同一份参数新建执行会话，消息里只放工单号与「先 bind_session」；
4. `list_sessions` 反查执行会话确实存在。

执行会话自己 `bind_session`。协调会话**不要**替它绑定，也不要拿同一个 session_id
既 claim 又 bind——那会撞 `SELF_DISPATCH`。

## 四、写回执（第 5 步）：写什么、写在哪、缺了怎么办

前四步跑完、`list_sessions` 也认下了那条会话，才写回执。回执是一个 UTF-8 JSON 文件：

- 位置：`<workspace>/.data/onboard-receipt.json`（工作区根下的 `.data`，不是 home）；
- 四个字段，一个都不能少：

| 字段 | 写什么 | 不合格的样子 |
| --- | --- | --- |
| `project_path` | `create_project` 回执里的项目路径，绝对路径 | 写成 `./ws`、`ws` 这类相对路径 |
| `target_session_id` | `send_to_session` 回执里的会话标识，一字不差 | 空字符串、空格、自己编一个 |
| `working_dir` | 那个会话的工作目录，绝对路径 | 只写目录名 |
| `created_at` | 写入时刻，ISO 8601 | 空着 |

写完要能自己核一遍。校验器在**套件仓库**的 `verify/` 下（`install.py` 只装 `kit/`，装出来的
工作区里没有这一层），所以在克隆下来的套件仓库根跑：

```bash
python verify/verify_receipt.py --workspace <工作区绝对路径>
```

手上只有装好的工作区、没有仓库时，就照上表四个字段自己逐条比：缺字段、路径不是绝对路径、
`target_session_id` 为空，三条有一条不成立就不算写过。

缺文件时它打印 `receipt missing` 并非零退出；路径不是绝对路径、`target_session_id` 为空
各报各的原因，也不零退。**没有回执、或者回执没过这条命令，就不许说「已开工」**。

回执只是这一侧的声称：它证明「这个会话写下了它认领的会话标识」，不证明宿主里那个会话
真的还在。真实反查是 `list_sessions`，脚本通过时也会自己打印
「形状通过，未验证会话存在」。要证明项目真的开着，得另做一次反查，别拿回执当那个证据。

## 失败停点

| 步骤 | 现象 | 怎么处理 |
| --- | --- | --- |
| `list_projects` | 读不到清单 | 停下，核对宿主与工作区；不要猜一个路径继续。 |
| `create_project` | 目录不存在 / 权限被拒 / 同名冲突 | 先建目录或复用已有 `project_path`；不要重复登记。 |
| `send_to_session` | `NOT_FOUND` / `ARCHIVED` / `DELETED` | 不要重复新建项目；先 `list_sessions` 查实际状态。 |
| `send_to_session` | 回执标题不是本单 | 回执发错了地方：停掉误建任务，再带 `target_session_id` 重发。 |
| `list_sessions` | 找不到匹配的 `title` + `workingDir` | 会话没建出来；重发一次，仍失败就停下问人。 |
| `write_receipt` | 没有反查到的 `target_session_id` | 不要拿猜的 id 凑回执；停下说明会话没建出来。 |
| `write_receipt` | 文件写不进去（`.data` 建不出来 / 只读） | 停下说明，不要说已开工；回执落不了盘就没有可核的东西。 |
| `claim_ticket` | `DISPATCH_BUSY` / `SELF_DISPATCH` / `STALE_ATTEMPT` | 先做完在途单 / 换执行会话 / 重新取当前 revision。 |

## 不要做

- 不用显示名猜 `project_path`，不改 `~/.agents/center.json`，不覆盖用户已有项目。
- 不替执行会话 `bind_session`，不在没有反查的情况下报告「已开工」。
- 不在没有回执、或没过上面那条回执检查的情况下说「已开工」。
- 不在计划外自己拼 `cindy_helper` 工具名；清单里没有的名字一律停下核对。
