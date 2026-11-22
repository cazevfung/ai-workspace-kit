// AI 工作区套件 · 插件入口
//
// 只做一件事：把工作区自己的启动/派发计划原样交给当前 Agent。插件不创建项目、
// 不开会话、不写磁盘——那些是宿主操作，要在会话里逐条调、失败要看得见。
// bootstrap 计划最后一步 `write_receipt` 标 `local`：回执由会话自己在本地写，
// 插件同样不写；它只是把「写什么、写在哪」一并交出去，免得两条入口给出不同计划。
// 计划与 `<工作区>/onboard.py --json` 同源；tools/test_plugin_plan.py 会比对两边
// 的步骤名、顺序与失败停点，改了一边忘了另一边会当场红。

const PLANS = {
  "bootstrap": {
    "purpose": "把已装好的工作区加成宿主项目，开一个协调会话，并把第一条指令发进去。",
    "steps": [
      {
        "order": 1,
        "tool": "list_projects",
        "server": "cindy_helper",
        "args": { "include_hidden": true, "offset": 0, "limit": 100 },
        "check": "清单里已有 project_path == 工作区绝对路径的项目 → 跳过 create_project，直接用它。"
      },
      {
        "order": 2,
        "tool": "create_project",
        "server": "cindy_helper",
        "args": { "name": "{{PROJECT_NAME}}", "working_dir": "{{WORKSPACE}}" },
        "check": "回执的 project_path 必须等于工作区绝对路径；把它传给 send_to_session。"
      },
      {
        "order": 3,
        "tool": "send_to_session",
        "server": "cindy_helper",
        "args": {
          "message": "{{FIRST_INSTRUCTION}}",
          "title": "{{TITLE}}",
          "project_path": "{{WORKSPACE}}"
        },
        "mode": "create（不传 target_session_id 才是新建任务）",
        "check": "回执必须带 target_session_id 与 working_dir；把它原样交给下一步反查，不要改写或缩写。"
      },
      {
        "order": 4,
        "tool": "list_sessions",
        "server": "cindy_helper",
        "args": {
          "workdir": "{{WORKSPACE}}",
          "device": "local",
          "include_deleted": false,
          "limit": 100,
          "order": "desc"
        },
        "check": "看返回里有没有 title 与 workingDir 都对得上的那一条；对不上就是没建出来。"
      },
      {
        "order": 5,
        "tool": "write_receipt",
        "server": "local",
        "kind": "local",
        "call_via": "会话自己在本地写文件（不经宿主）",
        "args": {
          "path": "{{WORKSPACE}}/.data/onboard-receipt.json",
          "target_session_id": "<第 3 步回执里的 target_session_id>",
          "working_dir": "{{WORKSPACE}}",
          "fields": ["project_path", "target_session_id", "working_dir", "created_at"]
        },
        "check": "写完读一遍：四个字段都在、路径是绝对路径、target_session_id 与第 3 步回执一字不差；核对不上不算写过。回执只是声称，证明不了宿主里项目真的开着。"
      }
    ]
  },
  "dispatch_ticket": {
    "purpose": "工单已经 claim 之后，新开一个执行会话并把工单号发过去；send_to_session 与 bootstrap 同一份参数。",
    "steps": [
      {
        "order": 1,
        "tool": "get_ticket",
        "server": "task_center",
        "call_via": "python -m task_center --db <中心库> get_ticket",
        "args": { "project_id": "{{PROJECT_ID}}", "ticket_id": "{{TICKET_ID}}" },
        "check": "state 必须是 planned / ready / rework；不是就先看它停在哪一步。"
      },
      {
        "order": 2,
        "tool": "claim_ticket",
        "server": "task_center",
        "call_via": "python -m task_center --db <中心库> claim_ticket",
        "args": {
          "project_id": "{{PROJECT_ID}}",
          "ticket_id": "{{TICKET_ID}}",
          "expected_revision": "{{EXPECTED_REVISION}}",
          "coordinator_session_id": "{{COORDINATOR_SESSION_ID}}",
          "coordinator_workdir": "{{WORKSPACE}}"
        },
        "check": "回执带 attempt_id；把它和工单号一起写进下一条消息。"
      },
      {
        "order": 3,
        "tool": "send_to_session",
        "server": "cindy_helper",
        "args": {
          "message": "{{DISPATCH_MESSAGE}}",
          "title": "{{DISPATCH_TITLE}}",
          "project_path": "{{WORKSPACE}}"
        },
        "mode": "create（不传 target_session_id 才是新建任务）",
        "check": "回执必须带 target_session_id 与 working_dir；把它原样交给下一步反查，不要改写或缩写。"
      },
      {
        "order": 4,
        "tool": "list_sessions",
        "server": "cindy_helper",
        "args": {
          "workdir": "{{WORKSPACE}}",
          "device": "local",
          "include_deleted": false,
          "limit": 100,
          "order": "desc"
        },
        "check": "看返回里有没有 title 与 workingDir 都对得上的那一条；对不上就是没建出来。"
      }
    ]
  }
};

