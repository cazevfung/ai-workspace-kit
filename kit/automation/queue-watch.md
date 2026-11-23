【自动值守 · 队列巡检 · 单轮】直接执行，不提问、不等确认，遇事自己拍板。

工作区：{{WORKSPACE}}（下文写的 `<工作区>` 都是它）

**第 0 步 · 先自己算出这轮该扫哪些目录**（不写死路径，目录会变）
1) 调 cindy_helper 的 list_workdirs，得到每个 workingDir 的 lastSessionAt；
2) 选**近 3 小时内有过会话活动**的目录，全部纳入本轮；
3) 再加上工单库里登记的项目根：`select root from projects`（`<工作区>/.data/task-center.sqlite3`），因为它们即使刚才没人动，也可能有在途单；
4) 都不是的目录本轮不管。
把本轮实际扫的目录写进结论行，让人看得出覆盖面。

**扫会话**：对每个选中的目录调 list_sessions（args={"workdir":"<目录>","limit":30}），**不许用工单库查询代替会话扫描**。主任务不一定绑工单，只有会话列表看得见它卡住了。

**粗筛前移（单轮成本的大头在这里）**：list_sessions 回来后先只看 `phase` 与 `queuedCount`，**只有下面三种会话才调 get_task_digest 展开**：① queuedCount>0；② phase 属 waiting-answer / needs-interaction 这类等人档；③ 最后活动时间超过 90 分钟。其余会话扫一眼就过，不逐个 digest、也不逐个 get_session_runtime。

**四类要看的**
- A 队列堆积：status=active 且 queuedCount>0。
- B 等人：phase=waiting-answer / needs-interaction，idle≥10。
- B2 停在问句上：phase=finished、pending_question=null，但 last_assistant_line 末尾是「要不要 X」「你定」「选甲还是乙」这类——它把决定抛出来就收尾了，没人回就永远停着。这类和 B 一样处理。
- C 交了成果没往下走：查工单库，见下。

idle 以 get_task_digest 的 idle_minutes 为准（list_sessions 的 lastActivityAt 对在跑的会话不及时）。超过 90 分钟没动静的会话才值得看，刚起的下轮再看。

四类都没有 → schedule_silence_current_run，结束，不调其它工具。

**第 1b 步 · 交了成果没往下走**（在 `<工作区>` 下跑）
```
python - <<'PY'
import sqlite3, datetime
c = sqlite3.connect('{{WORKSPACE}}/.data/task-center.sqlite3'); c.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
for r in c.execute("""
select t.id ticket, a.session_id exec_id, a.coordinator_session_id coord,
       (select s.id from submissions s where s.attempt_id=a.id) sub_id,
       (select s.created_at from submissions s where s.attempt_id=a.id) sub_at,
       (select count(*) from notices n where n.ticket_id=t.id) notices
from tickets t join attempts a on a.id=t.current_attempt where t.state='awaiting_review'
""").fetchall():
    d = dict(r)
    if not d['sub_id']: continue
    age = (now - datetime.datetime.fromisoformat(d['sub_at'])).total_seconds() / 60
    print(d['ticket'], '|exec', (d['exec_id'] or '')[:8], '|coord', (d['coord'] or '')[:8], '|sub', d['sub_id'], '|notices', d['notices'], '|age_min', round(age))
PY
```
脚本跑不动就在结论行写「第 1b 步没跑成：<原因>」，不许静默跳过。候选 = age_min ≥ 15 的行。

每张候选查一次回执到没到：search_chat_history({query:"<sub_id>", session_ids:["<coord>"], limit:3})
- 没命中 → 回执没发出去。发给执行会话（target_session_id=exec_id）：「你的 <单号> 已 submit_result（submission <id>），协调会话没收到回执。补发一条给 <coord>，正文只写 单号 / awaiting_review / submission <id> / commit <sha>。」
- 命中 → 回执到了没推进。发给协调会话（target_session_id=coord）：「<单号> 的回执已收到（submission <id>），工单还停在 awaiting_review，已过 <N> 分钟。推进验收，收尾时 record_notice 落库。」
不许替任何一方验收、不改工单状态、不碰文件。每轮最多 1 张，同一张提醒过一次就不再发。

**第 1d 步 · 代码已经在分支上、工单还停在 planned**（这一条必须跑，不靠会话自报）
为什么加：活干完了、单也建了，工单却从头到尾没被领，分支上的提交没人看得见——只看工单库发现不了这一类。
在 `<工作区>` 下跑：
```
python - <<'PY'
import sqlite3, subprocess
c = sqlite3.connect('{{WORKSPACE}}/.data/task-center.sqlite3'); c.row_factory = sqlite3.Row
roots = {r['id']: r['root'] for r in c.execute('select id,root from projects')}
for t in c.execute("select id,project_id,state,created_at from tickets where state in ('planned','ready') and current_attempt is null"):
    root = roots.get(t['project_id'])
    if not root: continue
    b = subprocess.run(['git','-C',root,'branch','--list','ticket/'+t['id']],capture_output=True,text=True).stdout.strip()
    if not b: continue
    n = subprocess.run(['git','-C',root,'rev-list','--count','master..ticket/'+t['id']],capture_output=True,text=True).stdout.strip()
    print(t['id'], t['project_id'], t['state'], 'ahead=%s' % n, t['created_at'][11:19])
PY
```
有输出 = 成果在分支上、单子没进流程 → 发一句给建单人或该项目当下的协调会话：「<单号> 在库里还是 <state>，没有 attempt 和 submission，分支 ticket/<单号> 已经领先 <N> 个提交，补 claim_ticket → bind_session → submit_result。」不代它提交。

