# 缺口清单：没有 skill 可归口的节点

这 7 个节点的归口是一条动作，不是 skill。**本轮不新建方法类 skill**——理由逐个写在下面，不是「以后再补」一句了事。

## 缺口节点（无 skill 可归口）

| 编号 | 节点 | 兜底动作（可执行 / 可判定） | 这轮不新建 skill 的理由 |
| --- | --- | --- | --- |
| N02 | 动手前查有没有活跃尝试 | `python tools/work_gate.py --list`，输出为空才允许新建尝试 | 动作就是跑一条命令看输出，方法含量为零。写成 skill 等于把「跑一条命令」包装成方法，还会让人以为读了 skill 就不用跑命令 |
| N03 | 这件事要不要开工单、切多细 | 规则：一次能被另一个会话独立验收的改动 = 一张工单；验收条件写不出证据命令，说明切得还太粗 | 判据是一句话，且当场可判。skill 的价值在「有步骤、有反模式、有自检」，这里一样都没有 |
| N06 | 把需求落成工单：`deliverable` 的主题与形状、`criteria` 拆分 | 规则：`deliverable` 写清主题、形状、明确不覆盖什么；`criteria` 每条附一条可跑的证据命令 | 本工作区真正填过的工单只有个位数（AW-011、AW-017、AW-022 等），样本不足以归纳出稳定写法。现在写出来会是猜测，等积累到 5 张以上验收过的单再归纳 |
| N12 | 会话标识取证与 `list_sessions` 反查 | 规则：自己的 `session_id` 只从宿主取（`get_current_session_id` 或创建回执）；两个 id 都要在 `list_sessions` 里查到才允许绑定或提交；查不到就当标识不可信 | 规则已经写死在 `AGENTS.md` 硬规则五与 `docs/protocol.md`，两处都是权威口径。再写一份 skill 会出现第三份，三份一旦不同步，最危险的是看起来最详细的那份 |
| N23 | 进度记录与收口 | `python -m task_center --db {{WORKSPACE}}/.data/task-center.sqlite3 events --input -`（`project_id`、`ticket_id` 必填） | 中心库是唯一进度来源。把它包装成 skill 会暗示「按 skill 记一遍也行」，恰好是 AGENTS.md 明令禁止的「各会话各自维护一份进度」 |
| N24 | 写记忆（user / feedback / project / reference） | 规则：走 `cindy_memory` 的 `memory_write`；一条 description 写不出来就不写 | 记忆系统的写入规则由宿主常驻提示直接给出，逐类别与反例都在里面。skill 只会复制一份，且复制品会比原件先过期 |
| N29 | 把一条消息送进一个正在跑的会话：投还是插、发完怎么确认打中了 | 先 `list_session_queue` 看目标在不在跑；要改目标当前这一轮的产出就 `steer_session`，否则 `send_to_session` 并带 `target_session_id`；发完读回执 `wake_kind`，`created` 就是发错了目标 | 它是一次「看队列再选哪一档」的当场判断，判断依据已经定版在 `docs/decisions.md` 的「消息通道三档」（三档能力边界、四条判据）。为了两列好看另开一个只有三句话的 skill，只会多出一份与那份口径各自过期的副本 |

## 半接的三个节点（有 skill，但 skill 不覆盖全部判断）

这三个不列入缺口，但它们的缺口部分同样没有新 skill，靠兜底动作补：

| 编号 | 已有归口 | 缺的那一半 | 兜底动作 |
| --- | --- | --- | --- |
| N05 | `skill:prd-authoring` | PRD 一改，哪些在途工单失效，`prd-authoring` 只讲影响面怎么看，不讲怎么查 | `list_tickets` 列在途工单，逐张对 `prd_sha256`，对不上的按 `STALE_PRD` 处理 |
| N17 | `skill:ticket-dispatch` | `evidence` 的具体写法（贴什么、贴多少）协议里只说「自检说明」 | 每条验收条件一次运行 + 真实输出；跑不了的写明跑不了的原因 |
| N18 | `skill:ticket-dispatch` | 逐项判定时的判据粒度 | 判不出来的条件按不通过处理，不许整体感觉没问题 |

## 与需求文档附录 A 的口径核对

首版数错的数字已经改正过：15 个已接、3 个半接（N05/N17/N18）、4 个节点上装了没接（N15/N22/N26/N27，共 6 个 skill）、6 个没有任何 skill（N02/N03/N06/N12/N23/N24）。

本目录就是按这个口径建的：6 个无 skill 的节点逐一列在上面三列里；那 6 个装了没接的 skill 已全部接进 N15/N22/N26/N27（见 `installed-skills.md`）。本轮没有出现新的口径偏差。

**N29 的归口为何写成 action 而不是 skill（2026-11-17 定，对节点表生效）。** N29「把一条消息送进一个正在跑的会话」确实是一次判断，但它比其他动作节点多一步「看状态再选集」：先 `list_session_queue`，在跑且要改本轮就 `steer_session`，否则 `send_to_session`，发完看回执 `wake_kind`。这三步都能当场答「做到了没有」，判据也已定版在 `docs/decisions.md` 的「消息通道三档」与「送达判据」，因此归口写 `action:`，不新建 skill——另开一个只包三句话的 skill 会多出一份各自过期的副本。加它之后缺口节点由 6 个变为 7 个，节点表已同步这个数。
