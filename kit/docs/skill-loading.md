# 技能内容入库：两条加载路径实测

- 依据：工单 AW-036；路径与结论均为当场跑过的实测，不再依赖包外的需求文档。
- 执行会话：`<session-id>`（工作目录 `{{WORKSPACE}}/.worktrees/AW-036`）
- 记录时间：2026-11-17

这份文档回答的是「技能内容要放进仓库、又要让宿主读得到，哪条路在这台机器上真能用」，以及**不能用那条路究竟是断在哪一环**。结论都有当场跑出来的输出或另一个会话的原话，不是从文档推断的。

## 0. 结论摘要

| 路径 | 做法 | 结论 | 断在哪 |
| --- | --- | --- | --- |
| **P2** `.pi/settings.json` | 内容放 `<仓库>/skills/`，项目配置里写 `skills` | **不生效** | 两环都断：①项目配置是 project trust 保护资源，Cindy 每次以 `--no-approve` 启动 Pi，配置根本不被读；②PRD 拟的 `+skills/` 写法也不成立——`+path` 是筛选器不是新增扫描根，且项目设置路径以 `.pi` 为基准，应写 `../skills` |
| **P1** junction | 内容放 `<仓库>/skills/`，`~/.agents/skills/<名字>` 建 junction 指过去 | **生效**（本次实测，探测会话两次） | 唯一的代价是：合完删工作树后要手工重指一次，见 §7 |

一句话：**Cindy 每次启动 Pi 都带 `--no-approve`，项目级配置与项目级技能目录一律不参与启动**。能让技能在**所有**项目里都被看见的位置只有用户级目录（`~/.agents/skills`、`<agent-dir>/skills`），而把内容留在仓库、只在用户级留一个指向仓库的 junction，是唯一同时对得上「内容进版本控制」和「宿主读得到」的组合。

P2 不生效这件事**不是这单能修的**：它取决于 Cindy 的启动参数，而 PRD 明令不碰 run-tmp 配置与 UI 导入。所以后面谁看到 §3 里「`--approve` 下探针能进来」也不要据此去用 P2——真实会话里永远是 `--no-approve`。

## 1. 环境事实：这台机器上 Pi 是怎么被启动的

同一台机器上全部由 Cindy 拉起的 `pi.exe` 进程（14 个）命令行公共部分如下（PID 33032 的实测 argv，中间部分省略）：

```text
pi.exe --mode rpc --no-approve --no-extensions
       --session-dir X:/示例项目/AppData/Roaming/Cindy/pi-agent-home/sessions
       --provider cindy --model deepseek/deepseek-v4.1-flash
       --skill X:/示例项目/AppData/Roaming/Cindy/pi-agent-home/run-tmp/<hash>/cindy-managed-skills
       --append-system-prompt ...\append-system-prompt.md
       --extension ...\internal-extensions\cindy-bridge.ts
       --extension ...\internal-extensions\cindy-subagent.ts
       --extension <pi>\examples\extensions\plan-mode\index.ts
       --skill {{WORKSPACE}}\.agents\skills\pdsa-workflow
       --skill {{WORKSPACE}}\.agents\skills\plan-gate
       --skill {{WORKSPACE}}\.agents\skills\prd-authoring
       --skill {{WORKSPACE}}\.agents\skills\skill-routing
       --skill {{WORKSPACE}}\.agents\skills\ticket-dispatch
```

两件事直接从这行 argv 读出来，后面两条路径的结论都依赖它们：

1. **`--no-approve` 是固定的启动参数**，不是「用户这次没点信任」。Pi 文档写明命令行 `--approve/--no-approve` 优先于一切其它信任判断（`docs/security.md`「How Pi chooses a trust decision」），因此 `.pi/settings.json`、`.pi/skills`、项目 `.agents/skills` 这些受信任保护的资源，在 Cindy 下**每次都不加载**。
2. 本仓库 `.agents/skills/` 下那 5 个技能能用，不是因为 Pi 扫到了项目目录，而是 **Cindy 自己认出它们、再用 `--skill` 逐个显式传给 Pi**。命令行级资源不受 trust 约束，所以它们进来了。这条区别很关键：「项目技能目录」和「项目配置文件」在 Cindy 下是两种命运。

> 另注：本机没有 `~/.pi/agent/trust.json`；Cindy 每次给 Pi 指定的是 `run-tmp/<hash>/` 里的一份临时 agent-dir，用户级 settings 由 Cindy 生成。按 PRD 边界，这些 `run-tmp` 配置只读未改。

## 2. 探测方法

### 2.1 本地矩阵探针（可无限复跑，不花模型）

