"""IR (Intermediate Representation) tools for architecture diagrams.

Pure functions operating on the Archify JSON IR dict — no worker/task
dependencies, so both the workers (producers) and the manager (repair loop)
can use them.

Design contract (learned from real archify validate failures):
* The LLM defines *topology* (components + connections); these functions
  compute *geometry* (positions, sizes, sides, vias, viewBox).
* Everything is deterministic: same topology in → same valid layout out.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

# ---------------------------------------------------------------------------
# Constants / schema whitelists (mirror architecture.schema.json)
# ---------------------------------------------------------------------------

DEFAULT_X_GAP = 340
DEFAULT_Y_GAP = 150
LAYER_X_START = 40
LAYER_Y_START = 100
COMP_W = 170
COMP_H = 60

VALID_TYPES = {"frontend", "backend", "database", "cloud",
               "security", "messagebus", "external"}

IR_TOP_KEYS = {"schema_version", "diagram_type", "meta", "layout",
               "components", "boundaries", "connections", "cards"}
IR_META_KEYS = {"title", "locale", "subtitle", "output", "animation",
                "visual_preset", "quality_profile", "engineering_profile",
                "repository", "views", "legend", "viewBox"}
IR_BOUNDARY_KINDS = {"region", "security-group"}
BOUNDARY_KEYS = {"kind", "label", "wraps"}
COMPONENT_KEYS = {"id", "type", "label", "sublabel", "tag", "brand",
                  "sources", "row", "col", "pos", "size"}


# ---------------------------------------------------------------------------
# Text width estimation (matches archify's minimum legible sizing)
# ---------------------------------------------------------------------------

def label_px(text: Any) -> float:
    """Estimated rendered width: CJK ≈ 12px/char, ASCII ≈ 6.5px/char."""
    return sum(12.0 if ord(ch) > 0x2E80 else 6.5 for ch in str(text))


def truncate_px(text: str, max_px: float) -> str:
    """Truncate *text* to at most *max_px* rendered width (with ellipsis)."""
    out: list[str] = []
    width = 0.0
    for ch in str(text):
        ch_px = 12.0 if ord(ch) > 0x2E80 else 6.5
        if width + ch_px > max_px - 12.0:  # reserve ellipsis room
            return "".join(out) + "…"
        width += ch_px
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Normalisation — schema-level sanitising before any geometry work
# ---------------------------------------------------------------------------

def normalize_ir(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Mechanically sanitise LLM output: field whitelists, type clamps,
    viewBox shape.  Returns (data, fixes)."""
    fixes: list[str] = []

    for key in list(data.keys()):
        if key not in IR_TOP_KEYS:
            fixes.append(f"dropped unknown top-level key '{key}'")
            del data[key]

    meta = data.get("meta")
    if isinstance(meta, dict):
        for key in list(meta.keys()):
            if key not in IR_META_KEYS:
                fixes.append(f"dropped unknown meta key '{key}'")
                del meta[key]
        vb = meta.get("viewBox")
        valid_vb = (
            isinstance(vb, list) and len(vb) == 2
            and all(isinstance(v, (int, float)) for v in vb)
            and vb[0] >= 320 and vb[1] >= 240
        )
        if vb is not None and not valid_vb:
            fixes.append("dropped invalid meta.viewBox (renderer recomputes it)")
            del meta["viewBox"]

    for b in data.get("boundaries") or []:
        if not isinstance(b, dict):
            continue
        for key in list(b.keys()):
            if key not in BOUNDARY_KEYS:
                fixes.append(f"boundary '{b.get('label', '?')}': dropped field '{key}'")
                del b[key]
        if b.get("kind") not in IR_BOUNDARY_KINDS:
            fixes.append(f"boundary '{b.get('label', '?')}': kind → 'region'")
            b["kind"] = "region"

    for comp in data.get("components") or []:
        if not isinstance(comp, dict):
            continue
        for key in list(comp.keys()):
            if key not in COMPONENT_KEYS:
                fixes.append(f"component '{comp.get('id', '?')}': dropped field '{key}'")
                del comp[key]
        ctype = comp.get("type")
        if ctype not in VALID_TYPES:
            comp["type"] = "backend"
            fixes.append(f"component '{comp.get('id', '?')}': type '{ctype}' → 'backend'")
        if not comp.get("label"):
            comp["label"] = str(comp.get("id", "component"))
            fixes.append(f"component '{comp.get('id', '?')}': added missing label")

    for i, conn in enumerate(data.get("connections") or []):
        if not isinstance(conn, dict):
            continue
        if not conn.get("id"):
            conn["id"] = f"conn-{i}"
            fixes.append(f"connection #{i}: added missing id")

    return data, fixes


