from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel


STATE_PATH = Path("/data/state.json")
GCODE_LOG = Path("/data/gcode.log")
CFG_PATH = Path("/data/ace.cfg")

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
    return {
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
    }


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
        _save_state(state)
        return state
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    _apply_cfg_route(state)
    return state


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


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
    explicit = False
    for h in range(4):
        raw = str(cfg.get(f"head{h}_mode", "")).strip().lower()
        if raw in ("ace", "native"):
            explicit = True
            head_modes[str(h)] = raw
        else:
            head_modes[str(h)] = "native"

    if explicit:
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
        target_heads = sorted({h for h in ace_targets.values() if h is not None})
        if len(target_heads) == 1:
            mode = "single_head"
            primary = target_heads[0]
        elif not target_heads:
            mode = "native_only"
            primary = None
        else:
            mode = "multi_head"
            primary = target_heads[0]
    else:
        mode = cfg.get("ace_route_mode", "standard")
        if mode not in ("standard", "single_head"):
            mode = "standard"
        try:
            primary = int(cfg.get("ace_primary_head", "0"))
        except ValueError:
            primary = 0
        primary = max(0, min(3, primary))
        error = None
        if mode == "single_head":
            head_modes = {str(h): ("ace" if h == primary else "native") for h in range(4)}
            ace_targets = {str(ace): primary for ace in range(count)}
        else:
            head_modes = {str(h): "ace" for h in range(4)}
            ace_targets = {str(ace): (ace if ace < 4 else None) for ace in range(count)}

    slot_targets = {
        str(slot): primary if mode == "single_head"
        else (slot if mode == "standard" else None)
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


def _set_head_empty(state: dict[str, Any], head: int) -> None:
    state["ace"]["head_source"][str(head)] = None
    module, key = _head_key(head)
    feed = state[module][key]
    feed["filament_detected"] = False
    feed["filament_in_ace"] = False
    feed["filament_in_toolhead"] = False
    feed["filament_at_extruder"] = False


def _check_load_route(state: dict[str, Any], head: int, ace: int, slot: int) -> None:
    route = state["ace"].get("route", {}) or {}
    route_error = route.get("error")
    if route_error:
        raise ValueError(f"route error: {route_error}")
    aces = state["ace"].get("aces", []) or []
    if ace < 0 or ace >= len(aces):
        raise ValueError(f"ACE {ace} is not available")
    if slot < 0 or slot >= 4:
        raise ValueError(f"SLOT {slot} is outside 0..3")
    if head < 0 or head >= 4:
        raise ValueError(f"HEAD {head} is outside 0..3")

    head_modes = route.get("head_modes", {}) or {}
    if head_modes.get(str(head)) != "ace":
        raise ValueError(f"HEAD {head} is not configured as ACE")

    ace_targets = route.get("ace_targets", {}) or {}
    if str(ace) in ace_targets:
        target = ace_targets.get(str(ace))
        if target is None:
            raise ValueError(f"ACE {ace} is not assigned to any ACE toolhead")
        if int(target) != head:
            raise ValueError(f"ACE {ace} is assigned to HEAD {target}, not HEAD {head}")
        return

    mode = route.get("mode", "standard")
    if mode == "single_head":
        target = route.get("primary_head")
        if target is None or int(target) != head:
            raise ValueError(f"HEAD {head} is not the configured ACE toolhead")
    elif mode != "standard":
        raise ValueError(f"route mode {mode} requires explicit ACE target")


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
            head = int(args.get("HEAD", "0"))
            ace = int(args.get("ACE", "0"))
            slot = int(args.get("SLOT", str(head)))
            _check_load_route(state, head, ace, slot)
            _set_head_loaded(state, head, ace, slot)
        elif cmd == "ACE_UNLOAD_HEAD":
            _set_head_empty(state, int(args.get("HEAD", "0")))
        elif cmd == "ACE_SWAP_HEAD":
            head = int(args.get("HEAD", "0"))
            ace = int(args.get("ACE", "0"))
            slot = int(args.get("SLOT", str(head)))
            _check_load_route(state, head, ace, slot)
            _set_head_loaded(state, head, ace, slot)
        elif cmd == "ACE_UNLOAD_ALL_HEADS":
            for h in range(4):
                _set_head_empty(state, h)
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
    _save_state(state)


class ScriptPayload(BaseModel):
    script: str


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
    try:
        _apply_script(payload.script)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"result": "ok"}


@app.post("/printer/restart")
async def printer_restart() -> dict[str, Any]:
    return {"result": "ok"}


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


@app.post("/dry-run/reset")
async def reset() -> dict[str, Any]:
    state = _default_state()
    _apply_cfg_route(state)
    _save_state(state)
    if GCODE_LOG.exists():
        GCODE_LOG.unlink()
    return {"ok": True, "state": state}
