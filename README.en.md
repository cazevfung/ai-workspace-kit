# AI Workspace Kit

中文版：[README.md](README.md)

**A project-management layer for command-line agents. You bring requirements to one PM: it writes the ticket, dispatches it to a fresh session in the right project, watches the receipts, and has a different session review. Nothing merges until review and tests pass.**

![Architecture: you talk to one PM, it dispatches work to different projects, review is a separate session, and every record lands in one database](docs/architecture.svg)

[The problem](#the-problem-it-solves) · [What it is](#what-it-is) · [Context boundaries](#context-boundaries) · [Life of a ticket](#the-life-of-one-ticket) · [Gates](#gates-and-decision-points) · [Known limitations](#known-limitations) · [Install](#install) · [Demo](#verification-and-demo)

The workflow has run dozens of tickets in a real project; stripped of project-specific content, it is packaged here at version 0.4.0. It needs Python 3.10+ and an agent host that reads `AGENTS.md` (Cindy, Claude Code, Codex).

---

## The problem it solves

A command-line agent is rarely the bottleneck for one task; parallel sessions have no management layer, and progress, ownership, review and context all sit on one person. Four failures follow.

- **Scattered sessions.** Work runs across sessions in different projects; checking progress means remembering which project and which session, then reading the chat.
- **No queryable progress.** A session that stops to wait records nothing; two days later the context is cold.
- **No separation of doing and checking.** A session declares itself done while the diff misses files; without a forced second pair of eyes, "it said done" becomes "done".
- **Uncontrolled context.** Parallel sessions edit the same files and merges are resolved by hand; too much context makes an executor overreach, too little makes it wrong.

The common cause is not a weak model but the absence of a layer accountable to the project. This kit adds that layer.

---

## What it is

It plays the role of a project manager: breaking work down, handing it out, keeping it moving, bringing it home. The implementation has four parts: `task_center/`, the ticket core (standard library plus SQLite); `tools/`, the gate and merge scripts; `AGENTS.md`, the behaviour contract; and `skills/`, 26 skills (6 workspace, 20 general). The kit does not make the model stronger; it turns who does the work, what counts as done, who checks and why it merges into records and checks that fail.

### Breaking work down

A requirement becomes a ticket before anything runs: a deliverable (topic plus explicit exclusions), acceptance criteria, and a PRD pinned by path and content hash. Unwritable criteria mean the requirement is not ready. Change the PRD and the hash stops matching; old submissions are rejected with `STALE_PRD` until `revise_ticket` runs.

### Handing it out

A ticket is dispatched to its project's workspace and picked up by a newly opened executor session; a coordinator binding its own identifier is rejected as `SELF_DISPATCH`. One ticket per project runs at a time — a busy project answers `DISPATCH_BUSY` — and parallel work requires an explicit `allow_parallel`.

### Keeping it moving

Receipts land in the central database; the coordinator decides the next hop. Questions needing a human go through `ask_ticket` and wait in the database. There is no built-in scheduler: a host-side scheduled job checks the database on a timer and resumes stalled tickets.

### Bringing it home

Review is a different session reading ticket and evidence, not the chat; the doer cannot sign off — no switch. Merging only goes through `auto_merge.py`: state and branch checks, conflict preview, rollback point, tests in a temporary worktree, `--no-ff` merge, tests again, merge record; failure returns to the rollback point. Closing a round takes a seven-section packet judged by `close_gate.py`.

---

## Context boundaries

Every ticket leaves with exactly the context it needs, so the executor is replaceable.

- **Not too much.** The executor receives this ticket and the one PRD it cites — no other projects, no unrelated history.
- **Not too little.** Deliverables, exclusions, criteria, PRD version and hash, and evidence requirements are mandatory; cross-session handoffs add a six-section packet: the user's own words, the deliverable, why the last attempt failed, hard constraints, confirmed facts, open questions.
- **Replaceable.** The ticket is self-contained: a new session, a new person, a week later — same brief.

One analogy: a nurse's handover. Pass only what matters, but never omit an allergy. More is noise; less is an incident.

---

## Cross-project dispatch and external handoff

One table covers every project, each registering one root directory; a ticket belongs to one project, and ownership is never guessed — `probe` proposes candidates, a human confirms. Long-running work from another project enters through a fixed protocol; the original session stops and only relays receipts. Four installed parts:

| Piece | Installed at | Role |
| --- | --- | --- |
| `handoff-to-workspace` skill | `~/.agents/skills/` | When to hand off, to whom, the four steps |
| Handoff protocol | `~/.agents/intake/intake.md` | Message template, six-section structure |
| `intake_gate.py` | `<workspace>/tools/` | Missing sections exit non-zero |
| Routing and dispatch rules | `AGENTS.md`, skills `project-routing` and `ticket-dispatch` | Ownership is never guessed; dispatch opens a new session |

```mermaid
graph LR
  P["PRD<br/>ID + version"] --> T["Ticket<br/>scope + acceptance criteria"]
  T --> D["Dispatch: open a new session"]
  D --> A["Project A executor"]
  D --> B["Project B executor"]
  A --> V["Independent review"]
  B --> V
  V --> M["Merge + record"]
  O["Long-running work in another project"] -. "Six-section handoff + intake_gate" .-> T
```

---

## Six typical situations

| What you are doing | What you end up with |
| --- | --- |
| A feature from requirement to production | Versioned PRD criteria, a gated plan, separate reviewer, tests before merge, full records |
| Pushing several projects at once | One central database shows every ticket state |
| Long-running work from another project | A six-section packet passes the gate; the original session stops and relays receipts |
| A requirement changed mid-flight | Hash mismatch blocks submission with `STALE_PRD` until explicit revision |
| Uncertainty about merge risks | `auto_merge` previews conflicts, runs tests, tags a rollback point |
| Coming back the next day | State lives in the database; a new session or person sees where it stopped |

---

## The life of one ticket

Seven steps, each written to the database, each exiting non-zero on failure.

1. **Open.** `create_ticket` records deliverable, criteria, and the PRD path with its hash.
2. **Claim.** `claim_ticket` creates an attempt; the ticket enters `dispatching`.
3. **Bind.** A new executor session is bound with `bind_session`; the coordinator's own identifier is rejected as `SELF_DISPATCH`.
4. **Execute.** One worktree, one `ticket/<id>` branch; the main checkout stays on the main branch.
5. **Submit.** `submit_result` carries artifacts, per-file sha256, evidence and the reviewed tree's `head_sha`; the ticket enters `awaiting_review`.
6. **Review.** A different session calls `review_result` with `accept` or `rework`.
7. **Merge and close.** `auto_merge.py` merges; `close_gate.py` judges the closing packet.

State chain: `ready → dispatching → running → awaiting_review → done`, with side branches `blocked` (`ask_ticket` waits on a human) and `rework` (claimed again).

---

## Gates and decision points

Rules in documents usually go unread, so constraints live in exit codes; these checks fail by default.

| Gate | Blocks | Runs |
| --- | --- | --- |
| `work_gate.py` | Commits without a ticket or with an unbound attempt | commit hook |
| `doc_gate.py` | Plans with no PRD basis, missing sections, unverifiable evidence | before work starts |
| `close_gate.py` | Closing packets with missing sections or stale status | before a round ends |
| `intake_gate.py` | Handoff packets with missing sections | at handoff |
| `claims_gate.py` | Numbers not matching their source | when writing evidence |
| `ref_gate.py` | Citations to files, lines or headings that do not exist | on cross-references |
| `run_tests.py` | A red test suite | before commit and merge |
| `auto_merge.py` | Manual merges, merges without tests, forced merges with conflicts | at merge time |

A merge commit is the only commit allowed without a ticket: merges happen after review, when the ticket is already `done`. Every other commit follows the same rule.

---

## Why the rules look like this

Each rule traces to a specific incident. Three typical ones.

**A dispatch message once carried a well-formed session identifier that no session had.** The string was assembled from a temporary directory name; the record looked dispatched and pointed at nothing. Now the executor obtains its identifier via `get_current_session_id`, and the coordinator verifies it with `list_sessions` before binding.

**An executor once submitted four artifacts whose sha256 all mismatched.** The paths lacked the worktree prefix, so the database hashed same-named files in the main checkout — correct numbers, wrong files. Artifact paths now require the `.worktrees/<id>/` prefix and are checked one by one.

**A coordinator once skipped ticketing and dispatch, did the work itself, and declared it complete.** The record looked complete, but nobody else had seen it. `SELF_DISPATCH` now rejects that binding: the doer-reviewer split is a mechanism, not an agreement.

---

## When to use it and when not to

It is heavy, and it suits work that must be delivered, reviewed by someone else, and resumable across days and sessions; one-off tasks should be handled directly. A ticket takes five steps (create, claim, bind, submit, review), plans pass `doc_gate`, commits carry the ticket id, closing passes `close_gate`. The criterion: will anyone ask, two months from now, how this was decided, who reviewed it, why it merged? If yes, use the workspace; if not, skip it.

---

## How it relates to other orchestration

Team modes and subagents answer who runs and how work is assigned; this workspace answers what counts as done, who reviews, and how it merges. Three differences: a lead's "done" is final there, here a submission waits for another session's verdict; their worktrees only prevent collisions, here merges have previews, tests and rollback points; their state dies with the archived session, here it lives in SQLite. The layers compose: run parallel lanes as workers, each following the workspace flow in its own project; the default dispatch vehicle is an ordinary project session, not a worker.

---

## Known limitations

- Opening a new executor session after `claim_ticket` depends on host capability — `send_to_session` in Cindy; other hosts supply that step or bind a manually opened session with `bind_session`.
- No built-in scheduler: resuming stalled tickets requires a host-side scheduled job that checks the database and re-dispatches.
- One ticket runs per project by default; parallelism requires an explicit `allow_parallel`.
- The skills are written mainly in Chinese and are not yet translated.

---

## Install

Requires Python 3.10+. Git is recommended; without it, the default path that blocks ticketless commits is gone.

```bash
git clone https://github.com/cazevfung/ai-workspace-kit ai-workspace-kit
cd ai-workspace-kit
python install.py
```

The workspace goes to `~/ai-workspace` and the skills to `~/.agents/skills` (same-named skills are skipped). Common flags:

```bash
python install.py --workspace D:/ai-ws        # custom workspace directory
python install.py --dry-run                   # print the plan, touch nothing
python install.py --overwrite-skills          # overwrite same-named skills
python install.py --no-skills                 # workspace only
python install.py --link-agents               # link skills into ~/.claude and ~/.codex
```

Wrapper scripts: `bash scripts/install.sh` (macOS / Linux), `powershell -ExecutionPolicy Bypass -File scripts/install.ps1` (Windows).

---

## Verification and demo

After installing, run three commands:

```bash
cd ~/ai-workspace
python tools/work_gate.py --list     # the first command of any round
python tools/run_tests.py            # should be all green
python tools/doc_gate.py --all       # should exit 0
```

Then connect the workspace to the host and a business repository:

```bash
cd ~/ai-workspace
python onboard.py --json                       # create_project -> open session -> first instruction, full arguments
python tools/add_project.py D:/my-project      # writes .agents/ticket-config.json and points core.hooksPath at .githooks
```

**Run `add_project.py` before anything else.** Every business repository has to be connected first: `python tools/add_project.py D:/my-project` writes `<repo>/.agents/ticket-config.json` (`test_cmd` / `target_branch` / `test_timeout`) and points that repository's `core.hooksPath` at the kit's `.githooks`. `tools/auto_merge.py` reads that file at step 2 and stops there when it is missing — it never falls back to a default test command, because "no tests configured" must not look like "tests passed".

The test count is whatever `run_tests.py` reports in its summary line and changes between tags; the current kit installs 672.

Known differences from the upstream workspace the engine was synced from: the task-centre engine matches its schema 13 (migrations 9–13, id issuing, `planned` queue and dependency links, trips, conflict preview, `criteria_version`), and `kit/PRD/` now carries `PRD-001` plus the `PRD-010`~`PRD-015` bodies with paths rewritten for the kit (`grep -rl "D:/AI工作区" kit/PRD/` is empty) — `PRD-002`~`PRD-009` stay in `design/PRD/` as this toolkit's own design history. `auto_merge.py` keeps the kit variant that reads project config, and its pre-record schema gate is in place: it snapshots the database, migrates it with the merged code and reloads `task_center`, and it looks for the engine at **both** `task_center/db.py` and `kit/task_center/db.py` (looking only at the repository root is why that gate silently skipped in this very repo). Usage lines keep the `{{WORKSPACE}}` placeholder instead of a fixed path; and test fixtures are scrubbed (`<session-id>`, `X:/sample-project`). `kit/docs/protocol.md` still documents the pre-sync command surface (no `planned` pool, trips, dependency links or conflict preview); the `ALLOWED` table in `task_center/core.py` and the fields `get_ticket` really returns are authoritative until that doc is updated along with the PRD texts.

In the kit repository, the nine-step self-check installs into a temporary HOME and walks the whole loop:

```bash
python verify/verify_onboard.py
```

`examples/demo.py` walks the whole loop in a temporary database — create, dispatch, submit, review — touching no real session or workspace, deleting the database when done.

```bash
cd ~/ai-workspace
PYTHONPATH=. python examples/demo.py            # macOS / Linux
$env:PYTHONPATH="."; python examples/demo.py    # Windows PowerShell
```

The output is the real receipt of every step; the first submission comes back as rework, only the second passes.

```text
{"operation": "review_result", "result": {"state": "rework", "reason": "只有一项，需要返工", ...}}
{"operation": "review_result", "result": {"state": "done", "reason": "满足两项要求", ...}}
```



---

## What gets installed

```text
ai-workspace/
├── AGENTS.md              # behaviour contract
├── README.md
├── PRD/                   # requirements
├── tasks/                 # round.md, rounds.md, plan-*.md
├── task_center/           # ticket core (standard library, SQLite)
├── tools/                 # gates + auto_merge + run_tests
├── tests/                 # cases for the core and the gates
├── docs/                  # protocol / closing / probe / intake / ...
├── examples/
├── .agents/skills/        # workspace skills
└── .githooks/             # commit hooks
```

The skills are also installed to `~/.agents/skills`, so an agent can read them in any project; `v0.4.0` installs 26:

- **Workspace skills (6):** `prd-authoring`, `pdsa-workflow`, `ticket-dispatch`, `plan-gate`, `skill-routing`, `bootstrap-workspace`
- **General skills (20):** `brainstorming`, `writing-plans`, `executing-plans`, `test-driven-development`, `systematic-debugging`, `verification-before-completion`, `code-review-and-quality`, `requesting-code-review`, `receiving-code-review`, `merge-latest-branches`, `writing-skills`, `shuo-ren-hua`, `dispatching-parallel-agents`, `subagent-driven-development`, `resumable-implementation-contracts`, `using-git-worktrees`, `doubt-driven-development`, `planning-and-task-breakdown`, `project-routing`, `handoff-to-workspace`

The first workspace skill worth reading is `skill-routing`: it assigns each decision point to a skill or a fixed action, and `scripts/check_routing.py` fails when an assignment is missing or points nowhere.

---

## Distribution

The main channel is the GitHub repository plus `install.py`; the skill package and the Cindy plugin are thin front doors.

| Approach | Can it install everything? | Verdict |
| --- | --- | --- |
| **GitHub repo + `install.py`** | Yes: code, gates, hooks, skills in one go | **Main channel** |
| **Skill package (SkillHub)** | No: a skill is instructions; it cannot install Python modules or hooks | Front door pointing here |
| **Cindy plugin** | No: the sandbox cannot touch home directories or git hooks | Optional wrapper |

Full analysis in [`docs/SHARING.md`](docs/SHARING.md): half of the system's value lives on the filesystem (worktrees, commit hooks, a local database), which plugins and skills cannot reach.

---

## Uninstall

```bash
python uninstall.py --workspace ~/ai-workspace          # remove the workspace (asks first)
python uninstall.py --skills                            # remove only this kit's skills
```

`~/.agents/center.json` and `~/.agents/intake/` are kept by default, since other projects may use them; add `--all` for those too. `.data/` holds your progress and is kept.

---

## Maintainers

`kit/`, `skills/` and `design/` come from an upstream workspace. After it changes, rebuild in three steps, do not copy by hand:

```bash
python maintainer/sync_from_origin.py        # 1. sync, absolute paths become {{WORKSPACE}}
python maintainer/redact_for_public.py       # 2. strip usernames, session IDs, upstream names
python verify/verify_install.py              # 3. install into a temp dir; all 14 checks pass
```

Step 2 is not optional: `kit/tests/fixtures` is copied verbatim, so the sync brings real usernames and session IDs along; `redact_for_public.py --check` runs the same scan alone. `README`, `install.py`, `uninstall.py`, `kit/AGENTS.md`, `kit/PRD/`, `docs/*.md` are hand-maintained and never overwritten. Bump `VERSION` and update `CHANGELOG.md` on release.

---

## Provenance and license

The framework code, gates, skills and documents come from a personal AI workspace that has been running in production, packaged after removing project-specific content and local absolute paths. The design history, including the original PRDs and the AGENTS record, lives in `design/` for anyone asking why the rules look this way.

MIT License. See [`LICENSE`](LICENSE).
