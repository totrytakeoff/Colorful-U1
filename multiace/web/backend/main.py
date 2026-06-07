"""
multiACE Web - FastAPI backend.

Serves the REST + WebSocket API consumed by both the bundled Vue/CDN
frontend and any future mobile app. Auth is delegated to nginx
(auth_request /auth_check → Moonraker /access/user), so this service
trusts every request that reaches it.

Environment variables:
  MOONRAKER_URL          default http://127.0.0.1:7125
  MULTIACE_CFG_PATH      default /home/lava/printer_data/config/extended/ace.cfg
  MULTIACE_FRONTEND_DIR  default ../frontend (relative to this file)
  MULTIACE_WEB_VERSION   default "0.1.0"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import websockets

try:
    from . import source_graph as sg
except ImportError:
    import source_graph as sg

_trace = logging.getLogger("multiace")
_trace.setLevel(logging.INFO)
if not _trace.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[multiace] %(message)s"))
    _trace.addHandler(_h)
    _trace.propagate = False

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")
MULTIACE_CFG_PATH = os.environ.get(
    "MULTIACE_CFG_PATH",
    "/home/lava/printer_data/config/extended/ace.cfg",
)
SNAPSHOT_DIR = os.environ.get(
    "MULTIACE_SNAPSHOT_DIR",
    "/home/lava/printer_data/config/extended/multiace/filament_snapshots",
)
OVERRIDE_FILE = os.environ.get(
    "MULTIACE_OVERRIDE_FILE",
    "/home/lava/printer_data/config/extended/multiace/slot_overrides.json",
)
NATIVE_OVERRIDE_FILE = os.environ.get(
    "MULTIACE_NATIVE_OVERRIDE_FILE",
    "/home/lava/printer_data/config/extended/multiace/native_overrides.json",
)
MATERIALS_FILE = os.environ.get(
    "MULTIACE_MATERIALS_FILE",
    "/home/lava/printer_data/config/extended/multiace/materials.json",
)
MULTIACE_SOURCE_GRAPH_PATH = os.environ.get(
    "MULTIACE_SOURCE_GRAPH_PATH",
    "/home/lava/printer_data/config/extended/multiace/source_graph.json",
)
DEFAULT_MATERIALS = [
    "PLA", "PLA+", "PLA-CF",
    "PETG", "PETG-CF", "PETG-HF",
    "ABS", "ASA",
    "TPU",
    "PA", "PA-CF", "PA-GF", "PA6-CF", "PA6-GF",
    "PC", "PC-ABS",
    "PVA",
]
I18N_DIR = os.environ.get(
    "MULTIACE_I18N_DIR",
    str((Path(__file__).resolve().parent.parent / "i18n")),
)
SCREEN_PROBE_URL = os.environ.get("SCREEN_PROBE_URL", "http://127.0.0.1:8092/snapshot")

HOMING_FLAG_PATH = os.environ.get(
    "MULTIACE_HOMING_FLAG", "/tmp/multiace_homing_active")
HOMING_GATE_TTL = float(os.environ.get("MULTIACE_HOMING_GATE_TTL", "2.0"))

def _homing_active() -> bool:
    """True if ace.py signalled an in-progress homing/probe move recently
    (flag mtime within TTL). Best-effort; any error -> not gating."""
    try:
        age = time.time() - os.path.getmtime(HOMING_FLAG_PATH)
    except OSError:
        return False
    return 0.0 <= age < HOMING_GATE_TTL

PLUGIN_PORT_RANGE = os.environ.get("MULTIACE_PLUGIN_PORTS", "8089-8098")
PLUGIN_DISCOVERY_TTL = float(os.environ.get("MULTIACE_PLUGIN_TTL", "30"))
DEFAULT_FRONTEND = str((Path(__file__).resolve().parent.parent / "frontend"))
FRONTEND_DIR = os.environ.get("MULTIACE_FRONTEND_DIR", DEFAULT_FRONTEND)
def _resolve_version() -> str:
    v = os.environ.get("MULTIACE_WEB_VERSION", "")
    if v:
        return v
    for path in ("/home/lava/klipper/klippy/extras/ace.py",
                 "/home/printer_data/klipper/klippy/extras/ace.py",
                 "/usr/share/klipper/klippy/extras/ace.py"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue
        m_ver = re.search(r'^MULTIACE_VERSION\s*=\s*["\']([^"\']+)["\']',
                          head, re.MULTILINE)
        m_tag = re.search(r'^MULTIACE_BUILD_TAG\s*=\s*["\']([^"\']+)["\']',
                          head, re.MULTILINE)
        if m_ver:
            return ('%s+%s' % (m_ver.group(1), m_tag.group(1))
                    if m_tag else m_ver.group(1))
    return "0.2.0"


VERSION = _resolve_version()

ACE_OBJECTS = [
    "ace",
    "filament_feed left",
    "filament_feed right",
    "save_variables",
    "print_task_config",
    "print_stats",
    "idle_timeout",
]

def _slot_state_name(v: Any) -> str:
    if v is None:
        return "unknown"
    return {
        0: "empty", 1: "ready", 2: "loading", 3: "unloading",
        4: "error", 5: "feeding", 6: "assist",
    }.get(v, str(v))

def _resolve_head_source(src: Any) -> tuple[int | None, int | None]:
    """head_source[toolhead] can be null, an int (slot, device implied),
    a list [device, slot] or a dict with 'ace_index'+'slot' keys (the
    shape ace.py emits at LOAD_HEAD time)."""
    if src is None:
        return (None, None)
    if isinstance(src, int):
        return (None, src)
    if isinstance(src, (list, tuple)) and len(src) >= 2:
        return (src[0], src[1])
    if isinstance(src, dict):

        d = src["ace_index"] if "ace_index" in src else src.get("device")
        return (d, src.get("slot"))
    return (None, None)

def _head_source_load_failed(src: Any) -> bool:
    return isinstance(src, dict) and bool(src.get("load_failed"))

def _load_failed_toolheads(parsed: dict) -> list[dict]:
    return [
        t for t in (parsed.get("toolheads") or [])
        if isinstance(t, dict) and t.get("load_failed")
    ]

def _load_failed_message(t: dict) -> str:
    idx = t.get("idx")
    ace = t.get("failed_ace")
    slot = t.get("failed_slot")
    if ace is None or slot is None:
        return f"T{idx}: previous ACE load failed; recover this head first"
    return (
        f"T{idx}: previous ACE load failed on ACE {ace} / Slot {slot}; "
        "recover this head first"
    )

def _color_to_hex(c: Any) -> str | None:
    """[r,g,b] (0-255) → '#rrggbb', or None for [0,0,0]/missing."""
    if not isinstance(c, (list, tuple)) or len(c) < 3:
        return None
    r, g, b = int(c[0]), int(c[1]), int(c[2])
    if r == 0 and g == 0 and b == 0:
        return None
    return f"#{r:02x}{g:02x}{b:02x}"

def _parse_state(status: dict) -> dict:
    """
    Translate the raw multi-object status block into the dashboard schema.

    With ace.py's extended get_status() we now have aces[] with per-ACE
    per-slot detail (RFID, material, brand, colour). The toolheads table
    is enriched from filament_feed left/right + head_source, and we add
    a wiring[] list that shows only loaded source→toolhead links for the
    SVG diagram.
    """

    _reload_overrides_if_changed()
    _reload_native_overrides_if_changed()

    ace = status.get("ace", {}) or {}
    fl = status.get("filament_feed left",  {}) or {}
    fr = status.get("filament_feed right", {}) or {}

    device_count = int(ace.get("device_count", 1))
    active_device = int(ace.get("active_device", 0))
    head_source = ace.get("head_source", {}) or {}
    route = ace.get("route", {}) or {}
    route_mode = route.get("mode", "single_head")
    raw_primary_head = route.get("primary_head", 0)
    route_primary_head = None if raw_primary_head is None else int(raw_primary_head or 0)
    route_slot_targets = route.get("slot_targets", {}) or {}
    route_ace_targets = route.get("ace_targets", {}) or {}
    route_head_modes = route.get("head_modes", {}) or {}
    route_error = route.get("error")
    raw_aces = ace.get("aces", []) or []

    ptc = status.get("print_task_config", {}) or {}
    ptc_types  = ptc.get("filament_type", []) or []
    ptc_subs   = ptc.get("filament_sub_type", []) or []
    ptc_vendors = ptc.get("filament_vendor", []) or []
    ptc_rgbas  = ptc.get("filament_color_rgba", []) or []

    def _ptc_at(n: int) -> dict | None:
        if not (n < len(ptc_types) and n < len(ptc_rgbas)):
            return None
        mat = (ptc_types[n] or "").strip()
        rgba = (ptc_rgbas[n] or "").strip()
        if not mat and not rgba:
            return None

        if mat in ("", "NONE") and rgba in ("", "00000000", "000000FF"):
            return None
        color_hex = None
        if rgba and len(rgba) >= 6 and rgba.upper() != "00000000":
            color_hex = "#" + rgba[:6].lower()
        sub = (ptc_subs[n] or "").strip() if n < len(ptc_subs) else ""
        if sub == "NONE":
            sub = ""
        vendor = (ptc_vendors[n] or "").strip() if n < len(ptc_vendors) else ""
        return {
            "material": mat if mat != "NONE" else "",
            "sku":      sub,
            "brand":    vendor if vendor != "NONE" else "",
            "color":    color_hex,
        }

    def _native_override_at(n: int) -> dict | None:
        o = _native_overrides.get(str(n)) or _native_overrides.get(n)
        if not isinstance(o, dict):
            return None
        mat = (o.get("material") or "").strip()
        color = (o.get("color") or "").strip()
        if not mat and not color:
            return None
        return {
            "material": mat,
            "sku":      (o.get("subtype") or "").strip(),
            "brand":    (o.get("brand") or "").strip(),
            "color":    color.lower() if color else None,
        }

    SLOT_COUNT = 4
    by_idx = {a.get("idx", n): a for n, a in enumerate(raw_aces) if isinstance(a, dict)}

    def _route_target_head(slot: int, ace_idx: int | None = None) -> int | None:
        if ace_idx is not None:
            if str(ace_idx) in route_ace_targets:
                ace_val = route_ace_targets.get(str(ace_idx))
                return None if ace_val is None else int(ace_val)
            if ace_idx in route_ace_targets:
                ace_val = route_ace_targets.get(ace_idx)
                return None if ace_val is None else int(ace_val)
        val = route_slot_targets.get(str(slot), route_slot_targets.get(slot))
        if val is None:
            return None
        return int(val)

    def _route_head_mode(head: int) -> str:
        mode = route_head_modes.get(str(head), route_head_modes.get(head))
        if mode in ("ace", "native"):
            return mode
        if route_mode == "single_head" and route_primary_head == head:
            return "ace"
        return "native"

    def _head_in_op(t: int) -> bool:

        feed = (fl if t < 2 else fr).get(
            f"extruder{t}" if t > 0 else "extruder0", {}) or {}
        cs = (feed.get("channel_state") or "")
        if cs and not (cs.endswith("_finish") or cs.endswith("_fail")
                       or cs in ("wait_insert", "inited", "test")):
            if (cs.startswith("load_") or cs.startswith("unload_")
                    or cs.startswith("preload_") or cs.startswith("manual_sta_")):
                return True
        src = head_source.get(str(t)) or head_source.get(t)
        if isinstance(src, dict):
            stype = (src.get("type") or "").strip()
            scol = (src.get("color") or "").strip().lstrip("#").upper()
            if not stype or scol in ("", "000000", "00000000"):
                return True
        return False

    loaded_by_source: dict[tuple[int, int], int] = {}
    for t_key, src in (head_source or {}).items():
        if _head_source_load_failed(src):
            continue
        d_l, sl_l = _resolve_head_source(src)
        if d_l is None or sl_l is None:
            continue
        try:
            t_idx = int(t_key)
        except (TypeError, ValueError):
            continue
        if _head_in_op(t_idx):
            continue
        loaded_by_source[(int(d_l), int(sl_l))] = t_idx

    aces_out: list[dict] = []
    overrides_dirty = False
    for i in range(device_count):
        a = by_idx.get(i, {})
        gate_status = a.get("gate_status") or (
            ace.get("gate_status", []) if i == active_device else []
        )
        ace_slots = a.get("slots", []) or []
        slots_by_idx = {s.get("index", n): s for n, s in enumerate(ace_slots)}
        slots_out = []
        for s in range(SLOT_COUNT):
            sd = slots_by_idx.get(s, {}) or {}
            gate = gate_status[s] if s < len(gate_status) else None
            raw_status = sd.get("status", "") or ""

            is_empty = (
                gate == 0
                or raw_status.startswith("empty")
                or raw_status == ""
                and gate is None
            )

            if gate == 0:
                _now = time.time()
                _pending = _eject_pending_since.get((i, s))
                if _pending is None:
                    _eject_pending_since[(i, s)] = _now
                elif _now - _pending >= EJECT_DEBOUNCE_S:
                    if _drop_override_if_present(i, s):
                        overrides_dirty = True
                    _eject_pending_since.pop((i, s), None)
            else:
                _eject_pending_since.pop((i, s), None)
            override = _override_for(i, s)
            loaded_t = loaded_by_source.get((i, s))
            if override is not None:
                ptc_overlay = {
                    "material": override.get("material", ""),
                    "sku":      override.get("subtype", ""),
                    "brand":    override.get("brand", ""),
                    "color":    override.get("color") or None,
                }
            elif loaded_t is not None:
                ptc_overlay = _ptc_at(loaded_t)
            else:
                ptc_overlay = None

            rfid_status = sd.get("rfid", 0)
            rfid_data = None
            if rfid_status == 2:
                rfid_data = {
                    "material": sd.get("material", "") or sd.get("type", ""),
                    "brand":    sd.get("brand", ""),
                    "sku":      sd.get("sku", ""),
                    "color":    _color_to_hex(sd.get("color")),
                }

            if is_empty and ptc_overlay is None:
                slots_out.append({
                    "idx":       s,
                    "target_head": _route_target_head(s, i),
                    "state":     "empty",
                    "raw":       gate,
                    "status":    raw_status,
                    "rfid":      0,
                    "material":  "",
                    "brand":     "",
                    "sku":       "",
                    "color":     None,
                    "color_rgb": None,
                    "rfid_data": rfid_data,
                })
            else:

                if ptc_overlay is not None:
                    slots_out.append({
                        "idx":       s,
                        "target_head": _route_target_head(s, i),
                        "state":     "ready" if not is_empty else "empty",
                        "raw":       gate,
                        "status":    raw_status,
                        "rfid":      rfid_status,
                        "material":  ptc_overlay["material"],
                        "brand":     ptc_overlay["brand"],
                        "sku":       ptc_overlay["sku"],
                        "color":     ptc_overlay["color"],
                        "color_rgb": None,
                        "rfid_data": rfid_data,
                    })
                else:
                    slots_out.append({
                        "idx":       s,
                        "target_head": _route_target_head(s, i),
                        "state":     _slot_state_name(gate),
                        "raw":       gate,
                        "status":    raw_status,
                        "rfid":      rfid_status,
                        "material":  sd.get("material", "") or sd.get("type", ""),
                        "brand":     sd.get("brand", ""),
                        "sku":       sd.get("sku", ""),
                        "color":     _color_to_hex(sd.get("color")),
                        "color_rgb": sd.get("color"),
                        "rfid_data": rfid_data,
                    })
        aces_out.append({
            "idx":          i,
            "connected":    a.get("connected"),
            "protocol":     a.get("protocol", ""),
            "status":       a.get("status"),
            "temp":         a.get("temp"),

            "humidity":     a.get("humidity"),
            "dryer":        a.get("dryer_status") or {},
            "feed_assist":  a.get("feed_assist", -1),
            "slots":        slots_out,
        })

    if overrides_dirty:
        _save_overrides_to_disk()

    toolheads = []
    wiring = []
    for t in range(4):
        ext_key = f"extruder{t}" if t > 0 else "extruder0"
        feed = (fl if t < 2 else fr).get(ext_key, {}) or {}

        src_raw = head_source.get(str(t)) or head_source.get(t)
        load_failed = _head_source_load_failed(src_raw)
        d_explicit, sl_explicit = _resolve_head_source(src_raw)
        loaded = bool(feed.get("filament_detected"))
        color = None
        material = ""
        brand = ""
        sku = ""
        mode = _route_head_mode(t)
        ace_field = None
        slot_field = None
        if (not load_failed
                and d_explicit is not None and sl_explicit is not None):
            ace_field = d_explicit
            slot_field = sl_explicit
            if 0 <= d_explicit < len(aces_out):
                slots_arr = aces_out[d_explicit]["slots"]
                if 0 <= sl_explicit < len(slots_arr):
                    slot_obj = slots_arr[sl_explicit]
                    color = slot_obj.get("color")
                    material = slot_obj.get("material", "")
                    brand = slot_obj.get("brand", "")
                    sku = slot_obj.get("sku", "")
        else:
            meta = (_native_override_at(t) if mode == "native" else None) or _ptc_at(t)
            if meta:
                color = meta.get("color")
                material = meta.get("material", "")
                brand = meta.get("brand", "")
                sku = meta.get("sku", "")
        toolheads.append({
            "idx":                t,
            "name":               f"T{t}",
            "mode":               mode,
            "ace":                ace_field,
            "slot":               slot_field,
            "filament_detected":  feed.get("filament_detected"),
            "filament_in_ace":      feed.get("filament_in_ace"),
            "filament_in_toolhead": feed.get("filament_in_toolhead"),
            "filament_at_extruder": feed.get("filament_at_extruder"),
            "channel_state":      feed.get("channel_state"),
            "channel_error":      feed.get("channel_error"),
            "module_exist":       feed.get("module_exist"),
            "color":              color,
            "material":           material,
            "brand":              brand,
            "sku":                sku,
            "head_source_known":  (
                not load_failed and d_explicit is not None
            ),
            "load_failed":        load_failed,
            "failed_ace":         d_explicit if load_failed else None,
            "failed_slot":        sl_explicit if load_failed else None,
            "route_managed":      (
                t == route_primary_head if route_mode == "single_head" else True
            ),
        })

        if (not load_failed
                and d_explicit is not None and sl_explicit is not None):
            wiring.append({
                "ace": d_explicit, "slot": sl_explicit, "toolhead": t,
                "color": color, "material": material,
            })

    sv = status.get("save_variables", {})
    sv_vars = sv.get("variables", {}) if isinstance(sv, dict) else {}
    mode = "multi"

    ps = status.get("print_stats", {}) or {}
    it = status.get("idle_timeout", {}) or {}
    ps_state = (ps.get("state") or "").lower()
    if ps_state in ("printing", "paused", "complete", "error"):

        printer_state = ps_state
    else:

        raw_it = (it.get("state") or "Idle").lower()
        printer_state = "busy" if raw_it == "printing" else raw_it
    language = sv_vars.get("ace__language", os.environ.get("MULTIACE_LANGUAGE", "en"))
    idx_base = _read_display_index_base()
    return {
        "ace_status":         ace.get("status"),
        "ace_temp":           ace.get("temp"),
        "printer_state":      printer_state,
        "active_device":      active_device,
        "device_count":       device_count,
        "mode":               mode,
        "route":              {
            "mode": route_mode,
            "primary_head": route_primary_head,
            "slot_targets": route_slot_targets,
            "ace_targets": route_ace_targets,
            "head_modes": route_head_modes,
            "error": route_error,
        },
        "language":           language,
        "display_index_base": idx_base,
        "dryer":              ace.get("dryer_status"),
        "swap_in_progress":   bool(ace.get("swap_in_progress", False)),
        "aces":               aces_out,
        "toolheads":          toolheads,
        "wiring":             wiring,
        "save_variables":     sv_vars,
    }

async def _query_state() -> dict:
    qs = "&".join(o.replace(" ", "%20") for o in ACE_OBJECTS)
    data = await _mr_get(f"/printer/objects/query?{qs}")
    return data.get("result", {}).get("status", {})

class _StripMultiacePrefix:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") in ("http", "websocket") and (
                path == "/multiace" or path.startswith("/multiace/")):
            scope = dict(scope)
            new_path = path[len("/multiace"):] or "/"
            scope["path"] = new_path
            raw_path = scope.get("raw_path")
            if raw_path:
                scope["raw_path"] = new_path.encode("utf-8")
        await self.app(scope, receive, send)


app = FastAPI(title="multiACE Web", version=VERSION)
app.add_middleware(_StripMultiacePrefix)

@app.middleware("http")
async def _no_cache_frontend(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if not (path.startswith("/api/") or path.startswith("/plugin/")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

class MacroRequest(BaseModel):
    name: str
    args: dict[str, Any] | None = None

class MacroBatchRequest(BaseModel):
    commands: list[MacroRequest]

class ConfigUpdate(BaseModel):
    content: str
    restart_klipper: bool = False

class SourceGraphUpdate(BaseModel):
    graph: dict[str, Any]

class SourceActionPreview(BaseModel):
    source: str
    head: str | int
    action: str

class SourceActionPreviewBatch(BaseModel):
    actions: list[SourceActionPreview]

class SourceTransitionPreview(BaseModel):
    source: str
    head: str | int

class RoutePlanValidateRequest(BaseModel):
    route_plan: dict[str, Any]

EXPLICIT_ROUTE_MACROS = {"ACE_LOAD_HEAD", "ACE_SWAP_HEAD"}
EXPLICIT_ROUTE_ARGS = {"HEAD", "ACE", "SLOT"}
OBSOLETE_MACROS_BLOCKED = {"SET_ACE_MODE", "ACE_RUN_MODE_SWITCH"}
EXPLICIT_PLAN_MACROS = {"ACE_TEST", "ACE_SEQ", "ACE_PRELOAD"}

OBSOLETE_ACE_CONFIG_KEYS = {
    "ace_route_mode",
    "ace_primary_head",
    "print_mode",
}

OBSOLETE_GCODE_MACROS = {
    "SET_ACE_MODE",
    "ACEB__Load_0",
    "ACEB__Load_1",
    "ACEB__Load_2",
    "ACEB__Load_3",
    "ACEC__Load_T0",
    "ACEC__Load_T1",
    "ACEC__Load_T2",
    "ACEC__Load_T3",
    "ACEF__Mode_Normal",
    "ACEF__Mode_Multi",
}

def _macro_args_upper(args: dict[str, Any] | None) -> set[str]:
    return {str(k).upper() for k in (args or {}).keys()}

def _validate_feed_auto_args(args: dict[str, Any] | None) -> None:
    a = {str(k).upper(): v for k, v in (args or {}).items()}
    allowed = {
        "MODULE", "CHANNEL", "EXTRUDER", "LOAD", "UNLOAD", "STAGE",
        "AUTO", "SAVE", "PRINTING",
    }
    extra = sorted(set(a.keys()) - allowed)
    if extra:
        raise HTTPException(
            status_code=400,
            detail=f"FEED_AUTO rejects unsupported argument(s): {', '.join(extra)}")

    for key in ("MODULE", "CHANNEL", "EXTRUDER"):
        if key not in a:
            raise HTTPException(
                status_code=400,
                detail=f"FEED_AUTO requires {key}")
    module = str(a.get("MODULE") or "").lower()
    if module not in ("left", "right"):
        raise HTTPException(
            status_code=400,
            detail="FEED_AUTO MODULE must be left or right")
    try:
        channel = int(a.get("CHANNEL"))
        extruder = int(a.get("EXTRUDER"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="FEED_AUTO CHANNEL and EXTRUDER must be integers")
    if channel not in (0, 1) or extruder not in (0, 1, 2, 3):
        raise HTTPException(
            status_code=400,
            detail="FEED_AUTO CHANNEL must be 0..1 and EXTRUDER must be 0..3")
    expected = {
        0: ("left", 1),
        1: ("left", 0),
        2: ("right", 0),
        3: ("right", 1),
    }.get(extruder)
    if expected != (module, channel):
        raise HTTPException(
            status_code=400,
            detail=(
                "FEED_AUTO module/channel does not match EXTRUDER; "
                f"T{extruder} expects MODULE={expected[0]} CHANNEL={expected[1]}"))

    load = int(a.get("LOAD", 0) or 0)
    unload = int(a.get("UNLOAD", 0) or 0)
    auto = int(a.get("AUTO", 0) or 0)
    if sum(1 for v in (load, unload, auto) if v) != 1:
        raise HTTPException(
            status_code=400,
            detail="FEED_AUTO requires exactly one of LOAD=1, UNLOAD=1 or AUTO=1")
    if load not in (0, 1) or unload not in (0, 1) or auto not in (0, 1):
        raise HTTPException(
            status_code=400,
            detail="FEED_AUTO LOAD, UNLOAD and AUTO must be 0 or 1")
    if "STAGE" in a and str(a.get("STAGE") or "").lower() not in ("prepare", "doing", "cancel"):
        raise HTTPException(
            status_code=400,
            detail="FEED_AUTO STAGE must be prepare, doing or cancel")

def _validate_macro_request(name: str, args: dict[str, Any] | None) -> None:
    macro = str(name or "").strip().upper()
    if macro in OBSOLETE_MACROS_BLOCKED:
        raise HTTPException(
            status_code=400,
            detail=f"{macro} is obsolete and blocked; use dashboard topology.")
    if macro in EXPLICIT_ROUTE_MACROS:
        missing = sorted(EXPLICIT_ROUTE_ARGS - _macro_args_upper(args))
        if missing:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{macro} requires explicit HEAD, ACE and SLOT; "
                    f"missing {', '.join(missing)}"))
    if macro in EXPLICIT_PLAN_MACROS:
        _validate_plan_arg(macro, None if args is None else args.get("PLAN", args.get("plan")))
    if macro == "FEED_AUTO":
        _validate_feed_auto_args(args)

def _validate_plan_arg(macro: str, plan: Any) -> None:
    plan_str = str(plan or "").strip()
    if not plan_str:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{macro} requires PLAN with explicit HEAD:ACE:SLOT; "
                "implicit ACE/default-slot plans are blocked"))
    for item in plan_str.split(","):
        item = item.strip()
        if not item:
            continue
        if item == "U":
            continue
        if item.startswith("U") and item[1:].isdigit():
            continue
        if macro == "ACE_TEST":
            if item.startswith("S") and item[1:].isdigit():
                continue
            if item.startswith("W"):
                try:
                    seconds = float(item[1:])
                except ValueError:
                    seconds = -1.0
                if seconds >= 0:
                    continue
            if item.startswith("H") and ":" in item[1:]:
                parts = item[1:].split(":")
                if len(parts) == 3 and all(p.isdigit() for p in parts):
                    continue
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{macro} rejects PLAN item {item!r}; "
                        "use HHEAD:ACE:SLOT"))
        if item.startswith("A") and item[1:].isdigit():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{macro} rejects PLAN item {item!r}; A<ace> "
                    "implicit loads are blocked; use HEAD:ACE:SLOT"))
        if ":" in item:
            parts = item.split(":")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                continue
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{macro} rejects PLAN item {item!r}; "
                    "use HEAD:ACE:SLOT"))
        raise HTTPException(
            status_code=400,
            detail=(
                f"{macro} rejects PLAN item {item!r}; "
                "use explicit HEAD:ACE:SLOT"))

def _validate_gcode_script(script: str) -> None:
    for lineno, raw in enumerate((script or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        parts = line.split()
        if not parts:
            continue
        macro = parts[0].upper()
        if macro in OBSOLETE_MACROS_BLOCKED:
            raise HTTPException(
                status_code=400,
                detail=f"line {lineno}: {macro} is obsolete and blocked")
        gcode_args = {
            p.split("=", 1)[0].upper(): p.split("=", 1)[1]
            for p in parts[1:]
            if "=" in p
        }
        if macro in EXPLICIT_PLAN_MACROS:
            try:
                _validate_plan_arg(macro, gcode_args.get("PLAN"))
            except HTTPException as e:
                raise HTTPException(
                    status_code=e.status_code,
                    detail=f"line {lineno}: {e.detail}")
        if macro not in EXPLICIT_ROUTE_MACROS:
            continue
        keys = set(gcode_args.keys())
        missing = sorted(EXPLICIT_ROUTE_ARGS - keys)
        if missing:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"line {lineno}: {macro} requires explicit HEAD, ACE "
                    f"and SLOT; missing {', '.join(missing)}"))

class SnapshotSave(BaseModel):
    name: str
    description: str | None = None

class SlotOverride(BaseModel):
    ace: int
    slot: int
    material: str | None = ""
    brand: str | None = ""
    subtype: str | None = ""
    color: str | None = ""

class NativeOverride(BaseModel):
    head: int
    material: str | None = ""
    brand: str | None = ""
    subtype: str | None = ""
    color: str | None = ""

async def _mr_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{MOONRAKER_URL}{path}")
        r.raise_for_status()
        return r.json()

async def _mr_post(path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{MOONRAKER_URL}{path}", json=body or {})
        r.raise_for_status()
        return r.json()

@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "version": VERSION, "ts": time.time()}

@app.get("/api/version")
async def version() -> dict:

    printer = {}
    try:
        sysinfo = await _mr_get("/machine/system_info")
        pi = (sysinfo.get("result", {})
                     .get("system_info", {})
                     .get("product_info", {})) or {}
        printer = {
            "device_name":      pi.get("device_name"),
            "machine_type":     pi.get("machine_type"),
            "firmware_version": pi.get("firmware_version"),
        }
    except Exception:
        pass
    return {
        "web": VERSION,
        "moonraker_url": MOONRAKER_URL,
        "config_path": MULTIACE_CFG_PATH,
        "source_graph_path": MULTIACE_SOURCE_GRAPH_PATH,
        "frontend_dir": FRONTEND_DIR,
        "printer": printer,
    }

_PREFLIGHT_DIR = Path("/tmp/multiace-preflight")
_PREFLIGHT_TTL = 86400.0
_PREFLIGHT_FUZZY = 30
_PREFLIGHT_MIXED_FUZZY = int(os.environ.get("MULTIACE_MIXED_FUZZY", "90"))
_PREFLIGHT_SOURCE_MAP_VERSION = 1

_PREFLIGHT_MAX_SIZE = int(os.environ.get(
    "MULTIACE_PREFLIGHT_MAX_MB", "200")) * 1024 * 1024

_pp_module = None

def _load_post_processor():
    """Lazy-load the post-processor as a Python module so its parsing
    and remap helpers can be reused server-side without a subprocess."""
    global _pp_module
    if _pp_module is not None:
        return _pp_module
    candidates = [
        Path("/home/lava/printer_data/config/tools/post_process_virtual_toolheads.py"),
        Path(__file__).resolve().parent.parent.parent / "tools" / "post_process_virtual_toolheads.py",
    ]
    src = next((p for p in candidates if p.is_file()), None)
    if src is None:
        raise HTTPException(status_code=503,
                            detail="post-processor script not installed")
    import importlib.util
    spec = importlib.util.spec_from_file_location("multiace_postprocess", src)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"post-processor failed to load: {exc}")
    _pp_module = mod
    return mod

def _cleanup_preflight_dir() -> None:
    if not _PREFLIGHT_DIR.is_dir():
        return
    now = time.time()
    for p in _PREFLIGHT_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > _PREFLIGHT_TTL:
                p.unlink()
        except Exception:
            pass

async def _live_slots_async() -> list[dict]:
    status = await _query_state()
    parsed = _parse_state(status)
    return _live_slots_from_parsed(parsed)

def _live_slots_from_parsed(parsed: dict) -> list[dict]:
    out = []
    failed = _load_failed_toolheads(parsed)
    if failed:
        raise HTTPException(
            status_code=409,
            detail="; ".join(_load_failed_message(t) for t in failed),
        )
    for ace in parsed.get("aces", []) or []:
        for slot in ace.get("slots", []) or []:
            if slot.get("state") == "empty":
                continue
            out.append({
                "ace":      ace.get("idx"),
                "slot":     slot.get("idx"),
                "target_head": slot.get("target_head"),
                "material": (slot.get("material") or "").strip(),
                "color":    (slot.get("color") or "").strip().lower(),
            })
    return out

def _ace_count_from_parsed(parsed: dict) -> int:
    try:
        return max(1, len(parsed.get("aces", []) or []))
    except Exception:
        return 1

def _load_source_graph(parsed: dict | None = None) -> tuple[dict, dict]:
    return sg.load_graph(
        MULTIACE_SOURCE_GRAPH_PATH,
        ace_count=_ace_count_from_parsed(parsed or {}),
        parsed=parsed,
    )

def _live_loadout_from_parsed(parsed: dict, pp=None) -> list[dict]:
    graph, meta = _load_source_graph(parsed)
    if meta.get("errors"):
        raise HTTPException(
            status_code=409,
            detail="source graph invalid: " + "; ".join(meta.get("errors") or []),
        )
    color_name_fn = pp.approx_color_name if pp else None
    return sg.live_loadout(graph, parsed, color_name_fn=color_name_fn)

def _slot_to_dict(s: dict | None) -> dict | None:
    if s is None:
        return None
    return {
        "ace":      s.get("ace"),
        "slot":     s.get("slot"),
        "target_head": s.get("target_head"),
        "material": s.get("material") or "",
        "color":    s.get("color") or "",
    }

def _target_to_dict(target: dict | None) -> dict | None:
    if not target:
        return None
    out = {
        "kind": target.get("kind"),
        "head": target.get("head"),
    }
    for key in (
            "key", "source", "head_id", "edge", "execution_profile",
            "module", "channel"):
        if key in target and target.get(key) is not None:
            out[key] = target.get(key)
    if target.get("source"):
        out["source"] = target.get("source")
    if target.get("kind") == "ace":
        out["ace"] = target.get("ace")
        out["slot"] = target.get("slot")
    return out

def _norm_color_hex(value: Any) -> str:
    return str(value or "").strip().lower().lstrip("#")

def _norm_material(value: Any) -> str:
    return str(value or "").strip().lower()

def _hex_rgb(value: Any) -> tuple[int, int, int] | None:
    s = _norm_color_hex(value)
    if len(s) < 6:
        return None
    try:
        return int(s[:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None

def _color_distance(a: Any, b: Any) -> float | None:
    ar = _hex_rgb(a)
    br = _hex_rgb(b)
    if ar is None or br is None:
        return None
    return (
        (ar[0] - br[0]) ** 2
        + (ar[1] - br[1]) ** 2
        + (ar[2] - br[2]) ** 2
    ) ** 0.5

def _candidate_score(want_mat: str, want_color: str,
                     have_mat: str, have_color: str) -> tuple[int, float, str] | None:
    mat_match = bool(want_mat and have_mat and want_mat == have_mat)
    dist = _color_distance(want_color, have_color)
    exact_color = dist == 0
    fuzzy_color = dist is not None and dist <= _PREFLIGHT_MIXED_FUZZY
    if want_color and have_color:
        if mat_match and exact_color:
            return 0, 0.0, "material_color"
        if mat_match and fuzzy_color:
            return 1, float(dist), "material_fuzzy"
        if exact_color:
            return 2, 0.0, "color"
        if fuzzy_color:
            return 3, float(dist), "fuzzy"
        return None
    if mat_match and not want_color:
        return 4, 999.0, "material"
    if mat_match and not have_color:
        return 5, 999.0, "material_no_color"
    return None

def _candidate_target_key(target: dict) -> str:
    if target.get("key"):
        return str(target.get("key"))
    source = target.get("source")
    head_id = target.get("head_id")
    if source and head_id:
        return "%s->%s" % (source, head_id)
    if target.get("kind") == "native":
        return "native:%d" % int(target.get("head"))
    return "ace:%d:%d:%d" % (
        int(target.get("head")), int(target.get("ace")), int(target.get("slot")))

def _target_from_loadout_item(item: dict) -> dict:
    out = {
        "kind": item.get("kind"),
        "source": item.get("source"),
        "key": item.get("key"),
        "head": int(item.get("head")),
        "head_id": item.get("head_id") or "head:%d" % int(item.get("head")),
        "edge": item.get("edge") or {
            "source": item.get("source"),
            "head": item.get("head_id") or "head:%d" % int(item.get("head")),
        },
        "execution_profile": item.get("execution_profile") or "",
    }
    if item.get("kind") == "native":
        out["module"] = item.get("module")
        out["channel"] = item.get("channel")
        return out
    out.update({
        "kind": "ace",
        "ace": int(item.get("ace")),
        "slot": int(item.get("slot")),
    })
    return out

def _target_key_from_request(target: Any) -> str | None:
    if not isinstance(target, dict):
        return None
    if target.get("key"):
        return str(target.get("key"))
    edge = target.get("edge")
    if isinstance(edge, dict) and edge.get("source") and edge.get("head"):
        return "%s->%s" % (edge.get("source"), edge.get("head"))
    if target.get("source") and target.get("head_id"):
        return "%s->%s" % (target.get("source"), target.get("head_id"))
    if target.get("source") and target.get("head") is not None:
        try:
            return "%s->head:%d" % (target.get("source"), int(target.get("head")))
        except (TypeError, ValueError):
            return None
    kind = str(target.get("kind") or "").lower()
    try:
        if kind == "native":
            return "native:%d" % int(target.get("head"))
        if kind == "ace":
            return "ace:%d:%d:%d" % (
                int(target.get("head")),
                int(target.get("ace")),
                int(target.get("slot")),
            )
    except (TypeError, ValueError):
        return None
    return None

def _target_command_preview(target: dict | None) -> list[str]:
    if not target:
        return []
    try:
        head = int(target.get("head"))
    except (TypeError, ValueError):
        return []
    if target.get("kind") == "native":
        return [f"T{head}"]
    if target.get("kind") == "ace":
        try:
            ace = int(target.get("ace"))
            slot = int(target.get("slot"))
        except (TypeError, ValueError):
            return [f"T{head}"]
        return [
            f"T{head}",
            f"ACE_SWAP_HEAD HEAD={head} ACE={ace} SLOT={slot}",
        ]
    return []

def _event_target_key(target: dict | None) -> tuple | None:
    if not target:
        return None
    try:
        head = int(target.get("head"))
    except (TypeError, ValueError):
        return None
    if target.get("kind") == "native":
        return ("native", head)
    if target.get("kind") == "ace":
        try:
            ace = int(target.get("ace"))
            slot = int(target.get("slot"))
        except (TypeError, ValueError):
            return None
        return ("ace", head, ace, slot)
    return None

def _build_swap_stats(events: list[int] | tuple[int, ...],
                      tool_targets: dict[str, dict]) -> dict:
    """Estimate command-level ACE swap pressure for the resolved mapping.

    This is intentionally observational. It does not change rewrite behavior;
    it only summarizes the current resolver output so Phase 2 optimization has
    a stable baseline.
    """
    head_current: dict[int, tuple] = {}
    tool_counts: dict[str, int] = {}
    active_ace_swaps = 0
    skipped_same_ace = 0
    ace_events = 0
    native_events = 0
    unmapped_events = 0
    sample = []
    for idx, raw_t in enumerate(events or []):
        try:
            t = int(raw_t)
        except (TypeError, ValueError):
            continue
        tool_counts[str(t)] = tool_counts.get(str(t), 0) + 1
        target = tool_targets.get(str(t), tool_targets.get(t))
        key = _event_target_key(target)
        commands = _target_command_preview(target)
        action = "unmapped"
        if key is None:
            unmapped_events += 1
        elif key[0] == "native":
            native_events += 1
            action = "native"
            head_current[int(key[1])] = key
        else:
            ace_events += 1
            head = int(key[1])
            if head_current.get(head) == key:
                skipped_same_ace += 1
                action = "ace_skip_same_source"
            else:
                active_ace_swaps += 1
                action = "ace_swap"
                head_current[head] = key
        if len(sample) < 200:
            sample.append({
                "index": idx,
                "slicer_tool": t,
                "target": target,
                "commands": commands,
                "action": action,
            })
    total = len(events or [])
    return {
        "tool_events": total,
        "tool_counts": tool_counts,
        "native_events": native_events,
        "ace_events": ace_events,
        "active_ace_swaps": active_ace_swaps,
        "skipped_same_ace": skipped_same_ace,
        "unmapped_events": unmapped_events,
        "estimated_swap_seconds_min": active_ace_swaps * 120,
        "estimated_swap_seconds_max": active_ace_swaps * 240,
        "events_sample_limit": 200,
        "events_sample_truncated": total > 200,
        "events_sample": sample,
    }

def _build_fewer_swaps_suggestion(
        parsed: dict,
        slicer_colors: dict[int, str],
        slicer_types: dict[int, str],
        used_tools: set[int],
        events: list[int],
        current_tool_targets: dict[str, dict],
) -> dict:
    current_stats = _build_swap_stats(events, current_tool_targets)
    loadout = [
        item for item in _live_loadout_from_parsed(parsed)
        if item.get("ready") is not False
    ]
    if not used_tools or not loadout:
        return {
            "feasible": False,
            "reason": "no tools or no ready loadout",
            "current": current_stats,
        }

    tool_counts = current_stats.get("tool_counts", {}) or {}
    candidates_by_tool: dict[int, list[dict]] = {}
    for t in sorted(used_tools):
        want_mat = _norm_material(slicer_types.get(t))
        want_color = _norm_color_hex(slicer_colors.get(t))
        rows = []
        for order, item in enumerate(loadout):
            score = _candidate_score(
                want_mat,
                want_color,
                _norm_material(item.get("material")),
                _norm_color_hex(item.get("color")),
            )
            if score is None:
                continue
            rank, dist, tier = score
            target = _target_from_loadout_item(item)
            rows.append({
                "key": item.get("key"),
                "target": target,
                "tier": tier,
                "rank": rank,
                "distance": float(dist),
                "order": order,
                "kind_bias": 0 if item.get("kind") == "native" else 1,
            })
        if not rows:
            return {
                "feasible": False,
                "reason": f"T{t}: no matching ready target",
                "current": current_stats,
            }
        rows.sort(key=lambda r: (
            r["rank"], r["distance"], r["kind_bias"],
            _target_key_from_request(r["target"]) or "", r["order"]))
        candidates_by_tool[t] = rows[:8]

    order = sorted(
        used_tools,
        key=lambda t: (
            -int(tool_counts.get(str(t), 0) or 0),
            len(candidates_by_tool.get(t, [])),
            t,
        ),
    )

    beam = [({}, set(), 0, 0.0)]
    for t in order:
        expanded = []
        for assignment, used_keys, rank_sum, dist_sum in beam:
            for cand in candidates_by_tool[t]:
                key = cand.get("key")
                if not key or key in used_keys:
                    continue
                nxt = dict(assignment)
                nxt[str(t)] = cand["target"]
                nkeys = set(used_keys)
                nkeys.add(key)
                expanded.append((
                    nxt,
                    nkeys,
                    rank_sum + int(cand["rank"]),
                    dist_sum + float(cand["distance"]),
                ))
        if not expanded:
            return {
                "feasible": False,
                "reason": "not enough distinct matching targets",
                "current": current_stats,
            }
        expanded.sort(key=lambda row: (
            _build_swap_stats(events, row[0]).get("active_ace_swaps", 999999),
            row[2],
            row[3],
            json.dumps(row[0], sort_keys=True),
        ))
        beam = expanded[:256]

    best = min(
        beam,
        key=lambda row: (
            _build_swap_stats(events, row[0]).get("active_ace_swaps", 999999),
            row[2],
            row[3],
            json.dumps(row[0], sort_keys=True),
        ),
    )
    tool_targets = best[0]
    suggested_stats = _build_swap_stats(events, tool_targets)
    current_swaps = int(current_stats.get("active_ace_swaps") or 0)
    suggested_swaps = int(suggested_stats.get("active_ace_swaps") or 0)
    saved = max(0, current_swaps - suggested_swaps)
    return {
        "feasible": True,
        "reason": "",
        "saves_swaps": saved,
        "improves": suggested_swaps < current_swaps,
        "current": current_stats,
        "suggested": suggested_stats,
        "tool_targets": tool_targets,
    }

def _build_preflight_source_map(
        *,
        token: str,
        filename: str,
        slicer_colors: dict[int, str],
        slicer_types: dict[int, str],
        tool_targets: dict[str, dict],
        parsed: dict,
        used_tools: set[int],
        events: list[int] | tuple[int, ...] | None = None,
) -> dict:
    graph, graph_meta = _load_source_graph(parsed)
    route = parsed.get("route", {}) or {}
    events_list = list(events or [])
    entries = []
    for t in sorted(used_tools):
        target = tool_targets.get(str(t), tool_targets.get(t))
        commands = _target_command_preview(target)
        entries.append({
            "slicer_tool": t,
            "slicer_label": f"T{t}",
            "material": slicer_types.get(t, "") or "",
            "color": (slicer_colors.get(t, "") or "").lower(),
            "target": target,
            "commands": commands,
            "summary": " + ".join(commands),
        })
    return {
        "version": _PREFLIGHT_SOURCE_MAP_VERSION,
        "token": token,
        "filename": filename,
        "created_at": time.time(),
        "source_graph": {
            "hash": graph_meta.get("hash"),
            "source": graph_meta.get("source"),
            "path": graph_meta.get("path"),
            "errors": graph_meta.get("errors", []),
            "warnings": graph_meta.get("warnings", []),
        },
        "route": {
            "mode": route.get("mode"),
            "primary_head": route.get("primary_head"),
            "ace_targets": route.get("ace_targets", {}),
            "head_modes": route.get("head_modes", {}),
            "error": route.get("error"),
        },
        "used_tools": sorted(used_tools),
        "tool_targets": tool_targets,
        "swap_stats": _build_swap_stats(events_list, tool_targets),
        "optimization_suggestion": _build_fewer_swaps_suggestion(
            parsed,
            slicer_colors,
            slicer_types,
            used_tools,
            events_list,
            tool_targets,
        ),
        "entries": entries,
    }

def _preflight_events_path(token: str) -> Path:
    return _PREFLIGHT_DIR / (token + ".events")

def _save_preflight_events(token: str, events: list[int]) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return
    _preflight_events_path(token).write_text(
        json.dumps([int(t) for t in events]),
        encoding="utf-8")

def _load_preflight_events(token: str) -> list[int]:
    p = _preflight_events_path(token)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for t in data:
        try:
            out.append(int(t))
        except (TypeError, ValueError):
            continue
    return out

def _preflight_source_map_path(token: str) -> Path:
    return _PREFLIGHT_DIR / (token + ".source_map.json")

def _save_preflight_source_map(source_map: dict) -> None:
    token = source_map.get("token")
    if not token or not re.fullmatch(r"[0-9a-f]{32}", str(token)):
        return
    _preflight_source_map_path(str(token)).write_text(
        json.dumps(source_map, indent=2, sort_keys=True),
        encoding="utf-8")

def _load_preflight_source_map(token: str) -> dict:
    p = _preflight_source_map_path(token)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def _preflight_route_plan_path(token: str) -> Path:
    return _PREFLIGHT_DIR / (token + ".route_plan.json")

def _save_preflight_route_plan(route_plan: dict) -> None:
    token = route_plan.get("token")
    if not token or not re.fullmatch(r"[0-9a-f]{32}", str(token)):
        return
    _preflight_route_plan_path(str(token)).write_text(
        json.dumps(route_plan, indent=2, sort_keys=True),
        encoding="utf-8")

def _load_preflight_route_plan(token: str) -> dict:
    p = _preflight_route_plan_path(token)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            return data
    source_map = _load_preflight_source_map(token)
    return _route_plan_from_source_map(source_map) if source_map else {}

def _route_plan_target_entry(target: dict | None) -> dict:
    target = target or {}
    head = target.get("head")
    head_id = target.get("head_id")
    if not head_id and head is not None:
        head_id = f"head:{head}"
    edge = target.get("edge") if isinstance(target.get("edge"), dict) else {}
    return {
        "source": target.get("source"),
        "head": head_id,
        "edge": edge or {
            "source": target.get("source"),
            "head": head_id,
        },
        "execution_profile": target.get("execution_profile") or "",
        "target": target,
    }

def _format_profile_command(template: str | None, values: dict) -> str:
    if not template:
        return ""
    try:
        return str(template).format(**values)
    except Exception:
        return ""

def _head_index_from_id(head: str | int | None) -> int | None:
    if isinstance(head, int):
        return head
    raw = str(head or "").strip()
    if raw.startswith("head:"):
        raw = raw.split(":", 1)[1]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None

def _native_source_channel(source: dict) -> dict:
    module = source.get("module")
    channel = source.get("channel")
    try:
        source_head = int(source.get("head"))
    except (TypeError, ValueError):
        source_head = None
    if (module is None or channel is None) and source_head in sg.NATIVE_CHANNELS:
        default = sg.NATIVE_CHANNELS[source_head]
        if module is None:
            module = default.get("module")
        if channel is None:
            channel = default.get("channel")
    return {"module": module, "channel": channel}

def _profile_step(
        *,
        target: dict,
        profiles: dict | None,
        action: str,
) -> dict | None:
    profiles = profiles if isinstance(profiles, dict) else {}
    profile_id = target.get("execution_profile") or ""
    profile = profiles.get(profile_id) if profile_id else {}
    action_spec = profile.get(action) if isinstance(profile, dict) else {}
    if action_spec is None:
        return None
    if not isinstance(action_spec, dict):
        action_spec = {}
    try:
        head = int(target.get("head"))
    except (TypeError, ValueError):
        return None
    values = {
        "head": head,
        "source": target.get("source") or "",
        "module": target.get("module") or "",
        "channel": target.get("channel"),
        "ace": target.get("ace"),
        "slot": target.get("slot"),
    }
    command = _format_profile_command(action_spec.get("command"), values)
    if not command:
        return None
    step_kind = {
        "load": "load_source",
        "unload": "unload_source",
        "swap": "swap_source",
    }.get(action, action)
    step = {
        "kind": step_kind,
        "profile": profile_id,
        "profile_action": action,
        "source": target.get("source"),
        "head": f"head:{head}",
        "command": command,
    }
    for key in ("ace", "slot", "module", "channel"):
        if key in target and target.get(key) is not None:
            step[key] = target.get(key)
    return step

def _target_from_graph_source_edge(
        graph: dict,
        source_id: str,
        head_ref: str | int,
) -> dict | None:
    head_idx = _head_index_from_id(head_ref)
    if head_idx is None:
        return None
    head_id = f"head:{head_idx}"
    heads = graph.get("heads") or {}
    sources = graph.get("sources") or {}
    source = sources.get(source_id)
    head = heads.get(head_id)
    if not isinstance(source, dict) or not isinstance(head, dict):
        return None
    edge_obj = None
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or not edge.get("enabled", True):
            continue
        if edge.get("source") == source_id and edge.get("head") == head_id:
            edge_obj = edge
            break
    if edge_obj is None:
        return None
    kind = source.get("kind")
    target = {
        "kind": "native" if kind == "native_feeder" else "ace",
        "source": source_id,
        "key": f"{source_id}->{head_id}",
        "head": head_idx,
        "head_id": head_id,
        "edge": {
            "source": source_id,
            "head": head_id,
            "priority": edge_obj.get("priority", 100),
            "constraints": edge_obj.get("constraints") or {},
        },
        "execution_profile": source.get("execution_profile") or "",
    }
    if kind == "native_feeder":
        channel = _native_source_channel(source)
        target["module"] = channel.get("module")
        target["channel"] = channel.get("channel")
    elif kind == "ace_slot":
        target["ace"] = source.get("ace")
        target["slot"] = source.get("slot")
    else:
        return None
    return target

def _route_plan_steps(target: dict | None, source_changed: bool,
                      profiles: dict | None = None) -> list[dict]:
    target = target or {}
    if not target:
        return []
    profiles = profiles if isinstance(profiles, dict) else {}
    steps = []
    try:
        head = int(target.get("head"))
    except (TypeError, ValueError):
        return steps
    steps.append({
        "kind": "select_head",
        "head": f"head:{head}",
        "command": f"T{head}",
    })
    if target.get("kind") == "ace" and source_changed:
        step = _profile_step(target=target, profiles=profiles, action="swap")
        if step:
            steps.append(step)
    return steps

def _route_plan_event(
        *,
        index: int,
        slicer_tool: int,
        target: dict | None,
        source_changed: bool,
        profiles: dict | None = None,
) -> dict:
    target = target or {}
    entry = _route_plan_target_entry(target)
    if not target:
        action = "unmapped"
    elif target.get("kind") == "ace":
        action = "swap" if source_changed else "select_loaded"
    elif source_changed:
        action = "select"
    else:
        action = "select_loaded"
    steps = _route_plan_steps(target, source_changed, profiles)
    commands = [step.get("command") for step in steps if step.get("command")]
    return {
        "index": index,
        "event_type": "tool_select",
        "slicer_tool": slicer_tool,
        "source": entry.get("source"),
        "head": entry.get("head"),
        "edge": entry.get("edge"),
        "execution_profile": entry.get("execution_profile"),
        "action": action,
        "source_changed": bool(source_changed),
        "steps": steps,
        "commands": commands,
        "target": target,
    }

def _source_action_event(
        *,
        index: int,
        action: str,
        target: dict,
        step: dict,
) -> dict:
    entry = _route_plan_target_entry(target)
    return {
        "index": index,
        "event_type": "source_action",
        "action": action,
        "source": entry.get("source"),
        "head": entry.get("head"),
        "edge": entry.get("edge"),
        "execution_profile": entry.get("execution_profile"),
        "steps": [step],
        "commands": [step.get("command")] if step.get("command") else [],
        "target": target,
    }

def _source_transition_event(
        *,
        index: int,
        target: dict,
        initial_state: dict,
        graph: dict,
        current_source: str | None = None,
) -> dict:
    profiles = graph.get("profiles") or {}
    head_id = target.get("head_id") or target.get("head")
    if not str(head_id).startswith("head:"):
        head_id = "head:%s" % target.get("head")
    if current_source is None:
        current_source = (
            ((initial_state.get("heads") or {}).get(head_id) or {})
            .get("current_source")
        )
    steps = []
    current_target = None
    if current_source and current_source != target.get("source"):
        current_target = _target_from_graph_source_edge(graph, current_source, head_id)
        target_kind = target.get("kind")
        current_kind = (current_target or {}).get("kind")
        swap_handles_previous = target_kind == "ace" and current_kind == "ace"
        if current_target:
            if not swap_handles_previous:
                unload = _profile_step(
                    target=current_target,
                    profiles=profiles,
                    action="unload",
                )
                if unload:
                    steps.append(unload)
    try:
        head = int(target.get("head"))
    except (TypeError, ValueError):
        head = None
    if head is not None:
        steps.append({
            "kind": "select_head",
            "head": f"head:{head}",
            "command": f"T{head}",
        })
    if current_source != target.get("source"):
        action = "swap" if target.get("kind") == "ace" else "load"
        load_or_swap = _profile_step(
            target=target,
            profiles=profiles,
            action=action,
        )
        if load_or_swap:
            steps.append(load_or_swap)
    commands = [s.get("command") for s in steps if s.get("command")]
    entry = _route_plan_target_entry(target)
    return {
        "index": index,
        "event_type": "source_transition",
        "action": "select_loaded" if current_source == target.get("source")
                  else ("swap" if target.get("kind") == "ace" else "load"),
        "source": entry.get("source"),
        "head": entry.get("head"),
        "edge": entry.get("edge"),
        "execution_profile": entry.get("execution_profile"),
        "source_changed": current_source != target.get("source"),
        "previous_source": current_source,
        "steps": steps,
        "commands": commands,
        "target": target,
    }

def _build_route_plan(
        *,
        token: str,
        filename: str,
        graph_meta: dict,
        tool_targets: dict[str, dict],
        used_tools: set[int],
        events: list[int] | tuple[int, ...] | None,
        profiles: dict | None = None,
        initial_state: dict | None = None,
        graph: dict | None = None,
        stats: dict | None = None,
        created_at: float | None = None,
) -> dict:
    profiles = profiles if isinstance(profiles, dict) else {}
    tool_map = {}
    for t in sorted(used_tools):
        target = tool_targets.get(str(t), tool_targets.get(t))
        tool_map[str(t)] = _route_plan_target_entry(target)

    route_events = []
    head_current: dict[str, str | None] = {}
    for head_id, state in ((initial_state or {}).get("heads") or {}).items():
        if isinstance(state, dict):
            head_current[str(head_id)] = state.get("current_source")
    event_stream = list(events or [])
    for idx, raw_t in enumerate(event_stream):
        try:
            t = int(raw_t)
        except (TypeError, ValueError):
            continue
        target = tool_targets.get(str(t), tool_targets.get(t))
        entry = _route_plan_target_entry(target)
        head = entry.get("head")
        source = entry.get("source")
        changed = True
        if head:
            changed = head_current.get(str(head)) != source
        if graph and target:
            event = _source_transition_event(
                index=idx,
                target=target,
                initial_state=initial_state or {},
                graph=graph,
                current_source=head_current.get(str(head)) if head else None,
            )
            event["event_type"] = "tool_select"
            event["slicer_tool"] = t
        else:
            event = _route_plan_event(
                index=idx,
                slicer_tool=t,
                target=target,
                source_changed=changed,
                profiles=profiles,
            )
        route_events.append(event)
        if head:
            head_current[str(head)] = source

    if not route_events:
        for idx, t in enumerate(sorted(used_tools)):
            target = tool_targets.get(str(t), tool_targets.get(t))
            entry = _route_plan_target_entry(target)
            head = entry.get("head")
            source = entry.get("source")
            if graph and target:
                event = _source_transition_event(
                    index=idx,
                    target=target,
                    initial_state=initial_state or {},
                    graph=graph,
                    current_source=head_current.get(str(head)) if head else None,
                )
                event["event_type"] = "tool_select"
                event["slicer_tool"] = t
            else:
                event = _route_plan_event(
                    index=idx,
                    slicer_tool=t,
                    target=target,
                    source_changed=True,
                    profiles=profiles,
                )
            route_events.append(event)
            if head:
                head_current[str(head)] = source

    route_plan = {
        "version": 2,
        "token": token,
        "filename": filename,
        "created_at": created_at or time.time(),
        "source_graph_hash": graph_meta.get("hash"),
        "source_graph": {
            "hash": graph_meta.get("hash"),
            "source": graph_meta.get("source"),
            "path": graph_meta.get("path"),
            "errors": graph_meta.get("errors", []),
            "warnings": graph_meta.get("warnings", []),
        },
        "initial_state": initial_state or {},
        "used_tools": sorted(used_tools),
        "tool_map": tool_map,
        "events": route_events,
        "stats": stats or _build_swap_stats(event_stream, tool_targets),
    }
    route_plan["resources"] = _route_plan_resource_summary(
        route_plan, graph or {})
    route_plan["execution"] = _route_plan_execution_summary(
        route_plan, graph or {})
    return route_plan

def _route_plan_from_source_map(source_map: dict) -> dict:
    graph_meta = source_map.get("source_graph") or {}
    tool_targets = {}
    used_tools = set()
    for entry in source_map.get("entries") or []:
        target = entry.get("target") or {}
        tool = entry.get("slicer_tool")
        if tool is None:
            continue
        try:
            t = int(tool)
        except (TypeError, ValueError):
            continue
        used_tools.add(t)
        tool_targets[str(t)] = target
    sample_events = [
        int(e.get("slicer_tool"))
        for e in ((source_map.get("swap_stats") or {}).get("events_sample") or [])
        if isinstance(e, dict) and e.get("slicer_tool") is not None
    ]
    return _build_route_plan(
        token=source_map.get("token"),
        filename=source_map.get("filename"),
        graph_meta=graph_meta,
        tool_targets=tool_targets,
        used_tools=used_tools,
        events=sample_events or sorted(used_tools),
        stats=source_map.get("swap_stats") or {},
        created_at=source_map.get("created_at"),
    )

def _route_plan_targets(route_plan: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    tool_map = route_plan.get("tool_map") or {}
    if not isinstance(tool_map, dict):
        return out
    for tool, item in tool_map.items():
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        if isinstance(target, dict):
            out[str(tool)] = target
    return out

def _validate_route_plan_for_graph(route_plan: dict, graph: dict, meta: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(route_plan, dict):
        return ["route plan must be an object"]
    if route_plan.get("version") != 2:
        errors.append("route plan version must be 2")
    graph_hash = meta.get("hash")
    plan_hash = route_plan.get("source_graph_hash")
    if plan_hash and graph_hash and plan_hash != graph_hash:
        errors.append(
            "route plan source graph hash mismatch: plan=%s current=%s"
            % (plan_hash, graph_hash))
    if meta.get("errors"):
        errors.append("source graph invalid: " + "; ".join(meta.get("errors") or []))
    initial_state = route_plan.get("initial_state") or {}
    if initial_state:
        if not isinstance(initial_state, dict):
            errors.append("route plan initial_state must be an object")
            initial_state = {}
        elif initial_state.get("source_graph_hash") not in (None, graph_hash):
            errors.append(
                "route plan initial_state graph hash mismatch: initial=%s current=%s"
                % (initial_state.get("source_graph_hash"), graph_hash))
        heads_state = initial_state.get("heads") or {}
        if heads_state and not isinstance(heads_state, dict):
            errors.append("route plan initial_state.heads must be an object")
        elif isinstance(heads_state, dict):
            for head_id, state in heads_state.items():
                if head_id not in (graph.get("heads") or {}):
                    errors.append(
                        "route plan initial_state references unknown head %r"
                        % head_id)
                    continue
                if not isinstance(state, dict):
                    errors.append(
                        "route plan initial_state[%s] must be an object"
                        % head_id)
                    continue
                current = state.get("current_source")
                if current and current not in (graph.get("sources") or {}):
                    errors.append(
                        "route plan initial_state[%s] references unknown source %r"
                        % (head_id, current))

    sources = graph.get("sources") or {}
    heads = graph.get("heads") or {}
    profiles = graph.get("profiles") or {}
    enabled_edges = set()
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or not edge.get("enabled", True):
            continue
        enabled_edges.add((edge.get("source"), edge.get("head")))

    used_tools: set[int] = set()
    raw_used_tools = route_plan.get("used_tools") or []
    if raw_used_tools and not isinstance(raw_used_tools, list):
        errors.append("route plan used_tools must be a list")
        raw_used_tools = []
    for raw_t in raw_used_tools:
        try:
            used_tools.add(int(raw_t))
        except (TypeError, ValueError):
            errors.append("route plan used_tools contains invalid tool %r" % raw_t)

    tool_map = route_plan.get("tool_map") or {}
    if tool_map and not isinstance(tool_map, dict):
        errors.append("route plan tool_map must be an object")
        tool_map = {}
    mapped_tools: set[int] = set()
    for raw_t, item in (tool_map or {}).items():
        try:
            t = int(raw_t)
        except (TypeError, ValueError):
            errors.append("route plan tool_map contains invalid tool %r" % raw_t)
            continue
        mapped_tools.add(t)
        if not isinstance(item, dict):
            errors.append("route plan tool_map[%s] must be an object" % raw_t)
            continue
        target = item.get("target")
        if not isinstance(target, dict):
            errors.append("route plan tool_map[%s] missing target" % raw_t)
            continue
        target_source = target.get("source")
        target_head = target.get("head_id")
        if not target_head and target.get("head") is not None:
            target_head = "head:%s" % target.get("head")
        if not target_source or target_source not in sources:
            errors.append("route plan tool_map[%s] references unknown source %r"
                          % (raw_t, target_source))
        if not target_head or target_head not in heads:
            errors.append("route plan tool_map[%s] references unknown head %r"
                          % (raw_t, target_head))
        if target_source and target_head and (target_source, target_head) not in enabled_edges:
            errors.append("route plan tool_map[%s] has no enabled edge %s -> %s"
                          % (raw_t, target_source, target_head))
    for t in sorted(used_tools):
        if t not in mapped_tools:
            errors.append("route plan missing tool_map target for T%d" % t)
    require_tool_contract = bool(used_tools or mapped_tools)

    events = route_plan.get("events") or []
    if not isinstance(events, list) or not events:
        errors.append("route plan events must be a non-empty list")
        events = []
    event_tools: set[int] = set()
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append("route event[%d] must be an object" % idx)
            continue
        event_type = event.get("event_type")
        requires_slicer_tool = require_tool_contract or event_type == "tool_select"
        raw_slicer_tool = event.get("slicer_tool")
        slicer_tool = None
        try:
            if raw_slicer_tool is not None:
                slicer_tool = int(raw_slicer_tool)
                event_tools.add(slicer_tool)
        except (TypeError, ValueError):
            errors.append("route event[%d] missing slicer_tool" % idx)
        if requires_slicer_tool and slicer_tool is None:
            errors.append("route event[%d] missing slicer_tool" % idx)
        if slicer_tool is not None and mapped_tools and slicer_tool not in mapped_tools:
            errors.append("route event[%d] has no tool_map target for T%d"
                          % (idx, slicer_tool))
        if slicer_tool is not None and used_tools and slicer_tool not in used_tools:
            errors.append("route event[%d] references unused T%d"
                          % (idx, slicer_tool))
        source_id = event.get("source")
        head_id = event.get("head")
        if not source_id or source_id not in sources:
            errors.append("route event[%d] references unknown source %r"
                          % (idx, source_id))
        if not head_id or head_id not in heads:
            errors.append("route event[%d] references unknown head %r"
                          % (idx, head_id))
        if source_id and head_id and (source_id, head_id) not in enabled_edges:
            errors.append("route event[%d] has no enabled edge %s -> %s"
                          % (idx, source_id, head_id))
        target = event.get("target") if isinstance(event.get("target"), dict) else {}
        if target:
            if target.get("source") and target.get("source") != source_id:
                errors.append("route event[%d] target source mismatch" % idx)
            target_head = target.get("head_id")
            if not target_head and target.get("head") is not None:
                target_head = "head:%s" % target.get("head")
            if target_head and target_head != head_id:
                errors.append("route event[%d] target head mismatch" % idx)

        steps = event.get("steps") or []
        if not isinstance(steps, list) or not steps:
            errors.append("route event[%d] steps must be a non-empty list" % idx)
            steps = []
        commands = []
        for step_idx, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append("route event[%d] step[%d] must be an object"
                              % (idx, step_idx))
                continue
            kind = step.get("kind")
            command = step.get("command")
            step_source = step.get("source") or source_id
            if not kind:
                errors.append("route event[%d] step[%d] missing kind"
                              % (idx, step_idx))
            if command:
                commands.append(command)
            if step_source and step_source not in sources:
                errors.append("route event[%d] step[%d] references unknown source %r"
                              % (idx, step_idx, step_source))
            if step.get("source") and step.get("source") != source_id:
                if not (kind == "unload_source"
                        and step.get("source") == event.get("previous_source")):
                    errors.append("route event[%d] step[%d] source mismatch"
                                  % (idx, step_idx))
            if step.get("head") and step.get("head") != head_id:
                errors.append("route event[%d] step[%d] head mismatch"
                              % (idx, step_idx))
            if step_source and head_id and (step_source, head_id) not in enabled_edges:
                errors.append("route event[%d] step[%d] has no enabled edge %s -> %s"
                              % (idx, step_idx, step_source, head_id))
            action = step.get("profile_action")
            profile_id = step.get("profile") or event.get("execution_profile")
            if action:
                profile = profiles.get(profile_id)
                if not isinstance(profile, dict):
                    errors.append("route event[%d] step[%d] profile %r missing"
                                  % (idx, step_idx, profile_id))
                else:
                    action_spec = profile.get(action)
                    if not isinstance(action_spec, dict) or not action_spec.get("command"):
                        errors.append(
                            "route event[%d] step[%d] profile action %r missing command"
                            % (idx, step_idx, action))
                    elif step_source and head_id:
                        expected_target = _target_from_graph_source_edge(
                            graph, step_source, head_id)
                        expected_step = None
                        if expected_target:
                            expected_step = _profile_step(
                                target=expected_target,
                                profiles=profiles,
                                action=action,
                            )
                        if not expected_step:
                            errors.append(
                                "route event[%d] step[%d] cannot derive profile command"
                                % (idx, step_idx))
                        else:
                            if profile_id != expected_step.get("profile"):
                                errors.append(
                                    "route event[%d] step[%d] profile mismatch"
                                    % (idx, step_idx))
                            if command != expected_step.get("command"):
                                errors.append(
                                    "route event[%d] step[%d] command does not match profile"
                                    % (idx, step_idx))
                            for key in ("ace", "slot", "module", "channel"):
                                if key in expected_step and step.get(key) != expected_step.get(key):
                                    errors.append(
                                        "route event[%d] step[%d] %s mismatch"
                                        % (idx, step_idx, key))
            if kind in ("load_source", "unload_source"):
                source = sources.get(step_source) if step_source in sources else {}
                if source.get("kind") == "native_feeder":
                    if step.get("module") not in ("left", "right"):
                        errors.append("route event[%d] step[%d] native module missing"
                                      % (idx, step_idx))
                    if step.get("channel") not in (0, 1):
                        errors.append("route event[%d] step[%d] native channel missing"
                                      % (idx, step_idx))
            if kind == "swap_source":
                source = sources.get(step_source) if step_source in sources else {}
                if source.get("kind") == "ace_slot":
                    if step.get("ace") is None or step.get("slot") is None:
                        errors.append("route event[%d] step[%d] ACE slot missing"
                                      % (idx, step_idx))
        if commands != (event.get("commands") or []):
            errors.append("route event[%d] commands do not mirror steps" % idx)
    for t in sorted(used_tools):
        if t not in event_tools:
            errors.append("route plan missing tool_select event for T%d" % t)
    errors.extend(_validate_route_plan_resources(route_plan, graph))
    return errors

def _normalize_route_head_id(target: dict) -> str | None:
    head_id = target.get("head_id") or target.get("head")
    if head_id is None:
        return None
    if str(head_id).startswith("head:"):
        return str(head_id)
    try:
        return "head:%d" % int(head_id)
    except (TypeError, ValueError):
        return None

def _route_target_resource_entries(route_plan: dict) -> list[tuple[str, str, dict]]:
    entries: list[tuple[str, str, dict]] = []
    if not isinstance(route_plan, dict):
        return entries
    for raw_t, item in (route_plan.get("tool_map") or {}).items():
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        if isinstance(target, dict):
            entries.append(("tool_map[%s]" % raw_t, "target", target))
    for idx, event in enumerate(route_plan.get("events") or []):
        if not isinstance(event, dict):
            continue
        target = event.get("target")
        if isinstance(target, dict):
            entries.append(("event[%d]" % idx, "target", target))
        for step_idx, step in enumerate(event.get("steps") or []):
            if not isinstance(step, dict):
                continue
            if step.get("source") or step.get("head"):
                entries.append(
                    ("event[%d].step[%d]" % (idx, step_idx), "step", step))
    return entries

def _route_plan_resource_usage(route_plan: dict, graph: dict) -> dict:
    source_heads: dict[str, set[str]] = {}
    ace_heads: dict[int, set[str]] = {}
    ace_sources: dict[int, set[str]] = {}
    ace_slots: dict[int, set[int]] = {}
    sources = graph.get("sources") or {}
    for _label, _kind, target in _route_target_resource_entries(route_plan):
        source_id = target.get("source")
        head_id = _normalize_route_head_id(target)
        if not source_id or not head_id:
            continue
        source_heads.setdefault(str(source_id), set()).add(head_id)
        source = sources.get(source_id) if source_id in sources else {}
        if source.get("kind") == "ace_slot" or target.get("kind") == "ace":
            ace_raw = target.get("ace", source.get("ace") if isinstance(source, dict) else None)
            try:
                ace = int(ace_raw)
            except (TypeError, ValueError):
                continue
            ace_heads.setdefault(ace, set()).add(head_id)
            ace_sources.setdefault(ace, set()).add(str(source_id))
            slot_raw = target.get(
                "slot", source.get("slot") if isinstance(source, dict) else None)
            try:
                ace_slots.setdefault(ace, set()).add(int(slot_raw))
            except (TypeError, ValueError):
                pass
    return {
        "source_heads": source_heads,
        "ace_heads": ace_heads,
        "ace_sources": ace_sources,
        "ace_slots": ace_slots,
    }

def _route_plan_resource_summary(route_plan: dict, graph: dict) -> dict:
    usage = _route_plan_resource_usage(route_plan, graph)
    source_heads = usage.get("source_heads") or {}
    ace_heads = usage.get("ace_heads") or {}
    ace_sources = usage.get("ace_sources") or {}
    ace_slots = usage.get("ace_slots") or {}
    return {
        "version": 1,
        "heads": sorted({
            head
            for heads in source_heads.values()
            for head in heads
        }),
        "sources": {
            source_id: {
                "heads": sorted(heads),
            }
            for source_id, heads in sorted(source_heads.items())
        },
        "aces": {
            str(ace): {
                "heads": sorted(ace_heads.get(ace) or []),
                "sources": sorted(ace_sources.get(ace) or []),
                "slots": sorted(ace_slots.get(ace) or []),
            }
            for ace in sorted(ace_heads.keys())
        },
        "constraints": {
            "single_source_single_head": True,
            "single_ace_single_head_per_plan": True,
        },
    }

def _validate_route_plan_resources(route_plan: dict, graph: dict) -> list[str]:
    """Validate resource ownership constraints that are not profile-specific."""
    errors: list[str] = []
    if not isinstance(route_plan, dict):
        return errors
    usage = _route_plan_resource_usage(route_plan, graph)
    source_heads = usage.get("source_heads") or {}
    ace_heads = usage.get("ace_heads") or {}
    for source_id, heads in sorted(source_heads.items()):
        if len(heads) > 1:
            errors.append(
                "route plan maps source %s to multiple heads: %s"
                % (source_id, ", ".join(sorted(heads))))
    for ace, heads in sorted(ace_heads.items()):
        if len(heads) > 1:
            errors.append(
                "route plan maps ACE %d to multiple heads in one print plan: %s"
                % (ace, ", ".join(sorted(heads))))
    resources = route_plan.get("resources")
    if isinstance(resources, dict):
        expected = _route_plan_resource_summary(route_plan, graph)
        if resources != expected:
            errors.append("route plan resources summary does not match events")
    execution = route_plan.get("execution")
    if isinstance(execution, dict):
        expected_execution = _route_plan_execution_summary(route_plan, graph)
        if execution != expected_execution:
            errors.append("route plan execution summary does not match events")
    return errors

def _route_step_locks(event: dict, step: dict, graph: dict) -> dict:
    locks = {
        "heads": set(),
        "sources": set(),
        "aces": set(),
        "ace_slots": set(),
        "native_channels": set(),
    }
    head_id = step.get("head") or event.get("head")
    head_id = _normalize_route_head_id({"head": head_id})
    if head_id:
        locks["heads"].add(head_id)
    source_id = step.get("source") or event.get("source")
    if source_id:
        locks["sources"].add(str(source_id))
    sources = graph.get("sources") or {}
    source = sources.get(source_id) if source_id in sources else {}
    kind = source.get("kind") if isinstance(source, dict) else None
    if kind == "ace_slot" or step.get("ace") is not None:
        ace_raw = step.get(
            "ace", source.get("ace") if isinstance(source, dict) else None)
        slot_raw = step.get(
            "slot", source.get("slot") if isinstance(source, dict) else None)
        try:
            ace = int(ace_raw)
            locks["aces"].add("ace:%d" % ace)
            try:
                locks["ace_slots"].add("ace:%d:%d" % (ace, int(slot_raw)))
            except (TypeError, ValueError):
                pass
        except (TypeError, ValueError):
            pass
    if kind == "native_feeder" or step.get("module") is not None:
        module = step.get(
            "module", source.get("module") if isinstance(source, dict) else None)
        channel = step.get(
            "channel", source.get("channel") if isinstance(source, dict) else None)
        if module is not None and channel is not None:
            try:
                locks["native_channels"].add("%s:%d" % (module, int(channel)))
            except (TypeError, ValueError):
                pass
    return locks

def _merge_lock_sets(items: list[dict]) -> dict:
    merged = {
        "heads": set(),
        "sources": set(),
        "aces": set(),
        "ace_slots": set(),
        "native_channels": set(),
    }
    for item in items:
        for key in merged:
            merged[key].update(item.get(key) or set())
    return {
        key: sorted(values)
        for key, values in merged.items()
        if values
    }

def _route_plan_execution_summary(route_plan: dict, graph: dict) -> dict:
    phases = []
    for event_idx, event in enumerate(route_plan.get("events") or []):
        if not isinstance(event, dict):
            continue
        step_summaries = []
        step_locks = []
        for step_idx, step in enumerate(event.get("steps") or []):
            if not isinstance(step, dict):
                continue
            locks = _route_step_locks(event, step, graph)
            step_locks.append(locks)
            step_summaries.append({
                "index": step_idx,
                "kind": step.get("kind"),
                "profile_action": step.get("profile_action"),
                "source": step.get("source") or event.get("source"),
                "head": step.get("head") or event.get("head"),
                "command": step.get("command"),
                "locks": _merge_lock_sets([locks]),
            })
        phase = {
            "index": len(phases),
            "event_index": event.get("index", event_idx),
            "event_type": event.get("event_type"),
            "slicer_tool": event.get("slicer_tool"),
            "action": event.get("action"),
            "source": event.get("source"),
            "head": event.get("head"),
            "previous_source": event.get("previous_source"),
            "source_changed": bool(event.get("source_changed")),
            "commands": event.get("commands") or [],
            "locks": _merge_lock_sets(step_locks),
            "steps": step_summaries,
        }
        phases.append({
            key: value
            for key, value in phase.items()
            if value not in (None, [], {})
        })
    execution = {
        "version": 1,
        "mode": "sequential",
        "phases": phases,
        "constraints": {
            "sequential_hardware_actions": True,
            "allows_preload_phases": False,
        },
    }
    execution["preload_analysis"] = _route_plan_preload_analysis(
        route_plan, graph, phases)
    return execution

def _route_event_target(event: dict) -> dict:
    target = event.get("target")
    return target if isinstance(target, dict) else {}

def _route_target_can_preload(target: dict, graph: dict) -> tuple[bool, str]:
    source_id = target.get("source")
    source = (graph.get("sources") or {}).get(source_id) or {}
    profile_id = target.get("execution_profile") or source.get("execution_profile")
    profile = (graph.get("profiles") or {}).get(profile_id) or {}
    capabilities = profile.get("capabilities") if isinstance(profile, dict) else {}
    edge = target.get("edge") if isinstance(target.get("edge"), dict) else {}
    constraints = edge.get("constraints") if isinstance(edge.get("constraints"), dict) else {}
    if not isinstance(capabilities, dict) or not capabilities.get("can_preload"):
        return False, "profile_cannot_preload"
    if constraints and constraints.get("allows_preload_while_other_head_prints") is False:
        return False, "edge_blocks_preload"
    return True, "candidate"

def _route_plan_preload_analysis(route_plan: dict, graph: dict,
                                 phases: list[dict]) -> dict:
    candidates = []
    blocked = []
    events = route_plan.get("events") or []
    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        if not event.get("source_changed"):
            continue
        target = _route_event_target(event)
        if not target:
            continue
        can_preload, reason = _route_target_can_preload(target, graph)
        item = {
            "event_index": event.get("index", idx),
            "phase_index": idx if idx < len(phases) else None,
            "slicer_tool": event.get("slicer_tool"),
            "source": event.get("source"),
            "head": event.get("head"),
            "action": event.get("action"),
            "reason": reason,
        }
        if can_preload:
            item["status"] = "candidate_not_scheduled"
            item["blocked_by"] = "preload_scheduler_disabled"
            candidates.append(item)
        else:
            item["status"] = "blocked"
            blocked.append(item)
    return {
        "version": 1,
        "enabled": False,
        "candidates": candidates,
        "blocked": blocked,
        "summary": {
            "candidate_count": len(candidates),
            "blocked_count": len(blocked),
            "scheduled_count": 0,
        },
    }

def _route_plan_affected_heads(route_plan: dict) -> set[str]:
    heads: set[str] = set()
    if not isinstance(route_plan, dict):
        return heads
    for item in (route_plan.get("tool_map") or {}).values():
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        if not isinstance(target, dict):
            continue
        head_id = target.get("head_id")
        if not head_id and target.get("head") is not None:
            head_id = "head:%s" % target.get("head")
        if head_id:
            heads.add(str(head_id))
    for event in route_plan.get("events") or []:
        if not isinstance(event, dict):
            continue
        head_id = event.get("head")
        if head_id:
            heads.add(str(head_id))
        for step in event.get("steps") or []:
            if isinstance(step, dict) and step.get("head"):
                heads.add(str(step.get("head")))
    return heads

def _validate_route_plan_runtime_state(route_plan: dict,
                                       current_state: dict) -> list[str]:
    """Reject stale route plans before moving or uploading print G-code."""
    errors: list[str] = []
    if not isinstance(route_plan, dict):
        return ["route plan must be an object"]
    used_tools = route_plan.get("used_tools") or []
    has_tool_map = bool(route_plan.get("tool_map"))
    has_tool_select = any(
        isinstance(event, dict) and event.get("event_type") == "tool_select"
        for event in route_plan.get("events") or []
    )
    if not (used_tools or has_tool_map or has_tool_select):
        return []
    initial_state = route_plan.get("initial_state")
    if not isinstance(initial_state, dict) or not initial_state:
        return ["route plan initial_state missing; rerun preflight"]
    if not isinstance(current_state, dict):
        return ["current source state missing"]
    plan_hash = initial_state.get("source_graph_hash")
    current_hash = current_state.get("source_graph_hash")
    if plan_hash and current_hash and plan_hash != current_hash:
        errors.append(
            "route plan runtime graph hash mismatch: initial=%s current=%s"
            % (plan_hash, current_hash))
    planned_heads = initial_state.get("heads") or {}
    current_heads = current_state.get("heads") or {}
    if not isinstance(planned_heads, dict):
        return ["route plan initial_state.heads must be an object"]
    if not isinstance(current_heads, dict):
        return ["current source state heads must be an object"]
    # "stale" means the source record exists but the head sensor is empty. The
    # print start path can safely clean that up, so allow it only when the live
    # state still exactly matches the preflight snapshot.
    actionable = {"known", "empty", "stale"}
    for head_id in sorted(_route_plan_affected_heads(route_plan)):
        planned = planned_heads.get(head_id)
        live = current_heads.get(head_id)
        if not isinstance(planned, dict):
            errors.append(
                "route plan initial_state missing affected %s; rerun preflight"
                % head_id)
            continue
        if not isinstance(live, dict):
            errors.append("current source state missing affected %s" % head_id)
            continue
        planned_conf = str(planned.get("source_confidence") or "")
        live_conf = str(live.get("source_confidence") or "")
        if planned_conf not in actionable:
            errors.append(
                "route plan initial_state[%s] is %s; recover head state and rerun preflight"
                % (head_id, planned_conf or "unknown"))
        if live_conf not in actionable:
            errors.append(
                "current source state[%s] is %s; recover head state before print"
                % (head_id, live_conf or "unknown"))
        if planned.get("current_source") != live.get("current_source"):
            errors.append(
                "route plan initial_state[%s] stale: initial=%r current=%r"
                % (head_id, planned.get("current_source"),
                   live.get("current_source")))
        if planned_conf in actionable and live_conf in actionable and planned_conf != live_conf:
            errors.append(
                "route plan initial_state[%s] confidence changed: initial=%s current=%s"
                % (head_id, planned_conf, live_conf))
    return errors

def _source_map_slicer_meta(source_map: dict) -> tuple[dict[int, str], dict[int, str]]:
    colors: dict[int, str] = {}
    materials: dict[int, str] = {}
    for entry in source_map.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            t = int(entry.get("slicer_tool"))
        except (TypeError, ValueError):
            continue
        colors[t] = entry.get("color", "") or ""
        materials[t] = entry.get("material", "") or ""
    return colors, materials

def _validate_manual_tool_targets(parsed: dict, requested: Any,
                                  used_tools: set[int]) -> dict:
    if not isinstance(requested, dict):
        raise HTTPException(status_code=400, detail="tool_targets must be an object")
    available = {
        item.get("key"): item
        for item in _live_loadout_from_parsed(parsed)
        if item.get("ready")
    }
    out: dict[str, dict] = {}
    missing = []
    invalid = []
    for t in sorted(used_tools):
        raw = requested.get(str(t), requested.get(t))
        if raw is None:
            missing.append("T%d" % t)
            continue
        key = _target_key_from_request(raw)
        item = available.get(key or "")
        if item is None:
            invalid.append("T%d" % t)
            continue
        out[str(t)] = _target_from_loadout_item(item)
    if missing:
        raise HTTPException(
            status_code=400,
            detail="manual mapping missing " + ", ".join(missing))
    if invalid:
        raise HTTPException(
            status_code=409,
            detail="manual mapping target unavailable for " + ", ".join(invalid))
    return out

def _used_tools_for_preflight(used: set[int], slicer_colors: dict,
                              tool_targets: dict | None = None) -> set[int]:
    if used:
        return set(used)
    if tool_targets:
        try:
            return {int(t) for t in tool_targets.keys()}
        except Exception:
            pass
    return set(slicer_colors.keys())

def _build_mixed_resolver(parsed: dict, slicer_colors: dict,
                          slicer_types: dict, used_tools: set[int]) -> dict:
    """Resolve slicer tools to source graph route targets.

    This intentionally no longer builds candidates directly from the legacy
    head_mode / aceN_head route status. The only accepted print candidates are
    enabled source graph edges whose runtime source is ready.
    """
    candidates: list[dict] = []
    for item in _live_loadout_from_parsed(parsed):
        if item.get("ready") is False:
            continue
        target = _target_from_loadout_item(item)
        target.update({
            "material": item.get("material") or "",
            "color": item.get("color") or "",
            "priority": ((item.get("edge") or {}).get("priority", 100)),
        })
        candidates.append(target)

    resolver_candidates = []
    for c in candidates:
        target = _target_to_dict(c) or {}
        target.update({
            "material": c.get("material") or "",
            "color": c.get("color") or "",
            "key": _candidate_target_key(c),
        })
        resolver_candidates.append(target)

    scored: list[tuple[tuple, int, dict, str, float]] = []
    for t in sorted(used_tools):
        want_mat = _norm_material(slicer_types.get(t))
        want_color = _norm_color_hex(slicer_colors.get(t))
        for order, c in enumerate(candidates):
            score = _candidate_score(
                want_mat, want_color,
                _norm_material(c.get("material")),
                _norm_color_hex(c.get("color")))
            if score is None:
                continue
            rank, dist, tier = score
            key = (rank, dist, 0 if c.get("kind") == "native" else 1,
                   c.get("priority", 100), c.get("head", 99), c.get("ace", 99),
                   c.get("slot", 99), order, t)
            scored.append((key, t, dict(c), tier, float(dist)))

    assigned: dict[int, dict] = {}
    for _key, t, c, tier, dist in sorted(scored, key=lambda row: row[0]):
        if t in assigned:
            continue
        c["tier"] = tier
        c["distance"] = dist
        assigned[t] = c

    mapping: list[dict] = []
    tool_targets: dict[str, dict] = {}
    errors: list[dict] = []
    for t in sorted(used_tools):
        best = assigned.get(t)
        row = {
            "t": t,
            "target": _target_to_dict(best),
            "tier": best.get("tier") if best else "no_target",
            "distance": best.get("distance") if best else None,
            "loose_mat": False,
            "slot": None,
        }
        if best and best.get("kind") == "ace":
            row["slot"] = {
                "ace": best.get("ace"),
                "slot": best.get("slot"),
                "target_head": best.get("head"),
                "material": best.get("material") or "",
                "color": best.get("color") or "",
            }
        if best:
            target = _target_to_dict(best)
            tool_targets[str(t)] = target
        else:
            errors.append({
                "t": t,
                "kind": "unmapped_tool",
                "message": f"T{t}: no native head or ACE slot matches",
            })
        mapping.append(row)

    return {
        "mapping": mapping,
        "tool_targets": tool_targets,
        "errors": errors,
        "candidates": resolver_candidates,
    }

def _mapping_from_info(info: dict) -> list[dict]:
    out = []
    for t in sorted(info.keys()):
        out.append({
            "t":         t,
            "slot":      _slot_to_dict(info[t]["slot"]),
            "tier":      info[t]["tier"],
            "loose_mat": bool(info[t].get("loose_mat")),
        })
    return out

def _remap_mapping(base_mapping: list[dict], remap_t_to_t: dict[int, int]) -> list[dict]:
    """Apply a T-index → T-index remap on top of an existing slicer-T →
    physical-slot mapping. The remap is the format that
    compute_optimal_remap()/apply_layer_remap() emit: keys are
    post-live-lookup T-indices (= ace*4+slot), values are the
    optimized T-indices the rewritten gcode will use. We translate
    each base entry's slot back through that to land on the
    physical ACE/slot the new gcode will actually target."""
    out = []
    for m in base_mapping:
        if m["slot"] is None:
            out.append(m)
            continue
        live_t = m["slot"]["ace"] * 4 + m["slot"]["slot"]
        new_t = remap_t_to_t.get(live_t, live_t)
        new_slot = dict(m["slot"])
        new_slot["ace"]  = new_t // 4
        new_slot["slot"] = new_t % 4
        new_m = dict(m)
        new_m["slot"] = new_slot
        out.append(new_m)
    return out

def _real_swap_count(events, mapping):
    by_t = {m["t"]: (m.get("target") or m.get("slot"))
            for m in mapping if (m.get("target") or m.get("slot"))}
    head_current = {}
    swaps = 0
    for t in events:
        target = by_t.get(t)
        if target is None:
            continue
        if target.get("kind") == "native":
            h = target.get("head")
            key = ("native", h)
        else:
            h = target.get("head", target.get("target_head"))
            if h is None:
                h = target.get("slot")
            key = ("ace", target.get("ace"), target.get("slot"))
        if head_current.get(h) != key:
            swaps += 1
            head_current[h] = key
    return swaps


def _layout_from_head_assignment(c2h, slicer_colors, slicer_types):
    """Turn {color: head} into a mapping list with (ace, slot=head)
    per color. ACE within each head = first-come-first-served (sorted
    by T-index)."""
    head_ace = {h: 0 for h in range(4)}
    rows = []
    for c in sorted(c2h.keys(), key=lambda x: (c2h[x], x)):
        h = c2h[c]
        ace = head_ace[h]
        head_ace[h] += 1
        rows.append((ace, h, c, {
            "t":         c,
            "slot": {
                "ace":      ace,
                "slot":     h,
                "material": (slicer_types.get(c) or "") or "",
                "color":    (slicer_colors.get(c) or "").lower(),
            },
            "tier":      "planned",
            "loose_mat": False,
        }))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in rows]


def _disabled_plan() -> dict:
    return {
        "feasible":     False,
        "swaps":        0,
        "tool_changes": 0,
        "mapping":      [],
        "reason":       "",
    }


def _build_plan(pp, plan_name, body, result, mapping,
                slicer_colors=None, slicer_types=None, num_aces=4):
    slicer_colors = slicer_colors or {}
    slicer_types  = slicer_types  or {}
    events = result.get("events") or []
    tool_changes = int(result.get("total_changes") or 0)

    if plan_name == "slicer":
        return {
            "feasible":     True,
            "swaps":        _real_swap_count(events, mapping),
            "tool_changes": tool_changes,
            "mapping":      mapping,
        }

    if plan_name == "optimize":
        try:
            c2h, swaps = pp.compute_swap_aware_layout(
                events, num_aces=num_aces)
        except Exception:
            c2h, swaps = None, None
        if c2h is None:
            return {
                "feasible":     False,
                "swaps":        0,
                "tool_changes": tool_changes,
                "mapping":      [],
                "reason":       "no feasible head assignment",
            }
        return {
            "feasible":     True,
            "swaps":        swaps,
            "tool_changes": tool_changes,
            "mapping":      _layout_from_head_assignment(
                c2h, slicer_colors, slicer_types),
        }

    layer_info = result.get("layer_info") or {}
    layer_color_sets_raw = layer_info.get("layer_color_sets") or []
    layer_color_sets = [set(s) for s in layer_color_sets_raw]
    try:
        c2h, swaps = pp.compute_swap_aware_layout(
            events, num_aces=num_aces,
            layer_color_sets=layer_color_sets if layer_color_sets else None)
    except Exception:
        c2h, swaps = None, None
    if c2h is None:
        reason = "no layer-feasible head assignment"
        max_per = layer_info.get("max_per_layer", 0)
        if max_per > 4:
            reason = ">4 colors in some layer"
        return {
            "feasible":     False,
            "swaps":        0,
            "tool_changes": tool_changes,
            "mapping":      [],
            "reason":       reason,
        }
    return {
        "feasible":     True,
        "swaps":        swaps,
        "tool_changes": tool_changes,
        "mapping":      _layout_from_head_assignment(
            c2h, slicer_colors, slicer_types),
        "reason":       "",
    }

_TOOLCHANGE_RE = re.compile(
    r"^;\s*Change Tool\s*(\d+)\s*->\s*Tool\s*(\d+)", re.MULTILINE)

_GCODE_MACHINE_BLOCKERS = [
    re.compile(r";\s*=+\s*machine\s*:\s*(P1S|X1C?|A1(?:\s+mini)?)\b", re.I),
    re.compile(r"\bBambu\b", re.I),
]
_GCODE_DANGEROUS_COMMANDS = {
    "G380",
    "M290",
    "M620",
    "M620.1",
    "M620.11",
    "M621",
    "M628",
    "M629",
    "M710",
    "M960",
    "M970",
    "M974",
    "M975",
}
_GCODE_COMMAND_RE = re.compile(r"^\s*([GMT]\d+(?:\.\d+)?)\b", re.I)

def _validate_web_gcode_safety_text(gcode: str, *, filename: str = "") -> dict:
    errors: list[dict[str, Any]] = []
    machine_signature = ""
    dangerous: list[dict[str, Any]] = []
    for line_no, raw in enumerate(gcode.splitlines(), start=1):
        line = raw.strip()
        if line_no <= 500:
            for pattern in _GCODE_MACHINE_BLOCKERS:
                if pattern.search(line):
                    machine_signature = machine_signature or line[:200]
                    errors.append({
                        "kind": "machine_signature",
                        "line": line_no,
                        "text": line[:200],
                        "message": (
                            "G-code appears to target a non-U1/Bambu-style "
                            "machine profile"),
                    })
                    break
        command_part = raw.split(";", 1)[0]
        m = _GCODE_COMMAND_RE.match(command_part)
        if not m:
            continue
        command = m.group(1).upper()
        if command in _GCODE_DANGEROUS_COMMANDS:
            item = {
                "kind": "dangerous_command",
                "line": line_no,
                "command": command,
                "text": line[:200],
                "message": (
                    "%s is blocked by the Colorful-U1 Web preflight safety gate"
                    % command),
            }
            dangerous.append(item)
            errors.append(item)
    return {
        "ok": not errors,
        "filename": filename,
        "machine_signature": machine_signature,
        "dangerous_commands": dangerous,
        "errors": errors,
    }

def _validate_web_gcode_safety_bytes(data: bytes, *, filename: str = "") -> dict:
    text = data.decode("utf-8", errors="replace")
    return _validate_web_gcode_safety_text(text, filename=filename)

def _raise_gcode_safety_error(validation: dict) -> None:
    if validation.get("ok"):
        return
    parts = []
    for item in (validation.get("errors") or [])[:6]:
        line = item.get("line")
        if item.get("command"):
            parts.append("line %s: %s" % (line, item.get("command")))
        elif item.get("text"):
            parts.append("line %s: %s" % (line, item.get("text")))
    suffix = "; ".join(parts)
    message = (
        "G-code rejected by Colorful-U1 Web safety validation. "
        "Use a Snapmaker U1-compatible slicer profile."
    )
    if suffix:
        message += " Blocked entries: " + suffix
    raise HTTPException(
        status_code=400,
        detail={
            "message": message,
            "validation": validation,
        },
    )

def _used_tool_indices(pp, gcode: str) -> set[int]:
    """Return the set of T-indices actually activated by the gcode.
    Slicers declare a colour for every defined extruder in the
    profile header even if the print only uses a subset; we don't
    want those unused entries cluttering the preflight UI or the
    material check. 'Change Tool X -> Tool Y' comments enumerate
    every transition - union of X and Y is every T-index touched.
    For single-tool gcodes with no transitions we fall back to the
    post-processor's bare-T fallback so a one-colour print still
    shows its initial T."""
    used: set[int] = set()
    for m in _TOOLCHANGE_RE.finditer(gcode):
        used.add(int(m.group(1)))
        used.add(int(m.group(2)))
    if not used:
        try:
            used = set(pp.parse_toolchanges(gcode))
        except Exception:
            used = set()
    return used

_BARE_TOOL_RE = re.compile(r"^\s*T(\d{1,2})\s*(?:;.*)?$")

def _route_tool_events(gcode: str, used_tools: set[int] | None = None) -> list[int]:
    """Return ordered slicer tool selections for route planning.

    Route planning needs the initial tool selection as well as later
    changes.  The older swap estimator only looked at "; Change Tool"
    comments, which misses the first loaded source and breaks same-head
    source transitions.
    """
    events: list[int] = []
    used = set(int(t) for t in (used_tools or set()))
    for raw in gcode.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        m = _BARE_TOOL_RE.match(line)
        if not m:
            continue
        try:
            tool = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if used and tool not in used:
            continue
        if events and events[-1] == tool:
            continue
        events.append(tool)
    return events

async def _create_route_plan_preview_from_upload(file: UploadFile) -> dict:
    raw_name = file.filename or ""
    safe_name = os.path.basename(raw_name)
    if not safe_name or safe_name in (".", "..") or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not safe_name.lower().endswith((".gcode", ".gco", ".g")):
        raise HTTPException(status_code=400, detail="not a g-code file")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > _PREFLIGHT_MAX_SIZE:
        raise HTTPException(
            status_code=413,
            detail=(f"gcode too large for in-printer preflight "
                    f"({len(data)//1024//1024} MB > "
                    f"{_PREFLIGHT_MAX_SIZE//1024//1024} MB limit). "
                    f"Bypass: upload directly via Moonraker's normal "
                    f"upload endpoint, or raise the limit via "
                    f"MULTIACE_PREFLIGHT_MAX_MB env."))
    _raise_gcode_safety_error(
        _validate_web_gcode_safety_bytes(data, filename=safe_name))

    _cleanup_preflight_dir()
    _PREFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    import uuid as _uuid
    token = _uuid.uuid4().hex
    upload_size = len(data)
    src_path = _PREFLIGHT_DIR / (token + ".gcode")
    src_path.write_bytes(data)
    (_PREFLIGHT_DIR / (token + ".name")).write_text(safe_name, encoding="utf-8")
    del data

    pp = _load_post_processor()

    plan_keep_re = re.compile(
        r'^(;\s*Change Tool|;\s*LAYER_CHANGE|;\s*filament\b|T\d{1,2}\s*$)',
        re.IGNORECASE)
    head_lines: list[str] = []
    tail_lines: deque[str] = deque(maxlen=2000)
    plan_lines: list[str] = []
    used: set[int] = set()
    with open(src_path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < 300:
                head_lines.append(line)
            else:
                tail_lines.append(line)
            m = _TOOLCHANGE_RE.match(line)
            if m:
                used.add(int(m.group(1)))
                used.add(int(m.group(2)))
            if plan_keep_re.match(line):
                plan_lines.append(line.rstrip('\n'))
    meta_buf = "".join(head_lines) + "".join(tail_lines)
    plan_proxy = "\n".join(plan_lines)
    del head_lines, tail_lines, plan_lines
    if not used:
        used = _used_tool_indices(pp, plan_proxy)

    slicer_colors = pp.parse_color_names(meta_buf)
    slicer_types  = pp.parse_filament_types(meta_buf)
    num_aces      = pp.infer_num_aces(meta_buf)
    del meta_buf

    if used:
        slicer_colors = {t: c for t, c in slicer_colors.items() if t in used}
        slicer_types  = {t: m for t, m in slicer_types.items() if t in used}

    parsed = _parse_state(await _query_state())
    failed = _load_failed_toolheads(parsed)
    if failed:
        raise HTTPException(
            status_code=409,
            detail="; ".join(_load_failed_message(t) for t in failed),
        )
    live_slots = _live_slots_from_parsed(parsed)
    used_tools = _used_tools_for_preflight(used, slicer_colors)
    resolver = _build_mixed_resolver(
        parsed, slicer_colors, slicer_types, used_tools)
    if not resolver.get("candidates"):
        raise HTTPException(status_code=409,
                            detail="no native head or ACE slot is available")
    tool_targets = resolver.get("tool_targets", {})
    result = {}
    if not resolver.get("errors"):
        result = pp.plan_loadout(plan_proxy, num_aces=num_aces) or {}
    swap_events = [int(t) for t in (result.get("events") or [])]
    route_events = _route_tool_events(plan_proxy, used_tools)
    if not route_events:
        route_events = swap_events
    if not route_events:
        route_events = sorted(used_tools)
    if not swap_events:
        swap_events = route_events
    source_map = _build_preflight_source_map(
        token=token,
        filename=safe_name,
        slicer_colors=slicer_colors,
        slicer_types=slicer_types,
        tool_targets=tool_targets,
        parsed=parsed,
        used_tools=used_tools,
        events=swap_events,
    )
    graph, graph_meta = _load_source_graph(parsed)
    route_plan = _build_route_plan(
        token=token,
        filename=safe_name,
        graph_meta=graph_meta,
        tool_targets=tool_targets,
        used_tools=used_tools,
        events=route_events,
        profiles=(graph.get("profiles") or {}),
        initial_state=sg.source_state(graph, parsed),
        graph=graph,
        stats=source_map.get("swap_stats") or {},
        created_at=source_map.get("created_at"),
    )
    _save_preflight_events(token, route_events)
    _save_preflight_source_map(source_map)
    _save_preflight_route_plan(route_plan)
    (_PREFLIGHT_DIR / (token + ".used")).write_text(
        json.dumps(sorted(used_tools)),
        encoding="utf-8")
    num_aces = max(num_aces, max((s["ace"] for s in live_slots), default=0) + 1)
    missing_mats = []
    if resolver.get("errors"):
        missing_mats = []

    out = {
        "token":         token,
        "filename":      safe_name,
        "size":          upload_size,
        "num_aces":      num_aces,
        "slicer_colors": [
            {"t": t, "hex": (slicer_colors[t] or "").lower(),
             "name": pp.approx_color_name(slicer_colors[t]) or "",
             "material": slicer_types.get(t, "") or ""}
            for t in sorted(slicer_colors.keys())
        ],
        "live_slots": [
            {"ace": s["ace"], "slot": s["slot"],
             "target_head": s.get("target_head"),
             "material": s["material"], "color": s["color"],
             "name": pp.approx_color_name(s["color"]) or ""}
            for s in sorted(live_slots, key=lambda x: (x["ace"], x["slot"]))
        ],
        "live_loadout": sorted(
            _live_loadout_from_parsed(parsed, pp),
            key=lambda x: (
                0 if x.get("kind") == "native" else 1,
                x.get("head", 99),
                x.get("ace", 99),
                x.get("slot", 99),
            )),
        "missing_materials": missing_mats,
        "resolve_errors": resolver.get("errors", []),
        "resolve_candidates": resolver.get("candidates", []),
        "tool_targets": tool_targets,
        "source_map": source_map,
        "route_plan": route_plan,
        "plans": {},
    }
    if not missing_mats and not resolver.get("errors"):

        mapping = resolver.get("mapping", [])
        del plan_proxy
        out["plans"]["slicer"] = _build_plan(
            pp, "slicer", None, result, mapping,
            slicer_colors=slicer_colors, slicer_types=slicer_types,
            num_aces=num_aces)
        out["plans"]["optimize"] = _disabled_plan()
        out["plans"]["layer"] = _disabled_plan()
        del result
    else:
        out["plans"]["slicer"] = {
            "feasible": False,
            "swaps": 0,
            "tool_changes": 0,
            "mapping": resolver.get("mapping", []),
            "reason": "; ".join(e.get("message", "") for e in resolver.get("errors", [])),
        }
        out["plans"]["optimize"] = _disabled_plan()
        out["plans"]["layer"] = _disabled_plan()
    return out

@app.post("/api/preflight")
async def preflight(file: UploadFile = File(...)) -> dict:
    return await _create_route_plan_preview_from_upload(file)

@app.post("/api/route-plan/preview")
async def route_plan_preview(file: UploadFile = File(...)) -> dict:
    return await _create_route_plan_preview_from_upload(file)

_PREFLIGHT_JOBS: dict[str, dict] = {}
_PREFLIGHT_JOBS_LOCK = asyncio.Lock()
_PREFLIGHT_JOB_TTL = 600.0

def _set_stage(state: dict, stage: str, percent: float) -> None:
    state["stage"]   = stage
    state["percent"] = max(0.0, min(100.0, percent))
    state["ts"]      = time.time()

def _stage_progress(state: dict, base: float, span: float):
    """Return a (bytes_done, bytes_total) callable that maps the
    streaming-fn's progress into the job's overall percent track."""
    def cb(done: int, total: int) -> None:
        if total <= 0:
            return
        state["percent"] = max(state.get("percent", 0.0),
                                base + span * (done / total))
        state["ts"] = time.time()
    return cb

_PRINT_PREFS_LINE = ("SET_PRINT_PREFERENCES BED_LEVEL=0 "
                     "FLOW_CALIBRATE=0 TIME_LAPSE_CAMERA=0")

def _prepend_print_prefs(in_path: str, out_path: str) -> None:
    """Stream-copy in_path to out_path with the print-preference line
    prepended at the very top (before the start gcode's calibration).
    Any SET_PRINT_PREFERENCES the slicer already emits is commented out
    so it can't override ours from further down the file."""
    with open(out_path, "w", encoding="utf-8", errors="replace") as out:
        out.write("; multiACE preflight: print preferences\n")
        out.write(_PRINT_PREFS_LINE + "\n")
        with open(in_path, "r", encoding="utf-8", errors="replace") as src:
            for line in src:
                if line.lstrip().upper().startswith("SET_PRINT_PREFERENCES"):
                    out.write("; multiACE disabled: " + line.lstrip())
                    continue
                out.write(line)

def _prune_old_jobs() -> None:
    now = time.time()
    dead = [j for j, s in _PREFLIGHT_JOBS.items()
            if s.get("done") and now - s.get("ts", 0) > _PREFLIGHT_JOB_TTL]
    for j in dead:

        for k in ("tmp_in", "tmp_a", "tmp_b", "tmp_out"):
            p = _PREFLIGHT_JOBS[j].get(k)
            if p:
                try: Path(p).unlink()
                except Exception: pass
        del _PREFLIGHT_JOBS[j]

async def _run_preflight_pipeline(job_id: str, token: str, mode: str,
                                  safe_name: str,
                                  set_prefs: bool = False) -> None:
    state = _PREFLIGHT_JOBS[job_id]
    pp = _load_post_processor()
    src = _PREFLIGHT_DIR / (token + ".gcode")
    source_map = _load_preflight_source_map(token)
    route_plan = _load_preflight_route_plan(token)
    route_targets = _route_plan_targets(route_plan)
    if source_map:
        state["source_map"] = source_map
    if route_plan:
        state["route_plan"] = route_plan

    tmp_a = _PREFLIGHT_DIR / (job_id + ".a.gcode")
    tmp_b = _PREFLIGHT_DIR / (job_id + ".b.gcode")
    state["tmp_a"] = str(tmp_a)
    state["tmp_b"] = str(tmp_b)

    try:

        _set_stage(state, "analyze", 0.0)

        with open(src, "r", encoding="utf-8", errors="replace") as f:

            head_lines: list[str] = []
            tail_lines: deque[str] = deque(maxlen=2000)
            for i, line in enumerate(f):
                if i < 300:
                    head_lines.append(line)
                else:
                    tail_lines.append(line)
        meta_buf = "".join(head_lines) + "".join(tail_lines)
        slicer_colors = pp.parse_color_names(meta_buf)
        slicer_types  = pp.parse_filament_types(meta_buf)
        num_aces      = pp.infer_num_aces(meta_buf)
        del meta_buf, head_lines, tail_lines

        used: set[int] = set()
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _TOOLCHANGE_RE.match(line)
                if m:
                    used.add(int(m.group(1)))
                    used.add(int(m.group(2)))
        if not used:
            try:
                used = set(pp.parse_toolchanges(src.read_text(
                    encoding="utf-8", errors="replace")))
            except Exception:
                used = set()
        if used:
            slicer_colors = {t: c for t, c in slicer_colors.items() if t in used}
            slicer_types  = {t: m for t, m in slicer_types.items() if t in used}
        elif route_targets:
            used = {int(t) for t in route_targets.keys()}

        parsed = _parse_state(await _query_state())
        failed = _load_failed_toolheads(parsed)
        if failed:
            raise RuntimeError(
                "; ".join(_load_failed_message(t) for t in failed))
        live_slots = _live_slots_from_parsed(parsed)
        num_aces = max(num_aces, max((s["ace"] for s in live_slots), default=0) + 1)
        if mode == "slicer":
            if route_targets:
                missing_targets = sorted(
                    t for t in used if str(t) not in route_targets)
                if missing_targets:
                    raise RuntimeError(
                        "route plan mapping is incomplete for "
                        + ", ".join("T%d" % t for t in missing_targets))
                remap = {}
            else:
                raise RuntimeError(
                    "route plan missing; run route-plan preview again")
        else:
            raise RuntimeError(
                "optimize/layer print modes are disabled for the "
                "single-toolhead ACE MVP; use slicer mode")
            _set_stage(state, mode, 1.0)
            sa_result = await asyncio.to_thread(
                pp.plan_loadout_from_file, str(src), num_aces) or {}
            sa_events = sa_result.get("events") or []
            sa_layer_sets = None
            if mode == "layer":
                lcs = (sa_result.get("layer_info") or {}).get("layer_color_sets") or []
                sa_layer_sets = [set(s) for s in lcs] if lcs else None
            c2h, _sa_swaps = pp.compute_swap_aware_layout(
                sa_events, num_aces=num_aces,
                layer_color_sets=sa_layer_sets)
            if c2h is None:
                raise RuntimeError("no feasible head assignment for "
                                   "%s mode" % mode)
            head_ace_counter = {h: 0 for h in range(4)}
            remap = {}
            for c in sorted(c2h.keys(), key=lambda x: (c2h[x], x)):
                h = c2h[c]
                remap[c] = head_ace_counter[h] * 4 + h
                head_ace_counter[h] += 1

        _set_stage(state, "apply_remap", 5.0)
        await asyncio.to_thread(
            pp.apply_remap_to_file,
            str(src), str(tmp_a), remap,
            _stage_progress(state, 5.0, 25.0),
        )
        cur = tmp_a
        nxt = tmp_b

        _set_stage(state, "rewrite", 45.0)
        if not route_plan:
            raise RuntimeError(
                "route plan missing; run source graph preflight again")
        graph, graph_meta = _load_source_graph(parsed)
        route_errors = _validate_route_plan_for_graph(route_plan, graph, graph_meta)
        route_errors.extend(
            _validate_route_plan_runtime_state(
                route_plan, sg.source_state(graph, parsed)))
        if route_errors:
            raise RuntimeError(
                "route plan validation failed: " + "; ".join(route_errors[:8]))
        await asyncio.to_thread(
            pp.rewrite_to_file,
            str(cur), str(nxt),
            _stage_progress(state, 45.0, 30.0),
            route_plan=route_plan,
        )
        cur, nxt = nxt, cur

        _set_stage(state, "inject_auto_load", 75.0)
        await asyncio.to_thread(
            pp.inject_auto_load_to_file,
            str(cur), str(nxt),
            _stage_progress(state, 75.0, 10.0),
        )
        cur, nxt = nxt, cur

        if set_prefs:
            _set_stage(state, "print_prefs", 84.0)
            await asyncio.to_thread(
                _prepend_print_prefs, str(cur), str(nxt))
            cur, nxt = nxt, cur

        final_validation = _validate_web_gcode_safety_text(
            cur.read_text(encoding="utf-8", errors="replace"),
            filename=safe_name,
        )
        if not final_validation.get("ok"):
            blocked = []
            for item in (final_validation.get("errors") or [])[:6]:
                if item.get("command"):
                    blocked.append("line %s: %s" % (
                        item.get("line"), item.get("command")))
                elif item.get("text"):
                    blocked.append("line %s: %s" % (
                        item.get("line"), item.get("text")))
            raise RuntimeError(
                "final G-code safety validation failed: "
                + "; ".join(blocked))

        _set_stage(state, "upload", 85.0)
        with open(cur, "rb") as fh:
            files = {"file": (safe_name, fh, "application/octet-stream")}
            payload = {"root": "gcodes", "print": "true"}
            try:
                async with httpx.AsyncClient(timeout=600.0) as client:
                    r = await client.post(
                        f"{MOONRAKER_URL}/server/files/upload",
                        data=payload, files=files)
                    r.raise_for_status()
                    state["moonraker"] = r.json()
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"moonraker {e.response.status_code}: "
                                   f"{e.response.text}")
            except httpx.HTTPError as e:
                raise RuntimeError(f"moonraker: {e}")

        _set_stage(state, "done", 100.0)
        state["filename"] = safe_name
        state["mode"]     = mode
        state["source_map"] = _load_preflight_source_map(token) or source_map
        state["route_plan"] = _load_preflight_route_plan(token) or route_plan
        state["done"]     = True
    except Exception as exc:
        state["error"] = str(exc)
        state["done"]  = True
        state["ts"]    = time.time()
    finally:

        for p in (tmp_a, tmp_b):
            try: p.unlink()
            except Exception: pass

