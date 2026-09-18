# Document Organizer Skill

## Purpose

将 `doc/` 中的新文档自动识别、分类、归档到对应项目的文档目录
（`projects/<稳定项目ID>/documents/<类别>/`），维护 `MANIFEST.json` 与 `INDEX.md`，
并生成整理报告。实现位于 `document/` 包，入口 `document_run.py`（p11）。
历史目录的只读盘点、代码校验、修订草稿和主题归纳入口为
`document_import.py`（p13）。

## Input

默认输入目录：`doc/`（config.yaml `documents.input_dir`）

支持：Markdown / TXT / PDF / DOCX / PPTX / XLSX
（PDF 内容提取需安装 pypdf，否则仅按文件名/元数据分类）

## Workflow（7 节点，LangGraph）

1. **SCAN**：扫描 `doc/` 顶层，原子移入 `processing/documents/`（崩溃恢复：下次启动移回 `doc/`）
2. **EXTRACT**：SHA256 + front matter + 内容提取（md/txt 直读；docx/pptx/xlsx stdlib 解包）
3. **CLASSIFY**：识别项目 + 类别（见下方优先级；类别只能从固定 taxonomy 选择）
4. **PLAN**：生成结构化 DocumentPlan（action = `archive` / `skip_duplicate` / `review`）
5. **ARCHIVE**：Python Executor 执行 mkdir/move/hash 校验/审计记录 + 追加 MANIFEST.json
6. **INDEX**：从 MANIFEST.json 重新生成项目 `INDEX.md`（按 taxonomy 分组）
7. **REPORT**：生成 `projects/_runs/reports/doc-organize-{timestamp}.md`

## Project identification

优先级：

1. `--project` 命令行强制指定（confidence 1.0，跳过项目识别，不受阈值约束）
2. front matter `project`（config.yaml 项目 ID 或仓库路径）→ confidence 1.0
3. 文件名（项目 ID / `projects.<id>.keywords` 关键词，词边界匹配）
4. 内容关键词
5. LLM 语义判断（仅 opencode 模式，且规则置信度不足阈值时；
   LLM 只能从候选项目中选择，不允许发明项目）

## Category taxonomy

`requirements / design / architecture / research / meeting / api / task / report / decision / archive`

## Safety

- confidence < 0.8（`documents.confidence_threshold`）：禁止自动归档，
  移入 `doc/review/` 等人工确认
- 任何文件移动前必须生成 DocumentPlan；LLM 永远不执行文件操作
- 禁止删除原始文件；重复文件（SHA256 相同）→ `skip_duplicate`，
  原件存入 `processed/documents/` 并留审计记录
- 归档后重新校验 SHA256，不一致判为失败
- 同名不同内容：以 SHA256 前 8 位区分，不产生 `_1` / `_final` 副本

## Report

必须生成：`projects/_runs/reports/doc-organize-{timestamp}.md`
（总览 + 按项目归档/重复 + 待人工确认 + 失败）

## 运行

```powershell
python document_run.py --mode dry-run          # 纯规则，无需 OpenCode Server
python document_run.py --mode opencode         # 模糊文档用 LLM 语义判断
python document_run.py --mode dry-run --watch  # 持续监听 doc/
python document_run.py --project new-api       # 强制指定项目（ID 或仓库路径）
```

## P13 历史目录导入

```powershell
# 一步完成：只读盘点 → 冻结计划 → copy-only 归档
# --all = validate + repair + consolidate + merge（单个动作开关可覆盖，如 --all --no-repair）
python document_import.py --source D:/Archive/docs --project new-api --all --run

# 只做代码校验合并（不生成修订/归纳）；--mode opencode 由 LLM 综合分析
python document_import.py --source D:/Archive/docs --project new-api \
  --validate --merge --mode opencode --run

# 仍可先只预览计划（零写入），再回放已保存的计划
python document_import.py --source D:/Archive/docs --project new-api --all --plan-only
python document_import.py --apply p13-import-plan.json
```

- `validate`：绑定 committed Git revision 提取代码证据；
- `repair`：必须依赖 validate，只生成 DRAFT，不覆盖来源；
- `consolidate`：生成相似关系和 DRAFT 总结，不删除语义相似原文；
- `merge`：必须依赖 validate；**文件夹级合并**——阅读同一来源文件夹下的全部文档，
  **基于当前代码分析**合并为一份综合文档（主文档逐字节归档、其余状态 `MERGED`
  折叠）。合并文档**不使用** verified/partially_verified 等状态标签，代码证据以
  「主张 → 代码位置」形式列出。`--mode opencode` 时由 LLM 阅读全部文档并综合代码
  证据合成连贯分析；`--mode dry-run` 时确定性合并。默认写到源文件夹旁
  `<名>-综合分析（合并版）.md`，可用 `--merge-output` 指定；
  跨文件夹、跨项目不会合并；主文档归档失败时该组不折叠；
- 每个 source root 可通过 `documents.imports.profiles/sources` 独立配置；
- 计划后来源 SHA256 变化时拒绝 Apply；
- 不跟随符号链接，不修改或删除历史来源目录。
