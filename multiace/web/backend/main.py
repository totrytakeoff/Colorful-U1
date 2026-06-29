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
import errno
import json
import logging
import os
import re
import shutil
import sys
import threading
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
MULTIACE_HEAD_SOURCE_STATE_PATH = os.environ.get(
    "MULTIACE_HEAD_SOURCE_STATE_PATH",
    "/home/lava/printer_data/config/extended/multiace/head_source_state.json",
)
MULTIACE_PREFLIGHT_HISTORY_FILE = os.environ.get(
    "MULTIACE_PREFLIGHT_HISTORY_FILE",
    "/home/lava/printer_data/config/extended/multiace/preflight_history.json",
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
    "toolhead",
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

def _toolhead_load_failed_active(t: dict) -> bool:
    if not isinstance(t, dict) or not bool(t.get("load_failed")):
        return False
    if bool(t.get("filament_at_extruder")):
        return True
    if bool(t.get("filament_in_toolhead")):
        return True
    channel_error = t.get("channel_error")
    channel_state = str(t.get("channel_state") or "")
    if channel_error not in (None, "", "ok"):
        return True
    if channel_state and channel_state not in ("wait_insert", "inited", "test"):
        return not (channel_state.endswith("_finish")
                    or channel_state.endswith("_fail"))
    return False

def _load_failed_toolheads(parsed: dict) -> list[dict]:
    return [
        t for t in (parsed.get("toolheads") or [])
        if _toolhead_load_failed_active(t)
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

def _parse_state(status: dict, source_graph_overlay: bool = False) -> dict:
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
            native_path_loaded = (
                bool(feed.get("filament_detected"))
                and not bool(feed.get("filament_in_ace"))
            )
            meta = (
                (_native_override_at(t) if mode == "native" or native_path_loaded else None)
                or _ptc_at(t)
            )
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
    parsed = {
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
        "toolhead":           status.get("toolhead", {}) or {},
        "wiring":             wiring,
        "save_variables":     sv_vars,
    }
    if source_graph_overlay:
        _apply_source_graph_toolhead_overlay(parsed)
    return parsed

def _apply_source_graph_toolhead_overlay(parsed: dict) -> None:
    """Make dashboard toolhead state follow the source graph, not legacy route.

    Klipper still exposes ace.route.head_modes for older screens and saved
    configs.  The Colorful-U1 runtime source-of-truth is the source graph plus
    the runtime source-state attribution, so the dashboard must not display a
    native-loaded head as ACE just because a stale legacy mode says so.
    """
    try:
        graph, _meta = _load_source_graph(parsed)
        state = _source_state_for_parsed(graph, parsed)
        sources = sg.runtime_sources(graph, parsed)
    except Exception:
        return
    topology: dict[str, dict[str, Any]] = {}
    for head_id in (graph.get("heads") or {}).keys():
        topology[str(head_id)] = {"sources": [], "kinds": set()}
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or edge.get("enabled", True) is False:
            continue
        head_id = str(edge.get("head") or "")
        source_id = str(edge.get("source") or "")
        source = (graph.get("sources") or {}).get(source_id) or {}
        entry = topology.setdefault(head_id, {"sources": [], "kinds": set()})
        entry["sources"].append(source_id)
        if source.get("kind"):
            entry["kinds"].add(source.get("kind"))
    legacy_route = parsed.get("route") if isinstance(parsed.get("route"), dict) else {}
    parsed["legacy_route"] = legacy_route
    parsed["route"] = {
        "mode": "source_graph",
        "source_graph_hash": sg.graph_hash(graph),
        "heads": {
            head_id: {
                "sources": list(entry.get("sources") or []),
                "source_kinds": sorted(entry.get("kinds") or []),
            }
            for head_id, entry in sorted(topology.items())
        },
    }
    heads_state = state.get("heads") or {}
    for toolhead in parsed.get("toolheads", []) or []:
        try:
            idx = int(toolhead.get("idx"))
        except (TypeError, ValueError):
            continue
        head_id = f"head:{idx}"
        runtime = heads_state.get(head_id) or {}
        source_id = runtime.get("current_source")
        source = (graph.get("sources") or {}).get(source_id or "") or {}
        toolhead["current_source"] = source_id
        toolhead["source_confidence"] = (
            runtime.get("source_confidence") or "unknown")
        toolhead["source_ready"] = runtime.get("source_ready")
        toolhead["source_ready_reason"] = runtime.get("source_ready_reason") or ""
        toolhead["route_managed"] = bool(
            (topology.get(head_id) or {}).get("sources"))
        if source_id:
            kind = source.get("kind")
            toolhead["mode"] = "native" if kind == "native_feeder" else "ace"
            if kind == "ace_slot":
                try:
                    toolhead["ace"] = int(source.get("ace"))
                    toolhead["slot"] = int(source.get("slot"))
                except (TypeError, ValueError):
                    toolhead["ace"] = None
                    toolhead["slot"] = None
                toolhead["head_source_known"] = True
            elif kind == "native_feeder":
                toolhead["ace"] = None
                toolhead["slot"] = None
                toolhead["head_source_known"] = False
        else:
            kinds = (topology.get(head_id) or {}).get("kinds") or set()
            toolhead["mode"] = (
                "mixed" if len(kinds) > 1
                else "ace" if "ace_slot" in kinds
                else "native")
            toolhead["ace"] = None
            toolhead["slot"] = None
            toolhead["head_source_known"] = False
        if source_id:
            runtime_source = sources.get(source_id) or {}
            color = runtime_source.get("color")
            material = runtime_source.get("material")
            brand = runtime_source.get("brand")
            subtype = runtime_source.get("subtype")
            source = (graph.get("sources") or {}).get(source_id or "") or {}
            if source.get("kind") == "native_feeder":
                native_head = source.get("head")
                try:
                    native_head = int(native_head)
                except (TypeError, ValueError):
                    native_head = idx
                meta = _native_override_meta_at(native_head)
                if meta:
                    color = color or meta.get("color")
                    material = material or meta.get("material")
                    brand = brand or meta.get("brand")
                    subtype = subtype or meta.get("sku")
            if color:
                toolhead["color"] = color
            if material:
                toolhead["material"] = material
            if brand:
                toolhead["brand"] = brand
            if subtype:
                toolhead["sku"] = subtype
        elif not bool(toolhead.get("filament_at_extruder")):
            toolhead["color"] = None
            toolhead["material"] = ""
            toolhead["brand"] = ""
            toolhead["sku"] = ""

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

class HeadLoadRequest(BaseModel):
    head: str | int
    source: str
    execute: bool = True

class HeadUnloadRequest(BaseModel):
    head: str | int
    execute: bool = True

class HeadRecoverRequest(BaseModel):
    head: str | int
    execute: bool = True

class SourceFullUnloadRequest(BaseModel):
    source: str
    execute: bool = True

class AceDryStartRequest(BaseModel):
    ace: int
    temp: int | float | None = None
    duration: int | float | None = None
    execute: bool = True

class AceDryStopRequest(BaseModel):
    ace: int | None = None
    execute: bool = True

class OperationExecuteRequest(BaseModel):
    execute: bool = True

class RoutePlanValidateRequest(BaseModel):
    route_plan: dict[str, Any]

EXPLICIT_ROUTE_MACROS = {"ACE_LOAD_HEAD", "ACE_SWAP_HEAD"}
EXPLICIT_ROUTE_ARGS = {"HEAD", "ACE", "SLOT"}
OBSOLETE_MACROS_BLOCKED = {"SET_ACE_MODE", "ACE_RUN_MODE_SWITCH"}
EXPLICIT_PLAN_MACROS = {"ACE_TEST", "ACE_SEQ", "ACE_PRELOAD"}
OPERATION_ONLY_MACROS = {
    "ACE_LOAD_HEAD",
    "ACE_UNLOAD_HEAD",
    "ACE_SWAP_HEAD",
    "ACE_UNLOAD_ALL_HEADS",
    "ACE_STOP_TRANSPORT",
    "ACE_DRY",
    "ACE_STOP_DRYING",
    "FEED_AUTO",
    "FEED_AUTO_RETRACT",
    "FEED_AUTO_FULL_UNLOAD",
}

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

def _validate_feed_slot_motion_args(
        macro: str,
        args: dict[str, Any] | None,
) -> None:
    a = {str(k).upper(): v for k, v in (args or {}).items()}
    allowed = {
        "MODULE", "CHANNEL", "EXTRUDER", "LENGTH", "SPEED",
        "SYNC_LENGTH", "SYNC_SPEED",
    }
    extra = sorted(set(a.keys()) - allowed)
    if extra:
        raise HTTPException(
            status_code=400,
            detail=f"{macro} rejects unsupported argument(s): {', '.join(extra)}")
    for key in ("MODULE", "CHANNEL", "EXTRUDER", "LENGTH"):
        if key not in a:
            raise HTTPException(status_code=400, detail=f"{macro} requires {key}")
    module = str(a.get("MODULE") or "").lower()
    if module not in ("left", "right"):
        raise HTTPException(status_code=400, detail=f"{macro} MODULE must be left or right")
    try:
        channel = int(a.get("CHANNEL"))
        extruder = int(a.get("EXTRUDER"))
        length = float(a.get("LENGTH"))
        speed = float(a.get("SPEED", 25))
        sync_length = float(a.get("SYNC_LENGTH", 0))
        sync_speed = float(a.get("SYNC_SPEED", 10))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{macro} CHANNEL/EXTRUDER/LENGTH/SPEED/SYNC_LENGTH/"
                "SYNC_SPEED must be numeric"))
    if channel not in (0, 1) or extruder not in (0, 1, 2, 3):
        raise HTTPException(
            status_code=400,
            detail=f"{macro} CHANNEL must be 0..1 and EXTRUDER must be 0..3")
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
                f"{macro} module/channel does not match EXTRUDER; "
                f"T{extruder} expects MODULE={expected[0]} CHANNEL={expected[1]}"))
    if length < 0 or length > 3000:
        raise HTTPException(status_code=400, detail=f"{macro} LENGTH must be 0..3000")
    if speed <= 0 or speed > 120:
        raise HTTPException(status_code=400, detail=f"{macro} SPEED must be >0..120")
    if sync_length < 0 or sync_length > 3000:
        raise HTTPException(
            status_code=400,
            detail=f"{macro} SYNC_LENGTH must be 0..3000")
    if sync_speed <= 0 or sync_speed > 120:
        raise HTTPException(
            status_code=400,
            detail=f"{macro} SYNC_SPEED must be >0..120")

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
    if macro in ("FEED_AUTO_RETRACT", "FEED_AUTO_FULL_UNLOAD"):
        _validate_feed_slot_motion_args(macro, args)