class _PreflightPrint(BaseModel):
    token: str
    mode:  str
    set_prefs: bool = False
    tool_targets: dict[str, Any] | None = None

class RoutePlanPrintRequest(BaseModel):
    token: str
    set_prefs: bool = False

class RoutePlanRemapRequest(BaseModel):
    token: str
    tool_targets: dict[str, Any]

async def _apply_route_plan_tool_targets(token: str,
                                         requested: dict[str, Any]) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    gpath = _PREFLIGHT_DIR / (token + ".gcode")
    npath = _PREFLIGHT_DIR / (token + ".name")
    upath = _PREFLIGHT_DIR / (token + ".used")
    if not gpath.is_file():
        raise HTTPException(status_code=404,
                            detail="route plan token expired or unknown")
    try:
        used_raw = json.loads(upath.read_text(encoding="utf-8"))
        used_tools = {int(t) for t in used_raw}
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="used-tool metadata missing; run route-plan preview again")
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    failed = _load_failed_toolheads(parsed)
    if failed:
        raise HTTPException(
            status_code=409,
            detail="; ".join(_load_failed_message(t) for t in failed),
        )
    manual_targets = _validate_manual_tool_targets(
        parsed, requested, used_tools)
    safe_name = (npath.read_text(encoding="utf-8").strip()
                 if npath.is_file() else (token + ".gcode"))
    existing_map = _load_preflight_source_map(token)
    slicer_colors, slicer_types = _source_map_slicer_meta(existing_map)
    route_events = _load_preflight_events(token)
    source_map = _build_preflight_source_map(
        token=token,
        filename=safe_name,
        slicer_colors=slicer_colors,
        slicer_types=slicer_types,
        tool_targets=manual_targets,
        parsed=parsed,
        used_tools=used_tools,
        events=route_events,
    )
    graph, graph_meta = _load_source_graph(parsed)
    route_plan = _build_route_plan(
        token=token,
        filename=safe_name,
        graph_meta=graph_meta,
        tool_targets=manual_targets,
        used_tools=used_tools,
        events=route_events,
        profiles=(graph.get("profiles") or {}),
        initial_state=sg.source_state(graph, parsed),
        graph=graph,
        stats=source_map.get("swap_stats") or {},
        created_at=source_map.get("created_at"),
    )
    route_errors = _validate_route_plan_for_graph(route_plan, graph, graph_meta)
    route_errors.extend(
        _validate_route_plan_runtime_state(
            route_plan, sg.source_state(graph, parsed)))
    if route_errors:
        raise HTTPException(
            status_code=409,
            detail="route plan validation failed: " + "; ".join(route_errors[:8]))
    _save_preflight_source_map(source_map)
    _save_preflight_route_plan(route_plan)
    return {
        "ok": True,
        "token": token,
        "filename": safe_name,
        "source_map": source_map,
        "route_plan": route_plan,
        "tool_targets": manual_targets,
    }