Pi 的 RPC 模式有个 `get_commands` 命令，返回「扩展命令 + 模板 + 技能」的完整列表，每条带 `source` 与 `sourceInfo.path / scope / source`。它不调模型，所以拿它当探针比开真实会话更便宜、更可重复：改一下配置就能立刻再跑一遍，把「不生效」拆到具体哪一环。

脚本（临时目录里的一次性工具，不在仓库里；要复跑照抄即可）：

```python
# 用 pi --mode rpc 的 get_commands 列技能，不调模型
import json, subprocess, threading, time
PI = r"X:/示例项目/AppData/Roaming/Cindy/pi/1.5.2/pi.exe"

def probe(workdir, extra):
    p = subprocess.Popen([PI, "--mode", "rpc", "--no-session", "--no-extensions", *extra],
                         cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", bufsize=1)
    lines = []
    threading.Thread(target=lambda: [lines.append(l) for l in p.stdout], daemon=True).start()
    time.sleep(3)
    p.stdin.write(json.dumps({"type": "get_commands"}) + "\n"); p.stdin.flush()
    time.sleep(6); p.kill()
    for ln in lines:
        try: obj = json.loads(ln)
        except Exception: continue
        if obj.get("command") == "get_commands":
            return [c for c in obj["data"]["commands"] if c.get("source") == "skill"]
    return []

# 判读：命中说明该目录被扫描到，sourceInfo.path 指向哪一份就说明吃的是哪一份
```

### 2.2 协调会话开出的真实探测会话

本地探针能定位断点，但它毕竟不是「一个新会话自己说看见了什么」。PRD 要的是后者，所以两条路各开一个真实会话，工作目录都是 `{{WORKSPACE}}/.worktrees/AW-036`，只问几句，不加任何提示。

## 3. P2：`.pi/settings.json` 项目配置

### 3.1 做法

探针技能放仓库内 `skills/zz-load-probe/SKILL.md`（内容见 §8.1），项目配置写在工作树的 `.pi/settings.json`：

```json
{
  "skills": ["../skills"]
}
```

（最初按 PRD 原文写成 `["+skills/"]`，§3.2 证明那个写法不成立，这里记的是最终能用的写法。）

### 3.2 本地矩阵：把「不生效」拆到环

同一份配置、同一个工作目录、同一份探针内容，只改启动参数或配置写法：

| # | `.pi/settings.json` | 启动参数 | 技能总数 | `zz-load-probe` | `source`/`scope` |
| --- | --- | --- | --- | --- | --- |
| A | `["../skills"]` | `--no-approve`（Cindy 实际） | 37 | **不在** | — |
| B | `["../skills"]` | `--approve` | 43 | 在 | `local` / `project` |
| C | `["+../skills/"]` | `--approve` | 42 | **不在** | — |
| D | `["+../skills"]` | `--approve` | 42 | **不在** | — |
| E | `["+../skills/zz-load-probe/SKILL.md"]` | `--approve` | 42 | **不在** | — |
| F | `["+{{WORKSPACE}}/.worktrees/AW-036/skills/"]` | `--approve` | 42 | **不在** | — |
| G | 删掉配置，内容改放 `.pi/skills/zz-load-probe/` | `--approve` | 43 | 在 | `auto` / `project` |

A 与 B 的差别只有信任开关：37 → 43，多出来的 6 条正好是 5 个项目 `.agents/skills` 技能 + 1 个探针。**项目配置没被读，是因为 trust 被拒，不是因为路径写错。** 这是 P2 的第一处断环。

C–F 是第二处断环：在有信任的前提下，`+路径` 三种写法（相对目录、相对文件、绝对目录）一条都没加进来；`+` 去掉、写成 `["../skills"]` 就进来了（B），放进默认项目目录 `.pi/skills/` 也能进来（G）。所以：

- `+path` 的语义是「在已有扫描结果里精确地保留这个路径」，不是「新增一个扫描根」——它没法把仓库 `skills/` 这种默认不被扫描的目录拉进来；
- 项目设置的路径**从 `.pi` 目录解析**（`docs/settings.md`「Resources」），想指到 `<仓库>/skills` 应写 `../skills`；PRD 原文的 `+skills/` 即使去掉 `+` 也会指到 `.pi/skills`。

另有一条对照事实：`--no-approve` 下用户级目录照常扫描（A 的 37 条全部 `scope=user`，路径都在 `X:/示例项目/.agents/skills/`），被 trust 挡住的只有「项目级」这一档。

### 3.3 探测会话（真实会话，原话）

会话 `<session-id>`（标题「AW-036 加载探测 P2」，工作目录 `{{WORKSPACE}}/.worktrees/AW-036`，由协调会话 `<session-id>` 开出）。

