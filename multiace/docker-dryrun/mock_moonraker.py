from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel


STATE_PATH = Path("/data/state.json")
GCODE_LOG = Path("/data/gcode.log")
SCRIPT_CALLS_PATH = Path("/data/script_calls.json")
CFG_PATH = Path("/data/ace.cfg")
UPLOAD_DIR = Path("/data/uploaded")
SLOT_OVERRIDE_PATH = Path("/data/slot_overrides.json")
NATIVE_OVERRIDE_PATH = Path("/data/native_overrides.json")
SOURCE_GRAPH_PATH = Path("/data/source_graph.json")
HEAD_SOURCE_STATE_PATH = Path("/data/head_source_state.json")

app = FastAPI(title="multiACE dry-run Moonraker")


def _make_slots(ace_idx: int) -> list[dict[str, Any]]:
    slots = []
    colors = [[220, 40, 40], [30, 120, 220], [245, 210, 60], [40, 200, 120]]
    mats = ["PLA", "PETG", "PLA", "ABS"]
    for i in range(4):
        c = colors[(i + ace_idx) % len(colors)]
        slots.append({
            "index": i,
            "status": "ready",
            "sku": "",
            "type": mats[i],
            "material": mats[i],
            "rfid": 2,
            "brand": f"DryRun{ace_idx + 1}",
            "color": c,
        })
    return slots


def _make_ace(ace_idx: int) -> dict[str, Any]:
    return {
        "idx": ace_idx,
        "connected": True,
        "protocol": "v1",
        "status": "ready",
        "temp": 32 + ace_idx,
        "humidity": None,
        "dryer_status": {
            "status": "stop",
            "target_temp": 0,
            "duration": 0,
            "remain_time": 0,
        },
        "gate_status": [1, 1, 1, 1],
        "feed_assist": -1,
        "slots": _make_slots(ace_idx),
    }


def _default_state() -> dict[str, Any]:
    aces = [_make_ace(0)]
    state = {
        "ace": {
            "status": "ready",
            "temp": 32,
            "dryer_status": {
                "status": "stop",
                "target_temp": 0,
                "duration": 0,
                "remain_time": 0,
            },
            "gate_status": [1, 1, 1, 1],
            "active_device": 0,
            "device_count": 1,
            "head_source": {"0": None, "1": None, "2": None, "3": None},
            "route": {
                "mode": "single_head",
                "primary_head": 0,
                "slot_targets": {"0": 0, "1": 0, "2": 0, "3": 0},
                "ace_targets": {"0": 0},
                "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
                "error": None,
            },
            "swap_in_progress": False,
            "aces": aces,
        },
        "filament_feed left": {
            "extruder0": _feed_empty(),
            "extruder1": _feed_empty(),
        },
        "filament_feed right": {
            "extruder2": _feed_empty(),
            "extruder3": _feed_empty(),
        },
        "save_variables": {"variables": {"ace__mode": "multi"}},
        "print_task_config": {
            "filament_vendor": ["NONE", "NONE", "NONE", "NONE"],
            "filament_type": ["NONE", "NONE", "NONE", "NONE"],
            "filament_sub_type": ["NONE", "NONE", "NONE", "NONE"],
            "filament_color_rgba": ["FFFFFFFF", "FFFFFFFF", "FFFFFFFF", "FFFFFFFF"],
        },
        "print_stats": {
            "filename": "",
            "total_duration": 0.0,
            "print_duration": 0.0,
            "filament_used": 0.0,
            "state": "standby",
            "exception": {},
            "message": "",
            "info": {"total_layer": None, "current_layer": None},
        },
        "idle_timeout": {"state": "Idle", "printing_time": 0.0},
        "toolhead": {"homed_axes": "xyz"},
        "machine_state": {
            "main_state": "IDLE",
            "action_code": "IDLE",
            "strict_transitions": False,
            "linger_auto_unload_once": False,
        },
    }
    # Seed one native head with a known filament so mixed native/ACE
    # preflight can be tested without touching hardware.
    native_feed = state["filament_feed left"]["extruder1"]
    native_feed["filament_detected"] = True
    native_feed["filament_in_toolhead"] = True
    native_feed["filament_at_extruder"] = True
    native_feed["channel_state"] = "load_finish"
    native_feed["channel_error"] = "ok"
    state["print_task_config"]["filament_vendor"][1] = "DryRunNative"
    state["print_task_config"]["filament_type"][1] = "PLA"
    state["print_task_config"]["filament_sub_type"][1] = "Basic"
    state["print_task_config"]["filament_color_rgba"][1] = "DC2828FF"
    return state