async def _start_route_plan_print(token: str, *, set_prefs: bool = False,
                                  mode: str = "slicer") -> dict:
    if mode not in ("slicer", "optimize", "layer"):
        raise HTTPException(status_code=400, detail="invalid mode")
    if mode != "slicer":
        raise HTTPException(
            status_code=400,
            detail=("optimize/layer print modes are disabled for the "
                    "single-toolhead ACE MVP; use slicer mode"))
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    gpath = _PREFLIGHT_DIR / (token + ".gcode")
    npath = _PREFLIGHT_DIR / (token + ".name")
    if not gpath.is_file():
        raise HTTPException(status_code=404,
                            detail="route plan token expired or unknown")
    if not _load_preflight_route_plan(token):
        raise HTTPException(
            status_code=404,
            detail="route plan missing; run route-plan preview again")
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    failed = _load_failed_toolheads(parsed)
    if failed:
        raise HTTPException(
            status_code=409,
            detail="; ".join(_load_failed_message(t) for t in failed),
        )
    safe_name = (npath.read_text(encoding="utf-8").strip()
                 if npath.is_file() else (token + ".gcode"))
    _prune_old_jobs()
    import uuid as _uuid
    job_id = _uuid.uuid4().hex
    _PREFLIGHT_JOBS[job_id] = {
        "stage":    "queued",
        "percent":  0.0,
        "done":     False,
        "error":    None,
        "filename": safe_name,
        "mode":     mode,
        "ts":       time.time(),
        "source_map": _load_preflight_source_map(token),
        "route_plan": _load_preflight_route_plan(token),
    }
    asyncio.create_task(_run_preflight_pipeline(
        job_id, token, mode, safe_name, set_prefs))
    return {"job_id": job_id, "filename": safe_name, "mode": mode}

