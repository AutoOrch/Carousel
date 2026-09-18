# 自动运行agent
给定一个需求文档，可以自动运行实现，架构如下:
- LangGraph 负责任务编排、状态管理、依赖调度、并行、重试和动态 Re-plan；
- OpenCode 负责真正进入代码仓库执行编码任务。Git Worktree 提供隔离。

## P12 完整追踪与可靠性

当前运行时以 SQLite 中的结构化记录作为状态与审计来源：

```text
Requirement Run → Plan → Task → Attempt → Event / Operation
             → Dependency → ChangeSet → Validation → Commit / Merge
             → Artifact → Review / Review Item
```

- Attempt 由数据库原子编号，重启和重新入队不会重置 retry budget；
- heartbeat 绑定 `worker_id + lease_id`，其他进程不能替失联任务续租；
- commit/merge 使用 operation journal，启动恢复会对照 Git ancestry；
- `allowed_paths`、实际 changed files 和项目测试命令是提交前硬门禁；
- 报告、架构、worktree、需求运行索引和归档文档统一存放在
  `projects/<stable-project-id>/`；这些 Runner 控制面资产不会写入业务仓库；
- `/runs` 可从 Requirement Run 下钻到 Task、Attempt、Event 和 Validation；
- `GET /api/runs/<run-id>/export?format=json|md` 或 `python -m runtime.audit_export <run-id>` 导出完整审计包；
- 旧任务首次启动时自动修复 Attempt 投影，并生成 `runtime/migration-p12-v1.json`；资产迁移记录在 `projects/_runs/asset-migration-p12.json`。

运行前检查：

```powershell
python doctor.py
python -m unittest discover -s tests -v
python maintenance.py --integrity --backup
python maintenance.py --cleanup --days 30       # 仅预览
python maintenance.py --cleanup --days 30 --apply
```

数据库 schema 升级前可先运行 `python maintenance.py --backup`；回滚时停止 watcher，恢复该 SQLite 备份后再启动并执行 `doctor.py`。

## 五个入口

| 入口 | 命令 | 说明 |
|------|------|------|
| **Planner** | `python planner.py --requirement spec.md --project demo` | 从需求 .md 生成 task .md 文件到 `prompts/`（含依赖链） |
| **P1** | `python main.py --mode dry-run` | 固定 Planner 硬编码 2 个任务，并行执行后 Review |
| **P2-P5** | `python watcher.py --mode dry-run [--once]` | 文件夹监听 → 多项目 → 资源锁 → 依赖调度 → 合并 → 崩溃恢复 → 重试循环 |
| **Document** | `python document_run.py --mode dry-run [--watch]` | 文档整理流水线（p11）：doc/ → 分类 → 去重 → 归档 → 索引 → 报告 |
| **Historical Import** | `python document_import.py --source <目录> --project <项目> --all --run` | P13：历史目录盘点 → 代码校验 → 修订草稿 → 归纳总结 → 合并归档（一步完成） |

## 阶段总览

- **P1（`main.py`）**：固定 Planner 生成两个任务，并行执行后 Review。
- **P2（`watcher.py`）**：文件夹监听，往 `prompts/` 丢 `.md` 任务即自动执行。
- **P3（多项目）**：任务通过 front-matter 指定 `project`，由 `config.yaml` 解析到真实仓库。
- **P4（冲突治理）**：资源锁 + 自动合并 + Merge Agent，安全处理多任务改同一文件。
- **P5（崩溃恢复 + 失败重试）**：SQLite 状态机 + Lease/Heartbeat + Diagnose → Re-plan → Retry 循环。
- **P7（生产加固）**：Planner 需求拆任务 + 任务依赖 (`depends_on`) + Lease Fencing + LangGraph Checkpoint + 日志系统 + Worktree/Session 清理 + OpenCode API 全端点验证。
- **P13（文档知识化）**：任意显式历史目录只读盘点、冻结计划、copy-only 导入、代码证据校验、修订草稿、主题归纳和近重复文档合并归档。

## 前置条件

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 用到 `match`、`X \| Y` 类型语法 |
| Git | 任意 | Worktree 隔离 + 合并 |
| OpenCode | 1.18+（可选） | 仅 `--mode opencode` 时需要，`--mode dry-run` 无需 |

## 快速开始（端到端）

```powershell
# 1. 创建虚拟环境 + 安装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 初始化演示 Git 仓库（demo-repo / demo-repo-2）
.\.venv\Scripts\python.exe setup_demo_repo.py

# 3. 从需求生成任务（dry-run 模式生成 3 个有依赖链的测试任务）
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo --mode dry-run
#    → prompts/task-001-setup.md  (allowed_paths: [README.md])
#    → prompts/task-002-implement.md (depends_on: [task-001-setup])
#    → prompts/task-003-docs.md  (depends_on: [task-002-implement])

# 4. 执行任务队列（按依赖顺序：001 → 002 → 003）
.\.venv\Scripts\python.exe watcher.py --mode dry-run --once
#    每任务：worktree → execute → validate → (retry loop) → commit → merge
#    完成：processed/   失败：failed/

# 5. 连接真实 OpenCode Server（可选）
opencode serve --hostname 127.0.0.1 --port 4096
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo --mode opencode
.\.venv\Scripts\python.exe watcher.py --mode opencode
```

> 不想用 Planner？直接手写 `.md` 任务文件丢进 `prompts/` 即可，见下方「任务文件格式」。

## P5 完整链路

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

## 四大闭环

| 闭环 | 解决的问题 | 实现 |
|------|-----------|------|
| **Execution Loop** | Agent 怎么执行任务 | LangGraph task_graph |
| **Recovery Loop** | Runner 崩溃后怎么恢复 | SQLite Lease/Heartbeat + RecoveryManager |
| **Retry/Re-plan Loop** | 任务失败后怎么自我修复 | Diagnose → Replan → Retry (max_attempts) |
| **Merge Loop** | 多 Agent 修改冲突后怎么合并 | FileLockManager + Merge Agent |

## 四层防护 + 崩溃恢复 + 重试 + 生产加固

