from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any


GRAPH_VERSION = 1
HEAD_COUNT = 4
NATIVE_CHANNELS = {
    0: {"module": "left", "channel": 1},
    1: {"module": "left", "channel": 0},
    2: {"module": "right", "channel": 0},
    3: {"module": "right", "channel": 1},
}
SOURCE_EXECUTION_DEFAULTS = {
    "native_feeder": {
        "preload_length_mm": 950,
    },
    "ace_slot": {
        "preload_length_mm": 0,
    },
}

DEFAULT_PROFILES = {
    "ace_v1_slot": {
        "kind": "ace_slot",
        "load": {
            "command": "ACE_LOAD_HEAD HEAD={head} ACE={ace} SLOT={slot}",
            "requires_empty_head": True,
            "sets_current_source": True,
        },
        "unload": {
            "command": "ACE_UNLOAD_HEAD HEAD={head}",
            "requires_current_source": True,
            "clears_current_source": True,
        },
        "swap": {
            "command": "ACE_SWAP_HEAD HEAD={head} ACE={ace} SLOT={slot}",
            "requires_routed_edge": True,
            "sets_current_source": True,
        },
        "capabilities": {
            "can_preload": True,
            "can_swap_in_print": True,
            "requires_source_tracking": True,
        },
    },
    "u1_native_feeder": {
        "kind": "native_feeder",
        "load": {
            "command": "FEED_AUTO MODULE={module} CHANNEL={channel} EXTRUDER={head} LOAD=1",
            "requires_empty_head": True,
            "sets_current_source": True,
        },
        "unload": {
            "command": "FEED_AUTO MODULE={module} CHANNEL={channel} EXTRUDER={head} UNLOAD=1",
            "requires_current_source": True,
            "clears_current_source": True,
        },
        "swap": None,
        "capabilities": {
            "can_preload": False,
            "can_swap_in_print": False,
            "requires_source_tracking": False,
        },
    },
}


class SourceGraphError(ValueError):
    pass