@app.post("/api/preflight/print")
async def preflight_print(req: _PreflightPrint) -> dict:
    if req.tool_targets is not None:
        raise HTTPException(
            status_code=400,
            detail=("tool_targets override is no longer accepted; update the "
                    "source graph and run route-plan preview again"))
    return await _start_route_plan_print(
        req.token, set_prefs=req.set_prefs, mode=req.mode)

@app.post("/api/route-plan/print")
async def route_plan_print(req: RoutePlanPrintRequest) -> dict:
    return await _start_route_plan_print(
        req.token, set_prefs=req.set_prefs, mode="slicer")

@app.post("/api/route-plan/remap")
async def route_plan_remap(req: RoutePlanRemapRequest) -> dict:
    return await _apply_route_plan_tool_targets(req.token, req.tool_targets)

@app.get("/api/preflight/print/status")
async def preflight_print_status(job_id: str) -> dict:
    state = _PREFLIGHT_JOBS.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")

    return {
        "job_id":  job_id,
        "stage":   state.get("stage"),
        "percent": round(state.get("percent", 0.0), 1),
        "done":    bool(state.get("done")),
        "error":   state.get("error"),
        "filename": state.get("filename"),
        "mode":    state.get("mode"),
        "source_map": state.get("source_map") or {},
        "route_plan": state.get("route_plan") or {},
    }