**第 1e 步 · 已验收但没合并**（同样必须跑）
在 `<工作区>` 下跑 `python tools/auto_merge.py --list`。看「真待合并」那段：
- 有单且已验收超过 10 分钟 → 发一句给该单的协调会话：「<单号> 已 done 但还没合并记录，auto_merge --list 里挂在真待合并。推进合并；合不动就在待决策节写清卡在哪个前置条件。」
- 如果它回「主检出是脏的 / 工作树被占」这类前置条件，就在结论行记下来（光看工单库永远看不出这一层），不自己清理、不动任何文件。
为什么加：验收完到一直卡在「主检出脏、合不了」，而巡检每轮只报工单状态，没人看得见这一层。

**第 1c 步 · 同一张单两个验收会话**（扫会话时顺手判，零额外调用）
判据：同一张单出现 ≥2 个标题含「<单号> 验收」且 status=active 的会话。
处置：**只上报，不动手**——schedule_notify_current_run 写一行：「<单号> 有两个验收会话（<id1> / <id2>），有双写 review 的风险，留给派单器收口。」不建会话、不 stop、不 archive，也不代任何一方验收。

**第 1f 步 · 判据是空的单**：`select id,state,criteria from tickets where state in ('planned','ready')`，判据是 [] 的永远领不走，而 next_action 会报「没活」替它报平安。发一句给建单人（不知道是谁就发给该项目当下的协调会话）：「<单号> 判据是空的，永远领不走，补判据或取消。」判据内容不许自己写。

**判健康还是卡住**（健康什么都不做）
健康：队列是同一件事的先后链条；有 queued / wake_kind 这类回执短消息；phase=running 且 idle<10；队列 ≤3 条且无矛盾指令。
可介入：同一指令重复 3 条以上或结论矛盾；队列≥5 且没在跑；B/B2 类 idle≥10；两会话互回执成环；还在处理已被覆盖的旧指令；协调会话 phase=completed 而池子里还有能领的单。

**怎么介入（一步就够）**
1) 目标在跑 → steer_session。
2) 目标没在跑 / B 类 / B2 类 → send_to_session(target_session_id=目标)；先确认没有同步提问卡（见第 6 步）。
   话术就一句，不多写：
   「按你自己的判断继续往下做就行，取舍和默认值写进待决策节，别开卡等人。」
   **不许写**「主人不在」「今晚没人回」「别睡觉」这类；**不许**替它选方向、指定 API/机型/账号取值、排任务顺序、解释你为什么插话。
3) 队列里的重复消息只有本会话发的才能 cancel/move，其它来源改用 steer。
4) 只有明显跑飞（同一件事反复做、自派自验收成环、连续两轮无产出）才 stop_session_turn，停完补一条说明。
5) 一轮最多动 2 个会话，每会话 1 条；同一会话之前推过、情况没变就不发第二条。禁止代做业务、代提交、改验收条件、改文件、用提问卡片。

**第 6 步 · 同步提问卡**
认：phase=needs-interaction、attention=true、current_action_summary=「等待用户回答」、队列消息 consuming=false 一直不被读；再用 get_chat_history(roles=["ask_user"], limit=5) 确认最新一条 requestId 不以 async-question: 开头且 status=pending。
这种卡只有人能点，宿主无接口可答：send/steer 只入队、stop 卡在 waiting-for-safe-point、control_session 没有答卡、rewind 报 SESSION_RUNNING。所以只做两件事：写结论行；没通知过就 schedule_notify_current_run 说明「某会话挂在提问卡上，需要在界面点一下」，然后结束。不许反复发消息、反复 stop/rewind。
它自己结束后（phase=completed）：control_session(resume) → send_to_session 补话（正文写「（补发）」）→ 30 秒后复核 phase 应转 running。
`async-question:` 开头的异步卡不一样：它能自己撤，消息能进去就行。

**收尾**
- 有介入 → schedule_notify_current_run，两三行：哪个会话/哪张单、原来卡在哪、做了什么、它接下来自己往哪走。**上限就是这三件事**：不写「这一轮的意义是……」「顺带确认了一件事」这类段落，也不复述上一轮已经写过的判断。
- 没介入 → schedule_silence_current_run。
- 结论行一行一个：`巡检 <HH:MM>：<扫了哪几个目录> / <会话或单号> / <phase> / idle<N> / 队列<N> → <判断>`；C 类：`巡检 <HH:MM>：<单号> awaiting_review <N>min / notices<N> → 回执没到|到了没落库|已提醒未见变化`。不复述工具原始输出，不写自我感想。
- 第 1d、1e 步的检测结果即使为空也要在结论行写一句（「1d 无、1e 无」），不许默默跳过。

**红线**
- 只碰第 0 步算出来的目录；不碰 archived 会话、不碰本会话自己这条。
- 不确定就不介入，并写明不确定在哪。宁可漏一次，不要多改一次。
- 不改任何工单状态、验收条件、文件；不动其它 schedule。
- 上一轮已推过、情况没变的会话不发第二条。
- 目标已不可能达成（反复打回、额度耗尽）不硬推，写进 notify 等人看。