# ---------------------------------------------------------------------------
# Fitting helpers
# ---------------------------------------------------------------------------

def fit_sublabel(comp: dict[str, Any], comp_w: float = COMP_W) -> None:
    """Truncate a sublabel to the component's width."""
    sub = comp.get("sublabel")
    if not sub:
        return
    comp["sublabel"] = truncate_px(sub, comp_w - 10)


def fit_conn_label(conn: dict[str, Any], max_px: float) -> None:
    """Truncate (or drop) a connection label to at most *max_px*."""
    label = conn.get("label")
    if not label:
        return
    text = str(label).strip()
    if not text:
        del conn["label"]
        return
    if max_px < 30:  # gap too narrow for any label
        del conn["label"]
        return
    fitted = truncate_px(text, max_px)
    conn["label"] = fitted


def recompute_view_box(data: dict[str, Any]) -> None:
    """Set meta.viewBox to cover every component (min 320x240)."""
    max_x = max(
        (c["pos"][0] + c["size"][0]
         for c in data.get("components") or []
         if isinstance(c, dict) and c.get("pos") and c.get("size")),
        default=320,
    )
    max_y = max(
        (c["pos"][1] + c["size"][1]
         for c in data.get("components") or []
         if isinstance(c, dict) and c.get("pos") and c.get("size")),
        default=240,
    )
    data.setdefault("meta", {})["viewBox"] = [
        max(int(max_x) + 60, 320),
        max(int(max_y) + 60, 240),
    ]


# ---------------------------------------------------------------------------
# Auto-layout — layered geometry with verified clean-flow patterns
# ---------------------------------------------------------------------------