def graph_hash(graph: dict[str, Any]) -> str:
    payload = json.dumps(graph, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _coerce_head(value: Any) -> int | None:
    try:
        head = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= head < HEAD_COUNT:
        return head
    return None


def _legacy_route_head_mode(route: dict[str, Any], head: int) -> str:
    head_modes = route.get("head_modes") or {}
    mode = head_modes.get(str(head), head_modes.get(head))
    if str(mode).lower() in ("ace", "native"):
        return str(mode).lower()
    if route.get("mode") == "single_head":
        primary = _coerce_head(route.get("primary_head"))
        if primary == head:
            return "ace"
    return "native"


def _ace_edge(source_id: str, head: int, priority: int = 50) -> dict[str, Any]:
    return {
        "source": source_id,
        "head": f"head:{head}",
        "enabled": True,
        "priority": priority,
        "constraints": {
            "requires_empty_head_before_load": True,
            "allows_preload_while_other_head_prints": True,
        },
    }


def _legacy_ace_edges(parsed: dict[str, Any] | None,
                      ace_count: int) -> list[dict[str, Any]]:
    """Build ACE edges from the pre-source-graph route config.

    This is only used for generated graphs when source_graph.json does not
    exist yet. Saved source graphs are authoritative and are never expanded
    from legacy route fields.
    """
    if not isinstance(parsed, dict):
        return []
    route = ((parsed.get("route") or {}) if isinstance(parsed.get("route"), dict)
             else {})
    ace_targets = route.get("ace_targets") or {}
    slot_targets = route.get("slot_targets") or {}
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ace in range(max(1, min(8, int(ace_count or 1)))):
        ace_target = ace_targets.get(str(ace), ace_targets.get(ace))
        ace_head = _coerce_head(ace_target)
        for slot in range(4):
            slot_target = slot_targets.get(str(slot), slot_targets.get(slot))
            head = _coerce_head(slot_target)
            if head is None:
                head = ace_head
            if head is None:
                continue
            if _legacy_route_head_mode(route, head) != "ace":
                continue
            source_id = f"ace:{ace}:{slot}"
            head_id = f"head:{head}"
            key = (source_id, head_id)
            if key in seen:
                continue
            seen.add(key)
            edges.append(_ace_edge(source_id, head))
    return edges


def default_graph(ace_count: int = 1,
                  parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a conservative default graph.

    Native feeder edges are known from U1's fixed feed module/channel layout.
    ACE slot edges are intentionally not guessed here; the user must confirm
    PTFE wiring before an ACE slot can route to a head.
    """
    ace_count = max(1, min(8, int(ace_count or 1)))
    heads = {}
    sources = {}
    edges = []
    for head in range(HEAD_COUNT):
        head_id = f"head:{head}"
        native_id = f"native:{head}"
        heads[head_id] = {
            "index": head,
            "enabled": True,
            "label": f"T{head}",
            "native_channel": dict(NATIVE_CHANNELS[head]),
        }
        sources[native_id] = {
            "kind": "native_feeder",
            "head": head,
            "module": NATIVE_CHANNELS[head]["module"],
            "channel": NATIVE_CHANNELS[head]["channel"],
            "label": f"Native Slot {head + 1}",
            "material": "",
            "brand": "",
            "subtype": "",
            "color": "",
            "ready": False,
            "execution": deepcopy(SOURCE_EXECUTION_DEFAULTS["native_feeder"]),
            "execution_profile": "u1_native_feeder",
        }
        edges.append({
            "source": native_id,
            "head": head_id,
            "enabled": True,
            "priority": 10,
            "constraints": {
                "requires_empty_head_before_load": True,
                "allows_preload_while_other_head_prints": False,
            },
        })

    for ace in range(ace_count):
        for slot in range(4):
            source_id = f"ace:{ace}:{slot}"
            sources[source_id] = {
                "kind": "ace_slot",
                "ace": ace,
                "slot": slot,
                "label": f"ACE {ace + 1} Slot {slot + 1}",
                "material": "",
                "brand": "",
                "subtype": "",
                "color": "",
                "ready": False,
                "execution": deepcopy(SOURCE_EXECUTION_DEFAULTS["ace_slot"]),
                "execution_profile": "ace_v1_slot",
            }

    edges.extend(_legacy_ace_edges(parsed, ace_count))

    return {
        "version": GRAPH_VERSION,
        "heads": heads,
        "sources": sources,
        "edges": edges,
        "profiles": deepcopy(DEFAULT_PROFILES),
    }


def _merge_defaults(value: Any, defaults: Any) -> Any:
    if isinstance(value, dict) and isinstance(defaults, dict):
        out = deepcopy(value)
        for key, default_value in defaults.items():
            if key in out:
                out[key] = _merge_defaults(out[key], default_value)
            else:
                out[key] = deepcopy(default_value)
        return out
    return deepcopy(value)


def _normalize_source_labels(sources: dict[str, Any]) -> None:
    for source_id, source in sources.items():
        if not isinstance(source, dict):
            continue
        if source.get("kind") != "native_feeder":
            continue
        try:
            head = int(source.get("head", str(source_id).split(":")[1]))
        except (TypeError, ValueError, IndexError):
            continue
        current = str(source.get("label") or "").strip()
        legacy = "Native " + f"T{head}"
        if not current or current == legacy:
            source["label"] = f"Native Slot {head + 1}"


def normalize_graph(graph: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(graph if isinstance(graph, dict) else {})
    out["version"] = int(out.get("version") or GRAPH_VERSION)
    out.setdefault("heads", {})
    sources = out.setdefault("sources", {})
    if isinstance(sources, dict):
        _normalize_source_labels(sources)
    out.setdefault("edges", [])
    profiles = out.setdefault("profiles", {})
    for profile_id, profile in DEFAULT_PROFILES.items():
        if profile_id in profiles:
            profiles[profile_id] = _merge_defaults(profiles[profile_id], profile)
        else:
            profiles[profile_id] = deepcopy(profile)
    if isinstance(sources, dict):
        for source in sources.values():
            if not isinstance(source, dict):
                continue
            defaults = SOURCE_EXECUTION_DEFAULTS.get(source.get("kind"))
            if defaults is None:
                continue
            source["execution"] = _merge_defaults(
                source.get("execution") or {},
                defaults,
            )
    return out


def validate_graph(graph: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(graph, dict):
        return ["source graph must be an object"], warnings
    version = graph.get("version")
    if version != GRAPH_VERSION:
        errors.append(f"unsupported source graph version {version!r}")

    heads = graph.get("heads")
    sources = graph.get("sources")
    edges = graph.get("edges")
    profiles = graph.get("profiles") or {}
    if not isinstance(heads, dict):
        errors.append("heads must be an object")
        heads = {}
    if not isinstance(sources, dict):
        errors.append("sources must be an object")
        sources = {}
    if not isinstance(edges, list):
        errors.append("edges must be a list")
        edges = []
    if not isinstance(profiles, dict):
        errors.append("profiles must be an object")
        profiles = {}

    seen_indices: set[int] = set()
    for head_id, head in heads.items():
        if not isinstance(head, dict):
            errors.append(f"{head_id}: head entry must be an object")
            continue
        if head_id != f"head:{head.get('index')}":
            errors.append(f"{head_id}: id must match index as head:<index>")
        try:
            idx = int(head.get("index"))
        except (TypeError, ValueError):
            errors.append(f"{head_id}: index must be an integer")
            continue
        if idx < 0 or idx >= HEAD_COUNT:
            errors.append(f"{head_id}: index must be 0..3")
        if idx in seen_indices:
            errors.append(f"{head_id}: duplicate head index {idx}")
        seen_indices.add(idx)

    for source_id, source in sources.items():
        if not isinstance(source, dict):
            errors.append(f"{source_id}: source entry must be an object")
            continue
        kind = source.get("kind")
        profile_id = source.get("execution_profile")
        profile = profiles.get(profile_id)
        if kind not in ("native_feeder", "ace_slot"):
            errors.append(f"{source_id}: unknown source kind {kind!r}")
        execution = source.get("execution") or {}
        if execution and not isinstance(execution, dict):
            errors.append(f"{source_id}: execution must be an object")
            execution = {}
        if isinstance(execution, dict):
            for key in ("preload_length_mm",):
                if key not in execution or execution.get(key) in (None, ""):
                    continue
                try:
                    value = float(execution.get(key))
                except (TypeError, ValueError):
                    errors.append(f"{source_id}: execution.{key} must be a number")
                    continue
                if value < 0 or value > 3000:
                    errors.append(f"{source_id}: execution.{key} must be 0..3000")
        if not profile_id or not isinstance(profile, dict):
            errors.append(f"{source_id}: execution_profile {profile_id!r} missing")
        elif profile.get("kind") != kind:
            errors.append(
                f"{source_id}: profile {profile_id!r} kind does not match {kind!r}")
        if kind == "native_feeder":
            try:
                head = int(source.get("head"))
            except (TypeError, ValueError):
                head = None
            if head is not None and (head < 0 or head >= HEAD_COUNT):
                errors.append(f"{source_id}: native head must be 0..3")
            module = source.get("module")
            channel = source.get("channel")
            if module is None and head in NATIVE_CHANNELS:
                module = NATIVE_CHANNELS[head]["module"]
            if channel is None and head in NATIVE_CHANNELS:
                channel = NATIVE_CHANNELS[head]["channel"]
            if module not in ("left", "right"):
                errors.append(f"{source_id}: native module must be left or right")
            if channel not in (0, 1):
                errors.append(f"{source_id}: native channel must be 0 or 1")
        if kind == "ace_slot":
            try:
                ace = int(source.get("ace"))
                slot = int(source.get("slot"))
            except (TypeError, ValueError):
                errors.append(f"{source_id}: ace and slot must be integers")
                continue
            if ace < 0:
                errors.append(f"{source_id}: ace must be >= 0")
            if slot < 0 or slot >= 4:
                errors.append(f"{source_id}: slot must be 0..3")

    seen_edges: set[tuple[str, str]] = set()
    for idx, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f"edge[{idx}]: edge must be an object")
            continue
        source_id = str(edge.get("source") or "")
        head_id = str(edge.get("head") or "")
        if source_id not in sources:
            errors.append(f"edge[{idx}]: unknown source {source_id!r}")
        if head_id not in heads:
            errors.append(f"edge[{idx}]: unknown head {head_id!r}")
        key = (source_id, head_id)
        if key in seen_edges:
            errors.append(f"edge[{idx}]: duplicate edge {source_id} -> {head_id}")
        seen_edges.add(key)
        if edge.get("enabled", True) not in (True, False):
            errors.append(f"edge[{idx}]: enabled must be boolean")

    for source_id, source in sources.items():
        if source.get("kind") != "ace_slot":
            continue
        if not any(e.get("source") == source_id and e.get("enabled", True)
                   for e in edges if isinstance(e, dict)):
            warnings.append(f"{source_id}: ACE slot has no enabled head edge")

    return errors, warnings


def load_graph(path: str | Path, ace_count: int = 1,
               parsed: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        graph = default_graph(ace_count, parsed=parsed)
        source = "generated"
    else:
        try:
            graph = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            graph = default_graph(ace_count, parsed=parsed)
            errors = [f"failed to read source graph: {exc}"]
            return graph, {
                "path": str(p),
                "source": "error",
                "hash": graph_hash(graph),
                "errors": errors,
                "warnings": [],
            }
        graph = normalize_graph(graph)
        source = "file"
    errors, warnings = validate_graph(graph)
    return graph, {
        "path": str(p),
        "source": source,
        "hash": graph_hash(graph),
        "errors": errors,
        "warnings": warnings,
    }


def save_graph(path: str | Path, graph: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_graph(graph)
    errors, warnings = validate_graph(normalized)
    if errors:
        raise SourceGraphError("; ".join(errors))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(p)
    return {
        "path": str(p),
        "source": "file",
        "hash": graph_hash(normalized),
        "errors": [],
        "warnings": warnings,
    }


def _head_sensor(parsed: dict[str, Any], head: int) -> dict[str, Any]:
    for item in parsed.get("toolheads", []) or []:
        try:
            if int(item.get("idx")) == head:
                return item
        except (TypeError, ValueError):
            continue
    return {}


def _native_channel_for_source(source: dict[str, Any]) -> dict[str, Any]:
    module = source.get("module")
    channel = source.get("channel")
    try:
        head = int(source.get("head"))
    except (TypeError, ValueError):
        head = None
    if (module is None or channel is None) and head in NATIVE_CHANNELS:
        default = NATIVE_CHANNELS[head]
        if module is None:
            module = default.get("module")
        if channel is None:
            channel = default.get("channel")
    return {"module": module, "channel": channel}


def _source_for_loaded_toolhead(graph: dict[str, Any], parsed: dict[str, Any],
                                head: int) -> str | None:
    th = _head_sensor(parsed, head)
    if th.get("head_source_known"):
        try:
            return "ace:%d:%d" % (int(th.get("ace")), int(th.get("slot")))
        except (TypeError, ValueError):
            return None
    if th.get("filament_at_extruder") and th.get("filament_in_ace"):
        return None
    if th.get("filament_at_extruder") and not th.get("filament_in_ace"):
        head_id = f"head:{head}"
        native_sources = []
        sources = graph.get("sources") or {}
        for edge in graph.get("edges") or []:
            if not isinstance(edge, dict) or not edge.get("enabled", True):
                continue
            if edge.get("head") != head_id:
                continue
            source_id = edge.get("source")
            source = sources.get(source_id) or {}
            if source.get("kind") == "native_feeder":
                native_sources.append(source_id)
        if len(native_sources) == 1:
            return native_sources[0]
    return None


def source_state(graph: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    heads_state: dict[str, Any] = {}
    sources_runtime = runtime_sources(graph, parsed)
    for head_id, head in (graph.get("heads") or {}).items():
        try:
            idx = int(head.get("index"))
        except (TypeError, ValueError):
            continue
        th = _head_sensor(parsed, idx)
        sensor = bool(th.get("filament_at_extruder"))
        current = _source_for_loaded_toolhead(graph, parsed, idx)
        load_failed = bool(th.get("load_failed"))
        current_runtime = sources_runtime.get(current or "") or {}
        current_unready = (
            bool(current) and current_runtime.get("ready") is False)
        if load_failed:
            confidence = "failed"
        elif sensor and current_unready:
            confidence = "exhausted"
        elif sensor and current:
            confidence = "known"
        elif sensor and not current:
            confidence = "unknown"
        elif (not sensor) and current:
            confidence = "stale"
        else:
            confidence = "empty"
        heads_state[head_id] = {
            "head": head_id,
            "index": idx,
            "sensor_filament": sensor,
            "current_source": current,
            "source_confidence": confidence,
            "source_ready": current_runtime.get("ready") if current else None,
            "source_ready_reason": current_runtime.get("ready_reason") or "",
            "last_error": th.get("channel_error") or None,
            "updated_at": time.time(),
        }

    return {
        "version": GRAPH_VERSION,
        "source_graph_hash": graph_hash(graph),
        "heads": heads_state,
    }


def runtime_sources(graph: dict[str, Any], parsed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sources = deepcopy(graph.get("sources") or {})
    for th in parsed.get("toolheads", []) or []:
        try:
            head = int(th.get("idx"))
        except (TypeError, ValueError):
            continue
        source_id = f"native:{head}"
        if source_id not in sources:
            continue
        detected = bool(th.get("filament_detected"))
        in_ace_path = bool(th.get("filament_in_ace"))
        in_native_path = detected and not in_ace_path
        at_extruder = bool(th.get("filament_at_extruder"))
        channel_error = th.get("channel_error")
        channel_state = str(th.get("channel_state") or "")
        channel_ok = channel_error in (None, "", "ok")
        state_ok = channel_state in ("load_finish", "preload_finish")
        ready = bool(
            in_native_path
            and channel_ok
            and state_ok
        )
        reasons = []
        if not in_native_path:
            reasons.append("native slot empty")
        if not channel_ok:
            reasons.append("进料通道错误: %s" % channel_error)
        if in_native_path and not state_ok:
            reasons.append("通道状态: %s" % (channel_state or "unknown"))
        material = sources[source_id].get("material", "")
        brand = sources[source_id].get("brand", "")
        subtype = sources[source_id].get("subtype", "")
        color = sources[source_id].get("color", "")
        if in_native_path:
            material = th.get("material") or material
            brand = th.get("brand") or brand
            subtype = th.get("sku") or subtype
            color = th.get("color") or color
        sources[source_id].update({
            "material": material,
            "brand": brand,
            "subtype": subtype,
            "color": color,
            "ready": ready,
            "ready_reason": "ready" if ready else "; ".join(reasons),
            "state": channel_state,
            "status_details": {
                "filament_detected": detected,
                "filament_in_ace": in_ace_path,
                "filament_in_native_path": in_native_path,
                "filament_at_extruder": at_extruder,
                "channel_error": channel_error or "",
                "channel_state": channel_state,
            },
        })

    for ace in parsed.get("aces", []) or []:
        try:
            ace_idx = int(ace.get("idx"))
        except (TypeError, ValueError):
            continue
        for slot in ace.get("slots", []) or []:
            try:
                slot_idx = int(slot.get("idx"))
            except (TypeError, ValueError):
                continue
            source_id = f"ace:{ace_idx}:{slot_idx}"
            if source_id not in sources:
                continue
            state = slot.get("state") or ""
            empty = state == "empty"
            sources[source_id].update({
                "material": slot.get("material") or sources[source_id].get("material", ""),
                "brand": slot.get("brand") or sources[source_id].get("brand", ""),
                "subtype": slot.get("sku") or sources[source_id].get("subtype", ""),
                "color": slot.get("color") or sources[source_id].get("color", ""),
                "ready": not empty,
                "ready_reason": "ready" if not empty else "slot empty",
                "state": state,
                "status_details": {
                    "slot_state": state,
                },
            })
    return sources


def live_loadout(graph: dict[str, Any], parsed: dict[str, Any],
                 color_name_fn=None, include_unready: bool = False) -> list[dict[str, Any]]:
    sources = runtime_sources(graph, parsed)
    out: list[dict[str, Any]] = []
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or not edge.get("enabled", True):
            continue
        source_id = edge.get("source")
        head_id = edge.get("head")
        source = sources.get(source_id)
        head = (graph.get("heads") or {}).get(head_id)
        if not source or not head or not head.get("enabled", True):
            continue
        if source.get("ready") is False and not include_unready:
            continue
        try:
            head_idx = int(head.get("index"))
        except (TypeError, ValueError):
            continue
        color = str(source.get("color") or "").strip().lower()
        kind = source.get("kind")
        row = {
            "kind": "native" if kind == "native_feeder" else "ace",
            "source": source_id,
            "edge": {
                "source": source_id,
                "head": head_id,
                "priority": edge.get("priority", 100),
                "constraints": deepcopy(edge.get("constraints") or {}),
            },
            "key": "%s->%s" % (source_id, head_id),
            "head": head_idx,
            "head_id": head_id,
            "material": source.get("material") or "",
            "color": color,
            "name": color_name_fn(color) if (color_name_fn and color) else "",
            "ready": bool(source.get("ready")),
            "ready_reason": source.get("ready_reason") or (
                "ready" if source.get("ready") else "not ready"),
            "status_details": deepcopy(source.get("status_details") or {}),
            "state": source.get("state") or "",
            "execution_profile": source.get("execution_profile") or "",
            "execution": deepcopy(source.get("execution") or {}),
        }
        if kind == "native_feeder":
            channel = _native_channel_for_source(source)
            row.update({
                "module": channel.get("module"),
                "channel": channel.get("channel"),
            })
        if kind == "ace_slot":
            row.update({
                "ace": int(source.get("ace")),
                "slot": int(source.get("slot")),
            })
        out.append(row)
    out.sort(key=lambda x: (
        0 if x.get("kind") == "native" else 1,
        x.get("head", 99),
        x.get("ace", 99),
        x.get("slot", 99),
    ))
    return out