问的是：

> 1. 你的可用技能列表（系统提示里那份技能清单）里有没有 `zz-load-probe`？有就原样贴出那一条（名字 + description）；没有就明确说没有，并报一下清单总条数。
> 2. 你的工作目录是什么？
> 3. 清单里有没有 `pdsa-workflow`？

它的原话（把这三个回答摘出来，其余是它发的回执）：

> 1. **没有 `zz-load-probe`**。清单共 **40 条**。
> 2. 工作目录：`{{WORKSPACE}}/.worktrees/AW-036`
> 3. **有 `pdsa-workflow`**（`{{WORKSPACE}}\.worktrees\AW-036\.agents\skills\pdsa-workflow\SKILL.md`）。

三条合起来才有意义：cwd 对上了（排掉「开错目录」）、`pdsa-workflow` 在（说明 Cindy 的 `--skill` 通道是通的，不是整个技能机制坏了）、而探针不在。所以「探针看不到」只能归到项目配置没被读。

（它自报 40 条，与我本地按同类 flag 复现的 42 条略有差别，差在 Cindy 额外传的 `cindy-managed-skills` 与我脚本传入的那一份不完全等价；这点差别不影响结论——`zz-load-probe` 在任何一侧都不在。）

### 3.4 结论

**P2 不生效，断在两环，两环都要说清，缺一个就会把后来人引到错误的修法上：**

1. **项目配置根本没被读**：Cindy 每次以 `--no-approve` 启动 Pi，`.pi/settings.json` 属于 project trust 保护资源，直接被跳过。这一环不是本工作区能改的——要改就得动 Cindy 的启动参数，PRD 明令不碰。
2. **即使给了信任，PRD 拟的 `skills: ["+skills/"]` 也加载不到**：`+` 是筛选器不是新增根；且项目设置路径以 `.pi` 为基准，要写 `../skills`。

也就是说，P2 即使将来 Cindy 允许信任，也必须**同时**改路径写法才可能生效；当前它连第一步都过不去。

## 4. P1：`~/.agents/skills` 下的 junction

### 4.1 做法

内容留在仓库 `skills/<名字>/`，`~/.agents/skills/<名字>` 建成指向它的 junction（Windows 目录联接，`mklink /J`，不需要管理员）：

```bash
cmd //c "mklink /J zz-load-probe {{WORKSPACE}}\\.worktrees\\AW-036\\skills\\zz-load-probe"
```

### 4.2 机制前提：用户级目录不受 trust 约束

`~/.agents/skills` 是用户级扫描目录（`source=auto`、`scope=user`），**不经过 project trust**。这一点在动手前就已在数据里：`--no-approve` 下 37 条 user 技能照常加载，其中 15 条本身就是 junction（10 条指向 `D:\agent-skills\installed\`、5 条指向 `X:/示例项目/.codex/skills/`）。所以「junction 这个机制通不通」不需要新建就能看出来；新建这一步要验的是**能不能指进仓库**。

### 4.3 本地验证（junction 建好、`--no-approve` 下）

```text
=== skills in list: 38
  - skill:zz-load-probe  scope=user source=auto path=X:/示例项目/.agents/skills/zz-load-probe/SKILL.md