| 机制 | 文件 | 说明 |
|------|------|------|
| Worktree 隔离 | `worktree.py` | 每 Task 独立 Branch + Worktree |
| 资源锁 | `runtime/file_lock_manager.py` | `allowed_paths` 串行化同文件任务 |
| Git Merge | `task_graph.py:merge` | commit 后自动 merge 回 base_branch |
| Merge Agent | `workers/merge_agent.py` | 冲突时自动解决 |
| **SQLite 状态机** | `runtime/task_store.py` | 任务状态、Lease、Heartbeat、Attempt、Checkpoint |
| **崩溃恢复** | `runtime/recovery_manager.py` | 启动时扫描 processing/，过期 lease 自动重新入队 |
| **失败重试** | `task_graph.py` diagnose/replan | Diagnose → Replan → Retry 循环，最多 max_attempts 次 |
| **Diagnosis Agent** | `workers/diagnoser.py` | 分析失败原因，输出结构化诊断 |
| **Lease Fencing** | `task_graph.py:_verify_lease` | commit/merge 前校验 lease token，防止双执行 |
| **任务依赖** | `runtime/worker_pool.py` | `depends_on` 前置任务 COMPLETED 后才调度 |
| **LangGraph Checkpoint** | `task_graph.py` | MemorySaver + thread_id，节点异常后状态可恢复 |
| **Worktree 清理** | `task_graph.py:_cleanup_task` | 任务完成后移除 worktree + agent/* 分支 |
| **Session 清理** | workers/*.py | OpenCode session 用完即删 |
| **日志系统** | `log.py` | stdout + `logs/agent.log` 双输出 |

## 目录结构

源码与运行时数据物理分离：`langgraph-opencode/` 只放源码和配置，
所有运行时工作目录（prompts/processing/projects/logs/SQLite DB/doc 收件箱等）
统一放在 `data_root`（默认 `../runner-data/`，可通过 `config.yaml` 的 `data_root`
或环境变量 `RUNNER_DATA_ROOT` 覆盖）。

```text
langgraph-opencode/              # 源码 + 配置（不含运行时数据）
├── config.yaml          # 项目 + 重试 + 恢复配置 + data_root
├── config.py            # 配置加载 + DATA_ROOT 路径解析
├── watcher.py           # 入口（轮询 + 恢复 + Worker Pool）
├── planner.py           # Planner 入口（需求 .md → task .md）
├── task_graph.py        # 单任务 LangGraph（含重试循环 + fencing + checkpoint）
├── opencode_client.py   # OpenCode Server HTTP 客户端
├── worktree.py          # Git Worktree + Merge 封装
├── setup_demo_repo.py   # 初始化演示 Git 仓库
├── log.py               # 日志（stdout + <data_root>/logs/agent.log）
├── runtime/             # 仅 .py 源码（tasks.db 在 data_root 下）
│   ├── watcher.py       # 扫描 + 原子抢占
│   ├── task_resolver.py # 解析 front-matter（project ID 或路径）
│   ├── task_store.py    # SQLite 任务状态存储
│   ├── recovery_manager.py  # 崩溃恢复 + 心跳
│   ├── file_lock_manager.py # 资源锁
│   ├── worker_pool.py   # 线程池 + 锁/依赖感知 + Lease
│   └── assets.py        # 项目资产路径解析
├── workers/
│   ├── opencode_worker.py  # 在 worktree 中调用 OpenCode
│   ├── merge_agent.py      # 冲突解决
│   ├── diagnoser.py        # 失败诊断
│   └── requirement_closure.py  # 最终需求审查 + 补充规划
├── schemas/
│   └── task.py          # Task（含 allowed_paths, depends_on, simulate_failure）
├── scripts/
│   └── package_code.py  # 源码打包（见「源码打包」）
├── dashboard.py         # Web Dashboard 入口（FastAPI 只读 + SSE）
├── web/                 # Dashboard 静态页面（index/graph/tasks/task.html）
├── requirement_run.py   # Requirement Run 管理（run_id/快照/plan.json）
├── requirement_closure.py  # 最终需求审查 CLI（Requirement Closure）
├── architecture_init.py    # 架构初始化任务生成 CLI（--project/--all/--list）
├── architecture/           # 架构资产管理（p10）
│   ├── manager.py          # 编排：revision → JSON → validate → render → 持久化
│   ├── archify_runner.py   # Archify CLI 包装（validate/render + 无工具降级）
│   ├── repository.py       # projects/<id>/architecture/ 布局 + 快照归档
│   └── schemas.py          # ArchitectureSnapshot
├── document_run.py     # Document pipeline 入口（p11）
├── document_import.py  # P13 历史目录导入（--all --run 一步 / --plan-only + --apply 两步）
├── document_validate.py # P13 单文档代码校验与 stale 对账
├── document_summarize.py # P13 既有归档的主题总结刷新
├── document_graph.py   # Document pipeline LangGraph（SCAN→…→REPORT 7 节点）
├── document/           # 文档整理流水线实现（p11）
│   ├── scanner.py      # SCAN：扫描 doc/ + 原子抢占 + 崩溃恢复
│   ├── extractor.py    # EXTRACT：SHA256 + front matter + 内容提取
│   ├── classifier.py   # CLASSIFY：规则优先，模糊时 LLM 判断
│   ├── planner.py      # PLAN：结构化 DocumentPlan + hash 去重
│   ├── executor.py     # ARCHIVE：纯 Python 执行器 + MANIFEST 追加
│   ├── importer.py     # P13：source root / frozen plan / hash-fenced copy
│   ├── validator.py    # P13：主张提取 + 指定 Git revision 代码证据
│   ├── consolidator.py # P13：规范化去重、相似关系、主题总结草稿
│   ├── indexer.py      # INDEX：项目 INDEX.md 重新生成
│   └── reporter.py     # REPORT：整理报告
├── doc/                # 文档输入目录（p11；review/ 为人工确认区）
├── projects/           # Runner 控制面项目资产
│   ├── <stable-project-id>/
│   │   ├── reports/
│   │   ├── architecture/
│   │   ├── worktrees/
│   │   ├── requirement-runs/
│   │   └── documents/      # MANIFEST/INDEX + <category>/
│   └── _runs/              # 跨项目运行快照与报告
├── prompts/ processing/ processed/ failed/
├── logs/                # agent.log
├── runtime/tasks.db     # SQLite 状态库
├── worktrees/           # 旧 worktree 根（仅迁移读取）
└── main.py / graph.py   # P1 入口
```

> 上述 `doc/ projects/ prompts/ processing/ processed/ failed/ logs/ runtime/tasks.db`
> 均在 `data_root`（默认 `../runner-data/`）下，不与源码混放。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_demo_repo.py
```

> 详细步骤见上方「快速开始」。

## 任务文件格式

```markdown
---
project: demo
allowed_paths:
  - README.md
depends_on:
  - task-001-setup
simulate_failure: 2
---

# Task: 某个任务

请完成以下任务：
1. ...

## 验收标准
- ...
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `project` | 是 | config.yaml 中的项目 ID |
| `allowed_paths` | 否 | 声明修改范围，相同文件的任务自动串行化 |
| `depends_on` | 否 | 依赖的任务 id 列表，前置任务 COMPLETED 后才调度 |
| `simulate_failure` | 否 | dry-run 测试：前 N 次执行模拟失败（0 = 不模拟） |

## 运行

```powershell
# 处理队列并等待全部完成
.\.venv\Scripts\python.exe watcher.py --mode dry-run --once

# 持续监听
.\.venv\Scripts\python.exe watcher.py --mode dry-run

# 跳过启动恢复扫描
.\.venv\Scripts\python.exe watcher.py --mode dry-run --once --no-recovery

# 连接 OpenCode Server
opencode serve --hostname 127.0.0.1 --port 4096
.\.venv\Scripts\python.exe watcher.py --mode opencode

# P1 仍可用
.\.venv\Scripts\python.exe main.py --mode dry-run
```

## Planner：从需求生成任务

`planner.py` 接收一个高层需求 `.md` 文件，拆分为若干可独立执行的 task `.md` 文件写入 `prompts/`。

```powershell
# dry-run：生成 3 个有依赖链的测试任务（setup → implement → docs）
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo --mode dry-run

# opencode：调 OpenCode Server 用 LLM 分析需求，输出结构化任务（含 allowed_paths + depends_on）
opencode serve --port 4096
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo --mode opencode
```

| 模式 | 任务来源 | 依赖链 | 外部依赖 |
|------|---------|--------|---------|
| `dry-run` | 硬编码 3 个任务 | setup → implement → docs | 无 |
| `opencode` | LLM 分析需求生成 | LLM 自动推导 | 需 `opencode serve` + AI model |

生成的任务文件自动包含 front-matter（`project`、`allowed_paths`、`depends_on`、`simulate_failure`），可直接被 `watcher.py` 消费。

**全局连续编号**：每次生成的任务自动接续历史最大序号（扫描 `prompts/`、`processing/`、`processed/`、`failed/`、`reports/` + SQLite），`depends_on` 引用同步重写，不会与历史任务重号：

```text
第 1 批: task-001..005   →  第 2 批: task-006..010   →  第 3 批: task-011..014 ...
```

## 任务执行报告

每个任务执行后自动生成
`projects/<stable-project-id>/reports/<task_id>.md`，完整记录 OpenCode
的反馈和建议（session 删除后仍可追溯）：

```markdown
## Task  (2026-09-11 22:01:06)          ← 任务元信息（项目/仓库/分支/依赖）
## Execute attempt 1 — OpenCode response ← OpenCode 原始回复
                                          （STATUS / CHANGED_FILES / SUMMARY / TESTS / ISSUES）
## Validate attempt 1                    ← 验证结果
## Diagnosis attempt 1                   ← 失败诊断（根因/涉及文件/建议/是否可重试）
## Commit                                ← commit sha
## Merge                                 ← 合并结果（clean / 冲突及解决方式）
## FINAL — SUCCESS / FAILED              ← 终态 + 原因
```

同时 SQLite `attempts` 表新增 `response` 列（自动迁移），可用 SQL 查询历史：

```sql
SELECT attempt, status, failure_type, response FROM attempts WHERE task_id = 'task-001-xxx';
```

## 项目路径支持

### 存储边界（统一约定）

`--project` 表示“本次任务属于哪个项目、代码仓库在哪里”，不表示把 Runner
自身的运行资料写入该仓库。统一规则如下：

| 内容 | 存放位置 |
|------|----------|
| Runner 项目级资产（报告、架构、worktree、需求运行索引、归档资料） | `projects/<stable-project-id>/` |
| 跨项目运行快照和汇总报告 | `projects/_runs/` |
| 全局调度队列 | `prompts/`、`processing/`、`processed/`、`failed/` |
| 待整理资料与人工确认区 | `doc/`、`doc/review/` |
| 明确作为代码任务交付物的 README、项目 `docs/` 等 | `--project` 指向的业务仓库，并受 `allowed_paths` 与 Git 门禁约束 |

因此，自动归档、架构分析和执行报告不会污染业务仓库；只有任务明确要求修改的
业务文档才随业务代码提交。

`--project` 参数和任务 front-matter 的 `project` 字段均支持**两种写法**：

1. **项目 ID**：config.yaml 中配置的 ID（可自定义分支、测试命令、agent/model）
2. **仓库路径**：直接指向一个 Git 仓库（绝对或相对路径），无需在 config.yaml 注册

```powershell
# 方式一：config.yaml 项目 ID
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo

# 方式二：直接指定仓库路径（绝对路径，支持正/反斜杠）
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project D:/Workspace/new-api

# 方式三：相对路径（相对于 runner 根目录）
.\.venv\Scripts\python.exe planner.py --requirement spec.md --project demo-repo-2
```

### 多项目（跨仓库需求）

需求横跨多个仓库时，`--project` 逗号分隔传入多个项目，planner（opencode 模式）会让 LLM 为每个任务标注所属项目；dry-run 模式轮流分配：

```powershell
.\.venv\Scripts\python.exe planner.py --requirement res_score.md --project "D:/Workspace/Talen,D:/Workspace/resource" --mode opencode
```

- 每个任务文件有自己的 `project` front-matter → 各自在对应仓库的 worktree 中执行
- `depends_on` 支持跨项目依赖（依赖检查基于 SQLite 状态，与项目无关）
- LLM 声明的项目不在 `--project` 列表内时，回退到第一个项目（不信任 LLM 发明路径）
- 每个任务只能属于**一个**项目；跨仓库改动会提示 LLM 拆成多个任务

或直接在任务文件 front-matter 中写路径：

```markdown
---
project: D:/Workspace/new-api
---
```

**解析规则**（`config.py:get_project`）：

| 优先级 | 规则 | 说明 |
|--------|------|------|
| 1 | config.yaml 项目 ID | ID 与路径同名时，config 优先 |
| 2 | 文件系统路径 | 目录存在即生效；默认分支自动探测（当前 HEAD） |

**OpenCode 如何读取路径项目**：每个任务在
`projects/<stable-project-id>/worktrees/<task_id>/` 创建 worktree 后，创建
OpenCode session 时传 `directory=<worktree>`（`workers/opencode_worker.py:80`），
OpenCode Server 即读取该目录下的项目执行任务——与项目来源（config 或路径）无关。

注意事项：
- 路径项目使用**目录名 + 规范化绝对路径哈希**作为稳定项目 ID，避免两个同名目录
  的状态、锁和资产碰撞；config 中注册的项目使用配置 ID
- 路径项目的 `test.command`、`agent`、`model` 为空（用默认值）；需要配置请走 config.yaml
- front-matter 中路径建议使用**正斜杠**（`D:/xxx`）；planner 生成时已自动归一化

## dry-run vs opencode

| | dry-run | opencode |
|---|---|---|
| 代码修改 | 本地模拟（往 README.md 追加） | 调 OpenCode Server HTTP API |
| worktree/commit/merge | 真实执行 | 真实执行 |
| 外部依赖 | 无 | 需 `opencode serve --port 4096` |
| 用途 | 验证调度/恢复/合并/重试全链路 | 真实编码任务 |

## 崩溃恢复流程

```text
Runner 启动
    │
    ▼
扫描 processing/*.md
    │
    ├── 无任务 → 正常启动
    │
    └── 有任务 → 检查 SQLite lease
                    │
                    ├── heartbeat 未超时 → 跳过（worker 还活着）
                    │
                    └── heartbeat 已超时 → 移回 prompts/ → 重新执行
```

## 失败重试流程

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

## 配置项

| 配置 | 来源 | 默认值 | 说明 |
|------|------|--------|------|
| `worker.max_workers` | config.yaml | `3` | 并行 Worker 数 |
| `worker.poll_interval` | config.yaml | `2` | 轮询间隔（秒） |
| `retry.max_attempts` | config.yaml | `3` | 最大重试次数 |
| `retry.backoff_seconds` | config.yaml | `10` | 重试退避时间（秒） |
| `recovery.lease_timeout` | config.yaml | `300` | Lease 超时（秒） |
| `recovery.heartbeat_interval` | config.yaml | `30` | 心跳间隔（秒） |
| `opencode.base_url` | config.yaml | `http://127.0.0.1:4096` | OpenCode Server |
| `projects.<id>.*` | config.yaml | — | 项目路径、分支、测试命令、agent/model |
| `project` | prompt front-matter | 必填 | 项目 ID **或** 仓库路径 |
| `allowed_paths` | prompt front-matter | 无 | 资源锁范围 |
| `depends_on` | prompt front-matter | 无 | 前置任务 id 列表 |
| `simulate_failure` | prompt front-matter | `0` | dry-run 模拟失败次数 |

## 已实现能力

| 阶段 | 能力 | 验证 |
|------|------|------|
| P1 | 固定 Planner + 并行 + Review | ✅ |
| P2 | 文件夹监听 + 抢占式 claim | ✅ |
| P3 | 多项目（config.yaml + front-matter） | ✅ |
| P4-L1 | Worktree 隔离 | ✅ |
| P4-L2 | 资源锁（allowed_paths 串行化） | ✅ |
| P4-L3 | Git Merge（自动合并回 base_branch） | ✅ |
| P4-L4 | Merge Agent（冲突自动解决） | ✅ |
| P5-崩溃恢复 | SQLite + Lease/Heartbeat + 启动恢复 | ✅ |
| P5-失败重试 | Diagnose→Replan→Retry 循环 | ✅ |
| P7-Planner | 从需求生成 task 文件（含依赖链） | ✅ |
| P7-Lease Fencing | commit/merge 前校验 lease token | ✅ |
| P7-任务依赖 | depends_on + 依赖感知调度 | ✅ |
| P7-Checkpoint | LangGraph MemorySaver + thread_id | ✅ |
| P7-OpenCode API | 对真实 server 1.18.30 全端点验证 | ✅ |
| P11 | 文档整理流水线（7 节点 + SHA256 去重 + review 安全网 + MANIFEST/INDEX） | ✅ |
| P12 | 运行资产按项目统一归档、迁移记录、审计和恢复加固 | ✅ |
| P13 | 多历史来源只读盘点、代码校验、修订草稿与主题归纳 | ✅ |

## OpenCode AI Model 配置

OpenCode 1.18.30 **开箱即用**，无需 `opencode auth login`（内置免费 provider）：

```powershell
opencode models          # 查看可用模型（opencode/* 为免费）
opencode auth list       # 查看已配置的凭证（可以为 0 个）
opencode serve --hostname 127.0.0.1 --port 4096
```

API 调用要点（实测验证）：

1. **默认模型**：`send_message` 不传 `model` 时自动使用服务器默认模型
2. **指定 model**：必须是 object 格式（客户端已自动转换 `"provider/model"` 字符串）
3. **响应格式**：`POST /session/:id/message` 返回 `{"info": ..., "parts": [...]}`，实际文本在 `type == "text"` 的 part 里
4. **目录绑定**：`POST /session?directory=<path>` 使 OpenCode 读取该路径下的项目（worktree 隔离的基础）

## 仍待解决

| # | 问题 | 优先级 |
|---|------|--------|
| 1 | MemorySaver → SqliteSaver（进程重启后 checkpoint 恢复） | 中 |

## 最终需求闭环审查（Requirement Closure）

「所有任务 COMPLETED」≠「原始需求完成」。Requirement Closure 在所有任务执行、测试、提交、合并完成后，重新对比五类证据，判断需求是否真正闭环：

```text
原始需求 vs 任务计划 vs 任务报告 vs Git 实际变更 vs 最终测试结果
```

### 流程

```text
planner.py（创建 Requirement Run：run_id + 需求快照 + base revision）
    ↓
watcher.py --once（执行全部任务）
    ↓
所有任务 COMPLETED
    ↓
Final Requirement Reviewer（自动触发）
    ↓
┌────────────┬─────────────┬─────────────┐
COMPLETE   PARTIAL/FAILED   BLOCKED
    ↓           ↓               ↓
Run 关闭   生成补充任务      等待人工
              → prompts/ → 执行 → 再次审查（≤ max_rounds 轮）
```

### 审查内容

| 维度 | 检查 |
|------|------|
| 需求覆盖 | 原始需求拆成 R-001…R-NNN，逐项给出状态 + 证据等级（A~E） |
| 代码证据 | Agent 声称的 CHANGED_FILES vs Git 实际 diff（AGENT_DIFF_MISMATCH） |
| 跨任务集成 | 接口/字段/状态/调用链一致性 |
| 回归风险 | 旧逻辑破坏、兼容路径遗漏、无关修改 |
| 最终测试 | 审查前运行项目 `test.command`（`final_review.require_tests`） |

### 产物

```text
reports/<run_id>-final-review-r<N>.md    # 人类可读（需求项表/风险表/补充任务）
reports/<run_id>-final-review-r<N>.json  # 结构化（coverage/requirements/risks/followup_tasks）
runtime/requirement_runs/<run_id>/       # 需求快照 + plan.json（含 base revisions）
SQLite: requirement_runs / requirement_reviews 表
```

### 使用

```powershell
# 1. 规划（自动创建 Run）
.\.venv\Scripts\python.exe planner.py --requirement ..\res.md --project D:\Workspace\resource --mode opencode

# 2. 执行 + 自动触发最终审查（final_review.enabled: true）
.\.venv\Scripts\python.exe watcher.py --mode opencode --once

# 或手动/单独审查
.\.venv\Scripts\python.exe requirement_closure.py --list
.\.venv\Scripts\python.exe requirement_closure.py --run-id run-20260912-103000 --mode opencode
.\.venv\Scripts\python.exe requirement_closure.py --requirement ..\res.md --project D:\Workspace\resource --mode opencode
```

审查结论：`COMPLETE` / `PARTIAL` / `FAILED` / `BLOCKED` / `RISK_ACCEPTED`。非完成结论且 `auto_replan: true` 时自动生成补充任务（带 `run_id`，编号全局递增，不重复生成整批任务）。

### 配置

```yaml
final_review:
  enabled: true          # watcher --once 完成后自动触发
  auto_replan: true      # PARTIAL/FAILED 时自动生成补充任务
  max_rounds: 3          # 最大闭环轮数
  require_tests: true    # 审查前执行项目 test.command
  fail_on_high_risk: true  # COMPLETE 但存在 high 风险时降级处理
  reviewer_agent: plan
  reviewer_model: ""
```

> dry-run 测试：`CLOSURE_SIMULATE=partial` 环境变量让首轮审查模拟 PARTIAL，可端到端验证补充任务→执行→二审闭环。

## Dashboard 可视化

`dashboard.py` 提供只读 Web UI（FastAPI + 无构建静态页），可视化任务状态、依赖图、执行历史和 OpenCode 反馈：

```powershell
.\.venv\Scripts\python.exe dashboard.py            # http://127.0.0.1:8080
.\.venv\Scripts\python.exe dashboard.py --port 9000
```

| 页面 | 路径 | 内容 |
|------|------|------|
| 总览 | `/` | 统计卡片、运行中任务（checkpoint/心跳活性）、队列/项目计数、最近事件流 |
| 依赖图 | `/graph` | 任务 DAG（Mermaid），状态着色、跨项目虚线框、缺失依赖红虚线 |
| 任务列表 | `/tasks` | 全部任务，搜索 + 状态/项目过滤 |
| 任务详情 | `/task?id=…` | 元信息、执行时间线（日志还原）、尝试历史、报告渲染（markdown + DOMPurify 防注入） |

特性：

- **实时更新**：SSE（`/api/events/stream`）每 2s 检测 tasks 表变化并推送，前端 EventSource 自动重连
- **只读安全**：dashboard 只读 SQLite（WAL 模式下与 watcher 读写互不阻塞），所有写操作仍归 watcher
- **API 独立可用**：`/api/summary`、`/api/tasks`、`/api/tasks/{id}`、`/api/tasks/{id}/report`、`/api/graph`、`/api/projects`（FastAPI 自带 `/docs` 交互文档）
- 依赖：`fastapi` + `uvicorn`（已入 requirements.txt），前端库全走 CDN

## 项目架构可视化（Archify 集成）

把 Archify 集成为一种 `ARCHITECTURE_INIT` 任务类型：分析项目代码 → 生成架构 JSON IR → archify validate → archify render → 持久化为架构资产，在 `/graph` 页面以双视图展示。

### 使用

#### 初始化某项目架构（路径直接指定，无需 config.yaml 注册）

```powershell
# 1. 启动 OpenCode Server（真实 LLM 分析需要；dry-run 模式无需）
opencode serve --hostname 127.0.0.1 --port 4096

# 2. 为项目生成架构初始化任务（--project 直接传仓库路径）
.\.venv\Scripts\python.exe architecture_init.py --project D:\Workspace\resource

# 也可以用 config.yaml 注册的项目 ID
.\.venv\Scripts\python.exe architecture_init.py --project demo

# 批量初始化（config.yaml 中 architecture.enabled: true 的项目）
.\.venv\Scripts\python.exe architecture_init.py --all

# 查看各项目基线状态
.\.venv\Scripts\python.exe architecture_init.py --list
```

生成的任务文件 `prompts/arch-init-<项目名>.md`（路径项目自动写归一化路径 ref，如 `D:/Workspace/resource`）；已有基线的项目自动跳过，`--force` 强制重新生成。

#### 执行分析任务

```powershell
# 真实 LLM 分析（需 OpenCode Server 在线）
.\.venv\Scripts\python.exe watcher.py --mode opencode --once

# 或确定性扫描（无需 Server，目录级组件，用于验证链路）
.\.venv\Scripts\python.exe watcher.py --mode dry-run --once
```

执行流程：只读分析仓库 → LLM 生成组件/关系拓扑 → 自动分层布局（Kahn 分层 + 已验证的连线方向模式 + 标签宽度适配）→ archify validate → archify render → 生成基线。业务仓库零改动（分析前后比对 `git status`，被改动则拒绝快照）。

失败自动走 Diagnose → Re-plan → Retry 循环（最多 `retry.max_attempts` 次）。

#### 查看架构图

```powershell
# 启动 Dashboard（若未运行）
.\.venv\Scripts\python.exe dashboard.py            # http://127.0.0.1:8080
```

浏览器打开 `http://127.0.0.1:8080/graph` → 视图切到「**项目架构图**」→ 选择项目（如 resource）→ iframe 内展示 Archify 交互式架构图（含 revision、快照信息、过期警告）。

#### 刷新基线（代码变更后）

```powershell
.\.venv\Scripts\python.exe architecture_init.py --project D:\Workspace\resource --force
.\.venv\Scripts\python.exe watcher.py --mode opencode --once
```

代码推进后 Dashboard 会显示「⚠ 架构图可能不是当前代码版本（快照 xxx · 当前 yyy）」；`--force` 重新分析，旧基线自动归档到 `snapshots/`。

#### 手写任务文件（可选）

```yaml
---
type: ARCHITECTURE_INIT
project: "D:/Workspace/resource"
depends_on: []
---
```

让普通任务 `depends_on` 它，即可保证「没有架构基线就不开始代码改造」。

### 关键机制

| 机制 | 说明 |
|------|------|
| 任务类型 | front-matter `type: ARCHITECTURE_INIT`（默认 `CODE_CHANGE`） |
| 只读分析 | 不建 worktree、不 commit、不 merge；分析前后检查 `git status`，业务仓库被改动则拒绝快照 |
| 职责分离 | **LLM 只定义拓扑**（组件 + 关系），**系统计算几何**（分层布局/连线方向/标签宽度/viewBox）——LLM 擅长架构语义、不擅长像素布局 |
| 输出清洗 | `_normalize_ir`：字段白名单（boundary 只允许 kind/label/wraps 等）、类型枚举纠正、viewBox 修复——机械性 JSON 错误不再消耗 LLM 重试 |
| 自动布局 | Kahn 分层（环安全）+ 4 种已验证连线方向模式 + 跳层边 via 过顶绕行 + 相邻层 gap 锚点 + 标签/sublabel 宽度适配 + viewBox 覆盖计算 |
| 两层验证 | archify validate（结构/布局规则）+ 产物完整性（json/html/metadata/revision） |
| 失败重试 | 走现有 Diagnose → Re-plan → Retry 循环（不直接进 failed/） |
| 架构锁 | 同项目的 arch-init 运行/排队期间，CODE_CHANGE 任务被挂起（p10 §17） |
| 资产持久化 | `projects/<stable-project-id>/architecture/baseline/`（重初始化时旧快照归档到 `snapshots/`）+ SQLite `architecture_snapshots` 表 |
| Revision 绑定 | 快照记录 `repository_revision`，页面显示对应代码版本 + 过期警告（§26.3） |
| 自动初始化 | `architecture.auto_initialize: true` 的项目，watcher 启动时自动生成 arch-init 任务 |
| 降级模式 | archify CLI 不可用时用内置校验 + Mermaid 渲染（config `architecture.archify_entry` 指定工具路径） |

### Dashboard

- `/graph` 视图切换：**任务依赖图**（Mermaid）/ **项目架构图**（项目 + 快照选择器，iframe 嵌入 Archify HTML，`sandbox="allow-scripts"`）
- API：`/api/architectures`、`/api/architectures/{project_id}`、`/api/architectures/{project_id}/{snapshot_id}/html|json|svg`（DB 映射 + 根目录包含检查，防路径穿越）；`/api/projects` 已含架构可用性字段

### 配置

```yaml
projects:
  new-api:
    path: D:/Workspace/new-api
    architecture:
      enabled: true            # architecture_init.py --all 会处理
      auto_initialize: true    # watcher 启动时自动生成 arch-init 任务（无基线时）
      max_core_nodes: 12
      include: [service, controller]   # 可选：仅分析这些顶层目录
      exclude: [vendor, node_modules]

architecture:                   # 全局
  default_snapshot: baseline
  archify_command: node
  archify_entry: ""             # 默认用本机 ~/.agents/skills/archify
  validation: { enabled: true }
  rendering: { enabled: true }
```

> 路径直传项目（`--project D:\Workspace\resource`）无需注册即可初始化；include/exclude/max_core_nodes 等项目级调优需 config.yaml 条目。

## 文档整理流水线（Document Organizer，p11）

把文档丢进 `doc/`，自动完成：识别项目 → 判断类别 → SHA256 去重 → 归档到
`projects/<stable-project-id>/documents/<类别>/` → 更新 `INDEX.md` /
`MANIFEST.json` → 生成整理报告。这里的 `doc/` 只是 Runner 的收件箱；
`--project` 只选择归属项目，不会把归档资料写进目标业务仓库。

```powershell
# dry-run（纯规则，无需 OpenCode Server）
.\.venv\Scripts\python.exe document_run.py --mode dry-run

# 连接 OpenCode Server（模糊文档用 LLM 语义判断）
opencode serve --hostname 127.0.0.1 --port 4096
.\.venv\Scripts\python.exe document_run.py --mode opencode

# 持续监听 doc/
.\.venv\Scripts\python.exe document_run.py --mode dry-run --watch

# 强制指定项目（config.yaml 项目 ID 或仓库路径），跳过项目识别，confidence 1.0
.\.venv\Scripts\python.exe document_run.py --mode dry-run --project new-api
```

### 流水线（7 节点，LangGraph）

```text
SCAN → EXTRACT → CLASSIFY → PLAN → ARCHIVE → INDEX → REPORT
```

| 节点 | 实现 | 说明 |
|------|------|------|
| SCAN | `document/scanner.py` | 扫描 `doc/` 顶层（`review/` 子目录永不重扫），原子移入 `processing/documents/`；崩溃恢复：下次启动移回 `doc/` |
| EXTRACT | `document/extractor.py` | SHA256 + front matter + 内容提取（md/txt 直读；docx/pptx/xlsx stdlib zip 解包；pdf 需 pypdf），多线程并行 |
| CLASSIFY | `document/classifier.py` | 规则优先（front matter `project` > `--project` 强制指定 > 文件名项目 ID/关键词 > 内容关键词），置信度不足且 opencode 模式时 LLM 判断（只能从候选项目中选择） |
| PLAN | `document/planner.py` | 生成结构化 DocumentPlan；SHA256 命中任一项目 MANIFEST（或同批次）→ `skip_duplicate`；confidence < 阈值 → `review` |
| ARCHIVE | `document/executor.py` | **纯 Python 执行器**：持久化 journal → move → hash 校验 → MANIFEST/审计 → commit；重启可对账 |
| INDEX | `document/indexer.py` | 从 MANIFEST.json 重新生成 `projects/<id>/documents/INDEX.md`（按固定 taxonomy 分组） |
| REPORT | `document/reporter.py` | `projects/_runs/reports/doc-organize-<时间戳>.md`（总览/按项目归档/重复/待人工确认/失败） |

> 同 p11 §7，互不冲突的文档并行处理 — 并行发生在 EXTRACT/CLASSIFY/ARCHIVE 节点内部（工作线程池），图保持线性。

### 目录与产物

```text
doc/                          # 输入：往这里丢文件
doc/review/                   # confidence < 阈值的文档等人工确认
processing/documents/         # 处理中（崩溃后下次启动自动移回 doc/）
processed/documents/          # 审计记录（<文件名>-<sha8>.json）
projects/<id>/documents/      # 归档目标：MANIFEST.json + INDEX.md + <category>/
projects/_runs/reports/doc-organize-*.md  # 整理报告
```

### 安全边界（p11 §2：LLM 规划，程序执行）

- **LLM 只产出分类结论**（project/category/confidence），文件移动全部由 Python Executor 执行
- **confidence < `documents.confidence_threshold`（默认 0.8）→ 禁止归档**，进 `doc/review/`
- **禁止删除原始文件**；所有移动写入审计记录（`processed/documents/`），可回滚
- **SHA256 去重**：命中 MANIFEST.json → `skip_duplicate`，不产生 `_1`/`_final` 副本
- 归档后重新校验 SHA256，不一致判为失败
- 类别固定 taxonomy：`requirements/design/architecture/research/meeting/api/task/report/decision/archive`
- P13 扩展类别：`implementation/validation`

### 配置

```yaml
documents:
  input_dir: ./doc
  confidence_threshold: 0.8
  max_workers: 4
  content_preview_chars: 4000

projects:
  new-api:
    path: D:/Workspace/new-api
    keywords: [new-api, 流量池, token, channel]   # 规则匹配关键词（p11 §5）
```

front matter 直接指定项目（优先级最高，confidence 1.0）：

```markdown
---
project: new-api
---
# 流量池方案
...
```

`--project` 命令行强制指定（对本次运行的所有文档生效，优先级同 front matter；支持 config.yaml 项目 ID 或仓库路径，与 `planner.py --project` 解析规则一致）；类别仍按规则判断，但**不受 confidence 阈值约束**，不会进 `doc/review/`：

```powershell
python document_run.py --project D:/Workspace/new-api   # 路径项目使用“目录名+路径哈希”的稳定 ID
```

人工确认或重分类后重新入队：

```powershell
python document_review.py --file 待确认.md --project demo --category requirements
python document_run.py --mode dry-run
```

Dashboard 默认只监听本机。绑定其他地址时必须设置 Bearer Token：

```powershell
python dashboard.py --host 0.0.0.0 --token <strong-token>
```

Agent 职责边界说明见 `skills/document-organizer/SKILL.md`。

## 历史文档导入、代码校验与归纳（P13）

P13 面向桌面、旧项目、备份盘、共享盘挂载目录等用户明确指定的历史来源。
来源目录始终只读；计划阶段不写 DB 和正式资产，Apply 默认只复制，不移动、
不删除原文件。

### 动作开关

每个来源可以独立开启：

| 开关 | 作用 | 产物 |
|------|------|------|
| `--all` | 一次开启下面四个动作（`--no-all` 一次全关） | — |
| `validate` | 从文档提取主张，与对应项目的 committed Git revision 比对 | claims、代码 evidence、validation 状态 |
| `repair` | 根据校验证据生成修订草稿；必须同时开启 validate | `_derived/repairs/<source-id>/` |
| `consolidate` | 规范化去重、相似关系和同项目/同专题归纳 | `_derived/topics/<topic>/SUMMARY.md` |
| `merge` | 文件夹级合并：阅读同一来源文件夹下的全部文档，**基于当前代码分析**合并为一份综合文档（主文档逐字节归档，其余成员折叠 `MERGED`）；必须同时开启 validate | `<source-folder>-综合分析（合并版）.md`（或 `--merge-output` 指定） |

安全默认值为四个动作全部关闭。配置优先级固定为：

```text
CLI > source 独立配置 > profile > defaults
```

`repair` 支持：

- `annotate`：不重写原文，生成勘误和代码证据；
- `rewrite-draft`：生成结构化修订草稿。

两种修订产物均为 `DRAFT`，不会覆盖原文或直接写入业务仓库。需求中的
“应该/必须”尚未实现时标记为 `not_implemented`，不会被改写成当前代码行为。

`merge` 的安全边界：

- **合并范围 = 一个来源文件夹 + 一个项目**；跨文件夹、跨项目的文档不会合并；
- 主文档（计划序首位）按正常流程逐字节归档（hash 校验 + MANIFEST），
  折叠成员不单独复制，`merged_into` 指向主文档归档路径；
- 合并后文档是一份**基于当前代码分析**的综合文档，**不使用**
  `verified`/`partially_verified`/`not_implemented`/`unverifiable` 等校验状态标签；
  代码证据以「文档主张 → 代码位置（file:line）」的形式作为事实列出；
- `--mode opencode` 时由 LLM 阅读全部文档并综合代码证据合成一份连贯分析；
  `--mode dry-run` 时确定性合并（来源列表 + 代码依据 + 各文档正文）；
- 合并文档默认写到源文件夹旁：`<source-folder>-综合分析（合并版）.md`；
  可用 `--merge-output <path>` 指定任意位置（如桌面）；输出路径在计划阶段冻结；
  写入只读来源目录内会被拒绝；
- 主文档归档失败时该组不折叠，标记 `REVIEW_REQUIRED`；来源文件始终不移动、不删除；
- `merge=true` 且 `validate=false` 直接报错；`--plan-only` 的计划 JSON 含
  `merge_groups` 预览（分组、主文档选择与输出路径在计划阶段冻结）。

#### 案例：桌面「服务质量」合并为一份综合文档

```powershell
# 阅读桌面「服务质量」下所有文件，基于当前代码（Talen）分析并合并一份文档，
# 写到桌面「服务质量-综合分析（合并版）.md」（默认输出位置）
python document_import.py `
  --source C:/Users/Administrator/Desktop/服务质量 `
  --project D:/Workspace/Talen `
  --merge --validate --mode opencode --run

# 也可显式指定输出文件名/位置
python document_import.py `
  --source C:/Users/Administrator/Desktop/服务质量 `
  --project D:/Workspace/Talen `
  --merge --validate --mode opencode `
  --merge-output "C:/Users/Administrator/Desktop/服务质量-综合分析（合并版）.md" `
  --run
```

运行结果 JSON 的 `merged_documents` 列出每个合并文档的路径、成员数与生成方式
（`generation: llm` 或 `rules`）。

### 一步运行（--all --run）

```powershell
# 盘点 → 冻结计划 → 代码校验 → 修订草稿 → 主题归纳 → 合并归档 → copy-only 发布，一次完成
python document_import.py `
  --source D:/Archive/old-resource `
  --project D:/Workspace/resource `
  --all --run
```

- `--all` = `--validate --repair --consolidate --merge` 一次开启四个动作；
  单个动作开关仍可覆盖（如 `--all --no-repair --no-merge` 只做校验和归纳）
- 只做代码校验合并（不生成修订/归纳）可用 `--validate --merge --mode opencode --run`；
  运行结果 JSON 会列出每个合并文档的路径（`merged_documents`）
- `--run` 在进程内先构建冻结计划再立即 Apply；Apply 前仍会重新计算
  每个来源文件 SHA256，计划后发生变化则拒绝该文件
- 计划仍会持久化到 `projects/_runs/imports/<import-run-id>/plan.json`，可追溯

仍可分两步（先预览、后 Apply）：

```powershell
# 只读盘点；JSON 输出由 shell 保存到用户明确指定的计划文件
python document_import.py `
  --source D:/Archive/old-resource `
  --project D:/Workspace/resource `
  --all --plan-only | Set-Content -Encoding utf8 p13-import-plan.json

# Apply 前重新计算每个来源文件 SHA256；计划后发生变化则拒绝该文件
python document_import.py --apply p13-import-plan.json
```

多个来源可以一起导入：

```powershell
python document_import.py `
  --source D:/Archive/design `
  --source E:/Backup/project-docs `
  --project resource `
  --all --run
```

也可以为不同来源指定不同 profile：

```powershell
python document_import.py `
  --source-profile D:/Archive/implementation=historical-implementation `
  --source-profile E:/Backup/requirements=historical-requirements `
  --project resource --run
```

### 配置

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

    # 可选：不写 sources 时通过 CLI --source 指定
    sources:
      - path: D:/Archive/old-resource
        profile: historical-implementation
        project: D:/Workspace/resource

    exclude_dirs: [.git, .venv, node_modules, vendor, dist, build, __pycache__]
    max_files: 10000
    max_file_bytes: 52428800
    max_total_bytes: 2147483648
```

CLI 的 `--all`（一次开启 validate + repair + consolidate + merge）、
`--validate/--no-validate`、`--repair/--no-repair`、
`--consolidate/--no-consolidate` 和 `--merge/--no-merge` 会覆盖来源 profile，
单个动作开关优先于 `--all`。`repair=true` 或 `merge=true` 且
`validate=false` 会在计划阶段直接报错。

### 流水线与产物

```text
DISCOVER → EXTRACT → CLASSIFY → EXACT_DEDUP
  ├─ validate → CLAIM_EXTRACT → CODE_VALIDATE → repair → REPAIR_DRAFT
  ├─ consolidate → CLUSTER → SUMMARY_DRAFT
  ├─ merge → MERGE_GROUP（按来源文件夹冻结分组）→ 主文档归档 + 其余折叠（MERGED）→ 代码分析合并 → 综合文档（--mode opencode 由 LLM 合成）
  └─ 无动作 → 仅归档计划
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

<source-folder>-综合分析（合并版）.md        # 合并文档默认写到源文件夹旁

projects/_runs/imports/<import-run-id>/
├── plan.json
├── inventory.json
└── report.md
```

代码校验默认绑定 committed `HEAD`，也可用 `--revision <commit>` 指定历史
revision。验证使用只读 `git rev-parse/status/grep`，不会 checkout、commit 或修改
业务仓库。结论包括 `verified`、`partially_verified`、`not_implemented`、
`unverifiable`；代码变化后，旧 revision 仍保留在证据中，供后续 stale 对账。

只有原始 SHA256 完全一致才自动去重；规范化正文相同仅标记为
`normalized_duplicate` 候选，语义相似只建立 `similar` 关系并生成总结草稿，
原文始终保留。Dashboard 的 `/operations` 展示 Import Run 汇总，API 为：

```powershell
# 不导入，直接对单份文档执行只读代码校验
python document_validate.py --project resource --file D:/Archive/design.md

# 查询旧 revision 证据；增加 --mark-stale 才会写入 stale 状态
python document_validate.py --project resource --stale-only

# 默认只预览主题成员；增加 --apply 才生成 DRAFT SUMMARY
python document_summarize.py --project resource --topic resource-lifecycle
python document_summarize.py --project resource --topic resource-lifecycle --apply
```

- `GET /api/document-imports`
- `GET /api/document-imports/{import_run_id}`
- `GET /api/projects/{project_id}` 中包含 imported documents 和 summaries

## 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `missing 'project' in front matter` | 文件带 UTF-8 BOM 或缺 front-matter | 已兼容 BOM（utf-8-sig）；检查 `---` 块 |
| `path is not a git repository (missing .git)` | `--project` 指向的目录不是 git 仓库 | 确认路径（如 `D:/Workspace/Talen` 而非 `D:/Workspace`） |
| `crashed: Git command failed: ...` | 任务图外的异常（git/环境问题） | 看 `logs/agent.log` 完整堆栈；任务文件会自动移入 `failed/` |
| 依赖任务 `cascade-failed` | 前置任务 FAILED 或其文件已消失 | 修复前置任务后，把 `failed/` 中相关文件全部移回 `prompts/` 重跑 |
| 任务卡在 `processing/` 且状态 RUNNING | watcher 进程被强杀（lease 未过期） | 等 `lease_timeout`（默认 300s）后重启 watcher 自动恢复；或调小 `recovery.lease_timeout` |
| `[MISSING .git]` 警告 | config.yaml 里的演示仓库未初始化 | 跑 `setup_demo_repo.py`，或删除无用项目配置 |
| `arch-init-*` 被 reject：`unknown project` | 任务 front-matter 写了裸目录名（不在 config.yaml） | 已修复：路径项目自动写归一化路径；重新运行 `architecture_init.py --project <路径>` |
| `archify validate failed: ...` | LLM 输出或布局违反 archify 规则 | 已由 `_normalize_ir` + `_auto_layout` 自动处理；仍失败会自动重试，3 次后进 `failed/` 可重排队 |
| OpenCode 30 分钟/10 分钟 Read timed out | Server 挂起或分析任务过大 | 重启 `opencode serve`；diagnose 已降为 600s 超时且失败不炸任务图 |
| 架构图显示「⚠ 可能不是当前代码版本」 | 仓库 HEAD 已超过快照 revision | `architecture_init.py --project <路径> --force` + watcher 重新分析 |

重跑失败任务的通用方法：

```powershell
# 1. 确认失败原因
Get-Content .\logs\agent.log -Tail 50

# 2. 把 failed/ 中要重跑的任务移回队列
Move-Item .\failed\task-001-*.md .\prompts\

# 3. 重新执行
.\.venv\Scripts\python.exe watcher.py --mode opencode --once
```

## 源码打包

`scripts/package_code.py` 将当前实现源码打包为单个可分享的文本文件（约 120KB），方便把整个 runner 发给 LLM 或他人 review。

```powershell
# 默认输出到项目根：langgraph-opencode-source-bundle.txt
.\.venv\Scripts\python.exe scripts\package_code.py

# 自定义输出位置
.\.venv\Scripts\python.exe scripts\package_code.py --output D:\Temp\runner-code.txt

# 附带演示仓库 README
.\.venv\Scripts\python.exe scripts\package_code.py --with-demo-repos
```

**包含**（27 个文件，仅实现代码）：

| 分类 | 文件 |
|------|------|
| 入口 | `watcher.py`、`planner.py`、`main.py`、`graph.py` |
| 核心 | `config.py`、`config.yaml`、`task_graph.py`、`opencode_client.py`、`worktree.py`、`log.py`、`setup_demo_repo.py` |
| runtime/ | `watcher`、`task_resolver`、`task_store`、`recovery_manager`、`file_lock_manager`、`worker_pool`、`__init__` |
| workers/ | `opencode_worker`、`merge_agent`、`diagnoser`、`__init__` |
| schemas/ | `task.py`、`__init__` |
| 文档/元 | `requirements.txt`、`.gitignore`、`README.md` |

**排除**（运行时产物与无关文件）：

```text
prompts/ processing/ processed/ failed/   # 任务生命周期目录
reports/                                  # 执行报告
worktrees/ logs/                          # worktree 与日志
runtime/tasks.db                          # SQLite 状态库
.venv/ __pycache__/ .git/                 # 环境
oc_stdout.txt oc_stderr.txt               # 调试输出
demo-repo/ demo-repo-2/                   # 演示仓库（--with-demo-repos 可附带 README）
```

输出格式：文件头（根目录/生成时间/文件数）+ 每个文件一段（`#### FILE: <相对路径>` 分隔），缺失文件输出告警。
