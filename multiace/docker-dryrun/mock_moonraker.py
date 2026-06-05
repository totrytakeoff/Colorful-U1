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
CFG_PATH = Path("/data/ace.cfg")
UPLOAD_DIR = Path("/data/uploaded")
SLOT_OVERRIDE_PATH = Path("/data/slot_overrides.json")
NATIVE_OVERRIDE_PATH = Path("/data/native_overrides.json")

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

    mode = route.get("mode", "single_head")
    if mode == "single_head":
        target = route.get("primary_head")
        if target is None or int(target) != head:
            raise ValueError(f"HEAD {head} is not the configured ACE toolhead")
    else:
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
            _set_head_loaded(state, head, ace, slot)
        elif cmd == "ACE_UNLOAD_ALL_HEADS":
            for h in range(4):
                _set_head_empty(state, h)
        elif cmd == "FEED_AUTO":
            head = int(args.get("EXTRUDER", "0"))
            module = args.get("MODULE", "")
            channel = int(args.get("CHANNEL", "0"))
            expected_module, expected_key = _head_key(head)
            expected_channel = {
                0: 1,
                1: 0,
                2: 0,
                3: 1,
            }.get(head)
            if f"filament_feed {module}" != expected_module or channel != expected_channel:
                raise ValueError(
                    f"FEED_AUTO route mismatch for T{head}: "
                    f"MODULE={module} CHANNEL={channel}")
            head_modes = (state["ace"].get("route", {}) or {}).get("head_modes", {}) or {}
            if head_modes.get(str(head)) != "native":
                raise ValueError(f"FEED_AUTO dry-run direct path is only for native T{head}")
            if int(args.get("LOAD", "0") or 0) == 1:
                _set_native_head_loaded(state, head)
            elif int(args.get("UNLOAD", "0") or 0) == 1:
                _set_head_empty(state, head)
            else:
                raise ValueError("FEED_AUTO dry-run supports LOAD=1 or UNLOAD=1")
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
    _save_state(state)
    if GCODE_LOG.exists():
        GCODE_LOG.unlink()
    for p in (SLOT_OVERRIDE_PATH, NATIVE_OVERRIDE_PATH):
        if p.exists():
            p.unlink()
    if UPLOAD_DIR.exists():
        for p in UPLOAD_DIR.iterdir():
            if p.is_file():
                p.unlink()
    return {"ok": True, "state": state}
