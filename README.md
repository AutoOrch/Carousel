# LangGraph + OpenCode Demo

LangGraph handles task orchestration, state management, dependency scheduling, parallelism, retries, and dynamic re-planning; OpenCode handles actually entering the code repository to execute coding tasks. Git Worktree provides isolation.

## P12 Full Traceability and Reliability

The current runtime uses structured records in SQLite as the source of state and audit:

```text
Requirement Run → Plan → Task → Attempt → Event / Operation
             → Dependency → ChangeSet → Validation → Commit / Merge
             → Artifact → Review / Review Item
```

- Attempts are atomically numbered by the database; restarts and re-queueing do not reset the retry budget;
- Heartbeats are bound to `worker_id + lease_id`; other processes cannot renew leases for unresponsive tasks;
- commit/merge uses an operation journal; startup recovery cross-checks against Git ancestry;
- `allowed_paths`, actual changed files, and project test commands are hard gates before commit;
- Reports, architecture, worktrees, requirement run indexes, and archived documents are uniformly stored under
  `projects/<stable-project-id>/`; these Runner control-plane assets are never written to the business repository;
- `/runs` allows drill-down from Requirement Run to Task, Attempt, Event, and Validation;
- `GET /api/runs/<run-id>/export?format=json|md` or `python -m runtime.audit_export <run-id>` exports the complete audit package;
- Legacy tasks auto-repair Attempt projections on first startup and generate `runtime/migration-p12-v1.json`; asset migration records are in `projects/_runs/asset-migration-p12.json`.

Pre-run checks:

```powershell
python doctor.py
python -m unittest discover -s tests -v
python maintenance.py --integrity --backup
python maintenance.py --cleanup --days 30       # preview only
python maintenance.py --cleanup --days 30 --apply
```

Before upgrading the database schema, run `python maintenance.py --backup` first; to roll back, stop the watcher, restore the SQLite backup, then restart and run `doctor.py`.

## Five Entry Points

| Entry Point | Command | Description |
|------|------|------|
| **Planner** | `python planner.py --requirement spec.md --project demo` | Generates task .md files from a requirement .md into `prompts/` (with dependency chains) |
| **P1** | `python main.py --mode dry-run` | Fixed Planner hardcodes 2 tasks, executes in parallel, then Reviews |
| **P2-P5** | `python watcher.py --mode dry-run [--once]` | Folder watch → multi-project → resource locks → dependency scheduling → merge → crash recovery → retry loop |
| **Document** | `python document_run.py --mode dry-run [--watch]` | Document organizer pipeline (p11): doc/ → classify → dedup → archive → index → report |
| **Historical Import** | `python document_import.py --source <dir> --project <project> --all --run` | P13: historical directory inventory → code validation → revision drafts → topic summarization → merge archive (one step) |

## Phase Overview

- **P1 (`main.py`)**: Fixed Planner generates two tasks, executes in parallel, then Reviews.
- **P2 (`watcher.py`)**: Folder watch; drop a `.md` task into `prompts/` for automatic execution.
- **P3 (Multi-project)**: Tasks specify `project` via front-matter, resolved to actual repositories by `config.yaml`.
- **P4 (Conflict governance)**: Resource locks + auto-merge + Merge Agent, safely handling multiple tasks modifying the same file.
- **P5 (Crash recovery + failure retry)**: SQLite state machine + Lease/Heartbeat + Diagnose → Re-plan → Retry loop.
- **P7 (Production hardening)**: Planner requirement-to-task splitting + task dependencies (`depends_on`) + Lease Fencing + LangGraph Checkpoint + logging system + Worktree/Session cleanup + OpenCode API full-endpoint validation.
- **P13 (Document knowledgeization)**: Read-only inventory of any explicit historical directory, frozen plan, copy-only import, code evidence validation, revision drafts, topic summarization, and near-duplicate document merge archiving.

## Prerequisites

| Dependency | Version | Notes |
|------|------|------|
| Python | 3.10+ | Uses `match`, `X | Y` type syntax |
| Git | Any | Worktree isolation + merge |
| OpenCode | 1.18+ (optional) | Only needed for `--mode opencode`; not required for `--mode dry-run` |

## Quick Start (End-to-End)

```powershell
# 1. Create virtual environment + install dependencies
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Initialize demo Git repositories (demo-repo / demo-repo-2)
.\.venv\Scripts\python.exe setup_demo_repo.py

# 3. Generate tasks from requirement (dry-run mode generates 3 test tasks with dependency chain)
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo --mode dry-run
#    → prompts/task-001-setup.md  (allowed_paths: [README.md])
#    → prompts/task-002-implement.md (depends_on: [task-001-setup])
#    → prompts/task-003-docs.md  (depends_on: [task-002-implement])

# 4. Execute task queue (in dependency order: 001 → 002 → 003)
.\.venv\Scripts\python.exe watcher.py --mode dry-run --once
#    Per task: worktree → execute → validate → (retry loop) → commit → merge
#    Completed: processed/   Failed: failed/

# 5. Connect to real OpenCode Server (optional)
opencode serve --hostname 127.0.0.1 --port 4096
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo --mode opencode
.\.venv\Scripts\python.exe watcher.py --mode opencode
```

> Don't want to use Planner? Just hand-write a `.md` task file and drop it into `prompts/`, see "Task File Format" below.

## P5 Full Pipeline

```text
prompts/*.md ──► Task Scanner ──► Task Resolver ──► Worker Pool
                                                      │
                    ┌─────────────────────────────────┘
                    │
                    ▼
              LangGraph (per task)
                    │
          ┌─────────┴──────────┐
          │                    │
    Recovery Manager      Task Store (SQLite)
    (lease/heartbeat)     (status/attempt/checkpoint)
          │                    │
          │     ┌──────────────┘
          │     │
          ▼     ▼
    ┌──────────────────────────────────────────────┐
    │                  Task Graph                   │
    │                                               │
    │  PREPARE → EXECUTE → VALIDATE                 │
    │                       │                       │
    │              ┌────────┴────────┐              │
    │              │ success         │ failure      │
    │              ▼                 ▼              │
    │            COMMIT           DIAGNOSE          │
    │              │                 │              │
    │              ▼                 ▼              │
    │            MERGE            REPLAN             │
    │              │                 │              │
    │       ┌──────┴──────┐   attempt < max?        │
    │       │             │     │         │         │
    │    SUCCESS       FAILED  yes       no         │
    │                           │         │         │
    │                         EXECUTE   FAILED      │
    └──────────────────────────────────────────────┘

    Startup Recovery: scan processing/ → expired lease → requeue
```

## Four Major Loops

