# 发布类工单的提交落点（AWK-116）

## 依据

- `kit/PRD/PRD-013-工单机制的执行面.md` **v0.3**（R10「合并唯一入口」与 AC15：合并只能由 `tools/auto_merge.py` 发起，主检出不切分支；本单不新增需求条目，只把 R10 落地之后仍然靠人记的那一段写成可判的话）。
- `kit/PRD/PRD-001-工作区使用与工单体系.md` **v0.1**（§ 调用顺序：`merge_ticket` 写在合并之后，合并记录是这单唯一的一条）。
- 既有事实：2026-11-23 一晚上连着三张发布单（0.5.0 / 0.5.1 / 0.5.2）都把提交直接落在 master 再推；`auto_merge --ticket` 停在第 2 步「源分支不领先」，`--record-only` 停在 `find_merge_commit`（找不到 `parent2 == 分支 tip` 的合并提交），最后三次都是人用 `merge_ticket` 的 `manual` 档补记。AWK-115 的现场可直接查：分支 `d849bc9`、被验收 `83e22f0`。
- 测试基线：`python kit/tools/run_tests.py` 当前两处用例数（本单开工时以实跑输出为准，全量实跑基线（开工时）：`tests/ 354` + `tools/ 658`，合计 1012 项。计划增量是 4 条，全在 `test_auto_merge.py`，`tests/ 354` 无改动）。

## 所用 skill

- `plan-gate`：把「先读依据、再点名 skill、计划落盘过闸」变成开工前的硬顺序，本计划的六节与 `doc_gate` 校验都出自它。
- `ticket-dispatch`：确定本单的领取/绑定/提交/回执顺序与 `submit_result` 的必填字段，`head_sha` 必须等于被验收的树。
- `writing-plans`：把任务拆成「一个动作一个可检查结果」，每个任务写清改哪个文件、验证跑什么、看到什么算过。
- `test-driven-development`：先写会红的用例再改工具，改动前先确认红的是「拒绝理由不够可判」而不是别的；没有失败用例就不动 `auto_merge.py`。
- `executing-plans`：按本计划的顺序逐任务做，一个任务绿了再进下一个，不并行改同一个文件。
- `verification-before-completion`：说「做完了」之前把 `python kit/tools/run_tests.py` 与 `doc_gate` 真跑一遍并贴输出，不引用记忆里的旧绿灯。

## 交付物

- **主题**：发布类工单的提交落在哪里——先在 `ticket/<工单号>` 分支提交，再由 `auto_merge` 合入 master、由合并那一步推、最后打 tag；并且让「没走合并就上了 master」当场难看。
- **形状**：①`kit/docs/protocol.md` 新增一节「发布类工单的提交落在哪里」，写清顺序与直接落 master 会让哪两条记录路径都失效；②`kit/tools/auto_merge.py` 在「源分支不领先」时，若 master 上已有像这张单的提交，直接说「你可能把提交直接落 master 了」并指向那一节；③`--record-only` 在「分支 tip 与被验收 sha 不一致」时点名两个 sha，不再笼统说快进不支持；④三条用例钉住上面三件事。
- **给谁看**：跑发布单的执行会话与合并的协调会话；读 `protocol.md` 就知道该在哪提交，被工具挡住时那条输出直接说明下一步。
- **明确不覆盖**：不改 `merge_ticket` 的授权与幂等语义；不自动给任何单补记录；不回写已发的 tag / Release；不改合并的成功路径与回退逻辑；不替历史那三张单补记。
- **服务本轮目标第 5 条**（`kit/tasks/round.md`「引擎同步：…让套件自带的 `auto_merge` 能读同一个共享库并完成一次真实合并」）：本单把「真实合并合不进去」的一整类现场变成当场可判的拒绝理由。

## 任务

### 任务 1：`protocol.md` 写一节「发布类工单的提交落在哪里」

- 文件：`kit/docs/protocol.md`（新增一节，放在「验收通过不等于已合并」之后、「`--record-only`」之前的位置关系写清，正文同时给 `--record-only` 那句「也做同一道比对」补一句指向本节）。
- 正文必含四件事：①顺序：先在 `ticket/<工单号>` 提交 → `auto_merge --ticket` 合入 master → **由合并那一步推**（不在工单分支上 push，也不在主检出上手推 master）→ 合并记录写成功之后才打 tag；②直接落 master 会让两条记录路径同时失效：`auto_merge --ticket` 停在第 2 步（源分支不领先）、`--record-only` 也拒（它要求分支 tip == 被验收的 sha，而分支停在上一版提交），并点名 AWK-115 那一组具体的两个 sha 作例子；③打 tag 之前必须已经有一条 `merges` 行——tag 是发出去就收不回来的东西，记录没写就打 tag 等于把「没合过」印在版本号上；④和「先合并、后清理」的关系：分支要留到合并记录写成功之后才删，删早了 `auto_merge` 连源分支都找不到。
- 验证：`grep -n "发布类工单的提交落在哪里" kit/docs/protocol.md`，期望输出里有这一节的标题行且只有一处定义；`grep -n "直接落" kit/docs/protocol.md`，期望命中本节那句「直接落 master 会让两条记录路径同时失效」。