const FAILURES = {
  "bootstrap": {
    "list_projects": "读不到项目清单就停下：不要凭名字猜路径，也不要为了继续而新建一个项目。",
    "create_project": "目录不存在或不是目录 → 先建目录；同名项目已存在 → 复用它的 project_path，不要重复登记；返回 SOURCE_OUTSIDE_WORKDIR / 权限被拒 → 把工作区放到会话可写的目录再试。",
    "send_to_session": "返回 NOT_FOUND / ARCHIVED / DELETED → 不要重复新建项目；先 list_sessions 查实际状态。返回 PROJECT_NOT_FOUND → 回到 create_project 的 project_path，不要传显示名。",
    "list_sessions": "列表里找不到 title 与 workdir 同时匹配的会话 → 会话没建出来，不要说已经开工；重发一次 send_to_session，仍失败就停下问人。",
    "write_receipt": "没有反查到的 target_session_id → 不要拿猜的 id 凑一份回执；写不进去（目录只读、.data 建不出来）→ 停下说明，不要说已经开工。回执只是一份声称，它成立与否由 verify/verify_receipt.py 事后核。"
  },
  "dispatch_ticket": {
    "get_ticket": "工单不是 planned / ready / rework → 不要重复领取；先看 state 说明它停在哪一步。",
    "claim_ticket": "DISPATCH_BUSY → 同项目已有在途单，先做完那张；SELF_DISPATCH → 换一个执行会话，不要协调会话自己做；STALE_ATTEMPT → 重新 get_ticket 取当前 revision。",
    "send_to_session": "返回 NOT_FOUND / ARCHIVED / DELETED → 不要换一个字符串冒充执行者；先修会话再重派。返回 created 但标题不是本单 → 回执发错了地方，停掉误建任务再带 target_session_id 重发。",
    "list_sessions": "找不到 title/workingDir 匹配的执行会话 → 派发未完成；不要自己 bind_session 顶替执行者。"
  }
};

const CALL_VIA = "cindy_mcp_call_tool";

function fail(callId, code, message) {
  cindy.send({ type: "tool-result", callId: callId, ok: false, errorCode: code, message: message });
}

function requireString(args, key, callId) {
  const value = args[key];
  if (typeof value !== "string" || !value.trim()) {
    fail(callId, "INVALID_ARGS", `缺少参数 ${key}：请传它的绝对路径或名称后重试。`);
    return null;
  }
  return value.trim();
}

function fillPlan(template, values) {
  const text = JSON.stringify(template);
  const filled = text.replace(/\{\{([A-Z_]+)\}\}/g, function (match, key) {
    return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : match;
  });
  return JSON.parse(filled);
}