@app.get("/api/preflight/source-map")
async def preflight_source_map(token: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    source_map = _load_preflight_source_map(token)
    if not source_map:
        raise HTTPException(status_code=404, detail="source map not found")
    return source_map

@app.get("/api/preflight/route-plan")
async def preflight_route_plan(token: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    route_plan = _load_preflight_route_plan(token)
    if not route_plan:
        raise HTTPException(status_code=404, detail="route plan not found")
    return route_plan

@app.get("/api/preflight/route-plan/validate")
async def preflight_route_plan_validate(token: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    route_plan = _load_preflight_route_plan(token)
    if not route_plan:
        raise HTTPException(status_code=404, detail="route plan not found")
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    errors = _validate_route_plan_for_graph(route_plan, graph, meta)
    current_state = sg.source_state(graph, parsed)
    errors.extend(_validate_route_plan_runtime_state(route_plan, current_state))
    return {
        "ok": not errors,
        "errors": errors,
        "route_plan": {
            "version": route_plan.get("version"),
            "source_graph_hash": route_plan.get("source_graph_hash"),
            "events": len(route_plan.get("events") or []),
        },
        "source_graph": {
            "hash": meta.get("hash"),
            "source": meta.get("source"),
            "path": meta.get("path"),
            "errors": meta.get("errors", []),
            "warnings": meta.get("warnings", []),
        },
        "current_state": current_state,
    }

@app.post("/api/route-plan/validate")
async def route_plan_validate(payload: RoutePlanValidateRequest) -> dict:
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    route_plan = payload.route_plan
    errors = _validate_route_plan_for_graph(route_plan, graph, meta)
    current_state = sg.source_state(graph, parsed)
    errors.extend(_validate_route_plan_runtime_state(route_plan, current_state))
    return {
        "ok": not errors,
        "errors": errors,
        "route_plan": {
            "version": route_plan.get("version") if isinstance(route_plan, dict) else None,
            "source_graph_hash": (
                route_plan.get("source_graph_hash")
                if isinstance(route_plan, dict) else None
            ),
            "events": (
                len(route_plan.get("events") or [])
                if isinstance(route_plan, dict) else 0
            ),
        },
        "source_graph": {
            "hash": meta.get("hash"),
            "source": meta.get("source"),
            "path": meta.get("path"),
            "errors": meta.get("errors", []),
            "warnings": meta.get("warnings", []),
        },
        "current_state": current_state,
    }

_cfg_scalar_cache: dict = {"mtime": 0.0, "values": {}}

def _read_cfg_scalars() -> dict:
    try:
        st = Path(MULTIACE_CFG_PATH).stat()
    except OSError:
        return _cfg_scalar_cache["values"]
    if st.st_mtime == _cfg_scalar_cache["mtime"]:
        return _cfg_scalar_cache["values"]
    try:
        text = Path(MULTIACE_CFG_PATH).read_text(encoding="utf-8")
        main, _per_ace = _extract_params(text)
    except Exception:
        return _cfg_scalar_cache["values"]
    _cfg_scalar_cache["mtime"] = st.st_mtime
    _cfg_scalar_cache["values"] = main
    return main

def _read_display_index_base() -> int:
    """ace.cfg is the source of truth, with the env-var (passed by the
    Klipper-side spawn) as a fallback for setups where multiace-web
    was started by /etc/init.d/S98multiace-web (which doesn't forward
    the cfg value) instead of by ace.py's _spawn_multiace_web."""
    scalars = _read_cfg_scalars()
    raw = scalars.get("display_index_base")
    if raw is None:
        raw = os.environ.get("MULTIACE_DISPLAY_INDEX_BASE", "0")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return 0 if v < 0 else (1 if v > 1 else v)

def _read_update_cfg() -> dict[str, str]:
    """Pull update_repo, update_prerelease and update_url_base from
    ace.cfg so the Web backend uses the same source as the gcode
    ACE_UPDATE_* commands. Falls back to defaults if the cfg isn't
    parseable or keys are missing."""
    repo = "decay71/multiACE"
    prerelease = "0"
    url_base = ""
    try:
        text = Path(MULTIACE_CFG_PATH).read_text(encoding="utf-8")
        main, _per_ace = _extract_params(text)
        if "update_repo" in main and main["update_repo"]:
            repo = main["update_repo"]
        v = main.get("update_prerelease", "").strip().lower()
        if v in ("true", "1", "yes", "on"):
            prerelease = "1"
        if "update_url_base" in main and main["update_url_base"]:
            url_base = main["update_url_base"].strip()
    except Exception:
        pass
    return {
        "MULTIACE_UPDATE_REPO":      repo,
        "MULTIACE_UPDATE_PRERELEASE": prerelease,
        "MULTIACE_UPDATE_URL_BASE":  url_base,
    }

async def _run_update_script(args: list[str], timeout: float) -> dict:
    """Exec the bundled multiace_update.sh and capture stdout+rc."""

    update_script = None
    for candidate in (
        "/home/lava/multiace_update.sh",
        "/home/lava/multiace/tools/multiace_update.sh",
    ):
        if Path(candidate).is_file():
            update_script = candidate
            break
    if update_script is None:
        raise HTTPException(
            status_code=503,
            detail=("Updater script not found at "
                    "/home/lava/multiace/tools/multiace_update.sh "
                    "or /home/lava/multiace_update.sh. "
                    "Re-run install_multiace.sh from the repo to ship it."))
    env = os.environ.copy()
    env.update(_read_update_cfg())
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", update_script, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(),
                                               timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504,
                                detail=f"Updater timed out after {timeout}s")
    except FileNotFoundError:
        raise HTTPException(status_code=500,
                            detail="bash not on PATH on this host")
    out = (stdout or b"").decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "stdout": out,

        "status_lines": [
            line.split("STATUS:", 1)[1].strip()
            for line in out.splitlines() if "STATUS:" in line
        ],
    }

@app.get("/api/update/check")
async def update_check() -> dict:
    return await _run_update_script(["check"], timeout=30.0)

@app.post("/api/update/apply")
async def update_apply(force: bool = False) -> dict:

    if not _DEBUG_FLAG_PATH.exists():
        raise HTTPException(
            status_code=409,
            detail=("Persistent updates disabled. Enable debug mode "
                    "(touch /oem/.debug) and reboot before applying "
                    "updates, otherwise the install is wiped on next "
                    "boot."))
    args = ["apply"]
    if force:
        args.append("--force")
    return await _run_update_script(args, timeout=600.0)

_DEBUG_FLAG_PATH = Path("/oem/.debug")

async def _sudo_run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "timeout"
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "sudo not on PATH"

@app.get("/api/debug-mode")
async def debug_mode_get() -> dict:
    return {"enabled": _DEBUG_FLAG_PATH.exists()}

@app.post("/api/debug-mode/enable")
async def debug_mode_enable() -> dict:
    rc, out = await _sudo_run(["/usr/bin/touch", str(_DEBUG_FLAG_PATH)])
    if rc != 0:
        raise HTTPException(
            status_code=500,
            detail=(f"sudo touch /oem/.debug failed (rc={rc}): {out.strip()}. "
                    "Sudoers drop-in /etc/sudoers.d/multiace-debug may be "
                    "missing - re-run install_multiace.sh."))
    return {"enabled": _DEBUG_FLAG_PATH.exists(), "stdout": out}

@app.post("/api/debug-mode/disable")
async def debug_mode_disable() -> dict:
    if not _DEBUG_FLAG_PATH.exists():
        return {"enabled": False, "stdout": "already disabled"}
    rc, out = await _sudo_run(["/bin/rm", "-f", str(_DEBUG_FLAG_PATH)])
    if rc != 0:
        raise HTTPException(
            status_code=500,
            detail=f"sudo rm /oem/.debug failed (rc={rc}): {out.strip()}")
    return {"enabled": _DEBUG_FLAG_PATH.exists(), "stdout": out}

@app.post("/api/reboot")
async def reboot() -> dict:

    try:
        result = await _mr_post("/machine/reboot", timeout=10.0)
        return {"ok": True, "moonraker": result}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502,
                            detail=f"moonraker reboot failed: {e}")