def _feed_empty() -> dict[str, Any]:
    return {
        "module_exist": True,
        "filament_detected": False,
        "filament_in_ace": False,
        "filament_in_toolhead": False,
        "filament_at_extruder": False,
        "disable_auto": False,
        "channel_state": "wait_insert",
        "channel_error": "ok",
        "channel_error_state": "none",
        "channel_action_state": "none",
    }


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        state = _default_state()
        _apply_cfg_route(state)
        _write_source_graph_from_route(state)
        _save_state(state)
        return state
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    _apply_cfg_route(state)
    state.setdefault("toolhead", {"homed_axes": "xyz"})
    return state


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _native_channel(head: int) -> dict[str, int | str]:
    return {
        0: {"module": "left", "channel": 1},
        1: {"module": "left", "channel": 0},
        2: {"module": "right", "channel": 0},
        3: {"module": "right", "channel": 1},
    }[head]


def _default_source_graph(ace_count: int) -> dict[str, Any]:
    heads: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    edges: list[dict[str, Any]] = []
    for head in range(4):
        heads[f"head:{head}"] = {
            "index": head,
            "enabled": True,
            "label": f"T{head}",
            "native_channel": _native_channel(head),
        }
        sources[f"native:{head}"] = {
            "kind": "native_feeder",
            "head": head,
            "module": _native_channel(head)["module"],
            "channel": _native_channel(head)["channel"],
            "label": f"Native Slot {head + 1}",
            "material": "",
            "brand": "",
            "subtype": "",
            "color": "",
            "ready": False,
            "execution": {
                "preload_length_mm": 950,
                "push_to_junction_length_mm": 0,
                "load_to_toolhead_length_mm": 750,
                "unload_to_junction_length_mm": 120,
                "full_unload_length_mm": 950,
                "toolhead_sync_retract_length_mm": 0,
                "feed_speed_mm_s": 25,
                "retract_speed_mm_s": 25,
                "toolhead_sync_retract_speed_mm_s": 10,
            },
            "execution_profile": "u1_native_feeder",
        }
        edges.append({
            "source": f"native:{head}",
            "head": f"head:{head}",
            "enabled": True,
            "priority": 10,
        })
    for ace in range(max(1, min(8, ace_count))):
        for slot in range(4):
            sources[f"ace:{ace}:{slot}"] = {
                "kind": "ace_slot",
                "ace": ace,
                "slot": slot,
                "label": f"ACE {ace + 1} Slot {slot + 1}",
                "material": "",
                "brand": "",
                "subtype": "",
                "color": "",
                "ready": False,
                "execution": {
                    "preload_length_mm": 0,
                    "push_to_junction_length_mm": 0,
                    "load_to_toolhead_length_mm": 0,
                    "unload_to_junction_length_mm": 0,
                    "full_unload_length_mm": 0,
                    "feed_speed_mm_s": 25,
                    "retract_speed_mm_s": 25,
                },
                "execution_profile": "ace_v1_slot",
            }
    return {
        "version": 1,
        "heads": heads,
        "sources": sources,
        "edges": edges,
        "profiles": {
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
                "retract": None,
                "full_unload": None,
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
                    "clears_current_source": False,
                },
                "retract": {
                    "command": "FEED_AUTO_RETRACT MODULE={module} CHANNEL={channel} EXTRUDER={head} LENGTH={unload_to_junction_length_mm} SPEED={retract_speed_mm_s} SYNC_LENGTH={toolhead_sync_retract_length_mm} SYNC_SPEED={toolhead_sync_retract_speed_mm_s}",
                    "requires_current_source": True,
                    "clears_current_source": True,
                },
                "full_unload": {
                    "command": "FEED_AUTO_FULL_UNLOAD MODULE={module} CHANNEL={channel} EXTRUDER={head} LENGTH={full_unload_length_mm} SPEED={retract_speed_mm_s}",
                    "requires_current_source": False,
                    "clears_current_source": False,
                },
                "swap": None,
                "capabilities": {
                    "can_preload": False,
                    "can_swap_in_print": False,
                    "requires_source_tracking": False,
                },
            },
        },
    }


