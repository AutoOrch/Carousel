# LangGraph + OpenCode Demo

LangGraph 负责任务编排、状态管理、依赖调度、并行、重试和动态 Re-plan；OpenCode 负责真正进入代码仓库执行编码任务。Git Worktree 提供隔离。

## 三个入口

| 入口 | 命令 | 说明 |
|------|------|------|
| **Planner** | `python planner.py --requirement spec.md --project demo` | 从需求 .md 生成 task .md 文件到 `prompts/`（含依赖链） |
| **P1** | `python main.py --mode dry-run` | 固定 Planner 硬编码 2 个任务，并行执行后 Review |
| **P2-P5** | `python watcher.py --mode dry-run [--once]` | 文件夹监听 → 多项目 → 资源锁 → 依赖调度 → 合并 → 崩溃恢复 → 重试循环 |

## 阶段总览

- **P1（`main.py`）**：固定 Planner 生成两个任务，并行执行后 Review。
- **P2（`watcher.py`）**：文件夹监听，往 `prompts/` 丢 `.md` 任务即自动执行。
- **P3（多项目）**：任务通过 front-matter 指定 `project`，由 `config.yaml` 解析到真实仓库。
- **P4（冲突治理）**：资源锁 + 自动合并 + Merge Agent，安全处理多任务改同一文件。
- **P5（崩溃恢复 + 失败重试）**：SQLite 状态机 + Lease/Heartbeat + Diagnose → Re-plan → Retry 循环。
- **P7（生产加固）**：Planner 需求拆任务 + 任务依赖 (`depends_on`) + Lease Fencing + LangGraph Checkpoint + 日志系统 + Worktree/Session 清理 + OpenCode API 全端点验证。

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

```text
langgraph-opencode/
├── config.yaml          # 项目 + 重试 + 恢复配置
├── watcher.py           # 入口（轮询 + 恢复 + Worker Pool）
├── planner.py           # Planner 入口（需求 .md → task .md）
├── config.py            # 配置加载 + 路径项目解析
├── task_graph.py        # 单任务 LangGraph（含重试循环 + fencing + checkpoint）
├── opencode_client.py   # OpenCode Server HTTP 客户端
├── worktree.py          # Git Worktree + Merge 封装
├── setup_demo_repo.py   # 初始化演示 Git 仓库
├── log.py               # 日志（stdout + logs/agent.log）
├── runtime/
│   ├── watcher.py       # 扫描 + 原子抢占
│   ├── task_resolver.py # 解析 front-matter（project ID 或路径）
│   ├── task_store.py    # SQLite 任务状态存储
│   ├── recovery_manager.py  # 崩溃恢复 + 心跳
│   ├── file_lock_manager.py # 资源锁
│   ├── worker_pool.py   # 线程池 + 锁/依赖感知 + Lease
│   └── tasks.db         # SQLite 数据库（gitignored）
├── workers/
│   ├── opencode_worker.py  # 在 worktree 中调用 OpenCode
│   ├── merge_agent.py      # 冲突解决
│   └── diagnoser.py        # 失败诊断
├── schemas/
│   └── task.py          # Task（含 allowed_paths, depends_on, simulate_failure）
├── scripts/
│   └── package_code.py  # 源码打包（见「源码打包」）
├── prompts/ processing/ processed/ failed/
├── reports/             # 每任务执行报告（OpenCode 反馈/诊断/commit）
├── worktrees/           # worktrees/<project>/<task_id>/
├── logs/                # agent.log（gitignored）
├── demo-repo/ demo-repo-2/
└── main.py / graph.py   # P1 入口
```

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

每个任务执行后自动生成 `reports/<task_id>.md`，完整记录 OpenCode 的反馈和建议（session 删除后仍可追溯）：

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

**OpenCode 如何读取路径项目**：每个任务在 `worktrees/<项目名>/<task_id>/` 创建 worktree 后，创建 OpenCode session 时传 `directory=<worktree>`（`workers/opencode_worker.py:80`），OpenCode Server 即读取该目录下的项目执行任务 — 与项目来源（config 或路径）无关。

注意事项：
- 路径项目使用**目录名**作为项目 ID（worktree 路径、SQLite 记录均用它）
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
| 1 | 无测试套件 | 中 |
| 2 | MemorySaver → SqliteSaver（进程重启后 checkpoint 恢复） | 中 |

## 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `missing 'project' in front matter` | 文件带 UTF-8 BOM 或缺 front-matter | 已兼容 BOM（utf-8-sig）；检查 `---` 块 |
| `path is not a git repository (missing .git)` | `--project` 指向的目录不是 git 仓库 | 确认路径（如 `D:/Workspace/Talen` 而非 `D:/Workspace`） |
| `crashed: Git command failed: ...` | 任务图外的异常（git/环境问题） | 看 `logs/agent.log` 完整堆栈；任务文件会自动移入 `failed/` |
| 依赖任务 `cascade-failed` | 前置任务 FAILED 或其文件已消失 | 修复前置任务后，把 `failed/` 中相关文件全部移回 `prompts/` 重跑 |
| 任务卡在 `processing/` 且状态 RUNNING | watcher 进程被强杀（lease 未过期） | 等 `lease_timeout`（默认 300s）后重启 watcher 自动恢复；或调小 `recovery.lease_timeout` |
| `[MISSING .git]` 警告 | config.yaml 里的演示仓库未初始化 | 跑 `setup_demo_repo.py`，或删除无用项目配置 |

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
