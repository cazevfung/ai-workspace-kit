# AI Workspace Kit

Give AI work a ticket, evidence, independent acceptance, and a record you can query later.

This is not a prompt pack or another board. It packages a workflow that has already run dozens
of real tickets: a local SQLite task center, four gates that **exit non-zero**, 376 tests,
25 skills, and the documents that explain why each rule exists.

You need **Python 3.10+** and an agent that reads `AGENTS.md` (Cindy, Claude Code, Codex, ...).

## Install

```bash
git clone <this repo> ai-workspace-kit
cd ai-workspace-kit
python install.py
```

It installs to `~/ai-workspace` and puts 25 skills into `~/.agents/skills`.

```bash
python install.py --workspace D:/ai-ws   # custom location
python install.py --dry-run              # plan only, no writes
python install.py --link-agents          # also link skills for ~/.claude and ~/.codex
```

Windows: `powershell -ExecutionPolicy Bypass -File scripts/install.ps1`
macOS/Linux: `bash scripts/install.sh`

Verify:

```bash
cd ~/ai-workspace
python tools/work_gate.py --list
python tools/run_tests.py
python tools/doc_gate.py --all
```

## What's inside

| Part | Role |
| --- | --- |
| `task_center/` | Ticket core: projects, tickets, attempts, submissions, reviews, events (SQLite, stdlib only) |
| `tools/` | Gates: `work_gate`, `doc_gate`, `close_gate`, `intake_gate`, `auto_merge`, `run_tests` |
| `.agents/skills/` | Workspace skills: `prd-authoring`, `pdsa-workflow`, `ticket-dispatch`, `plan-gate`, `skill-routing` |
| `docs/` | Protocol, closing, intake, probe, skill loading |
| `PRD/`, `tasks/` | Requirements and this round's goal; progress lives in the database, not in Markdown |

`AGENTS.md` is the behavior contract: six hard rules, message-channel discipline,
how a round closes, and branch/merge rules.

## Why a repo, not a plugin or a skill

Half of this system's value lives on the filesystem: worktrees, commit hooks, the local DB,
gate exit codes. Plugins run in a sandbox and cannot lay down a project skeleton;
skills are instructions, not installers. So the repo + installer is the only carrier that
installs everything; a SkillHub entry and an optional Cindy plugin are thin front doors
that lead here.

Full reasoning: [`docs/SHARING.md`](docs/SHARING.md). Install details: [`docs/INSTALL.md`](docs/INSTALL.md).
Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Uninstall

```bash
python uninstall.py --workspace ~/ai-workspace
python uninstall.py --skills
```

It only removes what the installer recorded in `~/.agents/.ai-workspace-kit.json`.

## License

MIT. Provenance and the design history live in `design/`.
