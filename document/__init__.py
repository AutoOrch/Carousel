"""Document pipeline package (p11).

Seven stages, one per module:

    scanner    SCAN      deterministic claim of doc/ files
    extractor  EXTRACT   sha256 + front matter + content
    classifier CLASSIFY  rules first, LLM only when ambiguous
    planner    PLAN      structured DocumentPlan (hash dedup, review gate)
    executor   ARCHIVE   pure-Python file moves + MANIFEST append
    indexer    INDEX     per-project INDEX.md regeneration
    reporter   REPORT    projects/_runs/reports/doc-organize-<timestamp>.md

Design rule (p11 §2): the LLM understands and plans; Python executes
and validates.  No LLM ever performs filesystem operations.
"""