def _write_source_graph_from_route(state: dict[str, Any]) -> None:
    ace_count = int(state["ace"].get("device_count", 1) or 1)
    graph = _default_source_graph(ace_count)
    route = state["ace"].get("route", {}) or {}
    ace_targets = route.get("ace_targets", {}) or {}
    slot_targets = route.get("slot_targets", {}) or {}
    for ace in range(ace_count):
        ace_target = ace_targets.get(str(ace), ace_targets.get(ace))
        for slot in range(4):
            slot_target = slot_targets.get(str(slot), slot_targets.get(slot))
            head = slot_target if slot_target is not None else ace_target
            try:
                head_i = int(head)
            except (TypeError, ValueError):
                continue
            if not 0 <= head_i < 4:
                continue
            graph["edges"].append({
                "source": f"ace:{ace}:{slot}",
                "head": f"head:{head_i}",
                "enabled": True,
                "priority": 50,
                "constraints": {
                    "requires_empty_head_before_load": True,
                    "allows_preload_while_other_head_prints": True,
                },
            })
    SOURCE_GRAPH_PATH.write_text(
        json.dumps(graph, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_ace_cfg() -> dict[str, str]:
    if not CFG_PATH.exists():
        return {}
    params: dict[str, str] = {}
    section = None
    for raw in CFG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section == "ace" and ":" in line:
            key, val = line.split(":", 1)
            params[key.strip()] = val.strip()
    return params


def _apply_cfg_route(state: dict[str, Any]) -> None:
    cfg = _read_ace_cfg()
    try:
        count = int(cfg.get("ace_device_count", "1"))
    except ValueError:
        count = 1
    count = max(1, min(8, count))
    aces = state["ace"].setdefault("aces", [])
    while len(aces) < count:
        aces.append(_make_ace(len(aces)))
    while len(aces) > count:
        aces.pop()
    for idx, ace in enumerate(aces):
        ace["idx"] = idx
    state["ace"]["device_count"] = count
    if state["ace"].get("active_device", 0) >= count:
        state["ace"]["active_device"] = 0

    head_modes: dict[str, str] = {}
    for h in range(4):
        raw = str(cfg.get(f"head{h}_mode", "")).strip().lower()
        if raw in ("ace", "native"):
            head_modes[str(h)] = raw
        else:
            head_modes[str(h)] = "ace" if h == 0 else "native"

    ace_heads = [int(h) for h, m in head_modes.items() if m == "ace"]
    ace_targets: dict[str, int | None] = {}
    error = None
    for ace_idx in range(count):
        raw = str(cfg.get(f"ace{ace_idx}_head", "")).strip().lower()
        if raw in ("", "none", "native", "off", "-1"):
            target = None
        else:
            try:
                target = int(raw)
            except ValueError:
                target = None
                error = f"ace{ace_idx}_head must be 0..3 or none, got {raw}"
            if target is not None and (target < 0 or target > 3):
                error = f"ace{ace_idx}_head must be 0..3 or none, got {raw}"
                target = None
            if target is not None and target not in ace_heads:
                error = f"ace{ace_idx}_head targets T{target}, but head{target}_mode is not ace"
        ace_targets[str(ace_idx)] = target
    if count >= 1 and ace_targets.get("0") is None and len(ace_heads) == 1:
        ace_targets["0"] = ace_heads[0]
    target_heads = sorted({h for h in ace_targets.values() if h is not None})
    unassigned = [h for h in ace_heads if h not in target_heads]
    if unassigned:
        error = "head(s) %s are configured as ACE but no aceN_head targets them" % (
            ", ".join(f"T{h}" for h in unassigned))
    if len(target_heads) == 1:
        mode = "single_head"
        primary = target_heads[0]
    elif not target_heads:
        mode = "native_only"
        primary = None
    else:
        mode = "multi_head"
        primary = target_heads[0]

    slot_targets = {
        str(slot): primary if mode == "single_head" else None
        for slot in range(4)
    }
    state["ace"]["route"] = {
        "mode": mode,
        "primary_head": primary,
        "slot_targets": slot_targets,
        "ace_targets": ace_targets,
        "head_modes": head_modes,
        "error": error,
    }


def _head_key(head: int) -> tuple[str, str]:
    module = "filament_feed left" if head < 2 else "filament_feed right"
    return module, f"extruder{head}" if head > 0 else "extruder0"


def _set_head_loaded(state: dict[str, Any], head: int, ace: int, slot: int) -> None:
    state["ace"]["head_source"][str(head)] = {
        "ace_index": ace,
        "slot": slot,
        "type": state["ace"]["aces"][ace]["slots"][slot].get("type", ""),
        "color": "ffffff",
        "brand": state["ace"]["aces"][ace]["slots"][slot].get("brand", ""),
    }
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = True
    feed["filament_in_ace"] = True
    feed["filament_in_toolhead"] = True
    feed["filament_at_extruder"] = True


def _set_native_head_loaded(state: dict[str, Any], head: int) -> None:
    state["ace"]["head_source"][str(head)] = None
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = True
    feed["filament_in_ace"] = False
    feed["filament_in_toolhead"] = True
    feed["filament_at_extruder"] = True
    feed["channel_state"] = "load_finish"
    feed["channel_error"] = "ok"


def _set_native_slot_preloaded(state: dict[str, Any], head: int) -> None:
    state["ace"]["head_source"][str(head)] = None
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = True
    feed["filament_in_ace"] = False
    feed["filament_in_toolhead"] = False
    feed["filament_at_extruder"] = False
    feed["channel_state"] = "preload_finish"
    feed["channel_error"] = "ok"


def _set_native_toolhead_unloaded(state: dict[str, Any], head: int) -> None:
    state["ace"]["head_source"][str(head)] = None
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = True
    feed["filament_in_ace"] = False
    feed["filament_in_toolhead"] = False
    feed["filament_at_extruder"] = False
    feed["channel_state"] = "unload_finish"
    feed["channel_error"] = "ok"

def _set_native_unload_finished_with_toolhead_filament(state: dict[str, Any], head: int) -> None:
    state["ace"]["head_source"][str(head)] = None
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = True
    feed["filament_in_ace"] = False
    feed["filament_in_toolhead"] = True
    feed["filament_at_extruder"] = True
    feed["channel_state"] = "unload_finish"
    feed["channel_error"] = "ok"


def _set_native_source_retracted(state: dict[str, Any], head: int) -> None:
    state["ace"]["head_source"][str(head)] = None
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = True
    feed["filament_in_ace"] = False
    feed["filament_in_toolhead"] = False
    feed["filament_at_extruder"] = False
    feed["channel_state"] = "preload_finish"
    feed["channel_error"] = "ok"


def _set_native_source_full_unloaded(state: dict[str, Any], head: int) -> None:
    state["ace"]["head_source"][str(head)] = None
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = False
    feed["filament_in_ace"] = False
    feed["filament_in_toolhead"] = False
    feed["filament_at_extruder"] = False
    feed["channel_state"] = "wait_insert"
    feed["channel_error"] = "ok"


def _set_head_empty(state: dict[str, Any], head: int) -> None:
    state["ace"]["head_source"][str(head)] = None
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = False
    feed["filament_in_ace"] = False
    feed["filament_in_toolhead"] = False
    feed["filament_at_extruder"] = False
    feed["channel_state"] = "unload_finish"
    feed["channel_error"] = "ok"


def _check_load_route(state: dict[str, Any], head: int, ace: int, slot: int) -> None:
    aces = state["ace"].get("aces", []) or []
    if ace < 0 or ace >= len(aces):
        raise ValueError(f"ACE {ace} is not available")
    if slot < 0 or slot >= 4:
        raise ValueError(f"SLOT {slot} is outside 0..3")
    if head < 0 or head >= 4:
        raise ValueError(f"HEAD {head} is outside 0..3")

    graph = _read_source_graph()
    if _source_graph_allows_ace(graph, head, ace, slot):
        return
    raise ValueError(
        f"source graph has no enabled edge ace:{ace}:{slot} -> head:{head}")


def _read_source_graph() -> dict[str, Any]:
    if not SOURCE_GRAPH_PATH.exists():
        raise ValueError(f"source graph not found at {SOURCE_GRAPH_PATH}")
    try:
        graph = json.loads(SOURCE_GRAPH_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to read source graph: {exc}")
    if not isinstance(graph, dict):
        raise ValueError("source graph must be an object")
    if graph.get("version") != 1:
        raise ValueError(f"unsupported source graph version {graph.get('version')!r}")
    return graph


def _source_graph_allows_ace(
    graph: dict[str, Any], head: int, ace: int, slot: int
) -> bool:
    sources = graph.get("sources") or {}
    heads = graph.get("heads") or {}
    source_id = f"ace:{ace}:{slot}"
    head_id = f"head:{head}"
    source = sources.get(source_id) or {}
    if source.get("kind") != "ace_slot":
        return False
    if int(source.get("ace", -1)) != ace or int(source.get("slot", -1)) != slot:
        return False
    if head_id not in heads:
        return False
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or edge.get("enabled", True) is False:
            continue
        if edge.get("source") == source_id and edge.get("head") == head_id:
            return True
    return False


def _source_graph_allows_native(
    graph: dict[str, Any], head: int, module: str, channel: int
) -> bool:
    sources = graph.get("sources") or {}
    head_id = f"head:{head}"
    if head_id not in (graph.get("heads") or {}):
        return False
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or edge.get("enabled", True) is False:
            continue
        if edge.get("head") != head_id:
            continue
        source = sources.get(edge.get("source")) or {}
        if source.get("kind") != "native_feeder":
            continue
        src_module = source.get("module")
        src_channel = source.get("channel")
        try:
            src_channel = int(src_channel)
        except (TypeError, ValueError):
            source_head = source.get("head")
            try:
                src_channel = int(_native_channel(int(source_head))["channel"])
            except Exception:
                continue
        if src_module is None:
            try:
                src_module = _native_channel(int(source.get("head")))["module"]
            except Exception:
                continue
        if str(src_module) == str(module) and int(src_channel) == int(channel):
            return True
    return False


def _source_graph_has_ace_head(graph: dict[str, Any], head: int) -> bool:
    sources = graph.get("sources") or {}
    head_id = f"head:{head}"
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or edge.get("enabled", True) is False:
            continue
        if edge.get("head") != head_id:
            continue
        source = sources.get(edge.get("source")) or {}
        if source.get("kind") == "ace_slot":
            return True
    return False


def _prune_stale_head_sources_for_graph(state: dict[str, Any]) -> None:
    graph = _read_source_graph()
    head_source = state["ace"].get("head_source") or {}
    for raw_head, src in list(head_source.items()):
        if src is None:
            continue
        try:
            head = int(raw_head)
            ace = int(src.get("ace_index"))
            slot = int(src.get("slot"))
        except Exception:
            head_source[str(raw_head)] = None
            continue
        if not _source_graph_allows_ace(graph, head, ace, slot):
            head_source[str(head)] = None


def _parse_args(line: str) -> tuple[str, dict[str, str]]:
    parts = line.strip().split()
    if not parts:
        return "", {}
    args = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            args[k.upper()] = v.strip('"')
    return parts[0], args


def _apply_script(script: str) -> None:
    state = _load_state()
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        cmd, args = _parse_args(line)
        GCODE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with GCODE_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
        if cmd == "ACE_LOAD_HEAD":
            if "HEAD" not in args or "ACE" not in args or "SLOT" not in args:
                raise ValueError("ACE_LOAD_HEAD requires HEAD, ACE and SLOT")
            head = int(args.get("HEAD", "0"))
            ace = int(args.get("ACE", "0"))
            slot = int(args.get("SLOT", "0"))
            _check_load_route(state, head, ace, slot)
            _set_head_loaded(state, head, ace, slot)
        elif cmd == "ACE_UNLOAD_HEAD":
            _set_head_empty(state, int(args.get("HEAD", "0")))
        elif cmd == "ACE_SWAP_HEAD":
            if "HEAD" not in args or "ACE" not in args or "SLOT" not in args:
                raise ValueError("ACE_SWAP_HEAD requires HEAD, ACE and SLOT")
            head = int(args.get("HEAD", "0"))
            ace = int(args.get("ACE", "0"))
            slot = int(args.get("SLOT", "0"))
            _check_load_route(state, head, ace, slot)
            if head in (state["ace"].get("ghost_heads") or []):
                raise ValueError(
                    f"SWAP refused: head {head} is a ghost with no head_source")
            _set_head_loaded(state, head, ace, slot)
        elif cmd == "ACE_UNLOAD_ALL_HEADS":
            for h in range(4):
                _set_head_empty(state, h)
            state["ace"]["ghost_heads"] = []
        elif cmd == "PRINT_START":
            graph = _read_source_graph()
            ghost_heads = []
            for h in range(4):
                src = state["ace"]["head_source"].get(str(h))
                if src is not None:
                    try:
                        participates = _source_graph_allows_ace(
                            graph,
                            h,
                            int(src.get("ace_index")),
                            int(src.get("slot")),
                        )
                    except Exception:
                        participates = False
                else:
                    participates = _source_graph_has_ace_head(graph, h)
                if not participates:
                    continue
                module, key = _head_key(h)
                feed = state[module][key]
                detected = bool(feed.get("filament_detected"))
                if detected and src is None:
                    ghost_heads.append(h)
                elif (not detected) and src is not None:
                    state["ace"]["head_source"][str(h)] = None
            state["ace"]["ghost_heads"] = ghost_heads
        elif cmd == "FEED_AUTO":
            head = int(args.get("EXTRUDER", "0"))
            module = args.get("MODULE", "")
            channel = int(args.get("CHANNEL", "0"))
            graph = _read_source_graph()
            if not _source_graph_allows_native(graph, head, module, channel):
                raise ValueError(
                    f"FEED_AUTO route mismatch for T{head}: "
                    f"MODULE={module} CHANNEL={channel}")
            if int(args.get("LOAD", "0") or 0) == 1:
                machine = state.setdefault("machine_state", {})
                if (machine.get("strict_transitions")
                        and machine.get("main_state") == "AUTO_UNLOAD"):
                    raise ValueError(
                        "Failed to change state: Invalid state transition "
                        "from AUTO_UNLOAD to AUTO_LOAD")
                machine["main_state"] = "AUTO_LOAD"
                machine["action_code"] = "AUTO_LOADING"
                _set_native_head_loaded(state, head)
                machine["main_state"] = "IDLE"
                machine["action_code"] = "IDLE"
            elif int(args.get("UNLOAD", "0") or 0) == 1:
                machine = state.setdefault("machine_state", {})
                machine["main_state"] = "AUTO_UNLOAD"
                machine["action_code"] = "AUTO_UNLOADING"
                if machine.get("linger_auto_unload_once"):
                    machine["linger_auto_unload_once"] = False
                    _set_native_unload_finished_with_toolhead_filament(state, head)
                else:
                    _set_native_toolhead_unloaded(state, head)
                    machine["main_state"] = "IDLE"
                    machine["action_code"] = "IDLE"
            else:
                raise ValueError("FEED_AUTO dry-run supports LOAD=1 or UNLOAD=1")
        elif cmd == "FEED_AUTO_RETRACT":
            head = int(args.get("EXTRUDER", "0"))
            module = args.get("MODULE", "")
            channel = int(args.get("CHANNEL", "0"))
            graph = _read_source_graph()
            if not _source_graph_allows_native(graph, head, module, channel):
                raise ValueError(
                    f"FEED_AUTO_RETRACT route mismatch for T{head}: "
                    f"MODULE={module} CHANNEL={channel}")
            _set_native_source_retracted(state, head)
        elif cmd == "FEED_AUTO_FULL_UNLOAD":
            head = int(args.get("EXTRUDER", "0"))
            module = args.get("MODULE", "")
            channel = int(args.get("CHANNEL", "0"))
            graph = _read_source_graph()
            if not _source_graph_allows_native(graph, head, module, channel):
                raise ValueError(
                    f"FEED_AUTO_FULL_UNLOAD route mismatch for T{head}: "
                    f"MODULE={module} CHANNEL={channel}")
            _set_native_source_full_unloaded(state, head)
        elif cmd == "G28":
            axes = str(args.get("AXES", "") or args.get("AXIS", "") or "").strip().lower()
            if not axes:
                state["toolhead"] = {"homed_axes": "xyz"}
            else:
                if axes in ("x", "y", "z"):
                    current = set(str((state.get("toolhead") or {}).get("homed_axes") or "").lower())
                    current.update(list(axes))
                    state["toolhead"] = {"homed_axes": "".join(sorted(current))}
                else:
                    state["toolhead"] = {"homed_axes": "xyz"}
        elif cmd == "SAVE_VARIABLE":
            var = args.get("VARIABLE")
            val = args.get("VALUE", "")
            if var:
                state["save_variables"]["variables"][var] = val.strip("'")
        elif cmd == "SET_PRINT_FILAMENT_CONFIG":
            head = int(args.get("CONFIG_EXTRUDER", "0"))
            ptc = state["print_task_config"]
            if 0 <= head < 4:
                ptc["filament_type"][head] = args.get("FILAMENT_TYPE", "").strip('"')
                ptc["filament_vendor"][head] = args.get("VENDOR", "").strip('"')
                ptc["filament_sub_type"][head] = args.get("FILAMENT_SUBTYPE", "").strip('"')
                ptc["filament_color_rgba"][head] = args.get("FILAMENT_COLOR_RGBA", "FFFFFFFF")
        elif cmd == "MULTIACE_REFRESH_SOURCE_GRAPH":
            _prune_stale_head_sources_for_graph(state)
    _save_state(state)


class ScriptPayload(BaseModel):
    script: str

class DryRunNativeHead(BaseModel):
    head: int
    material: str = "PLA"
    color: str = "#ffffff"
    vendor: str = "DryRunNative"
    subtype: str = "Basic"
    loaded: bool = True
    preloaded: bool = False

class DryRunSlot(BaseModel):
    ace: int = 0
    slot: int
    material: str = "PLA"
    color: str = "#ffffff"
    brand: str = "DryRunACE"
    status: str = "ready"

class DryRunHeadSource(BaseModel):
    head: int
    ace: int = 0
    slot: int = 0
    material: str = "PLA"
    color: str = "FFFFFF"
    brand: str = "DryRunACE"
    load_failed: bool = False
    sensor_loaded: bool = False

class DryRunUnknownLoadedHead(BaseModel):
    head: int
    channel_state: str = "load_finish"
    channel_error: str = "ok"

class DryRunScenario(BaseModel):
    ace_device_count: int = 1
    head_modes: dict[str, str] | None = None
    ace_targets: dict[str, int | None] | None = None
    native_heads: list[DryRunNativeHead] = []
    slots: list[DryRunSlot] = []
    head_sources: list[DryRunHeadSource] = []
    unknown_loaded_heads: list[DryRunUnknownLoadedHead] = []
    strict_machine_transitions: bool = False
    linger_auto_unload_once: bool = False
    homed_axes: str = "xyz"
    print_state: str = "standby"
    idle_state: str = "Idle"


@app.get("/server/info")
async def server_info() -> dict[str, Any]:
    return {"result": {"klippy_connected": True, "dry_run": True}}


@app.get("/printer/info")
async def printer_info() -> dict[str, Any]:
    return {"result": {"state": "ready", "state_message": "Dry-run printer is ready"}}


@app.get("/printer/objects/query")
async def objects_query() -> dict[str, Any]:
    return {"result": {"eventtime": time.time(), "status": _load_state()}}


@app.get("/printer/objects/list")
async def objects_list() -> dict[str, Any]:
    return {"result": {"objects": list(_load_state().keys())}}


@app.post("/printer/gcode/script")
async def gcode_script(payload: ScriptPayload) -> dict[str, Any]:
    calls = []
    if SCRIPT_CALLS_PATH.exists():
        try:
            calls = json.loads(SCRIPT_CALLS_PATH.read_text(encoding="utf-8"))
        except Exception:
            calls = []
    calls.append({"ts": time.time(), "script": payload.script})
    SCRIPT_CALLS_PATH.write_text(json.dumps(calls, indent=2), encoding="utf-8")
    try:
        _apply_script(payload.script)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"result": "ok"}


@app.post("/printer/restart")
async def printer_restart() -> dict[str, Any]:
    return {"result": "ok"}


@app.post("/server/files/upload")
async def files_upload(
    file: UploadFile = File(...),
    root: str = Form("gcodes"),
    print: str = Form("false"),
) -> dict[str, Any]:
    safe_name = Path(file.filename or "upload.gcode").name
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(await file.read())
    return {
        "result": {
            "item": {
                "root": root,
                "path": safe_name,
                "size": dest.stat().st_size,
            },
            "print_started": str(print).lower() == "true",
        }
    }


@app.post("/machine/reboot")
async def machine_reboot() -> dict[str, Any]:
    return {"result": "ok"}


@app.websocket("/websocket")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            try:
                msg = await ws.receive_text()
                data = json.loads(msg)
                await ws.send_json({"id": data.get("id"), "result": {}})
            except WebSocketDisconnect:
                return
            except Exception:
                await ws.send_json({"method": "notify_gcode_response", "params": ["dry-run"]})
    except WebSocketDisconnect:
        return


@app.get("/dry-run/gcode-log")
async def gcode_log() -> JSONResponse:
    if not GCODE_LOG.exists():
        return JSONResponse({"lines": []})
    return JSONResponse({"lines": GCODE_LOG.read_text(encoding="utf-8").splitlines()})


@app.get("/dry-run/script-calls")
async def script_calls() -> JSONResponse:
    if not SCRIPT_CALLS_PATH.exists():
        return JSONResponse({"calls": []})
    try:
        calls = json.loads(SCRIPT_CALLS_PATH.read_text(encoding="utf-8"))
    except Exception:
        calls = []
    return JSONResponse({"calls": calls})


@app.get("/dry-run/uploaded/{name:path}")
async def uploaded_file(name: str) -> JSONResponse:
    safe_name = Path(name or "").name
    if not safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = UPLOAD_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="uploaded file not found")
    return JSONResponse({
        "name": safe_name,
        "size": path.stat().st_size,
        "content": path.read_text(encoding="utf-8", errors="replace"),
    })


@app.post("/dry-run/reset")
async def reset() -> dict[str, Any]:
    state = _default_state()
    _apply_cfg_route(state)
    _write_source_graph_from_route(state)
    _save_state(state)
    if GCODE_LOG.exists():
        GCODE_LOG.unlink()
    if SCRIPT_CALLS_PATH.exists():
        SCRIPT_CALLS_PATH.unlink()
    for p in (SLOT_OVERRIDE_PATH, NATIVE_OVERRIDE_PATH, HEAD_SOURCE_STATE_PATH):
        if p.exists():
            p.unlink()
    if UPLOAD_DIR.exists():
        for p in UPLOAD_DIR.iterdir():
            if p.is_file():
                p.unlink()
    return {"ok": True, "state": state}


@app.post("/dry-run/scenario")
async def scenario(payload: DryRunScenario) -> dict[str, Any]:
    state = _default_state()
    cfg_lines = [
        "[save_variables]",
        "filename: /data/ace_vars.cfg",
        "[ace]",
        f"ace_device_count: {max(1, min(8, payload.ace_device_count))}",
    ]
    head_modes = payload.head_modes or {
        "0": "ace", "1": "native", "2": "native", "3": "native",
    }
    for head in range(4):
        mode = str(head_modes.get(str(head), head_modes.get(head, "native"))).lower()
        if mode not in ("ace", "native"):
            mode = "native"
        cfg_lines.append(f"head{head}_mode: {mode}")
    ace_targets = payload.ace_targets or {"0": 0}
    for ace_idx in range(max(1, min(8, payload.ace_device_count))):
        target = ace_targets.get(str(ace_idx), ace_targets.get(ace_idx))
        cfg_lines.append(
            f"ace{ace_idx}_head: {'none' if target is None else int(target)}")
    CFG_PATH.write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")
    _apply_cfg_route(state)
    _write_source_graph_from_route(state)
    state["machine_state"]["strict_transitions"] = bool(
        payload.strict_machine_transitions)
    state["machine_state"]["linger_auto_unload_once"] = bool(
        payload.linger_auto_unload_once)
    state["toolhead"] = {"homed_axes": payload.homed_axes}
    state["print_stats"]["state"] = payload.print_state
    state["idle_timeout"]["state"] = payload.idle_state

    for h in range(4):
        _set_head_empty(state, h)
        state["print_task_config"]["filament_vendor"][h] = "NONE"
        state["print_task_config"]["filament_type"][h] = "NONE"
        state["print_task_config"]["filament_sub_type"][h] = "NONE"
        state["print_task_config"]["filament_color_rgba"][h] = "FFFFFFFF"

    for native in payload.native_heads:
        head = int(native.head)
        if head < 0 or head >= 4:
            raise HTTPException(status_code=400, detail=f"invalid native head {head}")
        if native.loaded:
            _set_native_head_loaded(state, head)
        elif native.preloaded:
            _set_native_slot_preloaded(state, head)
        else:
            _set_head_empty(state, head)
        rgba = native.color.strip().lstrip("#")[:6].upper() or "FFFFFF"
        state["print_task_config"]["filament_vendor"][head] = native.vendor
        state["print_task_config"]["filament_type"][head] = native.material
        state["print_task_config"]["filament_sub_type"][head] = native.subtype
        state["print_task_config"]["filament_color_rgba"][head] = rgba + "FF"

    for item in payload.slots:
        ace = int(item.ace)
        slot = int(item.slot)
        if ace < 0 or ace >= len(state["ace"]["aces"]) or slot < 0 or slot >= 4:
            raise HTTPException(status_code=400, detail=f"invalid ACE slot {ace}/{slot}")
        c = item.color.strip().lstrip("#")[:6]
        rgb = [int(c[i:i + 2], 16) for i in (0, 2, 4)] if len(c) == 6 else [255, 255, 255]
        state["ace"]["aces"][ace]["slots"][slot].update({
            "status": item.status,
            "type": item.material,
            "material": item.material,
            "brand": item.brand,
            "color": rgb,
            "rfid": 0 if item.status == "empty" else 2,
        })
        state["ace"]["aces"][ace]["gate_status"][slot] = (
            0 if item.status == "empty" else 1)

    for src in payload.head_sources:
        head = int(src.head)
        if head < 0 or head >= 4:
            raise HTTPException(status_code=400, detail=f"invalid head source {head}")
        state["ace"]["head_source"][str(head)] = {
            "ace_index": int(src.ace),
            "slot": int(src.slot),
            "type": src.material,
            "color": src.color.strip().lstrip("#")[:6].upper() or "FFFFFF",
            "brand": src.brand,
            "load_failed": bool(src.load_failed),
        }
        if src.sensor_loaded:
            module, key = _head_key(head)
            feed = state[module][key]
            feed["filament_detected"] = True
            feed["filament_in_ace"] = True
            feed["filament_in_toolhead"] = True
            feed["filament_at_extruder"] = True

    for unknown in payload.unknown_loaded_heads:
        head = int(unknown.head)
        if head < 0 or head >= 4:
            raise HTTPException(status_code=400, detail=f"invalid unknown loaded head {head}")
        module, key = _head_key(head)
        feed = state[module][key]
        feed["filament_detected"] = True
        feed["filament_in_ace"] = False
        feed["filament_in_toolhead"] = True
        feed["filament_at_extruder"] = True
        feed["channel_state"] = unknown.channel_state
        feed["channel_action_state"] = unknown.channel_state
        feed["channel_error"] = unknown.channel_error
        feed["channel_error_state"] = (
            "none" if unknown.channel_error in ("", "ok") else unknown.channel_state)

    _save_state(state)
    for p in (SLOT_OVERRIDE_PATH, NATIVE_OVERRIDE_PATH, HEAD_SOURCE_STATE_PATH):
        if p.exists():
            p.unlink()
    if GCODE_LOG.exists():
        GCODE_LOG.unlink()
    if SCRIPT_CALLS_PATH.exists():
        SCRIPT_CALLS_PATH.unlink()
    if UPLOAD_DIR.exists():
        for p in UPLOAD_DIR.iterdir():
            if p.is_file():
                p.unlink()
    return {"ok": True, "state": state}


@app.post("/dry-run/source-graph")
async def source_graph(payload: dict[str, Any]) -> dict[str, Any]:
    graph = payload.get("graph") if isinstance(payload, dict) else None
    if not isinstance(graph, dict):
        raise HTTPException(status_code=400, detail="graph must be an object")
    SOURCE_GRAPH_PATH.write_text(
        json.dumps(graph, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"ok": True, "path": str(SOURCE_GRAPH_PATH)}
