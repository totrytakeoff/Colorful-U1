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


def default_graph(ace_count: int = 1) -> dict[str, Any]:
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
            "label": f"Native T{head}",
            "material": "",
            "brand": "",
            "subtype": "",
            "color": "",
            "ready": False,
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
                "execution_profile": "ace_v1_slot",
            }

    return {
        "version": GRAPH_VERSION,
        "heads": heads,
        "sources": sources,
        "edges": edges,
        "profiles": deepcopy(DEFAULT_PROFILES),
    }


def normalize_graph(graph: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(graph if isinstance(graph, dict) else {})
    out["version"] = int(out.get("version") or GRAPH_VERSION)
    out.setdefault("heads", {})
    out.setdefault("sources", {})
    out.setdefault("edges", [])
    profiles = out.setdefault("profiles", {})
    for profile_id, profile in DEFAULT_PROFILES.items():
        profiles.setdefault(profile_id, deepcopy(profile))
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
        graph = default_graph(ace_count)
        source = "generated"
    else:
        try:
            graph = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            graph = default_graph(ace_count)
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


def _source_for_loaded_toolhead(graph: dict[str, Any], parsed: dict[str, Any],
                                head: int) -> str | None:
    th = _head_sensor(parsed, head)
    if th.get("head_source_known"):
        try:
            return "ace:%d:%d" % (int(th.get("ace")), int(th.get("slot")))
        except (TypeError, ValueError):
            return None
    if th.get("filament_at_extruder"):
        head_id = f"head:{head}"
        native_sources = []
        non_native_sources = []
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
            else:
                non_native_sources.append(source_id)
        if len(native_sources) == 1 and not non_native_sources:
            return native_sources[0]
    return None


def source_state(graph: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    heads_state: dict[str, Any] = {}
    for head_id, head in (graph.get("heads") or {}).items():
        try:
            idx = int(head.get("index"))
        except (TypeError, ValueError):
            continue
        th = _head_sensor(parsed, idx)
        sensor = bool(th.get("filament_at_extruder"))
        current = _source_for_loaded_toolhead(graph, parsed, idx)
        load_failed = bool(th.get("load_failed"))
        if load_failed:
            confidence = "failed"
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
        sources[source_id].update({
            "material": th.get("material") or sources[source_id].get("material", ""),
            "brand": th.get("brand") or sources[source_id].get("brand", ""),
            "subtype": th.get("sku") or sources[source_id].get("subtype", ""),
            "color": th.get("color") or sources[source_id].get("color", ""),
            "ready": bool(th.get("filament_detected")
                          and th.get("filament_at_extruder")
                          and th.get("channel_error") in (None, "", "ok")
                          and str(th.get("channel_state") or "") in (
                              "load_finish", "preload_finish")),
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
            empty = slot.get("state") == "empty"
            sources[source_id].update({
                "material": slot.get("material") or sources[source_id].get("material", ""),
                "brand": slot.get("brand") or sources[source_id].get("brand", ""),
                "subtype": slot.get("sku") or sources[source_id].get("subtype", ""),
                "color": slot.get("color") or sources[source_id].get("color", ""),
                "ready": not empty,
                "state": slot.get("state") or "",
            })
    return sources


def live_loadout(graph: dict[str, Any], parsed: dict[str, Any],
                 color_name_fn=None) -> list[dict[str, Any]]:
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
        if source.get("ready") is False:
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
            "state": source.get("state") or "",
            "execution_profile": source.get("execution_profile") or "",
        }
        if kind == "native_feeder":
            row.update({
                "module": (head.get("native_channel") or {}).get("module"),
                "channel": (head.get("native_channel") or {}).get("channel"),
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
