# P12 端到端验证需求

在示例仓库中完成一组带依赖关系的变更，用于验证：

1. Requirement Run 与 Plan 被持久化；
2. Task 按依赖顺序执行；
3. Attempt、Validation、Operation、Commit 和 Event 可追踪；
4. 全部任务完成后执行 Final Requirement Review。

## 验收标准

- 三个任务全部完成；
- 每个任务的 Attempt 大于等于 1；
- 每个代码任务包含通过的 Validation；
- Run 最终状态为 COMPLETED。