def _validate_direct_macro_request(name: str, args: dict[str, Any] | None) -> None:
    _validate_macro_request(name, args)
    macro = str(name or "").strip().upper()
    if macro in OPERATION_ONLY_MACROS or macro in EXPLICIT_PLAN_MACROS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{macro} must be executed through /api/operation/* "
                "so source state, route validation, and the single-operation "
                "lock are enforced"))

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
        if macro == "FEED_AUTO":
            try:
                _validate_feed_auto_args(gcode_args)
            except HTTPException as e:
                raise HTTPException(
                    status_code=e.status_code,
                    detail=f"line {lineno}: {e.detail}")
        if macro in ("FEED_AUTO_RETRACT", "FEED_AUTO_FULL_UNLOAD"):
            try:
                _validate_feed_slot_motion_args(macro, gcode_args)
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

async def _mr_gcode(command: str, timeout: float | None = None) -> dict:
    return await _mr_post(
        "/printer/gcode/script",
        {"script": command},
        timeout=timeout,
    )

def _homed_axes_from_parsed(parsed: dict) -> set[str]:
    raw = ((parsed or {}).get("toolhead") or {}).get("homed_axes")
    if isinstance(raw, str):
        return {ch.lower() for ch in raw if ch.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(ch).lower() for ch in raw}
    return set()

def _requires_ace_motion_homed_axes(route_plan: dict) -> bool:
    for command in route_plan.get("commands") or []:
        macro = str(command or "").strip().split(None, 1)[0].upper()
        if macro in ("ACE_LOAD_HEAD", "ACE_SWAP_HEAD"):
            return True
    return False

def _route_plan_includes_home_axes(route_plan: dict) -> bool:
    for event in route_plan.get("events") or []:
        if not isinstance(event, dict):
            continue
        for step in event.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("kind") != "home_axes":
                continue
            if str(step.get("command") or "").strip().upper() == "G28":
                return True
    return False

def _printer_idle_for_auto_home(parsed: dict) -> bool:
    state = str((parsed or {}).get("printer_state") or "").lower()
    return state in ("", "idle", "standby", "ready")

def _validate_operation_printer_motion_ready(
        route_plan: dict,
        parsed: dict,
) -> list[str]:
    if not _requires_ace_motion_homed_axes(route_plan):
        return []
    homed = _homed_axes_from_parsed(parsed)
    missing = [axis for axis in ("x", "y", "z") if axis not in homed]
    if not missing:
        return []
    if _route_plan_includes_home_axes(route_plan):
        if _printer_idle_for_auto_home(parsed):
            return []
        return [
            "ACE load/swap requires homed axes, but printer state is %s. "
            "Automatic homing is only allowed while idle."
            % ((parsed or {}).get("printer_state") or "unknown")
        ]
    return [
        "ACE load/swap requires homed axes before moving the saved toolhead "
        "position. Home %s first, then retry."
        % "".join(axis.upper() for axis in missing)
    ]

def _auto_home_step_if_needed(route_plan: dict, parsed: dict) -> dict | None:
    if not _requires_ace_motion_homed_axes(route_plan):
        return None
    missing = [axis for axis in ("x", "y", "z")
               if axis not in _homed_axes_from_parsed(parsed)]
    if not missing:
        return None
    if not _printer_idle_for_auto_home(parsed):
        raise HTTPException(
            status_code=409,
            detail=(
                "ACE load/swap requires homed axes, but printer state is %s. "
                "Automatic homing is only allowed while idle."
                % ((parsed or {}).get("printer_state") or "unknown")
            ),
        )
    return {
        "kind": "home_axes",
        "command": "G28",
        "axes": "".join(axis.upper() for axis in missing),
        "reason": "ace_load_swap_requires_homed_axes",
    }

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
_PREFLIGHT_MAX_CACHE_SIZE = int(os.environ.get(
    "MULTIACE_PREFLIGHT_CACHE_MAX_MB", "260")) * 1024 * 1024
_PREFLIGHT_MIN_FREE_SIZE = int(os.environ.get(
    "MULTIACE_PREFLIGHT_MIN_FREE_MB", "160")) * 1024 * 1024
_PREFLIGHT_HISTORY_MAX_ITEMS = int(os.environ.get(
    "MULTIACE_PREFLIGHT_HISTORY_MAX_ITEMS", "80"))
_PREFLIGHT_HISTORY_LOCK = threading.Lock()

_pp_module = None
_pp_module_src: str | None = None
_pp_module_mtime: float | None = None

def _load_post_processor():
    """Lazy-load the post-processor as a Python module so its parsing
    and remap helpers can be reused server-side without a subprocess."""
    global _pp_module, _pp_module_src, _pp_module_mtime
    candidates = [
        Path("/home/lava/printer_data/config/tools/post_process_virtual_toolheads.py"),
        Path(__file__).resolve().parent.parent.parent / "tools" / "post_process_virtual_toolheads.py",
    ]
    src = next((p for p in candidates if p.is_file()), None)
    if src is None:
        raise HTTPException(status_code=503,
                            detail="post-processor script not installed")
    try:
        src_mtime = src.stat().st_mtime
    except OSError:
        src_mtime = None
    src_key = str(src)
    if (_pp_module is not None
            and _pp_module_src == src_key
            and _pp_module_mtime == src_mtime):
        return _pp_module
    import importlib.util
    spec = importlib.util.spec_from_file_location("multiace_postprocess", src)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"post-processor failed to load: {exc}")
    _pp_module = mod
    _pp_module_src = src_key
    _pp_module_mtime = src_mtime
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
    _prune_preflight_cache()

def _preflight_token_files() -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    if not _PREFLIGHT_DIR.is_dir():
        return groups
    for p in _PREFLIGHT_DIR.iterdir():
        if not p.is_file():
            continue
        token = p.name.split(".", 1)[0]
        if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
            continue
        groups.setdefault(token, []).append(p)
    return groups

def _preflight_cache_bytes(groups: dict[str, list[Path]] | None = None) -> int:
    total = 0
    for files in (groups or _preflight_token_files()).values():
        for p in files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total

def _preflight_report_path(token: str) -> Path:
    return _PREFLIGHT_DIR / (token + ".report.json")

def _save_preflight_report(report: dict) -> None:
    token = str((report or {}).get("token") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return
    _preflight_report_path(token).write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )

def _load_preflight_report(token: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return {}
    p = _preflight_report_path(token)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def _preflight_token_available(token: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return False
    return (
        (_PREFLIGHT_DIR / (token + ".gcode")).is_file()
        and _preflight_report_path(token).is_file()
    )

def _preflight_name_for_token(token: str) -> str:
    p = _PREFLIGHT_DIR / (token + ".name")
    if not p.is_file():
        return token + ".gcode"
    try:
        return p.read_text(encoding="utf-8").strip() or (token + ".gcode")
    except Exception:
        return token + ".gcode"

def _preflight_history_path() -> Path:
    return Path(MULTIACE_PREFLIGHT_HISTORY_FILE)

def _read_preflight_history_unlocked() -> dict:
    path = _preflight_history_path()
    if not path.is_file():
        return {"version": 1, "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "entries": []}
    if not isinstance(data, dict):
        return {"version": 1, "entries": []}
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    return {"version": 1, "entries": entries}

def _write_preflight_history_unlocked(history: dict) -> None:
    entries = history.get("entries")
    if not isinstance(entries, list):
        entries = []
    path = _preflight_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({
            "version": 1,
            "entries": entries[:_PREFLIGHT_HISTORY_MAX_ITEMS],
            "updated_at": time.time(),
        }, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)

def _history_entry_summary(report: dict) -> dict:
    route_plan = (report or {}).get("route_plan") or {}
    source_map = (report or {}).get("source_map") or {}
    stats = route_plan.get("stats") or source_map.get("swap_stats") or {}
    return {
        "used_tools": route_plan.get("used_tools")
            or sorted([int(i.get("t")) for i in (report.get("slicer_colors") or [])
                       if isinstance(i, dict) and str(i.get("t", "")).isdigit()]),
        "route_events": len(route_plan.get("events") or []),
        "resolve_errors": len(report.get("resolve_errors") or []),
        "active_ace_swaps": stats.get("active_ace_swaps", 0),
        "estimated_swap_seconds_min": stats.get("estimated_swap_seconds_min", 0),
        "estimated_swap_seconds_max": stats.get("estimated_swap_seconds_max", 0),
    }

def _record_preflight_history(report: dict, *,
                              client_mtime: int | float | None = None,
                              file_hash: str | None = None) -> None:
    token = str((report or {}).get("token") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return
    now = time.time()
    filename = str(report.get("filename") or _preflight_name_for_token(token))
    size = int(report.get("size") or 0)
    summary = _history_entry_summary(report)
    with _PREFLIGHT_HISTORY_LOCK:
        history = _read_preflight_history_unlocked()
        entries = []
        previous: dict | None = None
        for raw in history.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            if raw.get("token") == token:
                previous = raw
                continue
            if (raw.get("filename") == filename
                    and int(raw.get("size") or -1) == size
                    and client_mtime is not None
                    and raw.get("client_mtime") == client_mtime):
                continue
            entries.append(raw)
        if client_mtime is None and previous is not None:
            client_mtime = previous.get("client_mtime")
        if file_hash is None and previous is not None:
            file_hash = previous.get("file_hash")
        entry = {
            "token": token,
            "filename": filename,
            "size": size,
            "client_mtime": client_mtime,
            "file_hash": file_hash,
            "created_at": report.get("created_at") or now,
            "analyzed_at": now,
            "source_graph_hash": (
                (report.get("route_plan") or {}).get("source_graph_hash")
                or (report.get("source_map") or {}).get("source_graph_hash")
                or ""
            ),
            "summary": summary,
        }
        entries.insert(0, entry)
        history["entries"] = entries
        _write_preflight_history_unlocked(history)

def _update_preflight_print_history(token: str, *,
                                    job_id: str | None = None,
                                    status: str | None = None,
                                    moonraker: dict | None = None,
                                    error: str | None = None) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return
    now = time.time()
    with _PREFLIGHT_HISTORY_LOCK:
        history = _read_preflight_history_unlocked()
        changed = False
        for entry in history.get("entries") or []:
            if not isinstance(entry, dict) or entry.get("token") != token:
                continue
            entry["last_print_at"] = now
            if job_id:
                entry["last_print_job_id"] = job_id
            if status:
                entry["last_print_status"] = status
            if moonraker is not None:
                entry["last_moonraker"] = moonraker
            if error:
                entry["last_print_error"] = error
            elif status == "done":
                entry.pop("last_print_error", None)
            changed = True
            break
        if changed:
            _write_preflight_history_unlocked(history)

def _history_entry_public(entry: dict) -> dict:
    token = str(entry.get("token") or "")
    out = {
        "token": token,
        "filename": entry.get("filename") or _preflight_name_for_token(token),
        "size": entry.get("size") or 0,
        "client_mtime": entry.get("client_mtime"),
        "file_hash": entry.get("file_hash"),
        "created_at": entry.get("created_at"),
        "analyzed_at": entry.get("analyzed_at"),
        "source_graph_hash": entry.get("source_graph_hash") or "",
        "summary": entry.get("summary") or {},
        "last_print_at": entry.get("last_print_at"),
        "last_print_job_id": entry.get("last_print_job_id"),
        "last_print_status": entry.get("last_print_status"),
        "last_print_error": entry.get("last_print_error"),
        "available": _preflight_token_available(token),
    }
    return out

def _preflight_history_entries() -> list[dict]:
    with _PREFLIGHT_HISTORY_LOCK:
        history = _read_preflight_history_unlocked()
        entries = [
            _history_entry_public(entry)
            for entry in history.get("entries") or []
            if isinstance(entry, dict)
        ]
    return entries

def _preflight_history_match(filename: str, size: int,
                             client_mtime: int | float | None = None) -> dict | None:
    safe_name = _validate_preflight_filename(filename)
    try:
        size = int(size)
    except (TypeError, ValueError):
        return None
    entries = _preflight_history_entries()
    exact: list[dict] = []
    fallback: list[dict] = []
    for entry in entries:
        if not entry.get("available"):
            continue
        if entry.get("filename") != safe_name:
            continue
        if int(entry.get("size") or -1) != size:
            continue
        if client_mtime is not None and entry.get("client_mtime") == client_mtime:
            exact.append(entry)
        elif client_mtime is None:
            fallback.append(entry)
        else:
            fallback.append(entry)
    candidates = exact or fallback
    if not candidates:
        return None
    found = sorted(candidates, key=lambda e: e.get("analyzed_at") or 0,
                   reverse=True)[0]
    found = dict(found)
    found["match_confidence"] = "exact" if found in exact else "filename_size"
    return found

def _unlink_preflight_token(token: str) -> None:
    for p in _preflight_token_files().get(token, []):
        try:
            p.unlink()
        except OSError:
            pass

def _prune_preflight_cache(required_free: int = 0,
                           protected_token: str | None = None) -> None:
    if not _PREFLIGHT_DIR.is_dir():
        return
    groups = _preflight_token_files()
    def free_ok() -> bool:
        if required_free <= 0:
            return True
        try:
            usage = shutil.disk_usage(_PREFLIGHT_DIR)
            return usage.free >= required_free
        except OSError:
            return True
    total = _preflight_cache_bytes(groups)
    if total <= _PREFLIGHT_MAX_CACHE_SIZE and free_ok():
        return
    ordered: list[tuple[float, str]] = []
    for token, files in groups.items():
        if token == protected_token:
            continue
        mtimes = []
        for p in files:
            try:
                mtimes.append(p.stat().st_mtime)
            except OSError:
                pass
        ordered.append((min(mtimes) if mtimes else 0.0, token))
    for _, token in sorted(ordered):
        _unlink_preflight_token(token)
        groups.pop(token, None)
        total = _preflight_cache_bytes(groups)
        if total <= _PREFLIGHT_MAX_CACHE_SIZE and free_ok():
            break

def _raise_preflight_no_space() -> None:
    raise HTTPException(
        status_code=507,
        detail=("not enough temporary space for G-code preflight; old "
                "preflight files were cleaned, retry the upload"))

def _validate_preflight_filename(raw_name: str) -> str:
    safe_name = os.path.basename(raw_name or "")
    if (not safe_name or safe_name in (".", "..") or "/" in safe_name
            or "\\" in safe_name):
        raise HTTPException(status_code=400, detail="invalid filename")
    if not safe_name.lower().endswith((".gcode", ".gco", ".g")):
        raise HTTPException(status_code=400, detail="not a g-code file")
    return safe_name

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

def _head_source_state_path() -> Path:
    return Path(MULTIACE_HEAD_SOURCE_STATE_PATH)

def _empty_head_source_state() -> dict[str, Any]:
    return {
        "version": 1,
        "heads": {},
        "updated_at": 0.0,
    }

def _read_head_source_state() -> dict[str, Any]:
    path = _head_source_state_path()
    if not path.is_file():
        return _empty_head_source_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_head_source_state()
    if not isinstance(data, dict) or data.get("version") != 1:
        return _empty_head_source_state()
    heads = data.get("heads")
    if not isinstance(heads, dict):
        data["heads"] = {}
    return data

def _write_head_source_state(state: dict[str, Any]) -> None:
    normalized = _empty_head_source_state()
    normalized.update({
        "version": 1,
        "heads": state.get("heads") if isinstance(state.get("heads"), dict) else {},
        "updated_at": time.time(),
    })
    path = _head_source_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)

def _record_head_source(head_id: str | int, source_id: str | None,
                        *, reason: str = "operation") -> None:
    head_idx = _head_index_from_id(head_id)
    if head_idx is None:
        return
    state = _read_head_source_state()
    heads = state.setdefault("heads", {})
    key = f"head:{head_idx}"
    if source_id:
        heads[key] = {
            "source": str(source_id),
            "reason": reason,
            "updated_at": time.time(),
        }
    else:
        heads.pop(key, None)
    _write_head_source_state(state)

def _ace_head_sources_from_parsed(parsed: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for th in parsed.get("toolheads", []) or []:
        try:
            head = int(th.get("idx"))
        except (TypeError, ValueError):
            continue
        if not th.get("head_source_known"):
            continue
        try:
            out[f"head:{head}"] = {
                "source": "ace:%d:%d" % (int(th.get("ace")), int(th.get("slot"))),
                "reason": "ace_head_source",
                "updated_at": time.time(),
            }
        except (TypeError, ValueError):
            continue
    return out

def _head_source_records_for_state(graph: dict, parsed: dict) -> dict[str, Any]:
    records = _read_head_source_state().get("heads") or {}
    out = {
        str(head_id): value
        for head_id, value in records.items()
        if isinstance(value, dict)
    }
    out.update(_ace_head_sources_from_parsed(parsed))
    graph_heads = set((graph.get("heads") or {}).keys())
    graph_sources = graph.get("sources") or {}
    for head_id in list(out.keys()):
        if head_id not in graph_heads:
            out.pop(head_id, None)
            continue
        try:
            head_idx = int(str(head_id).split(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            head_idx = None
        if head_idx is not None:
            feed = _feed_status_for_head(parsed, head_idx)
            sensor = (
                bool(feed.get("filament_in_toolhead"))
                or bool(feed.get("filament_at_extruder"))
            )
            if not sensor:
                out.pop(head_id, None)
                continue
        source_id = (out.get(head_id) or {}).get("source")
        if source_id and source_id not in graph_sources:
            out.pop(head_id, None)
    return out

def _source_state_for_parsed(graph: dict, parsed: dict) -> dict:
    state = sg.source_state(
        graph,
        parsed,
        head_sources=_head_source_records_for_state(graph, parsed),
    )
    state["head_source_store"] = {
        "path": MULTIACE_HEAD_SOURCE_STATE_PATH,
        "heads": _head_source_records_for_state(graph, parsed),
    }
    return state

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

def _configured_loadout_from_parsed(parsed: dict, pp=None) -> list[dict]:
    graph, meta = _load_source_graph(parsed)
    if meta.get("errors"):
        raise HTTPException(
            status_code=409,
            detail="source graph invalid: " + "; ".join(meta.get("errors") or []),
        )
    color_name_fn = pp.approx_color_name if pp else None
    return sg.live_loadout(
        graph, parsed, color_name_fn=color_name_fn, include_unready=True)

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
        if mat_match and dist is not None:
            return 4, float(dist), "material_nearest_color"
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
    return {}

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
    execution = target.get("execution") if isinstance(target.get("execution"), dict) else {}
    for key in (
            "preload_length_mm",
            "push_to_junction_length_mm",
            "load_to_toolhead_length_mm",
            "unload_to_junction_length_mm",
            "full_unload_length_mm",
            "toolhead_sync_retract_length_mm",
            "feed_speed_mm_s",
            "retract_speed_mm_s",
            "toolhead_sync_retract_speed_mm_s"):
        values[key] = execution.get(key, "")
    command = _format_profile_command(action_spec.get("command"), values)
    if not command:
        return None
    step_kind = {
        "load": "load_source",
        "unload": "unload_source",
        "retract": "retract_source_to_junction",
        "full_unload": "full_unload_source",
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
    if "clears_current_source" in action_spec:
        step["clears_current_source"] = bool(action_spec.get("clears_current_source"))
    if "sets_current_source" in action_spec:
        step["sets_current_source"] = bool(action_spec.get("sets_current_source"))
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
        "execution": source.get("execution") or {},
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

def _target_for_source_slot_action(
        graph: dict,
        source_id: str,
) -> dict | None:
    sources = graph.get("sources") or {}
    source = sources.get(source_id)
    if not isinstance(source, dict):
        return None
    preferred_head = None
    try:
        if source.get("kind") == "native_feeder" and source.get("head") is not None:
            preferred_head = "head:%d" % int(source.get("head"))
    except (TypeError, ValueError):
        preferred_head = None
    candidates = []
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or not edge.get("enabled", True):
            continue
        if edge.get("source") != source_id:
            continue
        candidates.append(edge.get("head"))
    if preferred_head in candidates:
        return _target_from_graph_source_edge(graph, source_id, preferred_head)
    for head_id in candidates:
        target = _target_from_graph_source_edge(graph, source_id, head_id)
        if target:
            return target
    return None

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

def _native_retract_step_for_target(
        target: dict | None,
        profiles: dict | None,
) -> dict | None:
    if not target or target.get("kind") != "native":
        return None
    return _profile_step(
        target=target,
        profiles=profiles,
        action="retract",
    )

def _native_unload_is_at_retract_stage(head_state: dict | None) -> bool:
    if not isinstance(head_state, dict):
        return False
    reason = str(head_state.get("source_ready_reason") or "").lower()
    return "unload_finish" in reason

def _source_transition_event(
        *,
        index: int,
        target: dict,
        initial_state: dict,
        graph: dict,
        current_source: str | None = None,
        prefer_swap_for_ace: bool = False,
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
        current_head_state = (initial_state.get("heads") or {}).get(head_id) or {}
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
                retract = _native_retract_step_for_target(current_target, profiles)
                if retract and _native_unload_is_at_retract_stage(current_head_state):
                    steps.append(retract)
                elif unload:
                    if retract:
                        unload["requires_toolhead_empty"] = False
                    steps.append(unload)
                    if retract:
                        steps.append(retract)
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
        action = (
            "swap"
            if target.get("kind") == "ace" and (prefer_swap_for_ace or current_source)
            else "load"
        )
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
                  else ("swap" if target.get("kind") == "ace" and (prefer_swap_for_ace or current_source) else "load"),
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
    event_stream = _complete_route_tool_events(events, used_tools)
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
                prefer_swap_for_ace=True,
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
                    prefer_swap_for_ace=True,
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
        event_action = event.get("action")
        recover_event = event_action == "recover"
        if (not recover_event) and (not source_id or source_id not in sources):
            errors.append("route event[%d] references unknown source %r"
                          % (idx, source_id))
        if not head_id or head_id not in heads:
            errors.append("route event[%d] references unknown head %r"
                          % (idx, head_id))
        if (not recover_event) and source_id and head_id and (source_id, head_id) not in enabled_edges:
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
            if kind == "home_axes":
                if str(command or "").strip().upper() != "G28":
                    errors.append(
                        "route event[%d] step[%d] home_axes must be G28"
                        % (idx, step_idx))
                if command:
                    commands.append(command)
                continue
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
                if not (kind in ("unload_source", "retract_source_to_junction")
                        and step.get("source") == event.get("previous_source")):
                    errors.append("route event[%d] step[%d] source mismatch"
                                  % (idx, step_idx))
            if step.get("head") and step.get("head") != head_id:
                errors.append("route event[%d] step[%d] head mismatch"
                              % (idx, step_idx))
            if ((not recover_event) and step_source and head_id
                    and (step_source, head_id) not in enabled_edges):
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
            if kind in ("load_source", "unload_source",
                        "retract_source_to_junction", "full_unload_source"):
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
            "single_ace_multi_head_allowed": True,
            "sequential_ace_actions": True,
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

def _route_plan_operation_ready_sources(route_plan: dict) -> set[str]:
    """Sources that must be ready before an operation can begin.

    Manual unload/retract is explicitly allowed to operate on an unready source:
    that is the recovery path for "toolhead still has filament, source slot is
    no longer ready" states.  Load/swap still require the target source ready.
    """
    sources: set[str] = set()
    if not isinstance(route_plan, dict):
        return sources
    for event in route_plan.get("events") or []:
        if not isinstance(event, dict):
            continue
        for step in event.get("steps") or []:
            if not isinstance(step, dict):
                continue
            action = step.get("profile_action")
            kind = step.get("kind")
            if action in ("load", "swap") or kind in ("load_source", "swap_source"):
                source_id = step.get("source") or event.get("source")
                if source_id:
                    sources.add(str(source_id))
    return sources

def _route_plan_unload_only_head(route_plan: dict, head_id: str) -> bool:
    saw_unload = False
    for event in route_plan.get("events") or []:
        if not isinstance(event, dict):
            continue
        for step in event.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_head = _normalize_route_head_id({
                "head": step.get("head") or event.get("head")
            })
            if step_head != head_id:
                continue
            action = step.get("profile_action")
            kind = step.get("kind")
            if action in ("unload", "retract") or kind in (
                    "unload_source", "retract_source_to_junction"):
                saw_unload = True
                continue
            return False
    return saw_unload

def _route_plan_recover_only_head(route_plan: dict, head_id: str) -> bool:
    saw_recover = False
    for event in route_plan.get("events") or []:
        if not isinstance(event, dict):
            continue
        for step in event.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_head = _normalize_route_head_id({
                "head": step.get("head") or event.get("head")
            })
            if step_head != head_id:
                continue
            action = step.get("profile_action")
            kind = step.get("kind")
            if action == "recover" or kind == "recover_head":
                saw_recover = True
                continue
            return False
    return saw_recover

def _validate_route_plan_runtime_state(route_plan: dict,
                                       current_state: dict,
                                       current_sources: dict | None = None,
                                       *,
                                       operation_mode: bool = False) -> list[str]:
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
    has_events = bool(route_plan.get("events"))
    has_resources = bool((route_plan.get("resources") or {}).get("sources"))
    if not (used_tools or has_tool_map or has_tool_select or has_events or has_resources):
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
    if current_sources is None:
        current_sources = {}
    # "stale" means the source record says loaded but the toolhead sensor is
    # empty. Do not auto-correct that during print dispatch: it must be
    # recovered explicitly before any load/print operation can continue.
    actionable = {"known", "empty"}
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
        unload_recovery = (
            operation_mode
            and planned_conf == "exhausted"
            and live_conf == "exhausted"
            and _route_plan_unload_only_head(route_plan, head_id)
        )
        recover_recovery = (
            operation_mode
            and planned_conf in ("unknown", "stale", "failed", "exhausted")
            and live_conf in ("unknown", "stale", "failed", "exhausted")
            and _route_plan_recover_only_head(route_plan, head_id)
        )
        if planned_conf not in actionable and not unload_recovery:
            if recover_recovery:
                pass
            else:
                errors.append(
                    "route plan initial_state[%s] is %s; recover head state and rerun preflight"
                    % (head_id, planned_conf or "unknown"))
        if live_conf not in actionable and not unload_recovery:
            if recover_recovery:
                pass
            else:
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
        if recover_recovery:
            continue
    ready_sources = None
    if operation_mode:
        ready_sources = _route_plan_operation_ready_sources(route_plan)
    for source_id in sorted((route_plan.get("resources") or {}).get("sources") or {}):
        if ready_sources is not None and source_id not in ready_sources:
            continue
        source = current_sources.get(source_id) if isinstance(current_sources, dict) else None
        if source is None:
            errors.append(
                "current source state missing planned source %s" % source_id)
            continue
        if source.get("ready") is False:
            errors.append(
                "planned source %s is not ready: %s"
                % (source_id, source.get("ready_reason") or "not ready"))
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


def _disabled_plan() -> dict:
    return {
        "feasible":     False,
        "swaps":        0,
        "tool_changes": 0,
        "mapping":      [],
        "reason":       "",
    }


def _build_slicer_plan(result, mapping):
    events = result.get("events") or []
    tool_changes = int(result.get("total_changes") or 0)
    return {
        "feasible":     True,
        "swaps":        _real_swap_count(events, mapping),
        "tool_changes": tool_changes,
        "mapping":      mapping,
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

def _validate_web_gcode_safety_lines(lines, *, filename: str = "") -> dict:
    errors: list[dict[str, Any]] = []
    machine_signature = ""
    dangerous: list[dict[str, Any]] = []
    for line_no, raw in enumerate(lines, start=1):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = str(raw).rstrip("\r\n")
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

def _validate_web_gcode_safety_text(gcode: str, *, filename: str = "") -> dict:
    return _validate_web_gcode_safety_lines(
        gcode.splitlines(), filename=filename)

def _validate_web_gcode_safety_bytes(data: bytes, *, filename: str = "") -> dict:
    return _validate_web_gcode_safety_lines(
        data.splitlines(), filename=filename)

def _validate_web_gcode_safety_file(path: Path, *, filename: str = "") -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return _validate_web_gcode_safety_lines(fh, filename=filename)

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
    saw_change = False
    for raw in gcode.splitlines():
        line = raw.strip()
        if not line:
            continue
        change = _TOOLCHANGE_RE.match(line)
        if change:
            saw_change = True
            try:
                tools = (int(change.group(1)), int(change.group(2)))
            except (TypeError, ValueError):
                tools = ()
            for tool in tools:
                if used and tool not in used:
                    continue
                if events and events[-1] == tool:
                    continue
                events.append(tool)
            continue
        if line.startswith(";"):
            continue
        m = _BARE_TOOL_RE.match(line)
        if not m:
            continue
        if saw_change:
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

def _complete_route_tool_events(events: list[int] | tuple[int, ...] | None,
                                used_tools: set[int]) -> list[int]:
    """Normalize route events without inventing order for partial streams."""
    out: list[int] = []
    for raw in events or []:
        try:
            tool = int(raw)
        except (TypeError, ValueError):
            continue
        if used_tools and tool not in used_tools:
            continue
        if out and out[-1] == tool:
            continue
        out.append(tool)
    if not out:
        out = [int(t) for t in sorted(used_tools)]
    return out

async def _save_preflight_upload_file(file: UploadFile) -> tuple[str, str, int]:
    safe_name = _validate_preflight_filename(file.filename or "")
    _cleanup_preflight_dir()
    _PREFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    import uuid as _uuid
    token = _uuid.uuid4().hex
    src_path = _PREFLIGHT_DIR / (token + ".gcode")
    upload_size = 0
    try:
        _prune_preflight_cache(
            required_free=min(_PREFLIGHT_MAX_SIZE + _PREFLIGHT_MIN_FREE_SIZE,
                              _PREFLIGHT_MAX_CACHE_SIZE + _PREFLIGHT_MIN_FREE_SIZE),
            protected_token=token,
        )
        with open(src_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                upload_size += len(chunk)
                if upload_size > _PREFLIGHT_MAX_SIZE:
                    try:
                        src_path.unlink()
                    except Exception:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=(f"gcode too large for in-printer preflight "
                                f"({upload_size//1024//1024} MB > "
                                f"{_PREFLIGHT_MAX_SIZE//1024//1024} MB limit). "
                                f"Bypass: upload directly via Moonraker's normal "
                                f"upload endpoint, or raise the limit via "
                                f"MULTIACE_PREFLIGHT_MAX_MB env."))
                out.write(chunk)
    except OSError as exc:
        try:
            src_path.unlink()
        except Exception:
            pass
        if exc.errno == errno.ENOSPC:
            _prune_preflight_cache(protected_token=token)
            _raise_preflight_no_space()
        raise
    await file.close()
    if upload_size <= 0:
        try:
            src_path.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="empty file")
    (_PREFLIGHT_DIR / (token + ".name")).write_text(safe_name, encoding="utf-8")
    _prune_preflight_cache(protected_token=token)
    return token, safe_name, upload_size

async def _build_route_plan_preview_from_saved(token: str, safe_name: str,
                                               upload_size: int) -> dict:
    src_path = _PREFLIGHT_DIR / (token + ".gcode")
    if not src_path.is_file():
        raise HTTPException(status_code=404,
                            detail="route plan token expired or unknown")
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    return await asyncio.to_thread(
        _build_route_plan_preview_from_saved_sync,
        token, safe_name, upload_size, parsed)

def _build_route_plan_preview_from_saved_sync(token: str, safe_name: str,
                                              upload_size: int,
                                              parsed: dict) -> dict:
    pp = _load_post_processor()
    src_path = _PREFLIGHT_DIR / (token + ".gcode")
    if not src_path.is_file():
        raise HTTPException(status_code=404,
                            detail="route plan token expired or unknown")
    _raise_gcode_safety_error(
        _validate_web_gcode_safety_file(src_path, filename=safe_name))

    plan_keep_re = re.compile(
        r'^(;\s*Change Tool|;\s*LAYER_CHANGE|;\s*filament\b|T\d{1,2}\s*$)',
        re.IGNORECASE)
    head_lines: list[str] = []
    tail_lines: deque[str] = deque(maxlen=2000)
    plan_lines: list[str] = []
    used: set[int] = set()
    bare_used: set[int] = set()
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
            bm = _BARE_TOOL_RE.match(line.strip())
            if bm:
                bare_used.add(int(bm.group(1)))
            if plan_keep_re.match(line):
                plan_lines.append(line.rstrip('\n'))
    meta_buf = "".join(head_lines) + "".join(tail_lines)
    plan_proxy = "\n".join(plan_lines)
    del head_lines, tail_lines, plan_lines
    used.update(bare_used)
    used.update(_used_tool_indices(pp, plan_proxy))

    slicer_colors = pp.parse_color_names(meta_buf)
    slicer_types  = pp.parse_filament_types(meta_buf)
    num_aces      = pp.infer_num_aces(meta_buf)
    del meta_buf

    if used:
        slicer_colors = {t: c for t, c in slicer_colors.items() if t in used}
        slicer_types  = {t: m for t, m in slicer_types.items() if t in used}

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
    route_events = _complete_route_tool_events(route_events, used_tools)
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
    route_plan = None
    if not resolver.get("errors"):
        route_plan = _build_route_plan(
            token=token,
            filename=safe_name,
            graph_meta=graph_meta,
            tool_targets=tool_targets,
            used_tools=used_tools,
            events=route_events,
            profiles=(graph.get("profiles") or {}),
            initial_state=_source_state_for_parsed(graph, parsed),
            graph=graph,
            stats=source_map.get("swap_stats") or {},
            created_at=source_map.get("created_at"),
        )
    _save_preflight_events(token, route_events)
    _save_preflight_source_map(source_map)
    if route_plan:
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
        "configured_loadout": sorted(
            _configured_loadout_from_parsed(parsed, pp),
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
        out["plans"]["slicer"] = _build_slicer_plan(result, mapping)
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
    _save_preflight_report(out)
    return out

async def _create_route_plan_preview_from_upload(file: UploadFile) -> dict:
    token, safe_name, upload_size = await _save_preflight_upload_file(file)
    report = await _build_route_plan_preview_from_saved(
        token, safe_name, upload_size)
    _record_preflight_history(report)
    return report

@app.post("/api/preflight")
async def preflight(file: UploadFile = File(...)) -> dict:
    return await _create_route_plan_preview_from_upload(file)

@app.post("/api/route-plan/preview")
async def route_plan_preview(file: UploadFile = File(...)) -> dict:
    return await _create_route_plan_preview_from_upload(file)

_PREFLIGHT_JOBS: dict[str, dict] = {}
_PREFLIGHT_JOBS_LOCK = asyncio.Lock()
_PREFLIGHT_JOB_TTL = 600.0

class RoutePlanUploadStartRequest(BaseModel):
    filename: str
    size: int
    client_mtime: int | float | None = None

class RoutePlanUploadCommitRequest(BaseModel):
    token: str

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

def _job_response(job_id: str, state: dict, *,
                  include_artifacts: bool = True) -> dict:
    result = state.get("result")
    out = {
        "job_id": job_id,
        "kind": state.get("kind"),
        "stage": state.get("stage"),
        "percent": round(state.get("percent", 0.0), 1),
        "done": bool(state.get("done")),
        "error": state.get("error"),
        "filename": state.get("filename"),
        "mode": state.get("mode"),
        "token": state.get("token"),
        "result": result or None,
    }
    if include_artifacts:
        out["source_map"] = (
            (result or {}).get("source_map") or state.get("source_map") or {})
        out["route_plan"] = (
            (result or {}).get("route_plan") or state.get("route_plan") or {})
    return out

async def _run_route_plan_preview_job(job_id: str, token: str,
                                      safe_name: str,
                                      upload_size: int,
                                      client_mtime: int | float | None = None
                                      ) -> None:
    state = _PREFLIGHT_JOBS[job_id]
    try:
        await asyncio.sleep(0)
        _set_stage(state, "analyze", 5.0)
        result = await _build_route_plan_preview_from_saved(
            token, safe_name, upload_size)
        state["result"] = result
        state["source_map"] = result.get("source_map") or {}
        state["route_plan"] = result.get("route_plan") or {}
        _record_preflight_history(result, client_mtime=client_mtime)
        _set_stage(state, "done", 100.0)
        state["done"] = True
    except HTTPException as exc:
        detail = exc.detail
        state["error"] = detail if isinstance(detail, str) else json.dumps(detail)
        state["done"] = True
        state["ts"] = time.time()
    except Exception as exc:
        state["error"] = str(exc)
        state["done"] = True
        state["ts"] = time.time()

@app.post("/api/route-plan/preview/async")
async def route_plan_preview_async(file: UploadFile = File(...)) -> dict:
    _prune_old_jobs()
    token, safe_name, upload_size = await _save_preflight_upload_file(file)
    return _start_route_plan_preview_job(token, safe_name, upload_size)

def _start_route_plan_preview_job(token: str, safe_name: str,
                                  upload_size: int,
                                  client_mtime: int | float | None = None
                                  ) -> dict:
    _prune_old_jobs()
    job_id = uuid.uuid4().hex
    _PREFLIGHT_JOBS[job_id] = {
        "kind": "route_preview",
        "stage": "queued",
        "percent": 0.0,
        "done": False,
        "error": None,
        "filename": safe_name,
        "mode": "preview",
        "token": token,
        "size": upload_size,
        "client_mtime": client_mtime,
        "ts": time.time(),
    }
    asyncio.create_task(_run_route_plan_preview_job(
        job_id, token, safe_name, upload_size, client_mtime))
    return _job_response(job_id, _PREFLIGHT_JOBS[job_id])

@app.post("/api/route-plan/preview/upload/start")
async def route_plan_preview_upload_start(
        req: RoutePlanUploadStartRequest) -> dict:
    safe_name = _validate_preflight_filename(req.filename)
    try:
        total_size = int(req.size)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid upload size")
    if total_size <= 0:
        raise HTTPException(status_code=400, detail="empty file")
    if total_size > _PREFLIGHT_MAX_SIZE:
        raise HTTPException(
            status_code=413,
            detail=(f"gcode too large for in-printer preflight "
                    f"({total_size//1024//1024} MB > "
                    f"{_PREFLIGHT_MAX_SIZE//1024//1024} MB limit)"))
    _cleanup_preflight_dir()
    _PREFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    _prune_preflight_cache(
        required_free=min(total_size + _PREFLIGHT_MIN_FREE_SIZE,
                          _PREFLIGHT_MAX_CACHE_SIZE + _PREFLIGHT_MIN_FREE_SIZE),
    )
    token = uuid.uuid4().hex
    (_PREFLIGHT_DIR / (token + ".part")).write_bytes(b"")
    (_PREFLIGHT_DIR / (token + ".upload.json")).write_text(
        json.dumps({
            "token": token,
            "filename": safe_name,
            "size": total_size,
            "offset": 0,
            "client_mtime": req.client_mtime,
            "created_at": time.time(),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "token": token,
        "filename": safe_name,
        "size": total_size,
        "offset": 0,
    }

@app.post("/api/route-plan/preview/upload/chunk")
async def route_plan_preview_upload_chunk(
        token: str, offset: int, file: UploadFile = File(...)) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    meta_path = _PREFLIGHT_DIR / (token + ".upload.json")
    part_path = _PREFLIGHT_DIR / (token + ".part")
    if not meta_path.is_file() or not part_path.is_file():
        raise HTTPException(status_code=404, detail="upload session not found")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        expected_size = int(meta.get("size") or 0)
        expected_offset = int(meta.get("offset") or 0)
        offset = int(offset)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid upload metadata")
    if offset != expected_offset:
        raise HTTPException(
            status_code=409,
            detail=f"unexpected upload offset {offset}, expected {expected_offset}")
    written = 0
    try:
        with open(part_path, "ab") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if expected_offset + written > expected_size:
                    raise HTTPException(
                        status_code=413,
                        detail="chunk exceeds declared upload size")
                out.write(chunk)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            _raise_preflight_no_space()
        raise
    finally:
        await file.close()
    if written <= 0:
        raise HTTPException(status_code=400, detail="empty chunk")
    meta["offset"] = expected_offset + written
    meta["updated_at"] = time.time()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {
        "token": token,
        "offset": meta["offset"],
        "size": expected_size,
        "done": meta["offset"] >= expected_size,
    }

@app.post("/api/route-plan/preview/upload/commit")
async def route_plan_preview_upload_commit(
        req: RoutePlanUploadCommitRequest) -> dict:
    token = req.token
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    meta_path = _PREFLIGHT_DIR / (token + ".upload.json")
    part_path = _PREFLIGHT_DIR / (token + ".part")
    src_path = _PREFLIGHT_DIR / (token + ".gcode")
    if not meta_path.is_file() or not part_path.is_file():
        raise HTTPException(status_code=404, detail="upload session not found")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        safe_name = _validate_preflight_filename(str(meta.get("filename") or ""))
        expected_size = int(meta.get("size") or 0)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="invalid upload metadata")
    actual_size = part_path.stat().st_size
    if actual_size != expected_size:
        raise HTTPException(
            status_code=409,
            detail=f"incomplete upload {actual_size}/{expected_size} bytes")
    try:
        part_path.rename(src_path)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            _raise_preflight_no_space()
        raise
    (_PREFLIGHT_DIR / (token + ".name")).write_text(safe_name, encoding="utf-8")
    try:
        meta_path.unlink()
    except OSError:
        pass
    _prune_preflight_cache(protected_token=token)
    return _start_route_plan_preview_job(
        token, safe_name, actual_size, meta.get("client_mtime"))

@app.get("/api/route-plan/preview/status")
async def route_plan_preview_status(job_id: str) -> dict:
    state = _PREFLIGHT_JOBS.get(job_id)
    if state is None or state.get("kind") != "route_preview":
        raise HTTPException(status_code=404, detail="preview job not found")
    return _job_response(job_id, state, include_artifacts=False)

@app.get("/api/route-plan/history")
async def route_plan_history() -> dict:
    entries = _preflight_history_entries()
    return {
        "entries": entries[:_PREFLIGHT_HISTORY_MAX_ITEMS],
        "cache_dir": str(_PREFLIGHT_DIR),
    }

@app.get("/api/route-plan/history/match")
async def route_plan_history_match(filename: str, size: int,
                                   client_mtime: int | float | None = None
                                   ) -> dict:
    entry = _preflight_history_match(filename, size, client_mtime)
    return {
        "matched": bool(entry),
        "entry": entry,
    }

@app.get("/api/route-plan/history/report")
async def route_plan_history_report(token: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    if not _preflight_token_available(token):
        raise HTTPException(status_code=404,
                            detail="history cache expired; upload again")
    report = _load_preflight_report(token)
    if not report:
        raise HTTPException(status_code=404,
                            detail="history report missing; upload again")
    return report

_SNAPMAKER_STARTUP_MACROS_DISABLED_FOR_ROUTE_PLAN = {
    "SET_PRINT_PREFERENCES",
    "DEFECT_DETECTION_START",
    "DEFECT_DETECTION_DETECT_BED",
    "SM_PRINT_CHECK_SWITCH_EXTRUDER",
    "SM_PRINT_EXTRUDER_PREHEAT",
    "SM_PRINT_AUTO_FEED",
    "SM_PRINT_FLOW_CALIBRATE",
}

def _prepend_print_prefs(in_path: str, out_path: str) -> None:
    """Compatibility shim for the old set_prefs flag.

    Snapmaker rejects SET_PRINT_PREFERENCES once a print file has started
    executing, so route-plan printing must not prepend it into G-code.  Keep
    this step as a sanitizer only: strip any slicer-provided preference line.
    """
    with open(out_path, "w", encoding="utf-8", errors="replace") as out:
        out.write("; Colorful-U1 route-plan preference sanitizer\n")
        with open(in_path, "r", encoding="utf-8", errors="replace") as src:
            for line in src:
                if line.lstrip().upper().startswith("SET_PRINT_PREFERENCES"):
                    out.write("; Colorful-U1 disabled print preference: "
                              + line.lstrip())
                    continue
                out.write(line)

def _sanitize_route_plan_startup_gcode(in_path: str, out_path: str) -> None:
    """Disable Snapmaker startup macros that fight source-graph routing.

    Route-plan printing owns filament loading and tool/source mapping. The
    slicer's stock startup block may still contain bed defect detection and
    all-head native auto-feed/flow-calibration macros. On mixed ACE/native
    prints those macros can move or probe the wrong physical tool before the
    Colorful-U1 auto-load block is reached. Keep the original lines as
    comments for diagnosis, but do not execute them.
    """
    with open(out_path, "w", encoding="utf-8", errors="replace") as out:
        out.write("; Colorful-U1 route-plan startup safety\n")
        with open(in_path, "r", encoding="utf-8", errors="replace") as src:
            for line in src:
                stripped = line.lstrip()
                macro = stripped.split(None, 1)[0].upper() if stripped.strip() else ""
                if macro in _SNAPMAKER_STARTUP_MACROS_DISABLED_FOR_ROUTE_PLAN:
                    out.write("; Colorful-U1 disabled startup macro: "
                              + stripped)
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
    state["token"] = token
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
        if not used and route_targets:
            used = {int(t) for t in route_targets.keys()}

        parsed = _parse_state(await _query_state())
        failed = _load_failed_toolheads(parsed)
        if failed:
            raise RuntimeError(
                "; ".join(_load_failed_message(t) for t in failed))
        if mode == "slicer":
            if route_targets:
                missing_targets = sorted(
                    t for t in used if str(t) not in route_targets)
                if missing_targets:
                    raise RuntimeError(
                        "route plan mapping is incomplete for "
                        + ", ".join("T%d" % t for t in missing_targets))
            else:
                raise RuntimeError(
                    "route plan missing; run route-plan preview again")
        else:
            raise RuntimeError(
                "optimize/layer print modes are disabled for the "
                "single-toolhead ACE MVP; use slicer mode")

        cur = src
        nxt = tmp_a

        _set_stage(state, "rewrite", 5.0)
        if not route_plan:
            raise RuntimeError(
                "route plan missing; run source graph preflight again")
        graph, graph_meta = _load_source_graph(parsed)
        route_errors = _validate_route_plan_for_graph(route_plan, graph, graph_meta)
        current_state = _source_state_for_parsed(graph, parsed)
        current_sources = sg.runtime_sources(graph, parsed)
        route_errors.extend(
            _validate_route_plan_runtime_state(
                route_plan, current_state, current_sources))
        if route_errors:
            raise RuntimeError(
                "route plan validation failed: " + "; ".join(route_errors[:8]))
        await asyncio.to_thread(
            pp.rewrite_to_file,
            str(cur), str(nxt),
            _stage_progress(state, 5.0, 60.0),
            route_plan=route_plan,
        )
        cur, nxt = nxt, cur

        _set_stage(state, "inject_auto_load", 70.0)
        await asyncio.to_thread(
            pp.inject_auto_load_to_file,
            str(cur), str(nxt),
            _stage_progress(state, 70.0, 10.0),
        )
        cur, nxt = nxt, cur

        _set_stage(state, "sanitize_startup", 82.0)
        await asyncio.to_thread(
            _sanitize_route_plan_startup_gcode, str(cur), str(nxt))
        cur, nxt = nxt, cur

        if set_prefs:
            _set_stage(state, "print_prefs", 84.0)
            await asyncio.to_thread(
                _prepend_print_prefs, str(cur), str(nxt))
            cur, nxt = nxt, cur

        final_validation = _validate_web_gcode_safety_file(
            cur, filename=safe_name)
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
        _update_preflight_print_history(
            token, job_id=job_id, status="done",
            moonraker=state.get("moonraker"))
    except Exception as exc:
        state["error"] = str(exc)
        state["done"]  = True
        state["ts"]    = time.time()
        _update_preflight_print_history(
            token, job_id=job_id, status="error", error=str(exc))
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
        initial_state=_source_state_for_parsed(graph, parsed),
        graph=graph,
        stats=source_map.get("swap_stats") or {},
        created_at=source_map.get("created_at"),
    )
    route_errors = _validate_route_plan_for_graph(route_plan, graph, graph_meta)
    current_state = _source_state_for_parsed(graph, parsed)
    current_sources = sg.runtime_sources(graph, parsed)
    route_errors.extend(
        _validate_route_plan_runtime_state(
            route_plan, current_state, current_sources))
    if route_errors:
        raise HTTPException(
            status_code=409,
            detail="route plan validation failed: " + "; ".join(route_errors[:8]))
    _save_preflight_source_map(source_map)
    _save_preflight_route_plan(route_plan)
    report = _load_preflight_report(token)
    if report:
        report.update({
            "token": token,
            "filename": safe_name,
            "source_map": source_map,
            "route_plan": route_plan,
            "tool_targets": manual_targets,
            "resolve_errors": [],
        })
        _save_preflight_report(report)
        _record_preflight_history(report)
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
    route_plan = _load_preflight_route_plan(token)
    if not route_plan:
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
    graph, graph_meta = _load_source_graph(parsed)
    route_errors = _validate_route_plan_for_graph(route_plan, graph, graph_meta)
    current_state = _source_state_for_parsed(graph, parsed)
    current_sources = sg.runtime_sources(graph, parsed)
    route_errors.extend(
        _validate_route_plan_runtime_state(
            route_plan, current_state, current_sources))
    if route_errors:
        raise HTTPException(
            status_code=409,
            detail="route plan validation failed: " + "; ".join(route_errors[:8]))
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
        "token":    token,
        "ts":       time.time(),
        "source_map": _load_preflight_source_map(token),
        "route_plan": _load_preflight_route_plan(token),
    }
    _update_preflight_print_history(token, job_id=job_id, status="queued")
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
async def preflight_print_status(job_id: str,
                                 include_artifacts: int = 0) -> dict:
    state = _PREFLIGHT_JOBS.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")

    return _job_response(
        job_id, state, include_artifacts=bool(include_artifacts))

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
    current_state = _source_state_for_parsed(graph, parsed)
    current_sources = sg.runtime_sources(graph, parsed)
    errors.extend(_validate_route_plan_runtime_state(
        route_plan, current_state, current_sources))
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
    current_state = _source_state_for_parsed(graph, parsed)
    current_sources = sg.runtime_sources(graph, parsed)
    errors.extend(_validate_route_plan_runtime_state(
        route_plan, current_state, current_sources))
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
    """Colorful-U1 uses 0-based ACE/head/slot indexes everywhere.

    Older multiACE configs allowed a display_index_base toggle, but keeping
    that user-facing offset active is exactly how head/slot ambiguity leaks
    back into source graph routing and logs.
    """
    return 0

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
    raise HTTPException(
        status_code=410,
        detail=("direct upload-and-print is disabled; use "
                "/api/route-plan/preview followed by "
                "/api/route-plan/print"))

@app.get("/api/state")
async def get_state() -> dict:
    """Aggregated dashboard state (ACEs + toolheads + dryer + status)."""
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        return {"error": f"moonraker: {e}"}
    return _parse_state(status, source_graph_overlay=True)

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
    refresh: dict[str, Any] | None = None
    try:
        refresh = await _mr_post(
            "/printer/gcode/script",
            {"script": "MULTIACE_REFRESH_SOURCE_GRAPH"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        refresh = {"error": str(exc) or type(exc).__name__}
    return {
        "ok": True,
        "meta": meta,
        "refresh": refresh,
    }

@app.get("/api/source-state")
async def get_source_state() -> dict:
    """Return runtime source/head state interpreted through the source graph."""
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    state = _source_state_for_parsed(graph, parsed)
    state["meta"] = meta
    return state

@app.post("/api/source-action/preview")
async def preview_source_action(payload: SourceActionPreview) -> dict:
    """Build one profile-driven source action step without moving hardware."""
    action = str(payload.action or "").strip().lower()
    if action not in ("load", "unload", "swap", "retract", "full_unload"):
        raise HTTPException(
            status_code=400,
            detail="action must be load, unload, swap, retract or full_unload")
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
        if action not in ("load", "unload", "swap", "retract", "full_unload"):
            raise HTTPException(
                status_code=400,
                detail="action must be load, unload, swap, retract or full_unload")
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
        "initial_state": _source_state_for_parsed(graph, parsed),
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
    initial_state = _source_state_for_parsed(graph, parsed)
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

_operation_lock = asyncio.Lock()
_operation_job: dict[str, Any] | None = None

def _operation_public(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {"active": False}
    return {
        "active": job.get("status") in ("queued", "running"),
        "id": job.get("id"),
        "status": job.get("status"),
        "kind": job.get("kind"),
        "head": job.get("head"),
        "source": job.get("source"),
        "previous_source": job.get("previous_source"),
        "commands": job.get("commands") or [],
        "steps": job.get("steps") or [],
        "current_step": job.get("current_step"),
        "error": job.get("error"),
        "created": job.get("created"),
        "updated": job.get("updated"),
    }

def _operation_active() -> bool:
    return bool(_operation_job and _operation_job.get("status") in ("queued", "running"))

def _finalize_operation_route_plan(route_plan: dict, graph: dict) -> dict:
    route_plan["resources"] = _route_plan_resource_summary(route_plan, graph)
    route_plan["execution"] = _route_plan_execution_summary(route_plan, graph)
    return route_plan

def _validate_operation_route_plan(
        route_plan: dict,
        graph: dict,
        meta: dict,
        current_state: dict,
        current_sources: dict,
) -> list[str]:
    errors = _validate_route_plan_for_graph(route_plan, graph, meta)
    errors.extend(_validate_route_plan_runtime_state(
        route_plan, current_state, current_sources, operation_mode=True))
    script = _script_from_commands(route_plan.get("commands") or [])
    if script:
        try:
            _validate_gcode_script(script)
        except HTTPException as exc:
            errors.append(str(exc.detail))
    return errors

def _script_from_commands(commands: list[str] | tuple[str, ...]) -> str:
    lines = []
    for command in commands or []:
        line = str(command or "").strip()
        if line:
            lines.append(line)
    return "\n".join(lines)

def _operation_steps(route_plan: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event_idx, event in enumerate(route_plan.get("events") or []):
        if not isinstance(event, dict):
            continue
        for step_idx, step in enumerate(event.get("steps") or []):
            if not isinstance(step, dict):
                continue
            item = dict(step)
            item["_event_index"] = event_idx
            item["_step_index"] = step_idx
            item["_event"] = event
            out.append(item)
    return out

def _step_head_id(step: dict[str, Any], event: dict[str, Any] | None = None) -> str | None:
    event = event or step.get("_event") or {}
    head_id = step.get("head") or event.get("head")
    if head_id is None:
        return None
    return _normalize_route_head_id({"head": head_id})

def _step_source_id(step: dict[str, Any], event: dict[str, Any] | None = None) -> str | None:
    event = event or step.get("_event") or {}
    source_id = step.get("source") or event.get("source")
    return str(source_id) if source_id else None

def _stable_feed_state(channel_state: Any) -> bool:
    state = str(channel_state or "")
    if not state:
        return True
    if state in ("wait_insert", "inited", "test"):
        return True
    return state.endswith("_finish") or state.endswith("_fail")

def _feed_status_for_head(parsed: dict, head_idx: int) -> dict[str, Any]:
    for th in parsed.get("toolheads", []) or []:
        try:
            if int(th.get("idx")) == int(head_idx):
                return th
        except (TypeError, ValueError):
            continue
    return {}

async def _wait_for_step_quiescent(step: dict[str, Any],
                                   timeout: float = 8.0) -> dict:
    head_id = _step_head_id(step)
    head_idx = _head_index_from_id(head_id)
    deadline = time.time() + timeout
    parsed: dict[str, Any] = {}
    while True:
        parsed = _parse_state(await _query_state())
        if head_idx is None:
            return parsed
        feed = _feed_status_for_head(parsed, head_idx)
        if _stable_feed_state(feed.get("channel_state")):
            return parsed
        if time.time() >= deadline:
            raise RuntimeError(
                "%s did not settle after %s: channel_state=%s"
                % (head_id, step.get("command"), feed.get("channel_state")))
        await asyncio.sleep(0.25)

def _route_plan_graph_matches(route_plan: dict, meta: dict) -> None:
    plan_hash = route_plan.get("source_graph_hash")
    graph_hash = meta.get("hash")
    if plan_hash and graph_hash and plan_hash != graph_hash:
        raise RuntimeError(
            "source graph changed during operation: plan=%s current=%s"
            % (plan_hash, graph_hash))

def _postcheck_head_loaded(
        *,
        graph: dict,
        parsed: dict,
        head_id: str,
        source_id: str,
) -> dict:
    head_idx = _head_index_from_id(head_id)
    if head_idx is None:
        raise RuntimeError("invalid head for post-check: %s" % head_id)
    feed = _feed_status_for_head(parsed, head_idx)
    if not bool(feed.get("filament_at_extruder")):
        raise RuntimeError(
            "%s load did not reach toolhead sensor for %s"
            % (head_id, source_id))
    current_state = _source_state_for_parsed(graph, parsed)
    live_head = (current_state.get("heads") or {}).get(head_id) or {}
    source = (graph.get("sources") or {}).get(source_id) or {}
    if source.get("kind") == "native_feeder":
        if bool(feed.get("filament_in_ace")):
            raise RuntimeError(
                "%s native load ended in ACE path for %s"
                % (head_id, source_id))
        channel_error = feed.get("channel_error")
        if channel_error not in (None, "", "ok"):
            raise RuntimeError(
                "%s load reported feeder error: %s"
                % (head_id, channel_error))
        _record_head_source(head_id, source_id, reason="operation_load")
        current_state = _source_state_for_parsed(graph, parsed)
        live_head = (current_state.get("heads") or {}).get(head_id) or {}
    elif live_head.get("current_source") != source_id:
        raise RuntimeError(
            "%s ACE load post-check mismatch: expected %s, got %s"
            % (head_id, source_id, live_head.get("current_source")))
    else:
        _record_head_source(head_id, source_id, reason="operation_load")
    if live_head.get("source_confidence") not in ("known",):
        raise RuntimeError(
            "%s load post-check confidence is %s"
            % (head_id, live_head.get("source_confidence")))
    return live_head

def _postcheck_head_unloaded(
        *,
        graph: dict,
        parsed: dict,
        head_id: str,
        source_id: str | None,
        clear_current_source: bool = True,
        require_toolhead_empty: bool = True,
) -> dict:
    head_idx = _head_index_from_id(head_id)
    if head_idx is None:
        raise RuntimeError("invalid head for post-check: %s" % head_id)
    feed = _feed_status_for_head(parsed, head_idx)
    if require_toolhead_empty and bool(feed.get("filament_at_extruder")):
        raise RuntimeError(
            "%s unload completed but toolhead sensor still detects filament"
            % head_id)
    channel_error = feed.get("channel_error")
    if channel_error not in (None, "", "ok"):
        raise RuntimeError(
            "%s unload reported feeder error: %s" % (head_id, channel_error))
    if not clear_current_source:
        return {
            "head": head_id,
            "source": source_id,
            "toolhead_sensor_empty": not bool(feed.get("filament_at_extruder")),
            "current_source_cleared": False,
        }
    _record_head_source(head_id, None, reason="operation_unload")
    current_state = _source_state_for_parsed(graph, parsed)
    live_head = (current_state.get("heads") or {}).get(head_id) or {}
    if live_head.get("source_confidence") not in ("empty",):
        raise RuntimeError(
            "%s unload post-check confidence is %s"
            % (head_id, live_head.get("source_confidence")))
    return live_head

def _postcheck_head_recovered(
        *,
        graph: dict,
        parsed: dict,
        head_id: str,
) -> dict:
    head_idx = _head_index_from_id(head_id)
    if head_idx is None:
        raise RuntimeError("invalid head for post-check: %s" % head_id)
    feed = _feed_status_for_head(parsed, head_idx)
    if bool(feed.get("filament_at_extruder")):
        raise RuntimeError(
            "%s recover completed but toolhead sensor still detects filament"
            % head_id)
    if bool(feed.get("filament_detected")):
        raise RuntimeError(
            "%s recover completed but source sensor still detects filament"
            % head_id)
    _record_head_source(head_id, None, reason="operation_recover")
    current_state = _source_state_for_parsed(graph, parsed)
    live_head = (current_state.get("heads") or {}).get(head_id) or {}
    if live_head.get("source_confidence") not in ("empty",):
        raise RuntimeError(
            "%s recover post-check confidence is %s"
            % (head_id, live_head.get("source_confidence")))
    return live_head

def _postcheck_source_retracted(
        *,
        graph: dict,
        parsed: dict,
        head_id: str,
        source_id: str | None,
        clear_current_source: bool = True,
) -> dict:
    head_idx = _head_index_from_id(head_id)
    if head_idx is None:
        raise RuntimeError("invalid head for post-check: %s" % head_id)
    feed = _feed_status_for_head(parsed, head_idx)
    if bool(feed.get("filament_at_extruder")):
        raise RuntimeError(
            "%s source retract completed but toolhead sensor still detects filament"
            % head_id)
    channel_error = feed.get("channel_error")
    if channel_error not in (None, "", "ok"):
        raise RuntimeError(
            "%s source retract reported feeder error: %s" % (head_id, channel_error))
    if clear_current_source:
        _record_head_source(head_id, None, reason="operation_retract")
        current_state = _source_state_for_parsed(graph, parsed)
        live_head = (current_state.get("heads") or {}).get(head_id) or {}
        if live_head.get("source_confidence") not in ("empty",):
            raise RuntimeError(
                "%s retract post-check confidence is %s"
                % (head_id, live_head.get("source_confidence")))
        return live_head
    return {
        "head": head_id,
        "source": source_id,
        "toolhead_sensor_empty": True,
        "current_source_cleared": False,
    }

def _postcheck_source_full_unloaded(
        *,
        parsed: dict,
        head_id: str,
        source_id: str | None,
) -> dict:
    head_idx = _head_index_from_id(head_id)
    if head_idx is None:
        raise RuntimeError("invalid head for post-check: %s" % head_id)
    feed = _feed_status_for_head(parsed, head_idx)
    channel_error = feed.get("channel_error")
    if channel_error not in (None, "", "ok"):
        raise RuntimeError(
            "%s full unload reported feeder error: %s" % (head_id, channel_error))
    if bool(feed.get("filament_detected")):
        raise RuntimeError(
            "%s full unload completed but source sensor still detects filament"
            % head_id)
    if bool(feed.get("filament_at_extruder")):
        raise RuntimeError(
            "%s full unload completed but toolhead sensor still detects filament"
            % head_id)
    return {
        "head": head_id,
        "source": source_id,
        "source_empty": True,
    }

async def _postcheck_macro_operation(job: dict[str, Any]) -> None:
    if job.get("kind") != "unload_all_heads":
        return
    state = _read_head_source_state()
    state["heads"] = {}
    _write_head_source_state(state)

async def _postcheck_operation_step(
        *,
        graph: dict,
        step: dict[str, Any],
        parsed: dict,
) -> dict | None:
    action = step.get("profile_action")
    kind = step.get("kind")
    if action not in ("load", "unload", "swap", "retract", "full_unload") and kind not in (
            "load_source", "unload_source", "swap_source",
            "retract_source_to_junction", "full_unload_source"):
        return None
    head_id = _step_head_id(step)
    source_id = _step_source_id(step)
    if not head_id:
        raise RuntimeError("operation step missing head")
    if action in ("load", "swap") or kind in ("load_source", "swap_source"):
        if not source_id:
            raise RuntimeError("operation load step missing source")
        return _postcheck_head_loaded(
            graph=graph,
            parsed=parsed,
            head_id=head_id,
            source_id=source_id,
        )
    if action == "unload" or kind == "unload_source":
        return _postcheck_head_unloaded(
            graph=graph,
            parsed=parsed,
            head_id=head_id,
            source_id=source_id,
            clear_current_source=bool(step.get("clears_current_source", True)),
            require_toolhead_empty=bool(step.get("requires_toolhead_empty", True)),
        )
    if action == "retract" or kind == "retract_source_to_junction":
        return _postcheck_source_retracted(
            graph=graph,
            parsed=parsed,
            head_id=head_id,
            source_id=source_id,
            clear_current_source=bool(step.get("clears_current_source", True)),
        )
    if action == "recover" or kind == "recover_head":
        return _postcheck_head_recovered(
            graph=graph,
            parsed=parsed,
            head_id=head_id,
        )
    if action == "full_unload" or kind == "full_unload_source":
        return _postcheck_source_full_unloaded(
            parsed=parsed,
            head_id=head_id,
            source_id=source_id,
        )
    return None

async def _execute_operation_job(job: dict[str, Any]) -> None:
    global _operation_job
    job["status"] = "running"
    job["updated"] = time.time()
    script = _script_from_commands(job.get("commands") or [])
    route_plan = job.get("route_plan") or {}
    try:
        steps = _operation_steps(route_plan)
        if not steps and script:
            steps = [{"command": script, "kind": "script"}]
        parsed = _parse_state(await _query_state())
        graph, meta = _load_source_graph(parsed)
        _route_plan_graph_matches(route_plan, meta)
        motion_errors = _validate_operation_printer_motion_ready(
            route_plan, parsed)
        if motion_errors:
            raise RuntimeError("; ".join(motion_errors))
        if route_plan.get("source_graph_hash"):
            current_state = _source_state_for_parsed(graph, parsed)
            current_sources = sg.runtime_sources(graph, parsed)
            runtime_errors = _validate_operation_route_plan(
                route_plan, graph, meta, current_state, current_sources)
            if runtime_errors:
                raise RuntimeError(
                    "operation runtime validation failed: "
                    + "; ".join(runtime_errors[:8]))
        executed = []
        for index, step in enumerate(steps):
            command = str(step.get("command") or "").strip()
            if not command:
                continue
            if "\n" in command or "\r" in command:
                raise RuntimeError(
                    "operation step command must be a single G-code line")
            _validate_gcode_script(command)
            job["current_step"] = {
                "index": index,
                "kind": step.get("kind"),
                "command": command,
            }
            job["updated"] = time.time()
            await _wait_for_step_quiescent(step)
            result = await _mr_gcode(command, timeout=None)
            parsed = await _wait_for_step_quiescent(step)
            post_state = await _postcheck_operation_step(
                graph=graph,
                step=step,
                parsed=parsed,
            )
            executed.append({
                "index": index,
                "kind": step.get("kind"),
                "profile_action": step.get("profile_action"),
                "source": _step_source_id(step),
                "head": _step_head_id(step),
                "command": command,
                "result": result,
                "post_state": post_state,
            })
            job["steps"] = executed
            job["updated"] = time.time()
        job["current_step"] = None
        await _postcheck_macro_operation(job)
        job["result"] = {"executed_steps": len(executed)}
        job["status"] = "done"
        job["updated"] = time.time()
    except httpx.HTTPStatusError as e:
        job["status"] = "error"
        job["error"] = e.response.text or str(e)
        job["current_step"] = None
        job["updated"] = time.time()
        _trace.warning("operation %s failed for %r: %s",
                       job.get("id"), script, str(job.get("error"))[:300])
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e) or type(e).__name__
        job["current_step"] = None
        job["updated"] = time.time()
        _trace.warning("operation %s failed for %r: %s",
                       job.get("id"), script, job.get("error"))
    finally:
        if _operation_job is job:
            _operation_job = job

async def _start_operation_job(
        *,
        kind: str,
        head: str | None,
        source: str | None,
        previous_source: str | None,
        route_plan: dict,
        execute: bool,
) -> dict[str, Any]:
    global _operation_job
    commands = route_plan.get("commands") or []
    now = time.time()
    async with _operation_lock:
        if execute and _operation_active():
            raise HTTPException(
                status_code=409,
                detail="another hardware operation is already running")
        job = {
            "id": uuid.uuid4().hex,
            "status": "queued" if execute else "preview",
            "kind": kind,
            "head": head,
            "source": source,
            "previous_source": previous_source,
            "commands": commands,
            "route_plan": route_plan,
            "created": now,
            "updated": now,
            "error": None,
        }
        if execute:
            _operation_job = job
            asyncio.create_task(_execute_operation_job(job))
    return job

async def _build_head_load_operation(payload: HeadLoadRequest) -> dict[str, Any]:
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    target = _target_from_graph_source_edge(graph, payload.source, payload.head)
    if target is None:
        raise HTTPException(status_code=404, detail="enabled source edge not found")
    initial_state = _source_state_for_parsed(graph, parsed)
    head_id = target.get("head_id") or "head:%d" % int(target.get("head"))
    head_state = (initial_state.get("heads") or {}).get(head_id) or {}
    if head_state.get("source_confidence") in (
            "unknown", "failed", "exhausted", "stale"):
        raise HTTPException(
            status_code=409,
            detail=(
                "%s current source is %s; recover before loading"
                % (head_id, head_state.get("source_confidence"))))
    event = _source_transition_event(
        index=0,
        target=target,
        initial_state=initial_state,
        graph=graph,
    )
    homing_step = _auto_home_step_if_needed(event, parsed)
    if homing_step:
        event["steps"] = [homing_step] + (event.get("steps") or [])
        event["commands"] = [homing_step["command"]] + (event.get("commands") or [])
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
    route_plan = _finalize_operation_route_plan(route_plan, graph)
    current_sources = sg.runtime_sources(graph, parsed)
    errors = _validate_operation_route_plan(
        route_plan, graph, meta, initial_state, current_sources)
    errors.extend(_validate_operation_printer_motion_ready(route_plan, parsed))
    if errors:
        raise HTTPException(status_code=409, detail="; ".join(errors))
    job = await _start_operation_job(
        kind="head_load",
        head=head_id,
        source=target.get("source"),
        previous_source=event.get("previous_source"),
        route_plan=route_plan,
        execute=payload.execute,
    )
    return {
        "ok": True,
        "operation": _operation_public(job),
        "target": target,
        "event": event,
        "route_plan": route_plan,
    }

async def _build_head_unload_operation(payload: HeadUnloadRequest) -> dict[str, Any]:
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    initial_state = _source_state_for_parsed(graph, parsed)
    head_idx = _head_index_from_id(payload.head)
    if head_idx is None:
        raise HTTPException(status_code=400, detail="invalid head")
    head_id = f"head:{head_idx}"
    head_state = (initial_state.get("heads") or {}).get(head_id) or {}
    current_source = head_state.get("current_source")
    confidence = head_state.get("source_confidence")
    if not current_source or confidence == "empty":
        raise HTTPException(status_code=409, detail=f"{head_id} has no loaded source")
    if confidence in ("unknown", "failed", "stale"):
        raise HTTPException(
            status_code=409,
            detail=f"{head_id} current source is {confidence}; recover before unloading")
    target = _target_from_graph_source_edge(graph, current_source, head_id)
    if target is None:
        raise HTTPException(
            status_code=409,
            detail=f"{head_id} current source {current_source} has no enabled edge")
    step = _profile_step(
        target=target,
        profiles=(graph.get("profiles") or {}),
        action="unload",
    )
    if step is None:
        raise HTTPException(status_code=409, detail="current source cannot unload")
    retract = _native_retract_step_for_target(target, graph.get("profiles") or {})
    if retract:
        step["requires_toolhead_empty"] = False
    if retract and _native_unload_is_at_retract_stage(head_state):
        event = _source_action_event(
            index=0, action="retract", target=target, step=retract)
    else:
        event = _source_action_event(index=0, action="unload", target=target, step=step)
    if retract:
        existing = {id(item) for item in event.get("steps") or []}
        if id(retract) not in existing:
            event["steps"].append(retract)
            if retract.get("command"):
                event["commands"].append(retract.get("command"))
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
    route_plan = _finalize_operation_route_plan(route_plan, graph)
    current_sources = sg.runtime_sources(graph, parsed)
    errors = _validate_operation_route_plan(
        route_plan, graph, meta, initial_state, current_sources)
    if errors:
        raise HTTPException(status_code=409, detail="; ".join(errors))
    job = await _start_operation_job(
        kind="head_unload",
        head=head_id,
        source=current_source,
        previous_source=current_source,
        route_plan=route_plan,
        execute=payload.execute,
    )
    return {
        "ok": True,
        "operation": _operation_public(job),
        "target": target,
        "event": event,
        "route_plan": route_plan,
    }

async def _build_head_recover_operation(payload: HeadRecoverRequest) -> dict[str, Any]:
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    head_idx = _head_index_from_id(payload.head)
    if head_idx is None:
        raise HTTPException(status_code=400, detail="invalid head")
    head_id = f"head:{head_idx}"
    initial_state = _source_state_for_parsed(graph, parsed)
    head_state = (initial_state.get("heads") or {}).get(head_id) or {}
    confidence = head_state.get("source_confidence")
    sensor = bool(head_state.get("sensor_filament"))
    if confidence != "stale":
        raise HTTPException(
            status_code=409,
            detail=(
                f"{head_id} current source is {confidence}; "
                "only stale empty-head recovery can clear mappings"))
    if sensor:
        raise HTTPException(
            status_code=409,
            detail=f"{head_id} still detects filament; unload before recovery")
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
        "events": [{
            "index": 0,
            "event_type": "hardware_operation",
            "action": "recover",
            "head": head_id,
            "steps": [{
                "kind": "recover_head",
                "head": head_id,
                "command": "ACE_CLEAR_HEADS HEAD=%d" % head_idx,
            }],
            "commands": ["ACE_CLEAR_HEADS HEAD=%d" % head_idx],
        }],
        "commands": ["ACE_CLEAR_HEADS HEAD=%d" % head_idx],
    }
    route_plan = _finalize_operation_route_plan(route_plan, graph)
    current_sources = sg.runtime_sources(graph, parsed)
    errors = _validate_operation_route_plan(
        route_plan, graph, meta, initial_state, current_sources)
    if errors:
        raise HTTPException(status_code=409, detail="; ".join(errors))
    job = await _start_operation_job(
        kind="recover_head",
        head=head_id,
        source=None,
        previous_source=head_state.get("current_source"),
        route_plan=route_plan,
        execute=payload.execute,
    )
    return {
        "ok": True,
        "operation": _operation_public(job),
        "route_plan": route_plan,
    }

def _loaded_heads_for_source(initial_state: dict, source_id: str) -> list[str]:
    out = []
    for head_id, head_state in ((initial_state.get("heads") or {}).items()):
        if not isinstance(head_state, dict):
            continue
        if head_state.get("current_source") == source_id:
            out.append(str(head_id))
    return sorted(out)

async def _build_source_full_unload_operation(
        payload: SourceFullUnloadRequest,
) -> dict[str, Any]:
    try:
        parsed = _parse_state(await _query_state())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    graph, meta = _load_source_graph(parsed)
    source_id = str(payload.source or "").strip()
    if source_id not in (graph.get("sources") or {}):
        raise HTTPException(status_code=404, detail=f"unknown source {source_id!r}")
    initial_state = _source_state_for_parsed(graph, parsed)
    loaded_heads = _loaded_heads_for_source(initial_state, source_id)
    if loaded_heads:
        raise HTTPException(
            status_code=409,
            detail=(
                "%s is currently loaded in %s; unload the toolhead first"
                % (source_id, ", ".join(loaded_heads))))
    target = _target_for_source_slot_action(graph, source_id)
    if target is None:
        raise HTTPException(
            status_code=409,
            detail=f"{source_id} has no enabled edge for full unload")
    step = _profile_step(
        target=target,
        profiles=(graph.get("profiles") or {}),
        action="full_unload",
    )
    if step is None:
        raise HTTPException(
            status_code=409,
            detail=f"{source_id} does not support full unload")
    event = _source_action_event(
        index=0,
        action="full_unload",
        target=target,
        step=step,
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
    route_plan = _finalize_operation_route_plan(route_plan, graph)
    current_sources = sg.runtime_sources(graph, parsed)
    errors = _validate_operation_route_plan(
        route_plan, graph, meta, initial_state, current_sources)
    if errors:
        raise HTTPException(status_code=409, detail="; ".join(errors))
    job = await _start_operation_job(
        kind="source_full_unload",
        head=target.get("head_id"),
        source=source_id,
        previous_source=None,
        route_plan=route_plan,
        execute=payload.execute,
    )
    return {
        "ok": True,
        "operation": _operation_public(job),
        "target": target,
        "event": event,
        "route_plan": route_plan,
    }

def _macro_command(name: str, args: dict[str, Any] | None = None) -> str:
    _validate_macro_request(name, args)
    parts = [str(name).strip()]
    for key, value in (args or {}).items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)

async def _build_macro_operation(
        *,
        kind: str,
        command: str,
        execute: bool,
        head: str | None = None,
        source: str | None = None,
) -> dict[str, Any]:
    route_plan = {
        "version": 2,
        "events": [{
            "index": 0,
            "event_type": "hardware_operation",
            "action": kind,
            "head": head,
            "source": source,
            "steps": [{
                "kind": kind,
                "head": head,
                "source": source,
                "command": command,
            }],
            "commands": [command],
        }],
        "commands": [command],
    }
    errors = []
    try:
        _validate_gcode_script(command)
    except HTTPException as exc:
        errors.append(str(exc.detail))
    if errors:
        raise HTTPException(status_code=409, detail="; ".join(errors))
    job = await _start_operation_job(
        kind=kind,
        head=head,
        source=source,
        previous_source=None,
        route_plan=route_plan,
        execute=execute,
    )
    return {
        "ok": True,
        "operation": _operation_public(job),
        "route_plan": route_plan,
    }

async def _build_ace_dry_start_operation(payload: AceDryStartRequest) -> dict[str, Any]:
    ace = int(payload.ace)
    if ace < 0 or ace > 7:
        raise HTTPException(status_code=400, detail="ace must be 0..7")
    args: dict[str, Any] = {"ACE": ace}
    if payload.temp is not None:
        temp = float(payload.temp)
        if temp < 35 or temp > 80:
            raise HTTPException(status_code=400, detail="temp must be 35..80")
        args["TEMP"] = int(temp)
    if payload.duration is not None:
        duration = float(payload.duration)
        if duration <= 0 or duration > 600:
            raise HTTPException(status_code=400, detail="duration must be 1..600")
        args["DURATION"] = int(duration)
    return await _build_macro_operation(
        kind="ace_dry_start",
        command=_macro_command("ACE_DRY", args),
        execute=payload.execute,
        source=f"ace:{ace}",
    )

async def _build_ace_dry_stop_operation(payload: AceDryStopRequest) -> dict[str, Any]:
    args: dict[str, Any] = {}
    source = None
    if payload.ace is not None:
        ace = int(payload.ace)
        if ace < 0 or ace > 7:
            raise HTTPException(status_code=400, detail="ace must be 0..7")
        args["ACE"] = ace
        source = f"ace:{ace}"
    return await _build_macro_operation(
        kind="ace_dry_stop",
        command=_macro_command("ACE_STOP_DRYING", args),
        execute=payload.execute,
        source=source,
    )

async def _build_unload_all_operation(payload: OperationExecuteRequest) -> dict[str, Any]:
    return await _build_macro_operation(
        kind="unload_all_heads",
        command=_macro_command("ACE_UNLOAD_ALL_HEADS", {}),
        execute=payload.execute,
    )

@app.get("/api/operation/current")
async def get_current_operation() -> dict:
    return {"operation": _operation_public(_operation_job)}

@app.post("/api/operation/head/load")
async def operation_head_load(payload: HeadLoadRequest) -> dict:
    return await _build_head_load_operation(payload)

@app.post("/api/operation/head/unload")
async def operation_head_unload(payload: HeadUnloadRequest) -> dict:
    return await _build_head_unload_operation(payload)

@app.post("/api/operation/head/recover")
async def operation_head_recover(payload: HeadRecoverRequest) -> dict:
    return await _build_head_recover_operation(payload)

@app.post("/api/source/full-unload")
async def operation_source_full_unload(payload: SourceFullUnloadRequest) -> dict:
    return await _build_source_full_unload_operation(payload)

@app.post("/api/operation/ace/dry-start")
async def operation_ace_dry_start(payload: AceDryStartRequest) -> dict:
    return await _build_ace_dry_start_operation(payload)

@app.post("/api/operation/ace/dry-stop")
async def operation_ace_dry_stop(payload: AceDryStopRequest) -> dict:
    return await _build_ace_dry_stop_operation(payload)

@app.post("/api/operation/unload-all")
async def operation_unload_all(payload: OperationExecuteRequest) -> dict:
    return await _build_unload_all_operation(payload)

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
        _validate_direct_macro_request(c.name, c.args)
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
    _validate_direct_macro_request(req.name, req.args)
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
    _validate_direct_macro_request(req.name, req.args)
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

def _native_override_meta_at(n: int) -> dict | None:
    o = _native_overrides.get(str(n)) or _native_overrides.get(n)
    if not isinstance(o, dict):
        return None
    mat = (o.get("material") or "").strip()
    color = (o.get("color") or "").strip()
    if not mat and not color:
        return None
    return {
        "material": mat,
        "sku": (o.get("subtype") or "").strip(),
        "brand": (o.get("brand") or "").strip(),
        "color": color.lower() if color else None,
    }

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
                    payload = _parse_state(status, source_graph_overlay=True)
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
