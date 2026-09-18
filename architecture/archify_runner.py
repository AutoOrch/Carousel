"""Wrapper around the external Archify CLI (validate / render / inspect).

When the archify entry is unavailable the runner degrades gracefully:
* validate falls back to a structural check of the JSON IR;
* render falls back to a self-contained Mermaid-based HTML page.
This keeps the pipeline testable on machines without archify installed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import ArchitectureConfig
from log import get_logger

logger = get_logger(__name__)

REQUIRED_TOP_KEYS = ("schema_version", "diagram_type", "meta", "components", "connections")


@dataclass
class ArchifyResult:
    ok: bool
    output: str = ""
    fallback: bool = False   # True when produced by the internal fallback


class ArchifyRunner:
    def __init__(self, config: ArchitectureConfig) -> None:
        self._config = config
        self._available = bool(config.archify_entry) and Path(config.archify_entry).is_file()
        if not self._available:
            logger.warning(
                "[archify] entry not found (%r) — using internal fallback "
                "validation/rendering", config.archify_entry,
            )

    @property
    def available(self) -> bool:
        return self._available

    def version(self) -> str:
        if not self._available:
            return ""
        result = self._run_cli("--version")
        return result.output.strip() if result.ok else ""

    # ---------------------------------------------------------------- public

    def validate(self, json_path: Path, diagram_type: str = "architecture") -> ArchifyResult:
        if self._available and self._config.validation_enabled:
            return self._run_cli(
                "validate", diagram_type, str(json_path), "--json"
            )
        return _fallback_validate(json_path)

    def render(
        self, json_path: Path, html_path: Path, diagram_type: str = "architecture",
        revision: str = "",
    ) -> ArchifyResult:
        if self._available and self._config.rendering_enabled:
            return self._run_cli(
                "render", diagram_type, str(json_path), str(html_path)
            )
        return _fallback_render(json_path, html_path, revision)

    def inspect(self, json_path: Path, diagram_type: str = "architecture") -> ArchifyResult:
        if self._available:
            return self._run_cli("inspect", diagram_type, str(json_path))
        return ArchifyResult(ok=True, output="(inspect unavailable in fallback mode)", fallback=True)

    # --------------------------------------------------------------- internal

    def _run_cli(self, *args: str) -> ArchifyResult:
        cmd = [self._config.archify_command, self._config.archify_entry, *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
            if result.returncode != 0:
                return ArchifyResult(ok=False, output=output.strip())
            return ArchifyResult(ok=True, output=output.strip())
        except Exception as exc:
            return ArchifyResult(ok=False, output=f"archify failed to run: {exc}")


# --------------------------------------------------------------------------
# Fallbacks (no external archify installed)
# --------------------------------------------------------------------------

def _fallback_validate(json_path: Path) -> ArchifyResult:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ArchifyResult(ok=False, output=f"invalid JSON: {exc}", fallback=True)

    problems: list[str] = []
    for key in REQUIRED_TOP_KEYS:
        if key not in data:
            problems.append(f"missing top-level key: {key}")

    components = data.get("components") or []
    if not components:
        problems.append("components list is empty")
    ids = {c.get("id") for c in components if isinstance(c, dict)}
    if len(ids) != len(components):
        problems.append("duplicate component ids")
    for c in components:
        if not isinstance(c, dict):
            continue
        pos = c.get("pos")
        if not isinstance(pos, list) or len(pos) != 2:
            problems.append(f"component {c.get('id', '?')} missing pos [x, y]")

    for conn in data.get("connections") or []:
        if not isinstance(conn, dict):
            continue
        if conn.get("from") not in ids or conn.get("to") not in ids:
            problems.append(
                f"connection {conn.get('id', '?')} references unknown endpoint"
            )

    if problems:
        return ArchifyResult(ok=False, output="; ".join(problems), fallback=True)
    return ArchifyResult(
        ok=True, output=f"fallback validation passed ({len(components)} components)", fallback=True
    )


_FALLBACK_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{title} — architecture (fallback render)</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: "Segoe UI", system-ui, sans-serif; background: #0f1420;
         color: #dbe4f5; margin: 0; padding: 24px; }}
  h1 {{ font-size: 18px; }}
  .meta {{ color: #7c8bb0; font-size: 13px; margin-bottom: 16px; }}
  .diagram {{ background: #171e2e; border: 1px solid #2a3550; border-radius: 10px;
              padding: 18px; overflow-x: auto; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">revision {revision} · fallback render (archify CLI unavailable)</div>
<div class="diagram"><pre class="mermaid">{mermaid}</pre></div>
<script>mermaid.initialize({{ startOnLoad: true, theme: "dark" }});</script>
</body>
</html>
"""


def _fallback_render(json_path: Path, html_path: Path, revision: str = "") -> ArchifyResult:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ArchifyResult(ok=False, output=f"invalid JSON: {exc}", fallback=True)

    lines = ["flowchart LR"]
    for comp in data.get("components") or []:
        cid = str(comp.get("id", "")).replace(" ", "_")
        label = str(comp.get("label", comp.get("id", ""))).replace('"', "'")
        ctype = str(comp.get("type", "backend"))
        lines.append(f'  {cid}["{label}<br/><small>{ctype}</small>"]')
    for conn in data.get("connections") or []:
        src = str(conn.get("from", "")).replace(" ", "_")
        dst = str(conn.get("to", "")).replace(" ", "_")
        label = str(conn.get("label", "")).replace('"', "'")
        arrow = f'|"{label}"|' if label else ""
        lines.append(f"  {src} -->{arrow} {dst}")

    meta = data.get("meta") or {}
    html = _FALLBACK_HTML_TEMPLATE.format(
        title=meta.get("title", data.get("diagram_type", "architecture")),
        revision=revision or "n/a",
        mermaid="\n".join(lines),
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    return ArchifyResult(ok=True, output=f"fallback render written: {html_path}", fallback=True)