| Loop | Problem Solved | Implementation |
|------|-----------|------|
| **Execution Loop** | How the Agent executes tasks | LangGraph task_graph |
| **Recovery Loop** | How to recover after Runner crash | SQLite Lease/Heartbeat + RecoveryManager |
| **Retry/Re-plan Loop** | How to self-repair after task failure | Diagnose → Replan → Retry (max_attempts) |
| **Merge Loop** | How to merge after multi-Agent modification conflicts | FileLockManager + Merge Agent |

## Four Layers of Protection + Crash Recovery + Retry + Production Hardening

| Mechanism | File | Description |
|------|------|------|
| Worktree isolation | `worktree.py` | Independent Branch + Worktree per task |
| Resource lock | `runtime/file_lock_manager.py` | `allowed_paths` serializes same-file tasks |
| Git Merge | `task_graph.py:merge` | Auto-merge back to base_branch after commit |
| Merge Agent | `workers/merge_agent.py` | Auto-resolve conflicts |
| **SQLite state machine** | `runtime/task_store.py` | Task status, Lease, Heartbeat, Attempt, Checkpoint |
| **Crash recovery** | `runtime/recovery_manager.py` | Scans processing/ on startup, auto-requeues expired leases |
| **Failure retry** | `task_graph.py` diagnose/replan | Diagnose → Replan → Retry loop, up to max_attempts times |
| **Diagnosis Agent** | `workers/diagnoser.py` | Analyzes failure causes, outputs structured diagnosis |
| **Lease Fencing** | `task_graph.py:_verify_lease` | Verifies lease token before commit/merge, prevents double execution |
| **Task dependencies** | `runtime/worker_pool.py` | `depends_on` tasks scheduled only after prerequisites COMPLETED |
| **LangGraph Checkpoint** | `task_graph.py` | MemorySaver + thread_id, state recoverable after node exceptions |
| **Worktree cleanup** | `task_graph.py:_cleanup_task` | Removes worktree + agent/* branches after task completion |
| **Session cleanup** | workers/*.py | OpenCode sessions deleted immediately after use |
| **Logging system** | `log.py` | Dual output to stdout + `logs/agent.log` |

## Directory Structure

Source code and runtime data are physically separated: `langgraph-opencode/` contains only source code and configuration;
all runtime working directories (prompts/processing/projects/logs/SQLite DB/doc inbox, etc.) are uniformly placed under
`data_root` (default `../runner-data/`, overridable via `config.yaml`'s `data_root` or the `RUNNER_DATA_ROOT` environment variable).

```text
langgraph-opencode/              # Source + config (no runtime data)
├── config.yaml          # Projects + retry + recovery config + data_root
├── config.py            # Config loading + DATA_ROOT path resolution
├── watcher.py           # Entry point (polling + recovery + Worker Pool)
├── planner.py           # Planner entry (requirement .md → task .md)
├── task_graph.py        # Per-task LangGraph (with retry loop + fencing + checkpoint)
├── opencode_client.py   # OpenCode Server HTTP client
├── worktree.py          # Git Worktree + Merge wrapper
├── setup_demo_repo.py   # Initialize demo Git repositories
├── log.py               # Logging (stdout + <data_root>/logs/agent.log)
├── runtime/             # .py source only (tasks.db is under data_root)
│   ├── watcher.py       # Scan + atomic claim
│   ├── task_resolver.py # Parse front-matter (project ID or path)
│   ├── task_store.py    # SQLite task state storage
│   ├── recovery_manager.py  # Crash recovery + heartbeat
│   ├── file_lock_manager.py # Resource lock
│   ├── worker_pool.py   # Thread pool + lock/dependency-aware + Lease
│   └── assets.py        # Project asset path resolution
├── workers/
│   ├── opencode_worker.py  # Invoke OpenCode in worktree
│   ├── merge_agent.py      # Conflict resolution
│   ├── diagnoser.py        # Failure diagnosis
│   └── requirement_closure.py  # Final requirement review + supplementary planning
├── schemas/
│   └── task.py          # Task (with allowed_paths, depends_on, simulate_failure)
├── scripts/
│   └── package_code.py  # Source packaging (see "Source Packaging")
├── dashboard.py         # Web Dashboard entry (FastAPI read-only + SSE)
├── web/                 # Dashboard static pages (index/graph/tasks/task.html)
├── requirement_run.py   # Requirement Run management (run_id/snapshot/plan.json)
├── requirement_closure.py  # Final requirement review CLI (Requirement Closure)
├── architecture_init.py    # Architecture init task generation CLI (--project/--all/--list)
├── architecture/           # Architecture asset management (p10)
│   ├── manager.py          # Orchestration: revision → JSON → validate → render → persist
│   ├── archify_runner.py   # Archify CLI wrapper (validate/render + no-tool fallback)
│   ├── repository.py       # projects/<id>/architecture/ layout + snapshot archiving
│   └── schemas.py          # ArchitectureSnapshot
├── document_run.py     # Document pipeline entry (p11)
├── document_import.py  # P13 historical directory import (--all --run one-step / --plan-only + --apply two-step)
├── document_validate.py # P13 single-document code validation and stale reconciliation
├── document_summarize.py # P13 existing archive topic summary refresh
├── document_graph.py   # Document pipeline LangGraph (SCAN→…→REPORT 7 nodes)
├── document/           # Document organizer pipeline implementation (p11)
│   ├── scanner.py      # SCAN: scan doc/ + atomic claim + crash recovery
│   ├── extractor.py    # EXTRACT: SHA256 + front matter + content extraction
│   ├── classifier.py   # CLASSIFY: rules first, LLM judgment when ambiguous
│   ├── planner.py      # PLAN: structured DocumentPlan + hash dedup
│   ├── executor.py     # ARCHIVE: pure Python executor + MANIFEST append
│   ├── importer.py     # P13: source root / frozen plan / hash-fenced copy
│   ├── validator.py    # P13: claim extraction + specified Git revision code evidence
│   ├── consolidator.py # P13: normalized dedup, similarity relations, topic summary draft
│   ├── indexer.py      # INDEX: regenerate project INDEX.md
│   └── reporter.py     # REPORT: organize report
├── doc/                # Document input directory (p11; review/ is manual confirmation area)
├── projects/           # Runner control-plane project assets
│   ├── <stable-project-id>/
│   │   ├── reports/
│   │   ├── architecture/
│   │   ├── worktrees/
│   │   ├── requirement-runs/
│   │   └── documents/      # MANIFEST/INDEX + <category>/
│   └── _runs/              # Cross-project run snapshots and reports
├── prompts/ processing/ processed/ failed/
├── logs/                # agent.log
├── runtime/tasks.db     # SQLite state database
├── worktrees/           # Legacy worktree root (migration read-only)
└── main.py / graph.py   # P1 entry
```

> The above `doc/ projects/ prompts/ processing/ processed/ failed/ logs/ runtime/tasks.db`
> are all under `data_root` (default `../runner-data/`), not mixed with source code.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_demo_repo.py
```

> See "Quick Start" above for detailed steps.

## Task File Format

```markdown
---
project: demo
allowed_paths:
  - README.md
depends_on:
  - task-001-setup
simulate_failure: 2
---

# Task: Some Task

Please complete the following tasks:
1. ...

## Acceptance Criteria
- ...
```

Field descriptions:

| Field | Required | Description |
|------|------|------|
| `project` | Yes | Project ID in config.yaml |
| `allowed_paths` | No | Declares modification scope; tasks touching the same file are auto-serialized |
| `depends_on` | No | List of dependency task IDs; scheduled only after prerequisite tasks COMPLETED |
| `simulate_failure` | No | dry-run test: simulate failure for first N executions (0 = no simulation) |

## Running

```powershell
# Process queue and wait for all to complete
.\.venv\Scripts\python.exe watcher.py --mode dry-run --once

# Continuous watch
.\.venv\Scripts\python.exe watcher.py --mode dry-run

# Skip startup recovery scan
.\.venv\Scripts\python.exe watcher.py --mode dry-run --once --no-recovery

# Connect to OpenCode Server
opencode serve --hostname 127.0.0.1 --port 4096
.\.venv\Scripts\python.exe watcher.py --mode opencode

# P1 still available
.\.venv\Scripts\python.exe main.py --mode dry-run
```

## Planner: Generate Tasks from Requirements

`planner.py` takes a high-level requirement `.md` file, splits it into several independently executable task `.md` files written to `prompts/`.

```powershell
# dry-run: generates 3 test tasks with dependency chain (setup → implement → docs)
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo --mode dry-run

# opencode: calls OpenCode Server to analyze requirements with LLM, outputs structured tasks (with allowed_paths + depends_on)
opencode serve --port 4096
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo --mode opencode
```

| Mode | Task Source | Dependency Chain | External Dependencies |
|------|---------|--------|---------|
| `dry-run` | 3 hardcoded tasks | setup → implement → docs | None |
| `opencode` | LLM analyzes requirements to generate | LLM auto-derived | Requires `opencode serve` + AI model |

Generated task files automatically include front-matter (`project`, `allowed_paths`, `depends_on`, `simulate_failure`) and can be consumed directly by `watcher.py`.

**Global sequential numbering**: Each batch of generated tasks automatically continues from the historical maximum number (scanning `prompts/`, `processing/`, `processed/`, `failed/`, `reports/` + SQLite); `depends_on` references are rewritten synchronously, never colliding with historical tasks:

```text
Batch 1: task-001..005   →  Batch 2: task-006..010   →  Batch 3: task-011..014 ...
```

## Task Execution Report

After each task execution, a report is auto-generated at
`projects/<stable-project-id>/reports/<task_id>.md`, fully recording OpenCode
feedback and suggestions (traceable even after session deletion):

```markdown
## Task  (2026-09-11 22:01:06)          ← Task metadata (project/repo/branch/dependencies)
## Execute attempt 1 — OpenCode response ← OpenCode raw response
                                          (STATUS / CHANGED_FILES / SUMMARY / TESTS / ISSUES)
## Validate attempt 1                    ← Validation results
## Diagnosis attempt 1                   ← Failure diagnosis (root cause/affected files/suggestions/retryable)
## Commit                                ← commit sha
## Merge                                 ← Merge result (clean / conflicts and resolution)
## FINAL — SUCCESS / FAILED              ← Final state + reason
```

Additionally, the SQLite `attempts` table has a `response` column (auto-migrated); you can query history with SQL:

```sql
SELECT attempt, status, failure_type, response FROM attempts WHERE task_id = 'task-001-xxx';
```

## Project Path Support

### Storage Boundary (Unified Convention)

`--project` indicates "which project this task belongs to and where the code repository is," not "write the Runner's
own runtime materials into that repository." The unified rules are:

| Content | Storage Location |
|------|----------|
| Runner project-level assets (reports, architecture, worktrees, requirement run indexes, archived materials) | `projects/<stable-project-id>/` |
| Cross-project run snapshots and summary reports | `projects/_runs/` |
| Global scheduling queue | `prompts/`, `processing/`, `processed/`, `failed/` |
| Materials to be organized and manual confirmation area | `doc/`, `doc/review/` |
| Explicit code task deliverables like README, project `docs/`, etc. | The business repository pointed to by `--project`, subject to `allowed_paths` and Git gate constraints |

Therefore, auto-archiving, architecture analysis, and execution reports do not pollute the business repository;
only business documents explicitly required by the task are committed with business code.

The `--project` parameter and the `project` field in task front-matter both support **two formats**:

1. **Project ID**: An ID configured in config.yaml (can customize branch, test commands, agent/model)
2. **Repository path**: Directly points to a Git repository (absolute or relative path), no config.yaml registration needed

```powershell
# Method 1: config.yaml project ID
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo

# Method 2: Direct repository path (absolute path, supports forward/back slashes)
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project D:/Workspace/new-api

# Method 3: Relative path (relative to runner root directory)
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo-repo-2
```

### Multi-project (Cross-repository Requirements)

When requirements span multiple repositories, pass multiple projects comma-separated to `--project`; planner (opencode mode)
has the LLM annotate each task's project; dry-run mode assigns in round-robin:

```powershell
.\.venv\Scripts\python.exe planner.py --requirement res_score.md --project "D:/Workspace/Talen,D:/Workspace/resource" --mode opencode
```

- Each task file has its own `project` front-matter → executes in the worktree of the corresponding repository
- `depends_on` supports cross-project dependencies (dependency checking is based on SQLite state, project-agnostic)
- When the LLM declares a project not in the `--project` list, it falls back to the first project (LLM-invented paths are not trusted)
- Each task belongs to only **one** project; cross-repository changes prompt the LLM to split into multiple tasks

Or write the path directly in the task file front-matter:

```markdown
---
project: D:/Workspace/new-api
---
```

**Resolution rules** (`config.py:get_project`):

| Priority | Rule | Description |
|--------|------|------|
| 1 | config.yaml project ID | When ID and path share a name, config takes priority |
| 2 | Filesystem path | Takes effect if directory exists; default branch auto-detected (current HEAD) |

**How OpenCode reads path projects**: After creating a worktree for each task at
`projects/<stable-project-id>/worktrees/<task_id>/`, an OpenCode session is created with
`directory=<worktree>` (`workers/opencode_worker.py:80`), and the OpenCode Server reads the project in that directory
to execute the task—regardless of project source (config or path).

Notes:
- Path projects use **directory name + normalized absolute path hash** as the stable project ID to avoid
  state, lock, and asset collisions between two same-named directories; config-registered projects use the config ID
- Path projects have empty `test.command`, `agent`, `model` (use defaults); configure via config.yaml if needed
- Paths in front-matter should use **forward slashes** (`D:/xxx`); planner auto-normalizes on generation

## dry-run vs opencode

| | dry-run | opencode |
|---|---|---|
| Code changes | Local simulation (append to README.md) | Calls OpenCode Server HTTP API |
| worktree/commit/merge | Real execution | Real execution |
| External dependencies | None | Requires `opencode serve --port 4096` |
| Use case | Validate scheduling/recovery/merge/retry full pipeline | Real coding tasks |

## Crash Recovery Flow

```text
Runner startup
    │
    ▼
Scan processing/*.md
    │
    ├── No tasks → normal startup
    │
    └── Has tasks → check SQLite lease
                    │
                    ├── heartbeat not expired → skip (worker still alive)
                    │
                    └── heartbeat expired → move back to prompts/ → re-execute
```

## Failure Retry Flow

```text
EXECUTE → VALIDATE
              │
         ┌────┴────┐
       success   failure
         │         │
       COMMIT   DIAGNOSE
         │         │
       MERGE     REPLAN
         │         │
        ...   attempt < max_attempts?
                   │
              ┌────┴────┐
             yes        no
              │          │
           EXECUTE    FAILED → failed/
```

## Configuration

| Config | Source | Default | Description |
|------|------|--------|------|
| `worker.max_workers` | config.yaml | `3` | Parallel worker count |
| `worker.poll_interval` | config.yaml | `2` | Polling interval (seconds) |
| `retry.max_attempts` | config.yaml | `3` | Max retry attempts |
| `retry.backoff_seconds` | config.yaml | `10` | Retry backoff time (seconds) |
| `recovery.lease_timeout` | config.yaml | `300` | Lease timeout (seconds) |
| `recovery.heartbeat_interval` | config.yaml | `30` | Heartbeat interval (seconds) |
| `opencode.base_url` | config.yaml | `http://127.0.0.1:4096` | OpenCode Server |
| `projects.<id>.*` | config.yaml | — | Project path, branch, test commands, agent/model |
| `project` | prompt front-matter | Required | Project ID **or** repository path |
| `allowed_paths` | prompt front-matter | None | Resource lock scope |
| `depends_on` | prompt front-matter | None | Prerequisite task ID list |
| `simulate_failure` | prompt front-matter | `0` | dry-run simulated failure count |

## Implemented Capabilities

| Phase | Capability | Verified |
|------|------|------|
| P1 | Fixed Planner + parallel + Review | ✅ |
| P2 | Folder watch + preemptive claim | ✅ |
| P3 | Multi-project (config.yaml + front-matter) | ✅ |
| P4-L1 | Worktree isolation | ✅ |
| P4-L2 | Resource lock (allowed_paths serialization) | ✅ |
| P4-L3 | Git Merge (auto-merge back to base_branch) | ✅ |
| P4-L4 | Merge Agent (auto-resolve conflicts) | ✅ |
| P5-Crash recovery | SQLite + Lease/Heartbeat + startup recovery | ✅ |
| P5-Failure retry | Diagnose→Replan→Retry loop | ✅ |
| P7-Planner | Generate task files from requirements (with dependency chain) | ✅ |
| P7-Lease Fencing | Verify lease token before commit/merge | ✅ |
| P7-Task dependencies | depends_on + dependency-aware scheduling | ✅ |
| P7-Checkpoint | LangGraph MemorySaver + thread_id | ✅ |
| P7-OpenCode API | Full-endpoint validation against real server 1.18.30 | ✅ |
| P11 | Document organizer pipeline (7 nodes + SHA256 dedup + review safety net + MANIFEST/INDEX) | ✅ |
| P12 | Runtime assets unified archiving by project, migration records, audit and recovery hardening | ✅ |
| P13 | Multi-source historical read-only inventory, code validation, revision drafts and topic summarization | ✅ |

## OpenCode AI Model Configuration

OpenCode 1.18.30 works **out of the box**, no `opencode auth login` needed (built-in free provider):

```powershell
opencode models          # List available models (opencode/* are free)
opencode auth list       # List configured credentials (can be 0)
opencode serve --hostname 127.0.0.1 --port 4096
```

Key API notes (empirically verified):

1. **Default model**: `send_message` without `model` uses the server default model
2. **Specifying model**: Must be object format (client auto-converts `"provider/model"` strings)
3. **Response format**: `POST /session/:id/message` returns `{"info": ..., "parts": [...]}`; actual text is in parts where `type == "text"`
4. **Directory binding**: `POST /session?directory=<path>` makes OpenCode read the project at that path (basis for worktree isolation)

## Still To Be Solved

| # | Issue | Priority |
|---|------|--------|
| 1 | MemorySaver → SqliteSaver (checkpoint recovery after process restart) | Medium |

## Final Requirement Closure Review (Requirement Closure)

"All tasks COMPLETED" ≠ "original requirement fulfilled." Requirement Closure re-compares five types of evidence
after all tasks are executed, tested, committed, and merged, to determine whether the requirement is truly closed:

```text
Original requirement vs task plan vs task reports vs actual Git changes vs final test results
```

### Flow

```text
planner.py (creates Requirement Run: run_id + requirement snapshot + base revision)
    ↓
watcher.py --once (executes all tasks)
    ↓
All tasks COMPLETED
    ↓
Final Requirement Reviewer (auto-triggered)
    ↓
┌────────────┬─────────────┬─────────────┐
COMPLETE   PARTIAL/FAILED   BLOCKED
    ↓           ↓               ↓
Run closed  Generate supplementary tasks  Wait for human
              → prompts/ → execute → re-review (≤ max_rounds rounds)
```

### Review Content

| Dimension | Check |
|------|------|
| Requirement coverage | Original requirement split into R-001…R-NNN, each item given status + evidence level (A~E) |
| Code evidence | Agent-claimed CHANGED_FILES vs actual Git diff (AGENT_DIFF_MISMATCH) |
| Cross-task integration | Interface/field/state/call-chain consistency |
| Regression risk | Old logic breakage, compatibility path omissions, unrelated changes |
| Final tests | Run project `test.command` before review (`final_review.require_tests`) |

### Artifacts

```text
reports/<run_id>-final-review-r<N>.md    # Human-readable (requirement item table/risk table/supplementary tasks)
reports/<run_id>-final-review-r<N>.json  # Structured (coverage/requirements/risks/followup_tasks)
runtime/requirement_runs/<run_id>/       # Requirement snapshot + plan.json (with base revisions)
SQLite: requirement_runs / requirement_reviews tables
```

### Usage

```powershell
# 1. Plan (auto-creates Run)
.\.venv\Scripts\python.exe planner.py --requirement ..\res.md --project D:\Workspace\resource --mode opencode

# 2. Execute + auto-trigger final review (final_review.enabled: true)
.\.venv\Scripts\python.exe watcher.py --mode opencode --once

# Or manually/standalone review
.\.venv\Scripts\python.exe requirement_closure.py --list
.\.venv\Scripts\python.exe requirement_closure.py --run-id run-20260912-103000 --mode opencode
.\.venv\Scripts\python.exe requirement_closure.py --requirement ..\res.md --project D:\Workspace\resource --mode opencode
```

Review conclusions: `COMPLETE` / `PARTIAL` / `FAILED` / `BLOCKED` / `RISK_ACCEPTED`. When the conclusion is non-complete
and `auto_replan: true`, supplementary tasks are auto-generated (with `run_id`, globally incrementing numbering,
no full batch regeneration).

### Configuration

```yaml
final_review:
  enabled: true          # Auto-trigger after watcher --once completes
  auto_replan: true      # Auto-generate supplementary tasks on PARTIAL/FAILED
  max_rounds: 3          # Max closure rounds
  require_tests: true    # Execute project test.command before review
  fail_on_high_risk: true  # Downgrade handling when COMPLETE but high risk exists
  reviewer_agent: plan
  reviewer_model: ""
```

> dry-run testing: `CLOSURE_SIMULATE=partial` environment variable makes the first review simulate PARTIAL,
> enabling end-to-end validation of supplementary task → execute → re-review closure.

## Dashboard Visualization

`dashboard.py` provides a read-only Web UI (FastAPI + build-free static pages), visualizing task status,
dependency graphs, execution history, and OpenCode feedback:

```powershell
.\.venv\Scripts\python.exe dashboard.py            # http://127.0.0.1:8080
.\.venv\Scripts\python.exe dashboard.py --port 9000
```

| Page | Path | Content |
|------|------|------|
| Overview | `/` | Stat cards, running tasks (checkpoint/heartbeat liveness), queue/project counts, recent event stream |
| Dependency graph | `/graph` | Task DAG (Mermaid), status coloring, cross-project dashed boxes, missing dependency red dashed lines |
| Task list | `/tasks` | All tasks, search + status/project filter |
| Task detail | `/task?id=…` | Metadata, execution timeline (log reconstruction), attempt history, report rendering (markdown + DOMPurify injection prevention) |

Features:

- **Real-time updates**: SSE (`/api/events/stream`) detects tasks table changes every 2s and pushes; frontend EventSource auto-reconnects
- **Read-only safe**: Dashboard only reads SQLite (WAL mode doesn't block watcher read/write); all writes remain with watcher
- **API independently usable**: `/api/summary`, `/api/tasks`, `/api/tasks/{id}`, `/api/tasks/{id}/report`, `/api/graph`, `/api/projects` (FastAPI built-in `/docs` interactive docs)
- Dependencies: `fastapi` + `uvicorn` (in requirements.txt); frontend libraries all via CDN

## Project Architecture Visualization (Archify Integration)

Archify is integrated as an `ARCHITECTURE_INIT` task type: analyze project code → generate architecture JSON IR →
archify validate → archify render → persist as architecture asset, displayed in dual views on the `/graph` page.

### Usage

#### Initialize a Project's Architecture (path specified directly, no config.yaml registration needed)

```powershell
# 1. Start OpenCode Server (needed for real LLM analysis; not needed for dry-run mode)
opencode serve --hostname 127.0.0.1 --port 4096

# 2. Generate architecture init task for project (--project passes repository path directly)
.\.venv\Scripts\python.exe architecture_init.py --project D:\Workspace\resource

# Can also use a config.yaml registered project ID
.\.venv\Scripts\python.exe architecture_init.py --project demo

# Batch initialize (projects with architecture.enabled: true in config.yaml)
.\.venv\Scripts\python.exe architecture_init.py --all

# View baseline status of each project
.\.venv\Scripts\python.exe architecture_init.py --list
```

The generated task file `prompts/arch-init-<project-name>.md` (path projects auto-write normalized path ref,
e.g. `D:/Workspace/resource`); projects with existing baselines are auto-skipped; `--force` forces regeneration.

#### Execute Analysis Task

```powershell
# Real LLM analysis (requires OpenCode Server online)
.\.venv\Scripts\python.exe watcher.py --mode opencode --once

# Or deterministic scan (no Server needed, directory-level components, for pipeline validation)
.\.venv\Scripts\python.exe watcher.py --mode dry-run --once
```

Execution flow: read-only repository analysis → LLM generates component/relationship topology → auto layering layout
(Kahn layering + verified connection direction patterns + label width fitting) → archify validate → archify render →
generate baseline. Zero changes to business repository (compares `git status` before and after analysis; rejects
snapshot if changed).

Failures auto-enter the Diagnose → Re-plan → Retry loop (up to `retry.max_attempts` times).

#### View Architecture Diagram

```powershell
# Start Dashboard (if not running)
.\.venv\Scripts\python.exe dashboard.py            # http://127.0.0.1:8080
```

Open `http://127.0.0.1:8080/graph` in browser → switch view to "**Project Architecture Diagram**" → select project
(e.g. resource) → iframe displays Archify interactive architecture diagram (with revision, snapshot info, staleness warning).

#### Refresh Baseline (After Code Changes)

```powershell
.\.venv\Scripts\python.exe architecture_init.py --project D:\Workspace\resource --force
.\.venv\Scripts\python.exe watcher.py --mode opencode --once
```

After code advances, the Dashboard shows "⚠ Architecture diagram may not match current code version
(snapshot xxx · current yyy)"; `--force` re-analyzes, old baseline auto-archived to `snapshots/`.

#### Hand-written Task File (Optional)

```yaml
---
type: ARCHITECTURE_INIT
project: "D:/Workspace/resource"
depends_on: []
---
```

Make regular tasks `depends_on` it to ensure "no architecture baseline, no code changes begin."

### Key Mechanisms

| Mechanism | Description |
|------|------|
| Task type | front-matter `type: ARCHITECTURE_INIT` (default `CODE_CHANGE`) |
| Read-only analysis | No worktree, no commit, no merge; checks `git status` before and after analysis; rejects snapshot if business repository changed |
| Separation of concerns | **LLM only defines topology** (components + relationships), **system computes geometry** (layering layout/connection directions/label widths/viewBox)—LLM excels at architecture semantics, not pixel layout |
| Output sanitization | `_normalize_ir`: field whitelist (boundary only allows kind/label/wraps, etc.), type enum correction, viewBox fix—mechanical JSON errors no longer consume LLM retries |
| Auto layout | Kahn layering (cycle-safe) + 4 verified connection direction patterns + cross-layer edges via vertex routing + adjacent layer gap anchors + label/sublabel width fitting + viewBox coverage calculation |
| Two-layer validation | archify validate (structure/layout rules) + artifact integrity (json/html/metadata/revision) |
| Failure retry | Uses existing Diagnose → Re-plan → Retry loop (does not go directly to failed/) |
| Architecture lock | While a project's arch-init is running/queued, CODE_CHANGE tasks are suspended (p10 §17) |
| Asset persistence | `projects/<stable-project-id>/architecture/baseline/` (old snapshots archived to `snapshots/` on re-init) + SQLite `architecture_snapshots` table |
| Revision binding | Snapshot records `repository_revision`; page shows corresponding code version + staleness warning (§26.3) |
| Auto initialization | Projects with `architecture.auto_initialize: true` auto-generate arch-init tasks on watcher startup |
| Fallback mode | When archify CLI unavailable, uses built-in validation + Mermaid rendering (config `architecture.archify_entry` specifies tool path) |

### Dashboard

- `/graph` view switch: **Task Dependency Graph** (Mermaid) / **Project Architecture Diagram** (project + snapshot selector, iframe embeds Archify HTML, `sandbox="allow-scripts"`)
- API: `/api/architectures`, `/api/architectures/{project_id}`, `/api/architectures/{project_id}/{snapshot_id}/html|json|svg` (DB mapping + root directory containment check, prevents path traversal); `/api/projects` already includes architecture availability field

### Configuration

```yaml
projects:
  new-api:
    path: D:/Workspace/new-api
    architecture:
      enabled: true            # architecture_init.py --all will process
      auto_initialize: true    # Auto-generate arch-init task on watcher startup (when no baseline)
      max_core_nodes: 12
      include: [service, controller]   # Optional: only analyze these top-level directories
      exclude: [vendor, node_modules]

architecture:                   # Global
  default_snapshot: baseline
  archify_command: node
  archify_entry: ""             # Default uses local ~/.agents/skills/archify
  validation: { enabled: true }
  rendering: { enabled: true }
```

> Path-specified projects (`--project D:\Workspace\resource`) can initialize without registration;
> project-level tuning like include/exclude/max_core_nodes requires config.yaml entry.

## Document Organizer Pipeline (Document Organizer, p11)

Drop documents into `doc/` for automatic processing: identify project → determine category → SHA256 dedup →
archive to `projects/<stable-project-id>/documents/<category>/` → update `INDEX.md` / `MANIFEST.json` →
generate organize report. The `doc/` here is just the Runner's inbox; `--project` only selects the owning project
and does not write archived materials into the target business repository.

```powershell
# dry-run (pure rules, no OpenCode Server needed)
.\.venv\Scripts\python.exe document_run.py --mode dry-run

# Connect to OpenCode Server (LLM semantic judgment for ambiguous documents)
opencode serve --hostname 127.0.0.1 --port 4096
.\.venv\Scripts\python.exe document_run.py --mode opencode

# Continuous watch of doc/
.\.venv\Scripts\python.exe document_run.py --mode dry-run --watch

# Force specify project (config.yaml project ID or repository path), skip project identification, confidence 1.0
.\.venv\Scripts\python.exe document_run.py --mode dry-run --project new-api
```

### Pipeline (7 nodes, LangGraph)

```text
SCAN → EXTRACT → CLASSIFY → PLAN → ARCHIVE → INDEX → REPORT
```

| Node | Implementation | Description |
|------|------|------|
| SCAN | `document/scanner.py` | Scans top-level of `doc/` (`review/` subdirectory never re-scanned), atomically moves to `processing/documents/`; crash recovery: moved back to `doc/` on next startup |
| EXTRACT | `document/extractor.py` | SHA256 + front matter + content extraction (md/txt direct read; docx/pptx/xlsx stdlib zip unpacking; pdf requires pypdf), multi-threaded parallel |
| CLASSIFY | `document/classifier.py` | Rules first (front matter `project` > `--project` forced > filename project ID/keywords > content keywords), LLM judgment when confidence insufficient and opencode mode (can only select from candidate projects) |
| PLAN | `document/planner.py` | Generates structured DocumentPlan; SHA256 hit in any project MANIFEST (or same batch) → `skip_duplicate`; confidence < threshold → `review` |
| ARCHIVE | `document/executor.py` | **Pure Python executor**: persist journal → move → hash verify → MANIFEST/audit → commit; restart can reconcile |
| INDEX | `document/indexer.py` | Regenerates `projects/<id>/documents/INDEX.md` from MANIFEST.json (grouped by fixed taxonomy) |
| REPORT | `document/reporter.py` | `projects/_runs/reports/doc-organize-<timestamp>.md` (overview/by-project archive/duplicates/pending manual confirmation/failures) |

> As per p11 §7, non-conflicting documents process in parallel — parallelism occurs within EXTRACT/CLASSIFY/ARCHIVE
> nodes (worker thread pool); the graph remains linear.

### Directories and Artifacts

```text
doc/                          # Input: drop files here
doc/review/                   # Documents with confidence < threshold await manual confirmation
processing/documents/         # Processing (auto-moved back to doc/ on crash recovery next startup)
processed/documents/          # Audit records (<filename>-<sha8>.json)
projects/<id>/documents/      # Archive target: MANIFEST.json + INDEX.md + <category>/
projects/_runs/reports/doc-organize-*.md  # Organize report
```

### Safety Boundary (p11 §2: LLM plans, program executes)

- **LLM only produces classification conclusions** (project/category/confidence); all file moves executed by Python Executor
- **confidence < `documents.confidence_threshold` (default 0.8) → archiving prohibited**, goes to `doc/review/`
- **Original files never deleted**; all moves write audit records (`processed/documents/`), rollbackable
- **SHA256 dedup**: hit MANIFEST.json → `skip_duplicate`, no `_1`/`_final` copies generated
- Re-verify SHA256 after archiving; mismatch is judged as failure
- Fixed category taxonomy: `requirements/design/architecture/research/meeting/api/task/report/decision/archive`
- P13 extended categories: `implementation/validation`

### Configuration

```yaml
documents:
  input_dir: ./doc
  confidence_threshold: 0.8
  max_workers: 4
  content_preview_chars: 4000

projects:
  new-api:
    path: D:/Workspace/new-api
    keywords: [new-api, traffic-pool, token, channel]   # Rule match keywords (p11 §5)
```

Specify project directly in front matter (highest priority, confidence 1.0):

```markdown
---
project: new-api
---
# Traffic Pool Solution
...
```

`--project` CLI forced specification (applies to all documents in this run, same priority as front matter;
supports config.yaml project ID or repository path, same resolution rules as `planner.py --project`);
category still determined by rules, but **not subject to confidence threshold**, won't go to `doc/review/`:

```powershell
python document_run.py --project D:/Workspace/new-api   # Path projects use "directory name + path hash" stable ID
```

Re-queue after manual confirmation or reclassification:

```powershell
python document_review.py --file pending.md --project demo --category requirements
python document_run.py --mode dry-run
```

Dashboard listens on localhost by default. Binding to other addresses requires a Bearer Token:

```powershell
python dashboard.py --host 0.0.0.0 --token <strong-token>
```

See `skills/document-organizer/SKILL.md` for Agent responsibility boundaries.

## Historical Document Import, Code Validation and Summarization (P13)

P13 targets historical sources explicitly specified by the user, such as desktops, old projects, backup drives,
and shared drive mount directories. Source directories are always read-only; the planning phase does not write to
DB or formal assets; Apply defaults to copy-only, no moving or deleting original files.

### Action Switches

Each source can independently enable actions:

| Switch | Effect | Artifacts |
|------|------|------|
| `--all` | Enables all four actions below at once (`--no-all` disables all at once) | — |
| `validate` | Extracts claims from documents, compares against the corresponding project's committed Git revision | claims, code evidence, validation status |
| `repair` | Generates revision drafts based on validation evidence; must be used with validate | `_derived/repairs/<source-id>/` |
| `consolidate` | Normalized dedup, similarity relations, and same-project/same-topic summarization | `_derived/topics/<topic>/SUMMARY.md` |
| `merge` | Folder-level merge: reads all documents in the same source folder, **based on current code analysis** merges into one comprehensive document (main document archived byte-for-byte, other members folded as `MERGED`); must be used with validate | `<source-folder>-comprehensive-analysis-merged.md` (or `--merge-output` specified) |

Safe defaults have all four actions disabled. Configuration priority is fixed:

```text
CLI > source-specific config > profile > defaults
```

`repair` supports:

- `annotate`: Does not rewrite original text; generates errata and code evidence;
- `rewrite-draft`: Generates structured revision drafts.

Both revision products are `DRAFT` and will not overwrite the original text or write directly to the business repository.
When "should/must" requirements in the document are not yet implemented, they are marked as `not_implemented` and
will not be rewritten to current code behavior.

Safety boundaries of `merge`:

- **Merge scope = one source folder + one project**; documents across folders or projects are not merged;
- The main document (first in plan order) is archived byte-for-byte through the normal pipeline (hash verify + MANIFEST);
  folded members are not copied separately, `merged_into` points to the main document's archive path;
- The merged document is a comprehensive document **based on current code analysis** and **does not use**
  `verified`/`partially_verified`/`not_implemented`/`unverifiable` validation status labels;
  code evidence is listed as facts in the form "document claim → code location (file:line)";
- In `--mode opencode`, the LLM reads all documents and synthesizes a coherent analysis with code evidence;
  in `--mode dry-run`, deterministic merge (source list + code basis + each document body);
- The merged document is written next to the source folder by default:
  `<source-folder>-comprehensive-analysis-merged.md`;
  `--merge-output <path>` can specify any location (e.g. desktop); the output path is frozen at the planning stage;
  writing into a read-only source directory is rejected;
- If the main document fails to archive, the group is not folded, marked `REVIEW_REQUIRED`;
  source files are never moved or deleted;
- `merge=true` with `validate=false` errors directly; `--plan-only` plan JSON includes
  `merge_groups` preview (grouping, main document selection, and output path frozen at planning stage).

#### Example: Merging Desktop "service-quality" into One Comprehensive Document

```powershell
# Read all files under Desktop "service-quality", analyze and merge based on current code (Talen),
# write to Desktop "service-quality-comprehensive-analysis-merged.md" (default output location)
python document_import.py `
  --source C:/Users/Administrator/Desktop/service-quality `
  --project D:/Workspace/Talen `
  --merge --validate --mode opencode --run

# Can also explicitly specify output filename/location
python document_import.py `
  --source C:/Users/Administrator/Desktop/service-quality `
  --project D:/Workspace/Talen `
  --merge --validate --mode opencode `
  --merge-output "C:/Users/Administrator/Desktop/service-quality-comprehensive-analysis-merged.md" `
  --run
```

The run result JSON's `merged_documents` lists each merged document's path, member count, and generation method
(`generation: llm` or `rules`).

### One-step Run (--all --run)

```powershell
# Inventory → freeze plan → code validation → revision draft → topic summarization → merge archive → copy-only publish, all in one step
python document_import.py `
  --source D:/Archive/old-resource `
  --project D:/Workspace/resource `
  --all --run
```

- `--all` = `--validate --repair --consolidate --merge` enables all four actions at once;
  individual action switches can still override (e.g. `--all --no-repair --no-merge` only validates and consolidates)
- For code validation merge only (no revision/consolidation), use `--validate --merge --mode opencode --run`;
  the run result JSON lists each merged document's path (`merged_documents`)
- `--run` builds the frozen plan in-process then immediately Applies; Apply still recalculates
  each source file's SHA256, rejecting files that changed after planning
- The plan is still persisted to `projects/_runs/imports/<import-run-id>/plan.json`, traceable

Can also be done in two steps (preview first, then Apply):

```powershell
# Read-only inventory; JSON output saved by shell to a user-specified plan file
python document_import.py `
  --source D:/Archive/old-resource `
  --project D:/Workspace/resource `
  --all --plan-only | Set-Content -Encoding utf8 p13-import-plan.json

# Apply recalculates each source file's SHA256; rejects files changed after planning
python document_import.py --apply p13-import-plan.json
```

Multiple sources can be imported together:

```powershell
python document_import.py `
  --source D:/Archive/design `
  --source E:/Backup/project-docs `
  --project resource `
  --all --run
```

Different profiles can also be specified for different sources:

```powershell
python document_import.py `
  --source-profile D:/Archive/implementation=historical-implementation `
  --source-profile E:/Backup/requirements=historical-requirements `
  --project resource --run
```

### Configuration

```yaml
documents:
  imports:
    defaults:
      recursive: true
      copy_source: true
      actions: { validate: false, repair: false, consolidate: false, merge: false }
      repair_mode: annotate
      review_required: true

    profiles:
      historical-implementation:
        actions: { validate: true, repair: true, consolidate: true, merge: true }
        repair_mode: rewrite-draft
      historical-requirements:
        actions: { validate: true, repair: false, consolidate: true, merge: false }

    # Optional: specify via CLI --source when sources not written
    sources:
      - path: D:/Archive/old-resource
        profile: historical-implementation
        project: D:/Workspace/resource

    exclude_dirs: [.git, .venv, node_modules, vendor, dist, build, __pycache__]
    max_files: 10000
    max_file_bytes: 52428800
    max_total_bytes: 2147483648
```

CLI `--all` (enables validate + repair + consolidate + merge at once),
`--validate/--no-validate`, `--repair/--no-repair`,
`--consolidate/--no-consolidate` and `--merge/--no-merge` override source profiles;
individual action switches take priority over `--all`. `repair=true` or `merge=true` with
`validate=false` errors directly at the planning stage.

### Pipeline and Artifacts

```text
DISCOVER → EXTRACT → CLASSIFY → EXACT_DEDUP
  ├─ validate → CLAIM_EXTRACT → CODE_VALIDATE → repair → REPAIR_DRAFT
  ├─ consolidate → CLUSTER → SUMMARY_DRAFT
  ├─ merge → MERGE_GROUP (frozen grouping by source folder) → main document archive + others folded (MERGED) → code analysis merge → comprehensive document (--mode opencode synthesized by LLM)
  └─ no action → archive plan only
  → REVIEW → COPY/PUBLISH → INDEX/REPORT
```

```text
projects/<id>/documents/
├── MANIFEST.json
├── requirements/ design/ implementation/ validation/ ...
└── _derived/
    ├── validations/
    ├── repairs/<source-file-id>/
    ├── merged/<merge-id>/merge-metadata.json
    └── topics/<topic>/SUMMARY.md

<source-folder>-comprehensive-analysis-merged.md        # Merged document written next to source folder by default

projects/_runs/imports/<import-run-id>/
├── plan.json
├── inventory.json
└── report.md
```

Code validation defaults to committed `HEAD`, but `--revision <commit>` can specify a historical revision.
Validation uses read-only `git rev-parse/status/grep`, never checkout, commit, or modify the business repository.
Conclusions include `verified`, `partially_verified`, `not_implemented`, `unverifiable`;
after code changes, old revisions are retained in evidence for subsequent stale reconciliation.

Only exact SHA256 matches auto-dedup; identical normalized body text is only marked as
`normalized_duplicate` candidates; semantic similarity only establishes `similar` relations and generates summary drafts;
original text is always preserved. The Dashboard's `/operations` shows Import Run summaries; the API is:

```powershell
# No import, directly perform read-only code validation on a single document
python document_validate.py --project resource --file D:/Archive/design.md

# Query old revision evidence; add --mark-stale to write stale status
python document_validate.py --project resource --stale-only

# Default only previews topic members; add --apply to generate DRAFT SUMMARY
python document_summarize.py --project resource --topic resource-lifecycle
python document_summarize.py --project resource --topic resource-lifecycle --apply
```

- `GET /api/document-imports`
- `GET /api/document-imports/{import_run_id}`
- `GET /api/projects/{project_id}` includes imported documents and summaries

## Troubleshooting

| Symptom | Cause | Fix |
|------|------|------|
| `missing 'project' in front matter` | File has UTF-8 BOM or missing front-matter | BOM already handled (utf-8-sig); check the `---` block |
| `path is not a git repository (missing .git)` | `--project` points to a non-git directory | Verify path (e.g. `D:/Workspace/Talen` not `D:/Workspace`) |
| `crashed: Git command failed: ...` | Exception outside task graph (git/environment issue) | See full stack in `logs/agent.log`; task file auto-moves to `failed/` |
| Dependency task `cascade-failed` | Prerequisite task FAILED or its file disappeared | Fix prerequisite task, then move all related files in `failed/` back to `prompts/` and rerun |
| Task stuck in `processing/` with RUNNING status | Watcher process killed (lease not expired) | Wait for `lease_timeout` (default 300s) then restart watcher for auto-recovery; or reduce `recovery.lease_timeout` |
| `[MISSING .git]` warning | Demo repo in config.yaml not initialized | Run `setup_demo_repo.py`, or remove unused project config |
| `arch-init-*` rejected: `unknown project` | Task front-matter has bare directory name (not in config.yaml) | Fixed: path projects auto-write normalized path; rerun `architecture_init.py --project <path>` |
| `archify validate failed: ...` | LLM output or layout violates archify rules | Auto-handled by `_normalize_ir` + `_auto_layout`; still fails will auto-retry, moves to `failed/` after 3 times, can requeue |
| OpenCode 30 min/10 min Read timed out | Server hung or analysis task too large | Restart `opencode serve`; diagnose timeout reduced to 600s and failures don't crash task graph |
| Architecture diagram shows "⚠ may not match current code version" | Repository HEAD has advanced past snapshot revision | `architecture_init.py --project <path> --force` + watcher re-analysis |

General method to rerun failed tasks:

```powershell
# 1. Confirm failure reason
Get-Content .\logs\agent.log -Tail 50

# 2. Move tasks to rerun from failed/ back to queue
Move-Item .\failed\task-001-*.md .\prompts\

# 3. Re-execute
.\.venv\Scripts\python.exe watcher.py --mode opencode --once
```

## Source Packaging

`scripts/package_code.py` packages the current implementation source code into a single shareable text file
(~120KB), convenient for sending the entire runner to an LLM or others for review.

```powershell
# Default output to project root: langgraph-opencode-source-bundle.txt
.\.venv\Scripts\python.exe scripts\package_code.py

# Custom output location
.\.venv\Scripts\python.exe scripts\package_code.py --output D:\Temp\runner-code.txt

# Include demo repo READMEs
.\.venv\Scripts\python.exe scripts\package_code.py --with-demo-repos
```

**Includes** (27 files, implementation code only):

| Category | Files |
|------|------|
| Entry points | `watcher.py`, `planner.py`, `main.py`, `graph.py` |
| Core | `config.py`, `config.yaml`, `task_graph.py`, `opencode_client.py`, `worktree.py`, `log.py`, `setup_demo_repo.py` |
| runtime/ | `watcher`, `task_resolver`, `task_store`, `recovery_manager`, `file_lock_manager`, `worker_pool`, `__init__` |
| workers/ | `opencode_worker`, `merge_agent`, `diagnoser`, `__init__` |
| schemas/ | `task.py`, `__init__` |
| Docs/Meta | `requirements.txt`, `.gitignore`, `README.md` |

**Excludes** (runtime products and unrelated files):

```text
prompts/ processing/ processed/ failed/   # Task lifecycle directories
reports/                                  # Execution reports
worktrees/ logs/                          # Worktrees and logs
runtime/tasks.db                          # SQLite state database
.venv/ __pycache__/ .git/                 # Environment
oc_stdout.txt oc_stderr.txt               # Debug output
demo-repo/ demo-repo-2/                   # Demo repos (--with-demo-repos can include README)
```

Output format: file header (root directory/generation time/file count) + each file as a section
(separated by `#### FILE: <relative path>`), missing files output warnings.