=== PROBE HIT: True
```

38 = 原有 37 + 探针，路径报告的是 junction 本身（`C:\...\.agents\skills\zz-load-probe\SKILL.md`），读到的内容是仓库里那份。

顺带一个细节：`--approve` 下同一条技能会报成 `scope=project source=local`，指向仓库路径——也就是说仓库副本与 junction 同时可见时会合并成一条（名字冲突保留先被发现的），不会重复出现。

### 4.4 探测会话（真实会话，原话）

会话 `<session-id>`（标题「AW-036 加载探测 P1」，工作目录 `{{WORKSPACE}}/.worktrees/AW-036`，协调会话 `<session-id>` 开出，开出时间 10:52:23，晚于两个 junction 的建立时间 10:51）。

问的是（原话）：

> 1. 那份清单里有没有 `zz-load-probe`？有就原样贴出那一条（名字 + description）。
> 2. 清单里有没有 `merge-latest-branches`？有就把它那一条的 description 原样贴出来。
> 3. 你的工作目录是什么？

它的原话：

> 1. **有 `zz-load-probe`**，原文：
>    - name: zz-load-probe
>    - description: 本技能仅用于加载探测（AW-036 一次性探针），不含任何可执行内容。只在被问到「你的技能列表里有没有 zz-load-probe」时使用。
>
> 2. **有 `merge-latest-branches`**，description 原文：
>    > 验收通过之后把工单分支合进 master。用户问「验收完了要不要合」「合进 master」「冲突了怎么办」时使用；合并前工作区要干净、用例要全绿，有冲突先停下问人，不许自动解完直接合。
>
> 3. **工作目录**：`{{WORKSPACE}}/.worktrees/AW-036`

两条都命中，而且 `zz-load-probe` 的 description 与仓库文件逐字相同（「本技能仅用于加载探测（AW-036 一次性探针），不含任何可执行内容…」），说明读的就是仓库那一份。`merge-latest-branches` 也对得上：这句 description 是 AW-035 改过的版本，真目录原样拷过来后 description 一字未变。两个 junction 在**真实会话**里都通。

与 P2 的对比是这条结论的关键：**同一时刻、同一个工作目录、同一次启动方式（`--no-approve`）**，P2 的探针不在，P1 的探针在。

### 4.5 结论

**P1 生效。** 条件与限制：

- 只在**用户级**位置生效（`~/.agents/skills`）。把 junction 放进项目 `.pi/skills` 就没用了——那是 trust 保护资源。
- junction 的目标必须是**最终会被保留的路径**。本单期间目标是工作树路径，合完删工作树就悬空，必须重指一次（§7）。
- 这台机器上 15 个既有 junction 里，10 个指向 `D:\agent-skills\installed\`——那是上游共享库，上游重装会覆盖。本次迁的 `merge-latest-branches` 改成指向**我们自己的仓库**，这类 skill 从此可 diff、可回退。

## 5. 样本迁移：`merge-latest-branches`

### 5.1 迁移前（2026-11-17 动手前基线）

`~/.agents/skills` 共 37 条目 = **真目录 22** + **junction 15**。`merge-latest-branches` 是真目录，两个文件：

| 文件 | sha256 |
| --- | --- |
| `SKILL.md` | `5efded8797750b20f404743e2597fb96a4e0b8d41e74657215cff96862608c9a` |
| `agents/openai.yaml` | `91d74d9facbe09297b8f3598f3f2b5b8affb707d2412ba8768ecdddc6bc687f1` |

### 5.2 做了四步

1. 原样拷进仓库：`~/.agents/skills/merge-latest-branches/` → `<工作树>/skills/merge-latest-branches/`。两个文件 sha256 与上表逐个相同。
2. 原目录**移**到 `.data/aw-036-backup/merge-latest-branches-orig-20261117/`（不删，留作回滚与对照）。
3. 建 junction：`~/.agents/skills/merge-latest-branches` → `<工作树>/skills/merge-latest-branches`。
4. 透过 junction 读：`SKILL.md` sha256 仍是 `5efded87…`，`agents/openai.yaml` 仍是 `91d74d9f…`。切过去之后内容没变一个字节。

### 5.4 差点漏掉的一个坑：换行符

这台机器 `core.autocrlf=true`。拷进来的两份文件行尾不一样（`SKILL.md` 是 LF，`agents/openai.yaml` 是 CRLF），一入库就会被 git 按它自己的规则改写：

- 没加任何属性时，模拟一次新检出（`git --work-tree=<临时目录> checkout-index -f`）得到的 `SKILL.md` sha256 是 `8b1466c4968f0aaa69653ac5d28655ccc0cfc1d454234bf2caea3ba9634ee389`，**不是**原件那份 `5efded87…`；而 `agents/openai.yaml` 被反向规整成 LF，也不再是原件那份 `91d74d9f…`。
- 后果不只是“sha 变了”：合完把 junction 重指到主检出之后，`~/.agents/skills` 读到的就是这份换过行尾的内容，而 `check_index.py` 的正文比对是**逐字节**的。

所以加了一条 `.gitattributes`，只对身体这个目录生效：

```gitattributes
skills/** -text
```

加上之后重做同一个模拟检出，两份文件与 live 原件逐字节相同（`5efded87…` / `91d74d9f…`）。`agents/openai.yaml` 是重加一次才对的——它第一次入库时属性还不存在，索引里存的是被规整过的 LF（`git ls-files --eol` 报 `i/lf w/crlf`），加了 `-text` 后重加才变成 `i/crlf w/crlf`、取出即原件。

这条不是为了好看：**它是“junction 重指之后还是同一份内容”这个承诺的前提**，也是 AC05 那三次检查在合并后仍然能绿的前提。

### 5.5 迁后验证

- `check_index.py`（协调会话指定的主检出路径 `{{WORKSPACE}}/.agents/skills/skill-routing/scripts/check_index.py`）：`19 个 skill 全部符合期望（mode=all）`，rc=0。**这条正好证明迁移没弄坏那 19 个 description**：脚本会比对 live `description` 与快照、并要求正文逐字节一致，junction 之后读到的仍是同一份字节。
- 本地探针：`merge-latest-branches` 仍在列表里，`scope=user`，路径报的是 `X:/示例项目/.agents/skills/merge-latest-branches/SKILL.md`（junction）。

## 6. 回滚（真跑过一次，再恢复）

回滚动作只有两句，顺序反了会把内容丢掉：

```bash
# 1) 先删链接（rmdir 对 junction 只删链接，不动目标目录）
cmd //c "rmdir X:/示例项目//.agents//skills//merge-latest-branches"
# 2) 再把原目录放回原位
mv {{WORKSPACE}}/.data/aw-036-backup/merge-latest-branches-orig-20261117 \
   X:/示例项目/.agents/skills/merge-latest-branches
```

本次实测的六步与输出：

| 步 | 动作 | 结果 |
| --- | --- | --- |
| 1 | 记录回滚前状态 | `dir /AL` 里有 `merge-latest-branches [D:\...\.worktrees\AW-036\skills\merge-latest-branches]`，sha256 `5efded87…` |
| 2 | `rmdir` 删 junction | 「junction 已删」；`dir /AL` 里该名字 0 条 |
| 3 | 备份挪回原位 | 变成真目录（`ls -ld` 显示 `drwxr-xr-x`，不在 junction 列表），两个文件都在，sha256 `5efded87…` |
| 4 | 回滚态功能验证 | 本地探针里 `merge-latest-branches` 仍在（`scope=user`）；`check_index.py` 仍 `19 个 skill 全部符合期望`，rc=0 |
| 5 | 恢复：真目录挪回备份位 + 重建 junction | `dir /AL` 重新出现该 junction，sha256 仍是 `5efded87…` |
| 6 | 恢复后功能验证 | 本地探针 38 条、`merge-latest-branches` 与 `zz-load-probe` 都在；`check_index.py` rc=0 |

要点：**`rmdir` 对 junction 是安全的**（先删链接再动内容，反过来会连同目标一起出事）；回滚前后 `check_index.py` 都是 rc=0，说明这条路来回一趟不留残渣。

`~/.agents/skills/zz-load-probe` 这个 junction 不参与回滚——它是探针，用完就删（§7）。

## 7. 验收之后还有一个手工步骤（必读）

junction 现在指向**工作树**路径：

```text
X:/示例项目/.agents/skills/merge-latest-branches
  → {{WORKSPACE}}\.worktrees\AW-036\skills\merge-latest-branches
```

合完 master 按规矩要 `git worktree remove .worktrees/AW-036`，那一刻 junction 就指向一个不存在的目录，技能随之消失（且不报错，只是列表里没了）。**合并后必须重指一次**：

```bash
cmd //c "rmdir X:/示例项目//.agents//skills//merge-latest-branches"
cmd //c "mklink /J X:/示例项目//.agents//skills//merge-latest-branches {{WORKSPACE}}\\skills\\merge-latest-branches"
```

重指后内容不变（合入的就是同一份字节），但必须先确认 `{{WORKSPACE}}\skills\merge-latest-branches` 已经在主检出里（即合并已完成）。同样，回滚时 `.data/aw-036-backup/` 那份原目录还在，随时可取回。

## 8. 附录

### 8.1 探针技能

`skills/zz-load-probe/SKILL.md`（sha256 `ce042f2fcfbaa48a380507f477bafec69fc878a24019f4b394ea453c5cc8636d`，验证完成后已删除）：

```markdown
---
name: zz-load-probe
description: 本技能仅用于加载探测（AW-036 一次性探针），不含任何可执行内容。只在被问到「你的技能列表里有没有 zz-load-probe」时使用。
---

# zz-load-probe

这是一次加载探测用的空技能。

标识句：**本技能仅用于加载探测**。

它不提供任何方法、不要求任何动作、不引用任何文件。作用只有一个：如果它出现在某个新会话的可用技能列表里，就说明它所在的那个目录被宿主扫描到了。探测结束后应删除。
```

### 8.2 探针清理（已执行）

四处，缺一处就等于没删干净：

```bash
rm -rf {{WORKSPACE}}/.worktrees/AW-036/skills/zz-load-probe   # 仓库内
rm -rf {{WORKSPACE}}/.worktrees/AW-036/.pi                     # 项目配置（P2 用的）
cmd //c "rmdir X:/示例项目//.agents//skills//zz-load-probe"   # live junction
rm -rf X:/示例项目/AppData/Local/Temp/aw036-probe     # 本地探针脚本
```