@app.post("/api/upload-and-print")
async def upload_and_print(file: UploadFile = File(...)) -> dict:

    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    failed = _load_failed_toolheads(parsed)
    if failed:
        raise HTTPException(
            status_code=409,
            detail="; ".join(_load_failed_message(t) for t in failed),
        )

    raw_name = file.filename or ""
    safe_name = os.path.basename(raw_name)
    if not safe_name or safe_name in (".", "..") or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not safe_name.lower().endswith((".gcode", ".gco", ".g")):
        raise HTTPException(status_code=400, detail="not a g-code file")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    files = {"file": (safe_name, data, file.content_type or "application/octet-stream")}
    payload = {"root": "gcodes", "print": "true"}
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(f"{MOONRAKER_URL}/server/files/upload",
                                  data=payload, files=files)
            r.raise_for_status()
            return {"ok": True, "filename": safe_name, "moonraker": r.json()}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code,
                            detail=f"moonraker: {e.response.text}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")

@app.get("/api/state")
async def get_state() -> dict:
    """Aggregated dashboard state (ACEs + toolheads + dryer + status)."""
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        return {"error": f"moonraker: {e}"}
    return _parse_state(status)

@app.get("/api/source-graph")
async def get_source_graph() -> dict:
    """Return the configured source graph plus validation metadata."""
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    return {
        "graph": graph,
        "meta": meta,
    }