function valuesFor(args, callId) {
  const workspace = requireString(args, "workspace", callId);
  if (workspace === null) return null;
  const name = typeof args.project_name === "string" && args.project_name.trim()
    ? args.project_name.trim()
    : workspace.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "AI 工作区";
  return {
    WORKSPACE: workspace,
    PROJECT_NAME: name,
    TITLE: typeof args.title === "string" && args.title.trim() ? args.title.trim() : "AI 工作区 · 协调会话",
    FIRST_INSTRUCTION:
      "你是这个 AI 工作区的协调会话。先读 AGENTS.md，再跑 `python tools/work_gate.py --list` " +
      "确认没有在途工单；接着用 `python tools/run_tests.py` 与 `python tools/doc_gate.py --all` " +
      "确认环境全绿。要开工单时按 `.agents/skills/ticket-dispatch/SKILL.md` 的顺序走：" +
      "create_ticket → claim_ticket → 新开执行会话 → bind_session。"
  };
}

cindy.onHostMessage(function (msg) {
  if (msg.type !== "tool-call") return;
  const args = msg.args || {};

  if (msg.tool === "bootstrap") {
    const values = valuesFor(args, msg.callId);
    if (!values) return;
    const plan = fillPlan(PLANS.bootstrap, values);
    cindy.send({
      type: "tool-result",
      callId: msg.callId,
      ok: true,
      result: {
        plan: plan,
        call_via: CALL_VIA,
        envelope: { server: "cindy_helper", tool: "call_tool", args: { name: "<step.tool>", args: "<step.args>" } },
        failure_paths: FAILURES.bootstrap,
        note: "只返回计划，不执行任何宿主写操作。照 steps 逐条调 cindy_helper.call_tool；最后一步 write_receipt 是 local 步，由会话自己写 .data/onboard-receipt.json，不经宿主。失败按 failure_paths 停下。"
      }
    });
    return;
  }

  if (msg.tool === "dispatch_ticket") {
    const workspace = requireString(args, "workspace", msg.callId);
    if (workspace === null) return;
    const projectId = requireString(args, "project_id", msg.callId);
    if (projectId === null) return;
    const ticketId = requireString(args, "ticket_id", msg.callId);
    if (ticketId === null) return;
    const coordinator = requireString(args, "coordinator_session_id", msg.callId);
    if (coordinator === null) return;
    const revision = Number(args.expected_revision);
    if (!Number.isInteger(revision) || revision < 1) {
      fail(msg.callId, "INVALID_ARGS", "expected_revision 必须是正整数：先 get_ticket 取当前 revision 再重试。");
      return;
    }
    const values = valuesFor(args, msg.callId);
    if (!values) return;
    values.PROJECT_ID = projectId;
    values.TICKET_ID = ticketId;
    values.EXPECTED_REVISION = revision;
    values.COORDINATOR_SESSION_ID = coordinator;
    values.DISPATCH_TITLE = "执行工单 " + ticketId;
    values.DISPATCH_MESSAGE =
      "你是工单 " + ticketId + " 的执行会话。先跑 `python tools/work_gate.py --list` 确认这张单已领取，" +
      "再用 bind_session 绑定自己的 session_id（这一步只有执行者能做）；随后按 PRD 与验收条件执行，" +
      "提交时用 submit_result 并填 `git rev-parse HEAD` 的 40 位 sha。";
    const plan = fillPlan(PLANS.dispatch_ticket, values);
    cindy.send({
      type: "tool-result",
      callId: msg.callId,
      ok: true,
      result: {
        plan: plan,
        call_via: CALL_VIA,
        envelope: { server: "cindy_helper", tool: "call_tool", args: { name: "<step.tool>", args: "<step.args>" } },
        failure_paths: FAILURES.dispatch_ticket,
        note: "claim 由协调会话自己调；执行会话必须自己 bind_session，不要替它绑。"
      }
    });
    return;
  }

  fail(msg.callId, "UNKNOWN_TOOL", "这个插件只有 bootstrap 与 dispatch_ticket 两个工具；核对工具名后重试。");
});