### 任务 2：`auto_merge` 在「不领先」时当面指出「你可能把提交直接落 master 了」

- 文件：`kit/tools/auto_merge.py`（`_step_checks` 的 `require_ahead=True` 分支，`ahead == 0` 的 `Fail`）。
- 做法：新增一个只读辅助函数（例如 `landed_like_this_ticket(repo, target, branch, ticket_id, env)`），在 `git log --format=%H%x1f%s <branch>..<target>` 里找消息含工单号（大小写不敏感）的提交，返回命中列表；`ahead == 0` 且命中非空时，`reason` 里除了「没有领先」这句既有事实，再加一句「你可能把提交直接落 master 了」，并列出命中的 `<短 sha> <subject>`；`fix` 里指向 `docs/protocol.md` 的「发布类工单的提交落在哪里」，并说明 `--record-only` 也过不去（它要求分支 tip == 被验收的 sha）。命中为空时输出与现在一致，一个字不改。
- 验证：新用例 `test_ahead_zero_with_master_looking_like_this_ticket_points_at_the_protocol`：分支停在旧提交、master 上有一个 subject 含 `AW-900` 的提交 → `python -m unittest kit.tools.test_auto_merge` 里该用例通过；断言输出同时含 `没有领先`、`你可能把提交直接落 master 了`、`docs/protocol.md`、那个提交的短 sha，且 master、tag、记录三样都没动。
- 验证（回归）：`test_not_ahead_is_refused` 仍通过——「不领先但没有像这张单的提交」时只报既有那句话。

### 任务 3：`--record-only` 的拒绝理由点名「分支 tip 与被验收 sha 不一致」并报两个 sha

- 文件：`kit/tools/auto_merge.py`（`_record_only_steps` 里那次比对）。
- 做法：`--record-only` 不再复用 `guard_reviewed_sha` 的通用说法，改为一条专用判据：`head_sha` 与分支 tip 不等时 `Fail(STEP_SHA, ...)`，`reason` 里同时出现「分支 tip <tip 全 40 位>」与「被验收的 sha <head 全 40 位>」两个完整 sha，并写明这两个值不相等就是拒绝理由；`fix` 里给出两条真实出路（把分支退回到被验收的提交；或让验收会话对现在的 tip 重新验收一次），并指出「若提交本来就没落在分支上（落到了 master），`--record-only` 帮不上忙，按 `docs/protocol.md` 的「发布类工单的提交落在哪里」查该单怎么落点」。`head_sha` 为 NULL 的老提交照旧走 `unverified` 告警；相等照旧成功（成功路径一个字不改）。
- 验证：跑 `python -m unittest kit.tools.test_auto_merge.RecordOnlyTest.test_record_only_names_both_shas_when_branch_tip_is_not_the_reviewed_one`，期望退出码 0；该用例造「分支停在上一版提交 + master 已前进且被验收的是 master 上那个提交」，断言输出里两个完整 sha 都在、「分支 tip」与「被验收的 sha」两个词都在、`records` 仍为空、分支与工作树都还在。

### 任务 4：真 `--no-ff` 合并的现场，`--record-only` 仍成功

- 文件：`kit/tools/test_auto_merge.py`（`RecordOnlyTest`）。
- 做法：`test_record_only_still_records_after_a_real_no_ff_merge`——`branch_work` + `git merge --no-ff`，被验收的 sha 设为分支 tip，跑 `record_only=True`，断言退出 0、`records[0]["commit"]` 就是那次合并提交、工作树与分支都被清掉。
- 验证：跑 `python -m unittest kit.tools.test_auto_merge.RecordOnlyTest`，期望退出码 0 且 `OK` 行里的用例数比开工时多；若为任务 3 的判据把正常路径也拦了，这条会红。

### 任务 5：全量闸门与证据

- 验证：`python kit/tools/run_tests.py` 末行是 `结果：全部通过`，两处用例数都比开工时多出各自新增的条数（本单新增 4 条，全在 `tools/`：658 → 662，`tests/` 仍是 354）；`python kit/tools/doc_gate.py --all` 退出码 0；`python kit/tools/doc_gate.py kit/tasks/plan-018-发布类工单的提交落点.md` 输出 `闸门通过：1 个文件`。
- 验证（判断类）：本单的 diff 里没有 `merges` 写入、没有 tag 操作、没有 Release 相关命令；反例是顺手给 0.5.0 / 0.5.1 / 0.5.2 补一遍记录。

## 不做什么

- 不改 `merge_ticket`（`kit/task_center/`）的授权、幂等与 `SHA_MISMATCH` 语义，不改 schema。
- 不自动给任何工单补合并记录，不动 `backfill_merges.py` 的分档，不回写已发的 tag / Release。
- 不改合并的成功路径、回退点逻辑、journal 与锁。
- 不替 0.5.0 / 0.5.1 / 0.5.2 或 AWK-115 补记录、不改历史提交。
- 不动 `kit/tasks/round.md` 的状态位，不写收尾包，不推送、不打 tag、不发版。
- 不顺手改 `protocol.md` 里与本节无关的叙述（包括那两行重复的 `list_unmerged` 句子——要修是另一张单）。

## 未决

无。
