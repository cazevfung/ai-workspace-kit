---
name: project-routing
description: 在任意会话里判断这次任务属于哪个项目、该不该问人，并据此开单推进。用户说「这是哪个项目的事」「该不该问」「先弄清归属再动手」，或在没有项目名的对话里让你改某个项目的文件时使用。只定归属，不定一次改动怎么做。
---

# 项目定位与派发

## 顺序，不要跳步

1. **你已经在的地方就是答案。** 会话的 working_dir、宿主回执里的 `project_path`，有就直接用。不用跑脚本，不用问。
2. **话里有项目名或路径**，就按已注册项目比对：精确命中直接用；互为前缀（`X` 与 `X-2`）算歧义，并列出来。
3. **以上都没有**，跑一次探针（零参数，只出摘要）。
4. 摘要里 `here.registered` 有值 → 那就是项目，直接用。
5. `here.root` 为空，或 `near` 里没有对得上的 → **问**，列 2–3 个带路径的选项。不许挑一个最近的顶替。

## 命令

```powershell
python "$env:USERPROFILE\.agents\skills\project-routing\scripts\probe.py"            # 摘要
python "$env:USERPROFILE\.agents\skills\project-routing\scripts\probe.py" --full --explain
python "$env:USERPROFILE\.agents\skills\project-routing\scripts\probe.py" --roots "D:/" --coordinator <项目id>
```

摘要字段：`here`（cwd、项目根、依据、活动度、是否已注册）、`center`（中心库路径与来源）、`near`（最多 3 个附近项目）、`broken`（注册了但根目录不见了）。

## 四条硬规则

1. **任何已存在的目录都能直接开单，不必先登记项目。** `root` 是身份、`project_id` 是标签：`create_ticket` 带 `root`（该目录的绝对路径）时，未登记的目录会顺手记一行再开单（`name` 取目录名），`project_id` 由发起会话给。同一个 `root` 换个 `project_id` 会报 `ROOT_MISMATCH`：用它已登记的那个 `project_id`，或先 `unregister_project` 再重登（`projects.root` 不能改，写错登记的修法就是撤销重登）。标签重复（多个目录都叫 `workspace`）不阻断派发。口径见 `{{WORKSPACE}}/docs/decisions.md` 的「项目接入面」。
2. **归属真不确定时就问人，不要挑一个最近的顶替。** 探针不知道你在说什么，这一步只能靠判断；列 2–3 个带路径的候选让人选。
3. **与项目无关的请求不要硬塞进项目。** 一次问答、一句解释不需要归属，也不需要工单。
4. **执行与验收必须是不同会话。** 执行者对自己的工单调用 `review_result` 会被核心拒绝（`SELF_REVIEW`）。

## 推进

中心库位置写在 `~/.agents/center.json` 的 `db` 字段。探针会读它，`center.available=false` 时只做定位，不谈推进——**绝不新建第二个库**。

```powershell
python -m task_center --db <库> <操作> --request-id <唯一编号> --input <UTF-8 JSON 文件>
```

代码在 `{{WORKSPACE}}/.worktrees/ticket-core`，操作与错误码见那里的 `docs/protocol.md`。写操作必须带 `request_id`；重试要原样重复。

## 不要做

- 不要扫全盘。`--roots` 只在"想看别处"时才给。
- 不要因为探针返回了列表就主动汇报。话里没有歧义就闭嘴。
- 不要把摘要念给用户听。它只用来支撑一句提问。

## 脚本来源

`scripts/probe.py` 是 `{{WORKSPACE}}/.worktrees/ticket-core/task_center/probe.py` 的副本，仓库里有测试。改了仓库那份后重新同步：

```powershell
Copy-Item '{{WORKSPACE}}/.worktrees/ticket-core/task_center/probe.py' "$env:USERPROFILE\.agents\skills\project-routing\scripts\probe.py" -Force
```