@app.post("/api/source-graph")
async def update_source_graph(payload: SourceGraphUpdate) -> dict:
    """Persist a source graph without restarting Klipper or moving hardware."""
    try:
        meta = sg.save_graph(MULTIACE_SOURCE_GRAPH_PATH, payload.graph)
    except sg.SourceGraphError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "meta": meta,
    }

@app.get("/api/source-state")
async def get_source_state() -> dict:
    """Return runtime source/head state interpreted through the source graph."""
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    state = sg.source_state(graph, parsed)
    state["meta"] = meta
    return state

@app.post("/api/source-action/preview")
async def preview_source_action(payload: SourceActionPreview) -> dict:
    """Build one profile-driven source action step without moving hardware."""
    action = str(payload.action or "").strip().lower()
    if action not in ("load", "unload", "swap"):
        raise HTTPException(
            status_code=400,
            detail="action must be load, unload or swap")
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    target = _target_from_graph_source_edge(graph, payload.source, payload.head)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail="enabled source edge not found")
    step = _profile_step(
        target=target,
        profiles=(graph.get("profiles") or {}),
        action=action,
    )
    if step is None:
        raise HTTPException(
            status_code=409,
            detail=f"profile action {action!r} is not available")
    event = _source_action_event(
        index=0,
        action=action,
        target=target,
        step=step,
    )
    return {
        "source_graph": {
            "hash": meta.get("hash"),
            "source": meta.get("source"),
            "path": meta.get("path"),
            "errors": meta.get("errors", []),
            "warnings": meta.get("warnings", []),
        },
        "target": target,
        "step": step,
        "event": event,
        "command": step.get("command"),
    }

@app.post("/api/source-actions/preview")
async def preview_source_actions(payload: SourceActionPreviewBatch) -> dict:
    """Build a profile-driven source action route-plan fragment."""
    if not payload.actions:
        raise HTTPException(status_code=400, detail="actions must not be empty")
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    profiles = graph.get("profiles") or {}
    events = []
    commands = []
    targets = []
    for index, item in enumerate(payload.actions):
        action = str(item.action or "").strip().lower()
        if action not in ("load", "unload", "swap"):
            raise HTTPException(
                status_code=400,
                detail="action must be load, unload or swap")
        target = _target_from_graph_source_edge(graph, item.source, item.head)
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"enabled source edge not found for action {index}")
        step = _profile_step(target=target, profiles=profiles, action=action)
        if step is None:
            raise HTTPException(
                status_code=409,
                detail=f"profile action {action!r} is not available for action {index}")
        event = _source_action_event(
            index=index,
            action=action,
            target=target,
            step=step,
        )
        events.append(event)
        commands.extend(event.get("commands") or [])
        targets.append(target)
    route_plan = {
        "version": 2,
        "source_graph_hash": meta.get("hash"),
        "source_graph": {
            "hash": meta.get("hash"),
            "source": meta.get("source"),
            "path": meta.get("path"),
            "errors": meta.get("errors", []),
            "warnings": meta.get("warnings", []),
        },
        "initial_state": sg.source_state(graph, parsed),
        "events": events,
        "commands": commands,
    }
    return {
        "source_graph": route_plan["source_graph"],
        "targets": targets,
        "events": events,
        "commands": commands,
        "route_plan": route_plan,
    }

@app.post("/api/source-transition/preview")
async def preview_source_transition(payload: SourceTransitionPreview) -> dict:
    """Preview a graph-driven transition to one source on one head.

    This is planning only: it returns the unload/select/load-or-swap commands
    needed for the current source state, but it does not move hardware.
    """
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    target = _target_from_graph_source_edge(graph, payload.source, payload.head)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail="enabled source edge not found")
    initial_state = sg.source_state(graph, parsed)
    event = _source_transition_event(
        index=0,
        target=target,
        initial_state=initial_state,
        graph=graph,
    )
    route_plan = {
        "version": 2,
        "source_graph_hash": meta.get("hash"),
        "source_graph": {
            "hash": meta.get("hash"),
            "source": meta.get("source"),
            "path": meta.get("path"),
            "errors": meta.get("errors", []),
            "warnings": meta.get("warnings", []),
        },
        "initial_state": initial_state,
        "events": [event],
        "commands": event.get("commands") or [],
    }
    errors = _validate_route_plan_for_graph(route_plan, graph, meta)
    return {
        "ok": not errors,
        "errors": errors,
        "source_graph": route_plan["source_graph"],
        "initial_state": initial_state,
        "target": target,
        "event": event,
        "commands": route_plan["commands"],
        "route_plan": route_plan,
    }

@app.get("/api/aces")
async def list_aces() -> dict:
    """Backwards-compatible subset of /api/state - only the per-ACE list."""
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        return {"aces": [], "error": f"moonraker: {e}"}
    parsed = _parse_state(status)
    return {"aces": parsed["aces"], "active_device": parsed["active_device"]}

@app.get("/api/debug")
async def get_debug() -> dict:
    """Raw moonraker dump - useful for inspecting unknown fields."""
    try:
        return await _query_state()
    except httpx.HTTPError as e:
        return {"error": f"moonraker: {e}"}

_MACRO_PREFIX = "gcode_macro "
_MACRO_BUCKETS = (
    ("switch", lambda m: m.startswith("ACEA__Switch")),
    ("load",   lambda m: m.startswith("ACEC__Load")),
    ("unload", lambda m: m.startswith("ACEC__Unload")),
    ("dry",    lambda m: m.startswith("ACED__Dry")),
    ("status", lambda m: m.startswith("ACEG__")),
)

_macro_jobs: dict[str, dict[str, Any]] = {}
_MACRO_JOB_TTL = 3600.0

def _prune_macro_jobs() -> None:
    cutoff = time.time() - _MACRO_JOB_TTL
    for job_id, job in list(_macro_jobs.items()):
        if job.get("updated", job.get("created", 0.0)) < cutoff:
            _macro_jobs.pop(job_id, None)

@app.get("/api/macros")
async def list_macros() -> dict:
    """
    Auto-discover ACE-related gcode_macro objects from Moonraker and
    bucket them into categories that the frontend can render as button
    groups. Source of truth = whatever ace.cfg / printer.cfg defines.
    """
    try:
        data = await _mr_get("/printer/objects/list")
    except httpx.HTTPError as e:
        return {"all": [], "categorized": {}, "error": f"moonraker: {e}"}
    objs = data.get("result", {}).get("objects", []) or []
    macros = sorted(
        o[len(_MACRO_PREFIX):]
        for o in objs
        if isinstance(o, str) and o.startswith(_MACRO_PREFIX)
        and "ACE" in o
    )
    cats: dict[str, list[str]] = {name: [] for name, _ in _MACRO_BUCKETS}
    cats["other"] = []
    for m in macros:
        for name, pred in _MACRO_BUCKETS:
            if pred(m):
                cats[name].append(m)
                break
        else:
            cats["other"].append(m)
    return {"all": macros, "categorized": cats}

@app.post("/api/macro-batch", status_code=202)
async def run_macro_batch(req: MacroBatchRequest) -> dict:

    if not req.commands:
        raise HTTPException(status_code=400, detail="no commands")
    lines = []
    for c in req.commands:
        _validate_macro_request(c.name, c.args)
        parts = [c.name]
        if c.args:
            for k, v in c.args.items():
                parts.append(f"{k}={v}")
        lines.append(" ".join(parts))
    script = "\n".join(lines)

    async def _dispatch():
        try:
            await _mr_post("/printer/gcode/script", {"script": script},
                           timeout=None)
        except Exception as e:
            _trace.warning("macro-batch dispatch failed: %s", e)

    asyncio.create_task(_dispatch())
    _trace.info("macro-batch: dispatched %d commands to Moonraker", len(lines))
    return {"ok": True, "count": len(lines), "script_lines": lines}

@app.post("/api/macro-async", status_code=202)
async def run_macro_async(req: MacroRequest) -> dict:
    _validate_macro_request(req.name, req.args)
    parts = [req.name]
    if req.args:
        for k, v in req.args.items():
            parts.append(f"{k}={v}")
    script = " ".join(parts)
    _prune_macro_jobs()
    job_id = uuid.uuid4().hex
    now = time.time()
    _macro_jobs[job_id] = {
        "id": job_id,
        "script": script,
        "status": "queued",
        "error": None,
        "created": now,
        "updated": now,
    }

    async def _dispatch():
        job = _macro_jobs.get(job_id)
        if job is not None:
            job["status"] = "running"
            job["updated"] = time.time()
        try:
            result = await _mr_post("/printer/gcode/script",
                                    {"script": script}, timeout=None)
            job = _macro_jobs.get(job_id)
            if job is not None:
                job["status"] = "done"
                job["result"] = result
                job["updated"] = time.time()
        except httpx.HTTPStatusError as e:
            detail = e.response.text or str(e)
            job = _macro_jobs.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = detail
                job["updated"] = time.time()
            _trace.warning("macro-async dispatch failed for %r: %s",
                           script, detail[:300])
        except Exception as e:
            job = _macro_jobs.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = str(e) or type(e).__name__
                job["updated"] = time.time()
            _trace.warning("macro-async dispatch failed for %r: %s", script, e)

    asyncio.create_task(_dispatch())
    _trace.info("macro-async: dispatched %s job=%s", script, job_id)
    return {"ok": True, "script": script, "job_id": job_id}

@app.get("/api/macro-jobs/{job_id}")
async def get_macro_job(job_id: str) -> dict:
    _prune_macro_jobs()
    job = _macro_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="macro job not found")
    return job

@app.post("/api/macro")
async def run_macro(req: MacroRequest) -> dict:
    _validate_macro_request(req.name, req.args)
    parts = [req.name]
    if req.args:
        for k, v in req.args.items():
            parts.append(f"{k}={v}")
    script = " ".join(parts)
    try:
        result = await _mr_post("/printer/gcode/script",
                                {"script": script}, timeout=1800.0)
    except httpx.HTTPStatusError as e:
        print('[/api/macro] HTTPStatusError on %r: %d %s'
              % (script, e.response.status_code,
                 (e.response.text or '').strip()[:300]),
              file=sys.stderr, flush=True)
        raise HTTPException(
            status_code=e.response.status_code,
            detail=e.response.text,
        )
    except httpx.HTTPError as e:
        print('[/api/macro] HTTPError on %r: %s: %s'
              % (script, type(e).__name__, str(e) or '(no message)'),
              file=sys.stderr, flush=True)
        raise HTTPException(status_code=502,
            detail='moonraker: %s' % (str(e) or type(e).__name__))
    return {"script": script, "result": result}

def _extract_params(text: str) -> tuple[dict[str, str], dict[int, dict[str, str]]]:
    """Pull `key: value` pairs out of [ace] and per-ACE [ace N] sections.
    Returns (main_params, per_ace_params) where per_ace_params is a dict
    keyed by ACE index (int). Comments are skipped."""
    main: dict[str, str] = {}
    per_ace: dict[int, dict[str, str]] = {}
    section: object = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            head = s[1:-1].strip()
            if head == "ace":
                section = "ace"
            elif head.startswith("ace ") or head.startswith("ace\t"):
                try:
                    section = int(head.split(None, 1)[1])
                except (IndexError, ValueError):
                    section = None
            else:
                section = None
            continue
        if section is None or ":" not in s:
            continue
        k, v = s.split(":", 1)
        key, val = k.strip(), v.strip()
        if section == "ace":
            main[key] = val
        else:
            per_ace.setdefault(section, {})[key] = val
    return main, per_ace

def _sanitize_config_content(text: str) -> str:
    """Drop obsolete topology/mode keys and removed macro blocks.

    This runs on every config write, including raw-editor saves, so stale
    normal/multi/standard routing cannot be reintroduced through the web API.
    """
    out: list[str] = []
    section: str | None = None
    skip_macro = False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            name = s[1:-1].strip()
            if name.startswith("gcode_macro "):
                macro = name[len("gcode_macro "):].strip()
                skip_macro = macro in OBSOLETE_GCODE_MACROS
            else:
                skip_macro = False
            section = name
            if skip_macro:
                continue
            out.append(raw)
            continue
        if skip_macro:
            continue
        if section == "ace" and ":" in s and not s.startswith("#"):
            key = s.split(":", 1)[0].strip()
            if key in OBSOLETE_ACE_CONFIG_KEYS:
                continue
        out.append(raw)
    trailing_newline = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + trailing_newline

@app.get("/api/config")
async def get_config() -> dict:
    p = Path(MULTIACE_CFG_PATH)
    if not p.exists():
        raise HTTPException(404, f"config file not found: {MULTIACE_CFG_PATH}")
    text = p.read_text(encoding="utf-8")
    main, per_ace = _extract_params(text)
    return {"path": str(p), "content": text, "params": main, "per_ace_params": per_ace}

@app.put("/api/config")
async def update_config(payload: ConfigUpdate) -> dict:
    p = Path(MULTIACE_CFG_PATH)
    if not p.exists():
        raise HTTPException(404, f"config file not found: {MULTIACE_CFG_PATH}")
    backup = p.with_suffix(p.suffix + ".bak")
    backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    clean_content = _sanitize_config_content(payload.content)
    p.write_text(clean_content, encoding="utf-8")
    restart: dict | None = None
    if payload.restart_klipper:
        try:
            restart = await _mr_post("/printer/restart", {})
        except httpx.HTTPError as e:
            restart = {"error": str(e)}
    return {"path": str(p), "backup": str(backup), "restart": restart}

_LANG_NAME_RE = re.compile(r"^[A-Za-z]{2}(-[A-Za-z]{2})?$")

def _load_catalog(lang: str) -> dict:
    if not _LANG_NAME_RE.match(lang):
        raise HTTPException(400, "invalid language code")
    p = Path(I18N_DIR) / f"{lang}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _merge_dicts(base: dict, overlay: dict) -> dict:
    """Recursive overlay-merge: keys in `overlay` override `base`,
    nested dicts are merged the same way."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dicts(out[k], v)
        else:
            out[k] = v
    return out

@app.get("/api/i18n/{lang}")
async def get_i18n(lang: str) -> dict:
    """
    Return the catalog for `lang`, merged on top of the en.json fallback
    so missing keys still resolve to English.
    """
    en = _load_catalog("en")
    if lang == "en":
        return en
    catalog = _load_catalog(lang)
    if not catalog:
        raise HTTPException(404, f"language not found: {lang}")
    return _merge_dicts(en, catalog)

@app.get("/api/i18n")
async def list_i18n() -> dict:
    """List available catalog languages."""
    d = Path(I18N_DIR)
    if not d.is_dir():
        return {"languages": []}
    langs = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            meta = data.get("_meta", {}) or {}
            langs.append({
                "code": p.stem,
                "name": meta.get("name", p.stem),
                "fallback": meta.get("fallback"),
            })
        except Exception:
            continue
    return {"languages": langs}

@app.get("/api/screen-available")
async def screen_available() -> dict:
    """
    Probe paxx fb-http (port 8092). Returns {available: true} if reachable,
    {available: false, error: ...} otherwise. Frontend uses this to show
    or hide the Display tab.
    """
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.head(SCREEN_PROBE_URL)
            return {"available": r.status_code < 500}
    except httpx.HTTPError as e:
        return {"available": False, "error": str(e)}

_SNAP_NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]{1,64}$")

def _snap_path(name: str) -> Path:
    if not _SNAP_NAME_RE.match(name):
        raise HTTPException(400, "name must match [A-Za-z0-9_- ]{1,64}")
    return Path(SNAPSHOT_DIR) / f"{name}.json"

def _capture_snapshot(now_status: dict) -> dict:
    """Build a snapshot from the current parsed state - what's loaded and
    where. Used for both saving (after parse_state) and as preview data.

    Skips toolheads that have filament physically present but no
    explicit head_source - those land in the snapshot with ace=None /
    slot=None, which would later make apply emit a 'slot is empty'
    error. Without a known source ACE/slot we can't reproduce the
    load anyway, so dropping is the right move."""
    parsed = _parse_state(now_status)
    toolheads = []
    for t in parsed["toolheads"]:
        if not t.get("filament_detected"):
            continue
        ace = t.get("ace")
        slot = t.get("slot")
        if ace is None or slot is None:
            continue
        slot_obj = None
        if ace is not None and 0 <= ace < len(parsed["aces"]):
            slots = parsed["aces"][ace]["slots"]
            if slot is not None and 0 <= slot < len(slots):
                slot_obj = slots[slot]
        toolheads.append({
            "idx":      t["idx"],
            "ace":      ace,
            "slot":     slot,
            "material": (slot_obj or {}).get("material", ""),
            "brand":    (slot_obj or {}).get("brand", ""),
            "color":    (slot_obj or {}).get("color"),
            "color_rgb": (slot_obj or {}).get("color_rgb"),
            "sku":      (slot_obj or {}).get("sku", ""),
        })
    return {"toolheads": toolheads}

@app.get("/api/snapshots")
async def list_snapshots() -> dict:
    d = Path(SNAPSHOT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items.append({
                "name":        p.stem,
                "saved":       data.get("saved"),
                "description": data.get("description"),
                "toolheads":   data.get("toolheads", []),
            })
        except Exception as e:
            items.append({"name": p.stem, "error": str(e)})
    return {"snapshots": items}

@app.post("/api/snapshots")
async def save_snapshot(req: SnapshotSave) -> dict:
    p = _snap_path(req.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"moonraker: {e}")
    snap = _capture_snapshot(status)
    snap["name"] = req.name
    snap["description"] = req.description
    snap["saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(p), "snapshot": snap}

@app.get("/api/snapshots/{name}")
async def get_snapshot(name: str) -> dict:
    p = _snap_path(name)
    if not p.exists():
        raise HTTPException(404, "snapshot not found")
    return json.loads(p.read_text(encoding="utf-8"))

@app.delete("/api/snapshots/{name}")
async def delete_snapshot(name: str) -> dict:
    p = _snap_path(name)
    if not p.exists():
        raise HTTPException(404, "snapshot not found")
    p.unlink()
    return {"ok": True}

@app.post("/api/snapshots/{name}/apply")
async def apply_snapshot(name: str) -> dict:
    """
    Plan a snapshot apply. Computes the ordered command list to bring
    the printer from the current state to the snapshot, but does NOT
    execute. The caller (web frontend) enqueues each step into its
    command queue, so the user sees the full plan as queue chips and
    long-running commands don't time out our HTTP call.
    """
    p = _snap_path(name)
    if not p.exists():
        raise HTTPException(404, "snapshot not found")
    snap = json.loads(p.read_text(encoding="utf-8"))
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"moonraker: {e}")
    cur = _parse_state(status)
    cur_th = {t["idx"]: t for t in cur["toolheads"]}
    desired = {t["idx"]: t for t in snap.get("toolheads", [])}
    cur_aces = cur.get("aces", []) or []
    errors: list[dict] = []
    warnings: list[dict] = []

    for idx, ct in cur_th.items():
        if not ct.get("load_failed"):
            continue
        errors.append({
            "head": idx,
            "ace": ct.get("failed_ace"),
            "slot": ct.get("failed_slot"),
            "kind": "load_failed",
            "message": _load_failed_message(ct),
        })
    if errors:
        return {
            "snapshot": name,
            "actions": [],
            "errors": errors,
            "warnings": warnings,
            "override_proposals": [],
        }

    def _slot_view(ace_i, slot_i):
        if ace_i is None or slot_i is None:
            return None
        if not (0 <= ace_i < len(cur_aces)):
            return None
        slots = cur_aces[ace_i].get("slots") or []
        if not (0 <= slot_i < len(slots)):
            return None
        return slots[slot_i]

    for idx, dt in desired.items():
        ace_i  = dt.get("ace")
        slot_i = dt.get("slot")
        sv = _slot_view(ace_i, slot_i)
        if sv is None or sv.get("raw") == 0 or (sv.get("state") or "").startswith("empty"):
            errors.append({
                "head": idx, "ace": ace_i, "slot": slot_i,
                "kind": "empty",
                "message": (f"T{idx}: ACE {ace_i} / Slot {slot_i} ist leer "
                            f"({(dt.get('material') or '?')} erwartet)"),
            })
            continue

        want_mat = (dt.get("material") or "").strip()
        have_mat = (sv.get("material") or "").strip()
        want_col = (dt.get("color") or "")
        have_col = (sv.get("color") or "")
        want_brand = (dt.get("brand") or "").strip()
        have_brand = (sv.get("brand") or "").strip()
        if want_mat and have_mat and want_mat != have_mat:
            warnings.append({
                "head": idx, "ace": ace_i, "slot": slot_i, "kind": "material",
                "want": want_mat, "have": have_mat,
                "message": (f"T{idx}: Snapshot will {want_mat}, "
                            f"ACE {ace_i} / Slot {slot_i} hat {have_mat or '?'}"),
            })
        elif want_col and have_col and want_col.lower() != have_col.lower():
            warnings.append({
                "head": idx, "ace": ace_i, "slot": slot_i, "kind": "color",
                "want": want_col, "have": have_col,
                "message": (f"T{idx}: Farbabweichung - Snapshot {want_col}, "
                            f"Slot {have_col}"),
            })
        elif want_brand and have_brand and want_brand != have_brand:
            warnings.append({
                "head": idx, "ace": ace_i, "slot": slot_i, "kind": "brand",
                "want": want_brand, "have": have_brand,
                "message": (f"T{idx}: Hersteller-Abweichung - Snapshot {want_brand}, "
                            f"Slot {have_brand}"),
            })

    actions: list[dict] = []

    for idx, ct in cur_th.items():
        if not ct.get("head_source_known"):
            continue
        d = desired.get(idx)
        if (d is None
            or d.get("ace") != ct.get("ace")
            or d.get("slot") != ct.get("slot")):
            actions.append({"name": "ACE_UNLOAD_HEAD", "args": {"HEAD": idx}})

    by_ace: dict[int, list[int]] = {}
    for idx, dt in desired.items():
        ace_idx = dt.get("ace")
        if ace_idx is None:
            continue
        ct = cur_th.get(idx, {})
        if (ct.get("head_source_known")
            and ct.get("ace") == ace_idx
            and ct.get("slot") == dt.get("slot")):
            continue
        by_ace.setdefault(ace_idx, []).append(idx)

    for ace_idx in sorted(by_ace):
        for head in sorted(by_ace[ace_idx]):
            dt = desired.get(head, {})
            if dt.get("slot") is None:
                errors.append({
                    "head": head, "ace": ace_idx, "slot": None,
                    "kind": "missing_slot",
                    "message": (
                        f"T{head}: snapshot target needs explicit ACE slot"),
                })
                continue
            args = {"HEAD": head, "ACE": ace_idx, "SLOT": dt.get("slot")}
            actions.append({"name": "ACE_LOAD_HEAD", "args": args})

    override_proposals: list[dict] = []
    for idx, dt in desired.items():
        ace_i = dt.get("ace")
        slot_i = dt.get("slot")
        if ace_i is None or slot_i is None:
            continue
        material = (dt.get("material") or "").strip()
        color = (dt.get("color") or "").strip()
        if not material and not color:

            continue
        override_proposals.append({
            "ace":      ace_i,
            "slot":     slot_i,
            "material": material,
            "brand":    (dt.get("brand") or "").strip(),
            "subtype":  (dt.get("sku") or "").strip(),
            "color":    color,
        })

    return {
        "snapshot": name,
        "actions": actions,
        "errors":   errors,
        "warnings": warnings,
        "override_proposals": override_proposals,
    }

_slot_overrides: dict[str, dict] = {}
_native_overrides: dict[str, dict] = {}
_last_head_source: dict[int, tuple[int, int] | None] = {}

_overrides_mtime: float = 0.0
_native_overrides_mtime: float = 0.0

def _override_key(ace: int, slot: int) -> str:
    return f"{int(ace)}_{int(slot)}"

def _native_override_key(head: int) -> str:
    return str(int(head))

def _reload_overrides_if_changed() -> None:
    """Cheap mtime check; reloads only when the file has been touched
    since we last read it (e.g. by ace.py picking up a display edit)."""
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    if not p.exists():
        if _slot_overrides:
            _slot_overrides.clear()
        _overrides_mtime = 0.0
        return
    try:
        m = p.stat().st_mtime
    except OSError:
        return
    if m == _overrides_mtime:
        return
    _load_overrides_from_disk()
    _overrides_mtime = m

def _load_overrides_from_disk() -> None:
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _slot_overrides.clear()
            _slot_overrides.update(data)
        try:
            _overrides_mtime = p.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass

def _reload_native_overrides_if_changed() -> None:
    global _native_overrides_mtime
    p = Path(NATIVE_OVERRIDE_FILE)
    if not p.exists():
        if _native_overrides:
            _native_overrides.clear()
        _native_overrides_mtime = 0.0
        return
    try:
        m = p.stat().st_mtime
    except OSError:
        return
    if m == _native_overrides_mtime:
        return
    _load_native_overrides_from_disk()
    _native_overrides_mtime = m

def _load_native_overrides_from_disk() -> None:
    global _native_overrides_mtime
    p = Path(NATIVE_OVERRIDE_FILE)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _native_overrides.clear()
            _native_overrides.update(data)
        try:
            _native_overrides_mtime = p.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass

def _save_native_overrides_to_disk() -> None:
    global _native_overrides_mtime
    p = Path(NATIVE_OVERRIDE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(_native_overrides, indent=2), encoding="utf-8")
        try:
            st = p.parent.stat()
            os.chown(str(tmp), st.st_uid, st.st_gid)
        except OSError:
            pass
        try:
            os.chmod(str(tmp), 0o644)
        except OSError:
            pass
        os.replace(str(tmp), str(p))
        try:
            os.chmod(str(p), 0o644)
        except OSError:
            pass
        try:
            _native_overrides_mtime = p.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass

def _save_overrides_to_disk() -> None:
    """Atomic write: render to a sibling .tmp file then os.replace,
    so concurrent readers (= ace.py reverse-sync, mtime poller) never
    see a half-written file."""
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(_slot_overrides, indent=2), encoding="utf-8")
        try:
            st = p.parent.stat()
            os.chown(str(tmp), st.st_uid, st.st_gid)
        except OSError:
            pass
        try:
            os.chmod(str(tmp), 0o644)
        except OSError:
            pass
        os.replace(str(tmp), str(p))
        try:
            os.chmod(str(p), 0o644)
        except OSError:
            pass
        try:
            _overrides_mtime = p.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass

def _drop_override_if_present(ace: int, slot: int) -> bool:
    """Remove any manual slot override for (ace, slot). Returns True
    when an entry was popped so the caller can batch the file write
    across multiple drops in the same poll. Used both on
    toolhead-unload bookkeeping and on physical eject from the ACE
    slot (gate_status == 0)."""
    key = _override_key(ace, slot)
    if key in _slot_overrides:
        old = _slot_overrides.pop(key, None)
        _trace.info("override DROP gate==0 ACE %d / slot %d (was %s)", ace, slot, old)
        return True
    return False

EJECT_DEBOUNCE_S = 0.5
_eject_pending_since: dict[tuple[int, int], float] = {}

def _override_for(ace: int, slot: int) -> dict | None:
    """Return the override dict for this (ace, slot) if any meaningful
    fields are set, else None."""
    o = _slot_overrides.get(_override_key(ace, slot))
    if not o:
        return None
    mat = (o.get("material") or "").strip()
    col = (o.get("color") or "").strip()
    if not mat and not col:
        return None
    return o

def _track_unload_clears(head_source: dict) -> None:
    """Compare current head_source against last seen state. When a
    toolhead transitions from "loaded from (a,s)" to None, clear that
    (a,s)'s override."""
    changed = False
    for t in range(4):
        cur = head_source.get(str(t)) or head_source.get(t)
        if _head_source_load_failed(cur):
            d, sl = (None, None)
        else:
            d, sl = _resolve_head_source(cur)
        prev = _last_head_source.get(t)
        if prev is not None and (d, sl) != prev and d is None and sl is None:

            key = _override_key(prev[0], prev[1])
            if key in _slot_overrides:
                old = _slot_overrides.pop(key, None)
                _trace.info("override DROP unload T%d (was loaded from ACE %d / slot %d): %s",
                            t, prev[0], prev[1], old)
                changed = True
        _last_head_source[t] = (d, sl) if (d is not None and sl is not None) else None
    if changed:
        _save_overrides_to_disk()