def auto_layout(
    data: dict[str, Any],
    x_gap: float = DEFAULT_X_GAP,
    y_gap: float = DEFAULT_Y_GAP,
    max_comp_w: float | None = None,
) -> list[str]:
    """Replace component geometry with a layered auto-layout.

    Components are assigned to layers (Kahn — cycle-safe), stacked
    vertically per layer, widened to fit their labels (up to
    ``max_comp_w``, default ``min(x_gap - 100, 300)``).  Connections get
    verified side patterns; skip edges and crossings get via detours.
    Returns human-readable summary lines for logging.
    """
    if max_comp_w is None:
        max_comp_w = min(x_gap - 100, 300)

    components = data.get("components") or []
    connections = data.get("connections") or []
    if not components:
        return []

    comp_ids = {c.get("id") for c in components if isinstance(c, dict)}
    comp_map = {c["id"]: c for c in components if isinstance(c, dict) and c.get("id")}

    # --- component sizes: widen for labels, fit sublabels -----------------
    for comp in comp_map.values():
        need = label_px(comp.get("label", "")) + 16
        if need <= COMP_W:
            w = COMP_W
        elif need <= max_comp_w:
            w = need
        else:
            comp["label"] = truncate_px(comp.get("label", ""), max_comp_w - 16)
            w = max_comp_w
        comp["size"] = [w, COMP_H]

    max_w = max(c["size"][0] for c in comp_map.values())
    conn_label_cap = x_gap - max_w - 20

    # --- edges -------------------------------------------------------------
    edges: set[tuple[str, str]] = set()
    for conn in connections:
        if not isinstance(conn, dict):
            continue
        src, dst = conn.get("from"), conn.get("to")
        if src in comp_ids and dst in comp_ids and src != dst:
            edges.add((src, dst))

    # Kahn layering (cycle-safe).
    in_deg = {cid: 0 for cid in comp_ids}
    out_edges: dict[str, list[str]] = {cid: [] for cid in comp_ids}
    for s, d in edges:
        in_deg[d] += 1
        out_edges[s].append(d)

    layer: dict[str, int] = {}
    queue = deque(sorted(cid for cid in comp_ids if in_deg[cid] == 0))
    for cid in queue:
        layer[cid] = 0
    while queue:
        node = queue.popleft()
        for succ in out_edges[node]:
            if layer[node] + 1 > layer.get(succ, 0):
                layer[succ] = layer[node] + 1
            in_deg[succ] -= 1
            if in_deg[succ] == 0:
                queue.append(succ)

    # Cycle members: place just after their highest processed predecessor.
    for cid in sorted(cid for cid in comp_ids if cid not in layer):
        preds = [s for (s, d) in edges if d == cid and s in layer]
        layer[cid] = max((layer[p] for p in preds), default=-1) + 1

    max_layer = max(layer.values()) if layer else 0

    # Group by layer; busy components first (shorter wires).
    by_layer: dict[int, list[str]] = {}
    for cid, lv in layer.items():
        by_layer.setdefault(lv, []).append(cid)
    out_degree = {cid: 0 for cid in comp_ids}
    for conn in connections:
        if isinstance(conn, dict) and conn.get("from") in comp_ids:
            out_degree[conn["from"]] += 1
    for lv in by_layer:
        by_layer[lv].sort(key=lambda c: -out_degree.get(c, 0))

    # --- positions ---------------------------------------------------------
    def col_x(lv: int) -> float:
        return LAYER_X_START + lv * x_gap

    for lv, ids in sorted(by_layer.items()):
        for i, cid in enumerate(ids):
            comp_map[cid]["pos"] = [col_x(lv), LAYER_Y_START + i * y_gap]
            fit_sublabel(comp_map[cid], comp_map[cid]["size"][0])

    def bbox(cid: str) -> tuple[float, float, float, float]:
        p, s = comp_map[cid]["pos"], comp_map[cid]["size"]
        return (p[0], p[1], p[0] + s[0], p[1] + s[1])

    def gap_x(lv: int) -> float:
        """Anchor x inside the empty gap between columns *lv* and *lv+1*."""
        return col_x(lv + 1) - 30

    def vertical_crosses(x: float, y1: float, y2: float,
                         exclude: set[str]) -> str | None:
        lo, hi = min(y1, y2), max(y1, y2)
        for other in comp_ids:
            if other in exclude:
                continue
            x0, y0, x1, y1b = bbox(other)
            if x0 - 2 <= x <= x1 + 2 and lo < y1b - 2 and hi > y0 + 2:
                return other
        return None

    # --- connection sides (verified patterns) ------------------------------
    #   forward (layer increases)  → right / left
    #   backward (cycle back-link) → left / right
    #   same layer, downward       → bottom / top
    #   same layer, upward         → top / bottom
    for conn in connections:
        if not isinstance(conn, dict):
            continue
        src, dst = conn.get("from"), conn.get("to")
        if src not in comp_ids or dst not in comp_ids or src == dst:
            continue
        conn.pop("via", None)
        if layer[dst] > layer[src]:
            conn["fromSide"] = "right"
            conn["toSide"] = "left"
        elif layer[dst] < layer[src]:
            conn["fromSide"] = "left"
            conn["toSide"] = "right"
        else:
            if comp_map[src]["pos"][1] <= comp_map[dst]["pos"][1]:
                conn["fromSide"] = "bottom"
                conn["toSide"] = "top"
            else:
                conn["fromSide"] = "top"
                conn["toSide"] = "bottom"

    # --- skip edges (>1 layer): over-the-top via detours --------------------
    layer_boxes: dict[int, tuple[float, float]] = {}
    for cid, lv in layer.items():
        _, top, _, bot = bbox(cid)
        if lv in layer_boxes:
            t, b = layer_boxes[lv]
            layer_boxes[lv] = (min(t, top), max(b, bot))
        else:
            layer_boxes[lv] = (top, bot)

    for conn in connections:
        if not isinstance(conn, dict):
            continue
        src, dst = conn.get("from"), conn.get("to")
        if src not in comp_ids or dst not in comp_ids or src == dst:
            continue
        ls, ld = layer[src], layer[dst]
        if abs(ls - ld) <= 1:
            continue
        src_cy = bbox(src)[1] + COMP_H / 2
        dst_cy = bbox(dst)[1] + COMP_H / 2
        if ls < ld:
            x1, x2 = gap_x(ls), gap_x(ld - 1)
            mids = range(ls + 1, ld)
        else:
            x1, x2 = gap_x(ls - 1), gap_x(ld)
            mids = range(ld + 1, ls)
        tops = [layer_boxes[m][0] for m in mids if m in layer_boxes]
        y_detour = max((min(tops) - 40) if tops else min(src_cy, dst_cy), 20)
        via = [[x1, src_cy], [x1, y_detour], [x2, y_detour], [x2, dst_cy]]
        cleaned = [via[0]]
        for pt in via[1:]:
            if pt != cleaned[-1]:
                cleaned.append(pt)
        conn["via"] = cleaned

    # --- same-column / adjacent-column guards -------------------------------
    for conn in connections:
        if not isinstance(conn, dict):
            continue
        src, dst = conn.get("from"), conn.get("to")
        if src not in comp_ids or dst not in comp_ids or src == dst:
            continue
        ls2, ld2 = layer[src], layer[dst]
        if "via" in conn:
            continue
        s_box, d_box = bbox(src), bbox(dst)
        scy = (s_box[1] + s_box[3]) / 2
        dcy = (d_box[1] + d_box[3]) / 2
        if ls2 == ld2:
            # Same-column edges are cycle back-links: the column is too
            # narrow for a label — drop it.
            conn.pop("label", None)
            mid_x = (s_box[0] + s_box[2]) / 2
            if vertical_crosses(mid_x, s_box[3], d_box[1], exclude={src, dst}):
                col_x0 = s_box[0]
                under_y = max(s_box[3], d_box[3]) + 30
                conn["fromSide"] = "left"
                conn["toSide"] = "right"
                conn["via"] = [
                    [col_x0 - 40, scy],
                    [col_x0 - 40, under_y],
                    [s_box[2] + 40, under_y],
                    [s_box[2] + 40, dcy],
                ]
        elif abs(ls2 - ld2) == 1:
            # Anchor the renderer's bend in the empty inter-layer gap.
            if abs(scy - dcy) > 1:
                conn["via"] = [[gap_x(min(ls2, ld2)), scy],
                               [gap_x(min(ls2, ld2)), dcy]]

    # --- connection labels --------------------------------------------------
    for conn in connections:
        if isinstance(conn, dict):
            fit_conn_label(conn, conn_label_cap)

    recompute_view_box(data)

    return [
        f"{len(components)} components → {max_layer + 1} layers "
        f"(x_gap={x_gap}, y_gap={y_gap}, max_comp_w={max_w:.0f}, "
        f"viewBox={data['meta']['viewBox']})"
    ]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def guess_type(dir_name: str) -> str:
    mapping = {
        "controller": "backend", "api": "backend", "service": "backend",
        "services": "backend", "router": "cloud", "middleware": "security",
        "model": "backend", "models": "backend", "data": "database",
        "database": "database", "db": "database", "cache": "database",
        "queue": "messagebus", "event": "messagebus", "events": "messagebus",
        "web": "frontend", "frontend": "frontend", "ui": "frontend",
        "cmd": "backend", "config": "backend", "docs": "backend",
    }
    return mapping.get(dir_name.lower(), "backend")