@app.get("/api/slot-override")
async def list_slot_overrides() -> dict:
    return {"overrides": _slot_overrides}

@app.get("/api/native-override")
async def list_native_overrides() -> dict:
    _reload_native_overrides_if_changed()
    return {"overrides": _native_overrides}

@app.post("/api/native-override")
async def set_native_override(req: NativeOverride) -> dict:
    if req.head < 0 or req.head > 3:
        raise HTTPException(status_code=400, detail="head must be 0..3")
    key = _native_override_key(req.head)
    new = {
        "head":     req.head,
        "material": req.material or "",
        "brand":    req.brand or "",
        "subtype":  req.subtype or "",
        "color":    req.color or "",
    }
    old = _native_overrides.get(key)
    _native_overrides[key] = new
    _trace.info("native override SET via picker POST T%d: %s -> %s",
                req.head, old, new)
    _save_native_overrides_to_disk()
    return {"ok": True, "key": key, "override": _native_overrides[key]}

@app.delete("/api/native-override/{head}")
async def delete_native_override(head: int) -> dict:
    if head < 0 or head > 3:
        raise HTTPException(status_code=400, detail="head must be 0..3")
    key = _native_override_key(head)
    if key in _native_overrides:
        old = _native_overrides.pop(key, None)
        _trace.info("native override DROP via picker DELETE T%d (was %s)",
                    head, old)
        _save_native_overrides_to_disk()
    return {"ok": True}

@app.post("/api/slot-override")
async def set_slot_override(req: SlotOverride) -> dict:
    key = _override_key(req.ace, req.slot)
    new = {
        "ace":      req.ace,
        "slot":     req.slot,
        "material": req.material or "",
        "brand":    req.brand or "",
        "subtype":  req.subtype or "",
        "color":    req.color or "",
    }
    old = _slot_overrides.get(key)
    _slot_overrides[key] = new
    _trace.info("override SET via picker POST ACE %d / slot %d: %s -> %s",
                req.ace, req.slot, old, new)
    _save_overrides_to_disk()
    return {"ok": True, "key": key, "override": _slot_overrides[key]}

@app.delete("/api/slot-override/{ace}/{slot}")
async def delete_slot_override(ace: int, slot: int) -> dict:
    key = _override_key(ace, slot)
    if key in _slot_overrides:
        old = _slot_overrides.pop(key, None)
        _trace.info("override DROP via picker DELETE ACE %d / slot %d (was %s)",
                    ace, slot, old)
        _save_overrides_to_disk()
    return {"ok": True}

_load_overrides_from_disk()
_load_native_overrides_from_disk()

_notifications: deque = deque(maxlen=50)
_next_notification_id = int(time.time() * 1000)
_notifications_lock = asyncio.Lock()
_notification_cutoff_ts = 0.0

_NOTIF_ONLY_MULTIACE = os.environ.get(
    "MULTIACE_NOTIF_ONLY_MULTIACE", "1") in ("1", "true", "yes")

def _is_error_gcode_response(text: str) -> bool:
    """Filter for gcode_response strings that should surface as a
    notification. The ace.py module pumps a lot of plain status
    messages through respond_raw (= log_always); only log_error
    prepends '!!' so we can tell them apart by the prefix.

    Default mode (MULTIACE_NOTIF_ONLY_MULTIACE=1): require BOTH a
    '[multiACE]' tag AND an error marker (!!, Error:, aborting).
    Off (=0): catch any error-shaped Klipper response."""
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    body = s[3:].strip() if s.startswith("// ") else s
    is_error = (
        body.startswith("!!")
        or "Error:" in body
        or body.lower().startswith("aborting")
    )
    if _NOTIF_ONLY_MULTIACE:
        return is_error and "[multiACE]" in s
    if is_error:
        return True
    if body.lower().startswith("unknown command"):
        return True
    return False

def _notification_cutoff_from_status(status: dict) -> float:
    ps = (status or {}).get("print_stats", {}) or {}
    ps_state = (ps.get("state") or "").lower()
    if ps_state not in ("printing", "paused"):
        return 0.0
    if ps.get("exception") or ps.get("message"):
        return 0.0
    try:
        total_duration = float(ps.get("total_duration") or 0.0)
    except (TypeError, ValueError):
        total_duration = 0.0
    if total_duration <= 0:
        return 0.0
    return max(0.0, time.time() - total_duration - 5.0)

async def _prune_notifications_before(cutoff_ts: float) -> int:
    global _notification_cutoff_ts
    if cutoff_ts <= 0:
        return 0
    _notification_cutoff_ts = max(_notification_cutoff_ts, cutoff_ts)
    async with _notifications_lock:
        before = len(_notifications)
        keep = [n for n in _notifications
                if float(n.get("ts") or 0.0) >= _notification_cutoff_ts]
        _notifications.clear()
        _notifications.extend(keep)
        pruned = before - len(_notifications)
    if pruned:
        _trace.info("pruned %d stale notification(s) before %.3f",
                    pruned, _notification_cutoff_ts)
    return pruned

def _record_notification(text: str) -> dict | None:
    global _next_notification_id
    if not _is_error_gcode_response(text):
        return None
    _next_notification_id += 1
    msg = text.strip()

    for prefix in ("// !! ", "// Error:", "// ", "!! ", "!!", "Error:"):
        if msg.startswith(prefix):
            msg = msg[len(prefix):].strip()
            break

    if msg.startswith("[multiACE] "):
        msg = msg[len("[multiACE] "):].strip()
    elif msg.startswith("[multiACE]"):
        msg = msg[len("[multiACE]"):].strip()
    note = {
        "id":    _next_notification_id,
        "ts":    time.time(),
        "msg":   msg,
        "raw":   text.strip(),
        "level": "error",
    }
    _notifications.append(note)
    _trace.info("notification %d captured: %s", note["id"], note["msg"])
    return note

async def _moonraker_log_listener() -> None:
    """Background task that follows Moonraker's gcode_response stream
    via websocket and records error-level lines as notifications.
    Reconnects with backoff on any failure."""
    url = MOONRAKER_URL.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/websocket"
    backoff = 1.0
    debug_recv = os.environ.get("MULTIACE_WS_DEBUG", "0") in ("1", "true", "yes")
    while True:
        try:
            _trace.info("moonraker WS connecting to %s ...", url)

            async with websockets.connect(url, ping_interval=None, close_timeout=5) as ws:
                _trace.info("moonraker WS connected")

                try:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "server.connection.identify",
                        "params": {
                            "client_name": "multiace_web",
                            "version": VERSION,
                            "type": "agent",
                            "url": "https://github.com/decay71/multiACE",
                        },
                        "id": 1,
                    }))
                    _trace.info("moonraker WS identify sent")
                except Exception as ie:
                    _trace.warning("moonraker WS identify failed: %s", ie)
                backoff = 1.0
                msg_count = 0
                async for raw in ws:
                    msg_count += 1

                    if debug_recv:
                        _trace.warning("moonraker WS recv #%d: %s", msg_count, str(raw)[:240])
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    method = msg.get("method")
                    if method != "notify_gcode_response":
                        continue
                    params = msg.get("params") or []
                    if not params:
                        continue
                    text = params[0]
                    rec = _record_notification(text)
                    if rec is not None:
                        _trace.warning("Klipper error captured: %s", rec["msg"])
                _trace.info("moonraker WS loop ended after %d messages", msg_count)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _trace.warning("moonraker WS error: %s; reconnect in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
        else:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

@app.on_event("startup")
async def _start_log_listener() -> None:
    asyncio.create_task(_moonraker_log_listener())

@app.get("/api/notifications")
async def list_notifications() -> dict:
    cutoff_ts = 0.0
    try:
        cutoff_ts = _notification_cutoff_from_status(await _query_state())
        await _prune_notifications_before(cutoff_ts)
    except Exception as e:
        _trace.info("notification stale-prune skipped: %s", e)
    return {
        "notifications": list(_notifications),
        "cutoff_ts": max(_notification_cutoff_ts, cutoff_ts),
    }

@app.post("/api/notifications/test")
async def test_notification(payload: dict | None = None) -> dict:
    """Inject a fake Klipper-error notification - useful for verifying
    the WS bridge from the printer command line:
        curl -X POST http://127.0.0.1:7126/api/notifications/test
    """
    msg = (payload or {}).get("msg") if payload else None
    text = "!! " + (msg or "Test notification from /api/notifications/test")
    rec = _record_notification(text)
    return {"ok": rec is not None, "notification": rec}

@app.delete("/api/notifications/{nid}")
async def dismiss_notification(nid: int) -> dict:
    async with _notifications_lock:
        before = len(_notifications)
        keep = [n for n in _notifications if n["id"] != nid]
        _notifications.clear()
        _notifications.extend(keep)
    return {"ok": True, "dismissed": before - len(_notifications)}

@app.delete("/api/notifications")
async def clear_notifications() -> dict:
    async with _notifications_lock:
        n = len(_notifications)
        _notifications.clear()
    return {"ok": True, "cleared": n}

def _parse_port_range(spec: str) -> list[int]:
    out: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo <= hi:
                out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                continue
    return out

_PLUGIN_PORTS = _parse_port_range(PLUGIN_PORT_RANGE)
_plugin_cache: dict = {"ts": 0.0, "items": []}
_plugin_lock = asyncio.Lock()

async def _probe_plugin(client: httpx.AsyncClient, port: int) -> dict | None:
    base = f"http://127.0.0.1:{port}"
    try:
        r = await client.get(f"{base}/integration-manifest", timeout=0.4)
        if r.status_code != 200:
            return None
        m = r.json()
    except Exception:
        return None
    name = str(m.get("name") or "").strip()
    if not name or not re.match(r"^[A-Za-z0-9_.-]+$", name):
        return None
    return {
        "name":     name,
        "label":    str(m.get("label") or name),
        "version":  str(m.get("version") or ""),
        "tabs":     list(m.get("tabs") or []),
        "ui_url":   str(m.get("ui_url") or "/"),
        "port":     port,
        "base_url": f"/plugin/{name}",
    }

async def _discover_plugins(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and (now - _plugin_cache["ts"]) < PLUGIN_DISCOVERY_TTL:
        return _plugin_cache["items"]
    async with _plugin_lock:
        if not force and (time.time() - _plugin_cache["ts"]) < PLUGIN_DISCOVERY_TTL:
            return _plugin_cache["items"]
        items: list[dict] = []
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *(_probe_plugin(client, p) for p in _PLUGIN_PORTS),
                return_exceptions=True,
            )
        seen: set[str] = set()
        for res in results:
            if isinstance(res, dict) and res["name"] not in seen:
                seen.add(res["name"])
                items.append(res)
        _plugin_cache["ts"] = time.time()
        _plugin_cache["items"] = items
        return items

@app.get("/api/integrations")
async def list_integrations(refresh: bool = False) -> dict:
    items = await _discover_plugins(force=refresh)
    return {"plugins": items, "ports": _PLUGIN_PORTS}

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

async def _plugin_proxy_target(name: str) -> str:
    for p in await _discover_plugins():
        if p["name"] == name:
            return f"http://127.0.0.1:{p['port']}"
    raise HTTPException(status_code=404, detail=f"plugin '{name}' not registered")

@app.api_route(
    "/plugin/{name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def plugin_proxy(name: str, path: str, request: Request) -> Response:
    target_base = await _plugin_proxy_target(name)
    url = f"{target_base}/{path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP}
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.request(
                request.method, url,
                params=request.query_params,
                headers=headers,
                content=body,
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"plugin proxy: {e}")
    out_headers = {k: v for k, v in r.headers.items()
                   if k.lower() not in _HOP_BY_HOP}
    return Response(content=r.content, status_code=r.status_code,
                    headers=out_headers, media_type=r.headers.get("content-type"))

class _PluginGcode(BaseModel):
    script: str

@app.get("/api/plugin-api/state")
async def plugin_api_state() -> dict:
    """Aggregated host state - same shape as /api/state."""
    return await get_state()

@app.get("/api/plugin-api/aces")
async def plugin_api_aces() -> dict:
    """ACE list - same shape as /api/aces."""
    return await list_aces()

@app.post("/api/plugin-api/gcode")
async def plugin_api_gcode(req: _PluginGcode) -> dict:
    """Run a gcode script on the printer. Pass-through to Moonraker
    /printer/gcode/script - Moonraker enforces the print-state rules
    (busy / paused / printing) on its end."""
    script = (req.script or "").strip()
    if not script:
        raise HTTPException(status_code=400, detail="empty script")
    _validate_gcode_script(script)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{MOONRAKER_URL}/printer/gcode/script",
                json={"script": script},
            )
            r.raise_for_status()
            return {"ok": True, "moonraker": r.json()}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code,
                            detail=f"moonraker: {e.response.text}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")

@app.get("/api/materials")
async def get_materials() -> dict:
    """Return the user-editable filament material list. Seeds the file from
    DEFAULT_MATERIALS on first access if it doesn't exist, so it works no
    matter how multiACE was installed."""
    p = Path(MATERIALS_FILE)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            mats = data.get("materials") if isinstance(data, dict) else data
            if isinstance(mats, list) and mats:
                return {"materials": [str(m) for m in mats]}
        else:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"materials": DEFAULT_MATERIALS},
                                        indent=2, ensure_ascii=False),
                             encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass
    return {"materials": DEFAULT_MATERIALS}

@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """
    Push channel for live updates. v1: simple ping every 5s plus a
    periodic ACE snapshot every 1s. Clients can rely on this for
    dashboard liveness without polling REST themselves.
    """
    await websocket.accept()
    last_seen_notif_id = 0
    try:
        last_ts = 0.0
        while True:
            now = time.time()

            if now - last_ts >= 1.0 and not _homing_active():
                try:
                    status = await _query_state()
                    cutoff_ts = _notification_cutoff_from_status(status)
                    await _prune_notifications_before(cutoff_ts)
                    payload = _parse_state(status)
                    payload["type"] = "state"
                    payload["ts"] = now
                    payload["notification_cutoff_ts"] = max(
                        _notification_cutoff_ts, cutoff_ts)
                    await websocket.send_json(payload)
                except Exception as e:
                    await websocket.send_json({"type": "error", "ts": now, "error": str(e)})
                last_ts = now

            for n in list(_notifications):
                if n["id"] > last_seen_notif_id:
                    try:
                        await websocket.send_json({
                            "type":       "gcode_error",
                            "ts":         n["ts"],
                            "id":         n["id"],
                            "msg":        n["msg"],
                            "raw":        n["raw"],
                            "level":      n["level"],
                        })
                    except Exception:
                        return
                    last_seen_notif_id = n["id"]
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return
    except Exception:
        return

if Path(FRONTEND_DIR).is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
