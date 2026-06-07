#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


WEB = "http://127.0.0.1:7126/api"
MOONRAKER = "http://127.0.0.1:7125"


def request(method: str, url: str, data: bytes | None = None,
            headers: dict[str, str] | None = None,
            expect_status: int = 200) -> dict:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    if status != expect_status:
        detail = raw.decode("utf-8", errors="replace") if raw else ""
        raise RuntimeError(
            f"{method} {url} expected HTTP {expect_status}, got {status}: {detail}")
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def post_json(url: str, payload: dict[str, Any], expect_status: int = 200) -> dict:
    return request(
        "POST",
        url,
        json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json"},
        expect_status=expect_status,
    )


def multipart_upload(name: str, gcode: str, expect_status: int = 200) -> dict:
    boundary = "----colorful-u1-dryrun"
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / name
        path.write_text(gcode, encoding="utf-8")
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return request(
        "POST",
        f"{WEB}/preflight",
        body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        expect_status=expect_status,
    )


def assert_true(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def reset_default() -> None:
    request("POST", f"{MOONRAKER}/dry-run/reset")
    scenario = {
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "native_heads": [
            {"head": 1, "material": "PLA", "color": "#dc2828"},
        ],
        "slots": [
            {"ace": 0, "slot": 0, "material": "PLA", "color": "#dc2828"},
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
            {"ace": 0, "slot": 2, "material": "PLA", "color": "#f5d23c"},
            {"ace": 0, "slot": 3, "material": "ABS", "color": "#28c878"},
        ],
    }
    post_json(f"{MOONRAKER}/dry-run/scenario", scenario)
    push_source_graph(scenario)


def set_scenario(payload: dict[str, Any]) -> None:
    post_json(f"{MOONRAKER}/dry-run/scenario", payload)
    push_source_graph(payload)


def source_graph_for(payload: dict[str, Any]) -> dict[str, Any]:
    ace_count = max(1, min(8, int(payload.get("ace_device_count") or 1)))
    heads = {}
    sources = {}
    edges = []
    channels = {
        0: {"module": "left", "channel": 1},
        1: {"module": "left", "channel": 0},
        2: {"module": "right", "channel": 0},
        3: {"module": "right", "channel": 1},
    }
    for head in range(4):
        heads[f"head:{head}"] = {
            "index": head,
            "enabled": True,
            "label": f"T{head}",
            "native_channel": channels[head],
        }
        sources[f"native:{head}"] = {
            "kind": "native_feeder",
            "head": head,
            "module": channels[head]["module"],
            "channel": channels[head]["channel"],
            "label": f"Native T{head}",
            "material": "",
            "brand": "",
            "subtype": "",
            "color": "",
            "ready": False,
            "execution_profile": "u1_native_feeder",
        }
        edges.append({
            "source": f"native:{head}",
            "head": f"head:{head}",
            "enabled": True,
            "priority": 10,
        })
    for ace in range(ace_count):
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
                "execution_profile": "ace_v1_slot",
            }

    head_modes = payload.get("head_modes") or {
        "0": "ace", "1": "native", "2": "native", "3": "native",
    }
    ace_targets = payload.get("ace_targets") or {"0": 0}
    for ace in range(ace_count):
        target = ace_targets.get(str(ace), ace_targets.get(ace))
        try:
            head = int(target)
        except (TypeError, ValueError):
            continue
        if not 0 <= head < 4:
            continue
        if str(head_modes.get(str(head), head_modes.get(head, ""))).lower() != "ace":
            continue
        for slot in range(4):
            edges.append({
                "source": f"ace:{ace}:{slot}",
                "head": f"head:{head}",
                "enabled": True,
                "priority": 50,
                "constraints": {
                    "requires_empty_head_before_load": True,
                    "allows_preload_while_other_head_prints": True,
                },
            })

    return {
        "version": 1,
        "heads": heads,
        "sources": sources,
        "edges": edges,
        "profiles": {
            "ace_v1_slot": {
                "kind": "ace_slot",
                "capabilities": {
                    "can_preload": True,
                    "can_swap_in_print": True,
                    "requires_source_tracking": True,
                },
            },
            "u1_native_feeder": {
                "kind": "native_feeder",
                "capabilities": {
                    "can_preload": False,
                    "can_swap_in_print": False,
                    "requires_source_tracking": False,
                },
            },
        },
    }


def push_source_graph(payload: dict[str, Any]) -> None:
    push_graph(source_graph_for(payload))


def push_graph(graph: dict[str, Any]) -> None:
    mr_result = post_json(f"{MOONRAKER}/dry-run/source-graph", {"graph": graph})
    assert_true(mr_result.get("ok"), f"moonraker source graph save failed: {mr_result}")
    result = post_json(f"{WEB}/source-graph", {"graph": graph})
    assert_true(result.get("ok"), f"source graph save failed: {result}")


def gcode(types: str, colors: str, body: str) -> str:
    return (
        "; Colorful-U1 dry-run preflight regression\n"
        f"; filament_type = {types}\n"
        f"; filament_colour = {colors}\n"
        f"{textwrap.dedent(body).strip()}\n"
    )


def entries_by_tool(report: dict) -> dict[int, dict]:
    entries = (report.get("source_map") or {}).get("entries") or []
    return {int(e["slicer_tool"]): e for e in entries}


def target_for(report: dict, tool: int) -> dict:
    return entries_by_tool(report)[tool].get("target") or {}


def assert_target_matches(target: dict, expected: dict) -> None:
    for key, value in expected.items():
        assert_true(target.get(key) == value,
                    f"target field {key!r} mismatch: expected {value!r}, got {target}")
    assert_true(target.get("source"), f"target missing source id: {target}")
    assert_true(target.get("key") == "%s->head:%d" % (
        target.get("source"), int(target.get("head"))),
        f"target key should be source edge id: {target}")
    edge = target.get("edge") or {}
    assert_true(edge.get("source") == target.get("source"),
                f"target edge source mismatch: {target}")
    assert_true(edge.get("head") == "head:%d" % int(target.get("head")),
                f"target edge head mismatch: {target}")
    assert_true(target.get("execution_profile"),
                f"target missing execution profile: {target}")


def commands_for(report: dict, tool: int) -> list[str]:
    return entries_by_tool(report)[tool].get("commands") or []


def assert_route_plan_shape(report: dict) -> None:
    route_plan = report.get("route_plan") or {}
    source_map = report.get("source_map") or {}
    graph_meta = source_map.get("source_graph") or {}
    assert_true(route_plan.get("version") == 2,
                f"route plan should use v2 schema: {route_plan}")
    assert_true(route_plan.get("source_graph_hash") == graph_meta.get("hash"),
                f"route plan graph hash mismatch: {route_plan}")
    initial_state = route_plan.get("initial_state") or {}
    assert_true(initial_state.get("source_graph_hash") == route_plan.get("source_graph_hash"),
                f"route plan initial state graph hash mismatch: {route_plan}")
    heads = initial_state.get("heads") or {}
    assert_true(set(heads.keys()) >= {"head:0", "head:1", "head:2", "head:3"},
                f"route plan initial state missing heads: {route_plan}")
    for head_id, state in heads.items():
        assert_true(state.get("head") == head_id,
                    f"initial state head id mismatch: {state}")
        assert_true("current_source" in state,
                    f"initial state missing current source: {state}")
        assert_true(state.get("source_confidence"),
                    f"initial state missing confidence: {state}")
    events = route_plan.get("events") or []
    assert_true(events, f"route plan missing events: {route_plan}")
    for event in events:
        assert_true(event.get("event_type") == "tool_select",
                    f"route event missing event type: {event}")
        assert_true(event.get("source"), f"route event missing source: {event}")
        assert_true(str(event.get("head") or "").startswith("head:"),
                    f"route event missing graph head id: {event}")
        edge = event.get("edge") or {}
        assert_true(edge.get("source") == event.get("source"),
                    f"route event edge source mismatch: {event}")
        assert_true(edge.get("head") == event.get("head"),
                    f"route event edge head mismatch: {event}")
        assert_true("source_changed" in event,
                    f"route event missing source_changed: {event}")
        steps = event.get("steps") or []
        assert_true(steps, f"route event missing structured steps: {event}")
        for step in steps:
            assert_true(step.get("kind"), f"route step missing kind: {event}")
        swap_steps = [s for s in steps if s.get("kind") == "swap_source"]
        for step in swap_steps:
            assert_true(step.get("profile"), f"swap step missing profile: {step}")
            assert_true(step.get("profile_action") == "swap",
                        f"swap step missing profile action: {step}")
        step_commands = [
            step.get("command")
            for step in steps
            if isinstance(step, dict) and step.get("command")
        ]
        assert_true(step_commands == event.get("commands"),
                    f"route event commands should mirror steps: {event}")
        assert_true(event.get("commands"), f"route event missing commands: {event}")


def start_print(report: dict, tool_targets: dict | None = None,
                expect_status: int = 200) -> dict:
    payload = {
        "token": report["token"],
        "mode": "slicer",
        "tool_targets": report.get("tool_targets") if tool_targets is None else tool_targets,
    }
    return post_json(f"{WEB}/preflight/print", payload, expect_status=expect_status)


def wait_job(job_id: str) -> dict:
    status: dict = {}
    for _ in range(80):
        status = request(
            "GET",
            f"{WEB}/preflight/print/status?job_id={urllib.parse.quote(job_id)}",
        )
        if status.get("done"):
            break
        time.sleep(0.25)
    assert_true(status.get("done"), f"print job did not finish: {status}")
    assert_true(not status.get("error"), f"print job failed: {status.get('error')}")
    return status


def wait_job_error(job_id: str, needle: str) -> dict:
    status: dict = {}
    for _ in range(80):
        status = request(
            "GET",
            f"{WEB}/preflight/print/status?job_id={urllib.parse.quote(job_id)}",
        )
        if status.get("done"):
            break
        time.sleep(0.25)
    assert_true(status.get("done"), f"print job did not finish: {status}")
    error = str(status.get("error") or "")
    assert_true(needle.lower() in error.lower(),
                f"print job error should contain {needle!r}, got {status}")
    return status


def uploaded_content(filename: str) -> str:
    uploaded = request(
        "GET",
        f"{MOONRAKER}/dry-run/uploaded/{urllib.parse.quote(filename)}",
    )
    return uploaded.get("content") or ""


def load_postprocessor():
    path = Path(__file__).resolve().parents[1] / "tools" / "post_process_virtual_toolheads.py"
    spec = importlib.util.spec_from_file_location("multiace_postprocess_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def run_script(script: str, expect_status: int = 200) -> dict:
    return post_json(
        f"{MOONRAKER}/printer/gcode/script",
        {"script": script},
        expect_status=expect_status,
    )


def moonraker_state() -> dict:
    return request("GET", f"{MOONRAKER}/printer/objects/query")["result"]["status"]


def assert_source_map_shape(report: dict) -> None:
    source_map = report.get("source_map") or {}
    graph_meta = source_map.get("source_graph") or {}
    assert_true(str(graph_meta.get("hash") or "").startswith("sha256:"),
                f"source map missing source graph hash: {source_map}")
    stats = source_map.get("swap_stats") or {}
    assert_true("tool_events" in stats, f"source map missing swap stats: {source_map}")
    suggestion = source_map.get("optimization_suggestion") or {}
    assert_true("feasible" in suggestion,
                f"optimization suggestion missing feasibility: {suggestion}")
    assert_true("current" in suggestion,
                f"optimization suggestion missing current stats: {suggestion}")
    if suggestion.get("feasible"):
        assert_true("suggested" in suggestion and "tool_targets" in suggestion,
                    f"feasible suggestion missing suggested mapping: {suggestion}")


def test_mixed_native_ace_print() -> None:
    reset_default()
    report = multipart_upload(
        "mixed_native_ace.gcode",
        gcode(
            "PLA;PETG",
            "#dc2828;#1e78dc",
            """
            T0
            G92 E0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            ; Change Tool 1 -> Tool 0
            T0
            G1 X20 Y10 E1
            """,
        ),
    )
    assert_source_map_shape(report)
    assert_route_plan_shape(report)
    assert_true(len(entries_by_tool(report)) == 2, "mixed case should map two tools")
    t0 = target_for(report, 0)
    t1 = target_for(report, 1)
    assert_target_matches(t0, {"kind": "native", "head": 1, "source": "native:1"})
    assert_target_matches(t1, {
        "kind": "ace", "head": 0, "source": "ace:0:1", "ace": 0, "slot": 1})
    assert_true(commands_for(report, 0) == ["T1"],
                f"T0 command preview mismatch: {commands_for(report, 0)}")
    assert_true(commands_for(report, 1) == ["T0", "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1"],
                f"T1 command preview mismatch: {commands_for(report, 1)}")
    stats = (report.get("source_map") or {}).get("swap_stats") or {}
    assert_true(stats.get("tool_events") == 2, f"expected 2 tool events, got {stats}")
    assert_true(stats.get("active_ace_swaps") == 1,
                f"expected 1 active ACE swap, got {stats}")
    assert_true(stats.get("skipped_same_ace") == 0,
                f"expected 0 same-source skips, got {stats}")
    assert_true(stats.get("estimated_swap_seconds_min") == 120,
                f"unexpected min swap estimate: {stats}")
    assert_true(stats.get("estimated_swap_seconds_max") == 240,
                f"unexpected max swap estimate: {stats}")

    saved_map = request("GET", f"{WEB}/preflight/source-map?token={report['token']}")
    assert_true(saved_map.get("entries") == (report.get("source_map") or {}).get("entries"),
                "saved source map differs from preflight response")
    saved_plan = request("GET", f"{WEB}/preflight/route-plan?token={report['token']}")
    assert_true(saved_plan.get("events") == (report.get("route_plan") or {}).get("events"),
                "saved route plan differs from preflight response")
    validation = request(
        "GET",
        f"{WEB}/preflight/route-plan/validate?token={report['token']}",
    )
    assert_true(validation.get("ok"), f"saved route plan should validate: {validation}")
    started = start_print(report)
    status = wait_job(started["job_id"])
    assert_true(
        (status.get("route_plan") or {}).get("events")
        == (report.get("route_plan") or {}).get("events"),
        "print status route plan differs from preflight response")
    final_stats = ((status.get("source_map") or {}).get("swap_stats") or {})
    assert_true(final_stats.get("active_ace_swaps") == 1,
                f"final source map lost swap stats: {final_stats}")
    content = uploaded_content(started["filename"])
    assert_true("T1" in content, "rewritten upload missing native T1")
    assert_true("ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1" in content,
                "rewritten upload missing ACE slot1 swap")


def test_native_only_print() -> None:
    set_scenario({
        "head_modes": {"0": "native", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": None},
        "native_heads": [
            {"head": 0, "material": "PLA", "color": "#dc2828"},
            {"head": 1, "material": "PETG", "color": "#1e78dc"},
        ],
    })
    report = multipart_upload(
        "native_only.gcode",
        gcode(
            "PLA;PETG",
            "#dc2828;#1e78dc",
            """
            T0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            """,
        ),
    )
    assert_source_map_shape(report)
    assert_route_plan_shape(report)
    assert_target_matches(
        target_for(report, 0),
        {"kind": "native", "head": 0, "source": "native:0"})
    assert_target_matches(
        target_for(report, 1),
        {"kind": "native", "head": 1, "source": "native:1"})
    stats = (report.get("source_map") or {}).get("swap_stats") or {}
    assert_true(stats.get("active_ace_swaps") == 0,
                f"native-only should not estimate ACE swaps: {stats}")
    started = start_print(report)
    content = uploaded_content(wait_job(started["job_id"])["filename"])
    assert_true("ACE_SWAP_HEAD" not in content,
                "native-only upload should not contain ACE_SWAP_HEAD")


def test_single_ace_head_print() -> None:
    set_scenario({
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "native_heads": [],
        "slots": [
            {"ace": 0, "slot": 0, "material": "PLA", "color": "#dc2828"},
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
    })
    report = multipart_upload(
        "single_ace.gcode",
        gcode(
            "PLA;PETG",
            "#dc2828;#1e78dc",
            """
            T0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            ; Change Tool 1 -> Tool 0
            T0
            G1 X20 Y10 E1
            """,
        ),
    )
    assert_source_map_shape(report)
    assert_route_plan_shape(report)
    assert_target_matches(
        target_for(report, 0),
        {"kind": "ace", "head": 0, "source": "ace:0:0", "ace": 0, "slot": 0})
    assert_target_matches(
        target_for(report, 1),
        {"kind": "ace", "head": 0, "source": "ace:0:1", "ace": 0, "slot": 1})
    stats = (report.get("source_map") or {}).get("swap_stats") or {}
    assert_true(stats.get("active_ace_swaps") == 2,
                f"single ACE should estimate two active swaps: {stats}")
    started = start_print(report)
    content = uploaded_content(wait_job(started["job_id"])["filename"])
    assert_true("ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=0" in content,
                "single ACE upload missing slot0 swap")
    assert_true("ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1" in content,
                "single ACE upload missing slot1 swap")


def test_unmapped_tool_is_not_feasible() -> None:
    set_scenario({
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "native_heads": [{"head": 1, "material": "PLA", "color": "#dc2828"}],
        "slots": [{"ace": 0, "slot": 0, "material": "PLA", "color": "#dc2828"}],
    })
    report = multipart_upload(
        "unmapped.gcode",
        gcode(
            "TPU",
            "#444444",
            """
            T0
            G1 X10 Y10 E1
            """,
        ),
    )
    assert_true(report.get("resolve_errors"),
                f"unmapped tool should report resolver errors: {report}")
    plan = (report.get("plans") or {}).get("slicer") or {}
    assert_true(not plan.get("feasible"), f"unmapped plan should be infeasible: {plan}")


def test_invalid_bambu_p1s_gcode_blocked() -> None:
    reset_default()
    err = multipart_upload(
        "invalid_bambu_p1s.gcode",
        """
        ; generated by Snapmaker Orca 2.3.1
        ;===== machine: P1S ========================
        ; filament_type = PLA
        ; filament_colour = #dc2828
        G91
        G380 S2 Z-25 F300
        M620 S0A
        T0
        """,
        expect_status=400,
    )
    detail = err.get("detail") or {}
    validation = detail.get("validation") or {}
    assert_true(not validation.get("ok"),
                f"invalid P1S file should fail safety validation: {err}")
    errors = validation.get("errors") or []
    assert_true(any(e.get("kind") == "machine_signature" for e in errors),
                f"invalid P1S file should report machine signature: {err}")
    commands = {e.get("command") for e in errors if e.get("command")}
    assert_true({"G380", "M620"}.issubset(commands),
                f"invalid P1S file should report dangerous commands: {err}")
    assert_true("G-code rejected" in str(detail.get("message") or ""),
                f"invalid P1S error should include user-facing message: {err}")


def test_manual_mapping_can_reuse_source_edge() -> None:
    reset_default()
    report = multipart_upload(
        "reuse_source_edge_manual.gcode",
        gcode(
            "PLA;PLA",
            "#dc2828;#dc2828",
            """
            T0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            """,
        ),
    )
    native_target = target_for(report, 0)
    assert_target_matches(
        native_target,
        {"kind": "native", "head": 1, "source": "native:1"})
    started = start_print(
        report,
        tool_targets={"0": native_target, "1": native_target},
    )
    content = uploaded_content(wait_job(started["job_id"])["filename"])
    assert_true("T1" in content, "manual reused native source should select T1")
    assert_true("ACE_SWAP_HEAD" not in content,
                "manual reused native source should not emit ACE swaps")


def test_wrong_feed_auto_channel_rejected() -> None:
    set_scenario({
        "head_modes": {"0": "native", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": None},
    })
    err = run_script("FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 LOAD=1",
                     expect_status=400)
    assert_true("route mismatch" in str(err),
                f"wrong FEED_AUTO channel should be rejected: {err}")


def test_source_action_profile_preview() -> None:
    reset_default()
    native_load = post_json(f"{WEB}/source-action/preview", {
        "source": "native:1",
        "head": "head:1",
        "action": "load",
    })
    assert_true(
        native_load.get("command")
        == "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=1 LOAD=1",
        f"native load preview mismatch: {native_load}")
    native_step = native_load.get("step") or {}
    assert_true(native_step.get("profile") == "u1_native_feeder",
                f"native load should use native profile: {native_load}")
    assert_true(native_step.get("profile_action") == "load",
                f"native load action mismatch: {native_load}")
    native_event = native_load.get("event") or {}
    assert_true(native_event.get("event_type") == "source_action",
                f"native load should return source_action event: {native_load}")
    assert_true((native_event.get("commands") or []) == [native_load.get("command")],
                f"native load event commands mismatch: {native_load}")

    native_unload = post_json(f"{WEB}/source-action/preview", {
        "source": "native:1",
        "head": 1,
        "action": "unload",
    })
    assert_true(
        native_unload.get("command")
        == "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=1 UNLOAD=1",
        f"native unload preview mismatch: {native_unload}")

    ace_swap = post_json(f"{WEB}/source-action/preview", {
        "source": "ace:0:1",
        "head": "head:0",
        "action": "swap",
    })
    assert_true(
        ace_swap.get("command") == "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1",
        f"ACE swap preview mismatch: {ace_swap}")

    batch = post_json(f"{WEB}/source-actions/preview", {
        "actions": [
            {"source": "native:1", "head": "head:1", "action": "unload"},
            {"source": "native:1", "head": "head:1", "action": "load"},
            {"source": "ace:0:1", "head": "head:0", "action": "swap"},
        ],
    })
    assert_true(batch.get("commands") == [
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=1 UNLOAD=1",
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=1 LOAD=1",
        "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1",
    ], f"source action batch command mismatch: {batch}")
    route_plan = batch.get("route_plan") or {}
    assert_true(route_plan.get("version") == 2,
                f"source action batch should return route plan v2: {batch}")
    assert_true((route_plan.get("initial_state") or {}).get("source_graph_hash")
                == route_plan.get("source_graph_hash"),
                f"source action batch should include initial state: {batch}")
    events = route_plan.get("events") or []
    assert_true([e.get("event_type") for e in events] == [
        "source_action", "source_action", "source_action"],
        f"source action batch event types mismatch: {batch}")
    validation = post_json(f"{WEB}/route-plan/validate", {
        "route_plan": route_plan,
    })
    assert_true(validation.get("ok"),
                f"source action route plan should validate: {validation}")
    bad_plan = dict(route_plan)
    bad_plan["source_graph_hash"] = "sha256:bad"
    bad_validation = post_json(f"{WEB}/route-plan/validate", {
        "route_plan": bad_plan,
    })
    assert_true(not bad_validation.get("ok"),
                f"route plan validate should reject bad hash: {bad_validation}")
    assert_true("hash mismatch" in "; ".join(bad_validation.get("errors") or []).lower(),
                f"route plan validate hash error mismatch: {bad_validation}")


def test_source_transition_preview_unloads_previous_source() -> None:
    scenario = {
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "slots": [
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
        "head_sources": [
            {"head": 0, "ace": 0, "slot": 1, "material": "PETG", "color": "1E78DC"},
        ],
    }
    set_scenario(scenario)
    graph = source_graph_for(scenario)
    graph["edges"].append({
        "source": "native:1",
        "head": "head:0",
        "enabled": True,
        "priority": 20,
    })
    push_graph(graph)
    preview = post_json(f"{WEB}/source-transition/preview", {
        "source": "native:1",
        "head": "head:0",
    })
    assert_true(preview.get("ok"), f"source transition preview should validate: {preview}")
    commands = preview.get("commands") or []
    assert_true(commands == [
        "ACE_UNLOAD_HEAD HEAD=0",
        "T0",
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 LOAD=1",
    ], f"source transition commands mismatch: {preview}")
    event = preview.get("event") or {}
    assert_true(event.get("previous_source") == "ace:0:1",
                f"transition should record previous source: {preview}")
    assert_true([s.get("kind") for s in event.get("steps") or []] == [
        "unload_source", "select_head", "load_source"],
        f"transition step order mismatch: {preview}")


def test_route_plan_rewrite_includes_source_transition() -> None:
    scenario = {
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "native_heads": [
            {"head": 1, "material": "PLA", "color": "#dc2828"},
        ],
        "slots": [
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
        "head_sources": [
            {"head": 0, "ace": 0, "slot": 1, "material": "PETG", "color": "1E78DC"},
        ],
    }
    set_scenario(scenario)
    graph = source_graph_for(scenario)
    graph["edges"].append({
        "source": "native:1",
        "head": "head:0",
        "enabled": True,
        "priority": 20,
    })
    push_graph(graph)
    report = multipart_upload(
        "source_transition_print.gcode",
        gcode(
            "PETG;PLA",
            "#1e78dc;#dc2828",
            """
            T0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            """,
        ),
    )
    ace_target = {
        "kind": "ace", "head": 0, "source": "ace:0:1",
        "ace": 0, "slot": 1,
    }
    native_target = {
        "kind": "native", "head": 0, "source": "native:1",
    }
    started = start_print(
        report,
        tool_targets={"0": ace_target, "1": native_target},
    )
    status = wait_job(started["job_id"])
    route_events = (status.get("route_plan") or {}).get("events") or []
    transition = route_events[-1]
    assert_true(transition.get("previous_source") == "ace:0:1",
                f"print route plan should record previous source: {transition}")
    assert_true([s.get("kind") for s in transition.get("steps") or []] == [
        "unload_source", "select_head", "load_source"],
        f"print route plan transition order mismatch: {transition}")
    content = uploaded_content(status["filename"])
    assert_true("ACE_UNLOAD_HEAD HEAD=0" in content,
                f"rewritten upload missing source unload: {content}")
    assert_true("FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 LOAD=1" in content,
                f"rewritten upload missing native source load: {content}")


def _event_commands_for(status: dict, index: int) -> list[str]:
    route_events = (status.get("route_plan") or {}).get("events") or []
    assert_true(len(route_events) > index,
                f"missing route event {index}: {route_events}")
    event = route_events[index]
    commands = event.get("commands") or []
    assert_true(commands,
                f"route event {index} missing commands: {event}")
    return commands


def _content_contains_in_order(content: str, needles: list[str]) -> bool:
    pos = -1
    for needle in needles:
        found = content.find(needle, pos + 1)
        if found < 0:
            return False
        pos = found
    return True


def test_route_plan_rewrite_native_to_ace_transition() -> None:
    scenario = {
        "head_modes": {"0": "native", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": None},
        "native_heads": [
            {"head": 1, "material": "PLA", "color": "#dc2828"},
        ],
        "slots": [
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
    }
    set_scenario(scenario)
    graph = source_graph_for(scenario)
    graph["edges"].append({
        "source": "native:1",
        "head": "head:0",
        "enabled": True,
        "priority": 20,
    })
    graph["edges"].append({
        "source": "ace:0:1",
        "head": "head:0",
        "enabled": True,
        "priority": 50,
        "constraints": {
            "requires_empty_head_before_load": True,
            "allows_preload_while_other_head_prints": True,
        },
    })
    push_graph(graph)
    report = multipart_upload(
        "source_transition_native_to_ace.gcode",
        gcode(
            "PLA;PETG",
            "#dc2828;#1e78dc",
            """
            T0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            """,
        ),
    )
    started = start_print(
        report,
        tool_targets={
            "0": {"kind": "native", "head": 0, "source": "native:1"},
            "1": {
                "kind": "ace", "head": 0, "source": "ace:0:1",
                "ace": 0, "slot": 1,
            },
        },
    )
    status = wait_job(started["job_id"])
    commands = _event_commands_for(status, 1)
    assert_true(commands == [
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 UNLOAD=1",
        "T0",
        "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1",
    ], f"native -> ACE transition commands mismatch: {commands}")
    content = uploaded_content(status["filename"])
    assert_true(_content_contains_in_order(content, commands),
                f"rewritten upload missing native -> ACE order: {content}")


def test_route_plan_rewrite_ace_to_ace_transition() -> None:
    scenario = {
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "slots": [
            {"ace": 0, "slot": 0, "material": "PLA", "color": "#dc2828"},
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
        "head_sources": [
            {"head": 0, "ace": 0, "slot": 0, "material": "PLA", "color": "DC2828"},
        ],
    }
    set_scenario(scenario)
    report = multipart_upload(
        "source_transition_ace_to_ace.gcode",
        gcode(
            "PLA;PETG",
            "#dc2828;#1e78dc",
            """
            T0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            """,
        ),
    )
    started = start_print(
        report,
        tool_targets={
            "0": {
                "kind": "ace", "head": 0, "source": "ace:0:0",
                "ace": 0, "slot": 0,
            },
            "1": {
                "kind": "ace", "head": 0, "source": "ace:0:1",
                "ace": 0, "slot": 1,
            },
        },
    )
    status = wait_job(started["job_id"])
    commands = _event_commands_for(status, 1)
    assert_true(commands == [
        "T0",
        "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1",
    ], f"ACE -> ACE transition should be handled by ACE_SWAP_HEAD: {commands}")
    content = uploaded_content(status["filename"])
    assert_true("ACE_UNLOAD_HEAD HEAD=0" not in content,
                f"ACE -> ACE rewrite should not emit separate ACE unload: {content}")
    assert_true(_content_contains_in_order(content, commands),
                f"rewritten upload missing ACE -> ACE order: {content}")


def test_route_plan_rewrite_native_to_native_transition() -> None:
    scenario = {
        "head_modes": {"0": "native", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": None},
        "native_heads": [
            {"head": 1, "material": "PLA", "color": "#dc2828"},
            {"head": 2, "material": "PETG", "color": "#1e78dc"},
        ],
    }
    set_scenario(scenario)
    graph = source_graph_for(scenario)
    graph["edges"].append({
        "source": "native:1",
        "head": "head:0",
        "enabled": True,
        "priority": 20,
    })
    graph["edges"].append({
        "source": "native:2",
        "head": "head:0",
        "enabled": True,
        "priority": 30,
    })
    push_graph(graph)
    report = multipart_upload(
        "source_transition_native_to_native.gcode",
        gcode(
            "PLA;PETG",
            "#dc2828;#1e78dc",
            """
            T0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            """,
        ),
    )
    started = start_print(
        report,
        tool_targets={
            "0": {"kind": "native", "head": 0, "source": "native:1"},
            "1": {"kind": "native", "head": 0, "source": "native:2"},
        },
    )
    status = wait_job(started["job_id"])
    commands = _event_commands_for(status, 1)
    assert_true(commands == [
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 UNLOAD=1",
        "T0",
        "FEED_AUTO MODULE=right CHANNEL=0 EXTRUDER=0 LOAD=1",
    ], f"native -> native transition commands mismatch: {commands}")
    content = uploaded_content(status["filename"])
    assert_true(_content_contains_in_order(content, commands),
                f"rewritten upload missing native -> native order: {content}")


def test_route_plan_rewrite_repeated_tool_sequence() -> None:
    scenario = {
        "head_modes": {"0": "native", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": None},
        "native_heads": [
            {"head": 1, "material": "PLA", "color": "#dc2828"},
            {"head": 2, "material": "ABS", "color": "#28c878"},
        ],
        "slots": [
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
    }
    set_scenario(scenario)
    graph = source_graph_for(scenario)
    graph["edges"].append({
        "source": "native:1",
        "head": "head:0",
        "enabled": True,
        "priority": 20,
    })
    graph["edges"].append({
        "source": "native:2",
        "head": "head:0",
        "enabled": True,
        "priority": 30,
    })
    graph["edges"].append({
        "source": "ace:0:1",
        "head": "head:0",
        "enabled": True,
        "priority": 50,
        "constraints": {
            "requires_empty_head_before_load": True,
            "allows_preload_while_other_head_prints": True,
        },
    })
    push_graph(graph)
    report = multipart_upload(
        "source_transition_repeated_tools.gcode",
        gcode(
            "PLA;PETG;ABS",
            "#dc2828;#1e78dc;#28c878",
            """
            T0
            ; Change Tool 0 -> Tool 1
            T1
            G1 X10 Y10 E1
            ; Change Tool 1 -> Tool 0
            T0
            G1 X20 Y10 E1
            ; Change Tool 0 -> Tool 2
            T2
            G1 X30 Y10 E1
            """,
        ),
    )
    started = start_print(
        report,
        tool_targets={
            "0": {"kind": "native", "head": 0, "source": "native:1"},
            "1": {
                "kind": "ace", "head": 0, "source": "ace:0:1",
                "ace": 0, "slot": 1,
            },
            "2": {"kind": "native", "head": 0, "source": "native:2"},
        },
    )
    status = wait_job(started["job_id"])
    route_events = (status.get("route_plan") or {}).get("events") or []
    assert_true([e.get("slicer_tool") for e in route_events] == [0, 1, 0, 2],
                f"repeated route event stream mismatch: {route_events}")
    assert_true(_event_commands_for(status, 1) == [
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 UNLOAD=1",
        "T0",
        "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1",
    ], f"native -> ACE repeated transition mismatch: {route_events[1]}")
    assert_true(_event_commands_for(status, 2) == [
        "ACE_UNLOAD_HEAD HEAD=0",
        "T0",
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 LOAD=1",
    ], f"ACE -> native repeated transition mismatch: {route_events[2]}")
    assert_true(_event_commands_for(status, 3) == [
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 UNLOAD=1",
        "T0",
        "FEED_AUTO MODULE=right CHANNEL=0 EXTRUDER=0 LOAD=1",
    ], f"native -> native repeated transition mismatch: {route_events[3]}")
    content = uploaded_content(status["filename"])
    assert_true(_content_contains_in_order(content, [
        "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1",
        "ACE_UNLOAD_HEAD HEAD=0",
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 LOAD=1",
        "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 UNLOAD=1",
        "FEED_AUTO MODULE=right CHANNEL=0 EXTRUDER=0 LOAD=1",
    ]), f"rewritten upload missing repeated transition order: {content}")


def test_stale_head_source_cleared_on_print_start() -> None:
    set_scenario({
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "slots": [
            {"ace": 0, "slot": 0, "material": "PLA", "color": "#dc2828"},
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
        "head_sources": [
            {"head": 0, "ace": 0, "slot": 1, "material": "PETG", "color": "1E78DC"},
        ],
    })
    before = moonraker_state()
    assert_true(before["ace"]["head_source"]["0"] is not None,
                f"test setup should have stale head_source: {before['ace']['head_source']}")
    assert_true(not before["filament_feed left"]["extruder0"]["filament_detected"],
                "test setup should have an empty ACE head sensor")

    run_script("PRINT_START")
    after = moonraker_state()
    assert_true(after["ace"]["head_source"]["0"] is None,
                f"PRINT_START should clear stale head_source: {after['ace']['head_source']}")
    assert_true(after["ace"].get("ghost_heads", []) == [],
                f"empty stale source should not become ghost: {after['ace'].get('ghost_heads')}")
    run_script("ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=0")
    loaded = moonraker_state()
    assert_true(loaded["ace"]["head_source"]["0"]["slot"] == 0,
                f"swap after stale cleanup should load target slot: {loaded['ace']['head_source']}")


def test_ghost_head_refuses_swap() -> None:
    set_scenario({
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "native_heads": [
            {"head": 0, "material": "PLA", "color": "#dc2828"},
        ],
        "slots": [
            {"ace": 0, "slot": 0, "material": "PLA", "color": "#dc2828"},
        ],
    })
    before = moonraker_state()
    assert_true(before["ace"]["head_source"]["0"] is None,
                f"test setup should have no head_source: {before['ace']['head_source']}")
    assert_true(before["filament_feed left"]["extruder0"]["filament_detected"],
                "test setup should have filament detected at ACE head")

    run_script("PRINT_START")
    after = moonraker_state()
    assert_true(after["ace"].get("ghost_heads") == [0],
                f"PRINT_START should mark ghost head: {after['ace'].get('ghost_heads')}")
    err = run_script("ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=0", expect_status=400)
    assert_true("ghost" in str(err).lower(),
                f"ghost swap should be rejected: {err}")


def test_source_graph_edge_required_for_ace_swap() -> None:
    scenario = {
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "slots": [
            {"ace": 0, "slot": 0, "material": "PLA", "color": "#dc2828"},
        ],
    }
    set_scenario(scenario)
    graph = source_graph_for(scenario)
    graph["edges"] = [
        edge for edge in graph["edges"]
        if not str(edge.get("source") or "").startswith("ace:")
    ]
    push_graph(graph)
    err = run_script("ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=0", expect_status=400)
    assert_true("source graph" in str(err).lower(),
                f"ACE swap should require source graph edge: {err}")


def test_route_plan_rejects_graph_hash_change() -> None:
    reset_default()
    report = multipart_upload(
        "route_hash_change.gcode",
        gcode(
            "PETG",
            "#1e78dc",
            """
            T1
            G1 X10 Y10 E1
            """,
        ),
    )
    graph = source_graph_for({
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "slots": [
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
    })
    graph["edges"].append({
        "source": "ace:0:1",
        "head": "head:1",
        "enabled": False,
        "priority": 999,
    })
    push_graph(graph)
    validation = request(
        "GET",
        f"{WEB}/preflight/route-plan/validate?token={report['token']}",
    )
    assert_true(not validation.get("ok"),
                f"route plan validate should fail after graph change: {validation}")
    assert_true("hash mismatch" in "; ".join(validation.get("errors") or []).lower(),
                f"route plan validate error mismatch: {validation}")
    started = post_json(f"{WEB}/preflight/print", {
        "token": report["token"],
        "mode": "slicer",
    })
    wait_job_error(started["job_id"], "hash mismatch")


def test_route_plan_only_rewrite() -> None:
    pp = load_postprocessor()
    route_plan = {
        "version": 2,
        "events": [
            {
                "index": 0,
                "event_type": "tool_select",
                "slicer_tool": 0,
                "source": "native:1",
                "head": "head:1",
                "steps": [
                    {"kind": "select_head", "head": "head:1", "command": "T1"},
                    {"kind": "notify", "command": "M117 ROUTE_EVENT_T0"},
                ],
                "target": {
                    "kind": "native", "head": 1, "source": "native:1",
                },
            },
            {
                "index": 1,
                "event_type": "tool_select",
                "slicer_tool": 1,
                "source": "ace:0:1",
                "head": "head:0",
                "steps": [
                    {"kind": "select_head", "head": "head:0", "command": "T0"},
                    {
                        "kind": "swap_source",
                        "source": "ace:0:1",
                        "head": "head:0",
                        "ace": 0,
                        "slot": 1,
                        "command": "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1",
                    },
                    {"kind": "notify", "command": "M117 ROUTE_EVENT_T1"},
                ],
                "target": {
                    "kind": "ace", "head": 0, "source": "ace:0:1",
                    "ace": 0, "slot": 1,
                },
            },
        ],
    }
    src = gcode(
        "PLA;PETG",
        "#dc2828;#1e78dc",
        """
        T0
        ; Change Tool 0 -> Tool 1
        T1
        G1 X10 Y10 E1
        """,
    )
    out, active, _skipped, _swapbacks = pp.rewrite(src, route_plan=route_plan)
    assert_true("T1" in out, f"route-plan rewrite missing native T1: {out}")
    assert_true("ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1" in out,
                f"route-plan rewrite missing ACE swap: {out}")
    assert_true("M117 ROUTE_EVENT_T1" in out,
                f"route-plan rewrite should consume event commands: {out}")
    assert_true(active == 1, f"route-plan rewrite active swap mismatch: {active}")


def test_route_plan_only_rewrite_rejects_missing_event() -> None:
    pp = load_postprocessor()
    route_plan = {
        "version": 2,
        "events": [
            {
                "index": 0,
                "event_type": "tool_select",
                "slicer_tool": 0,
                "source": "native:1",
                "head": "head:1",
                "steps": [
                    {"kind": "select_head", "head": "head:1", "command": "T1"},
                ],
                "target": {
                    "kind": "native", "head": 1, "source": "native:1",
                },
            },
        ],
    }
    src = gcode(
        "PLA;PETG",
        "#dc2828;#1e78dc",
        """
        T0
        ; Change Tool 0 -> Tool 1
        T1
        G1 X10 Y10 E1
        """,
    )
    try:
        pp.rewrite(src, route_plan=route_plan)
    except ValueError as exc:
        assert_true("route plan missing tool_select" in str(exc),
                    f"unexpected missing-event error: {exc}")
    else:
        raise AssertionError("route-plan rewrite should reject missing T1 event")


def test_route_plan_only_rewrite_rejects_missing_target() -> None:
    pp = load_postprocessor()
    route_plan = {
        "version": 2,
        "events": [
            {
                "index": 0,
                "event_type": "tool_select",
                "slicer_tool": 0,
                "source": "native:1",
                "head": "head:1",
                "steps": [
                    {"kind": "select_head", "head": "head:1", "command": "T1"},
                ],
                "target": {
                    "kind": "native", "head": 1, "source": "native:1",
                },
            },
        ],
    }
    src = gcode(
        "PLA;PETG",
        "#dc2828;#1e78dc",
        """
        M104 S210 T1
        T0
        G1 X10 Y10 E1
        """,
    )
    try:
        pp.rewrite(src, route_plan=route_plan)
    except ValueError as exc:
        assert_true("route plan missing target for T1" in str(exc),
                    f"unexpected missing-target error: {exc}")
    else:
        raise AssertionError("route-plan rewrite should reject missing T1 target")


def test_route_plan_validate_rejects_incomplete_tool_contract() -> None:
    reset_default()
    graph = source_graph_for({
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "native_heads": [
            {"head": 1, "material": "PLA", "color": "#dc2828"},
        ],
        "slots": [
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
    })
    push_graph(graph)
    meta = request("GET", f"{WEB}/source-graph").get("meta") or {}
    route_plan = {
        "version": 2,
        "source_graph_hash": meta.get("hash"),
        "used_tools": [0, 1],
        "tool_map": {
            "0": {
                "source": "native:1",
                "head": "head:1",
                "target": {
                    "kind": "native", "head": 1, "head_id": "head:1",
                    "source": "native:1",
                },
            },
        },
        "events": [
            {
                "index": 0,
                "event_type": "tool_select",
                "slicer_tool": 0,
                "source": "native:1",
                "head": "head:1",
                "execution_profile": "u1_native_feeder",
                "steps": [
                    {"kind": "select_head", "head": "head:1", "command": "T1"},
                ],
                "commands": ["T1"],
                "target": {
                    "kind": "native", "head": 1, "head_id": "head:1",
                    "source": "native:1",
                },
            },
        ],
    }
    validation = post_json(f"{WEB}/route-plan/validate", {
        "route_plan": route_plan,
    })
    errors = "; ".join(validation.get("errors") or []).lower()
    assert_true(not validation.get("ok"),
                f"incomplete route plan should be rejected: {validation}")
    assert_true("missing tool_map target for t1" in errors,
                f"missing target error mismatch: {validation}")
    assert_true("missing tool_select event for t1" in errors,
                f"missing event error mismatch: {validation}")


def test_route_plan_validate_rejects_tampered_profile_command() -> None:
    scenario = {
        "head_modes": {"0": "ace", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": 0},
        "slots": [
            {"ace": 0, "slot": 1, "material": "PETG", "color": "#1e78dc"},
        ],
        "head_sources": [
            {"head": 0, "ace": 0, "slot": 1, "material": "PETG", "color": "1E78DC"},
        ],
    }
    set_scenario(scenario)
    graph = source_graph_for(scenario)
    graph["edges"].append({
        "source": "native:1",
        "head": "head:0",
        "enabled": True,
        "priority": 20,
    })
    push_graph(graph)
    preview = post_json(f"{WEB}/source-transition/preview", {
        "source": "native:1",
        "head": "head:0",
    })
    route_plan = preview.get("route_plan") or {}
    tampered = json.loads(json.dumps(route_plan))
    event = (tampered.get("events") or [])[0]
    steps = event.get("steps") or []
    for step in steps:
        if step.get("kind") == "load_source":
            step["channel"] = 1
            step["command"] = "FEED_AUTO MODULE=left CHANNEL=1 EXTRUDER=0 LOAD=1"
    event["commands"] = [
        step.get("command") for step in steps if step.get("command")
    ]
    validation = post_json(f"{WEB}/route-plan/validate", {
        "route_plan": tampered,
    })
    errors = "; ".join(validation.get("errors") or []).lower()
    assert_true(not validation.get("ok"),
                f"tampered route plan should be rejected: {validation}")
    assert_true("command does not match profile" in errors
                or "channel mismatch" in errors,
                f"tampered command error mismatch: {validation}")


def main() -> int:
    tests = [
        test_mixed_native_ace_print,
        test_native_only_print,
        test_single_ace_head_print,
        test_unmapped_tool_is_not_feasible,
        test_invalid_bambu_p1s_gcode_blocked,
        test_manual_mapping_can_reuse_source_edge,
        test_wrong_feed_auto_channel_rejected,
        test_source_action_profile_preview,
        test_source_transition_preview_unloads_previous_source,
        test_route_plan_rewrite_includes_source_transition,
        test_route_plan_rewrite_native_to_ace_transition,
        test_route_plan_rewrite_ace_to_ace_transition,
        test_route_plan_rewrite_native_to_native_transition,
        test_route_plan_rewrite_repeated_tool_sequence,
        test_stale_head_source_cleared_on_print_start,
        test_ghost_head_refuses_swap,
        test_source_graph_edge_required_for_ace_swap,
        test_route_plan_rejects_graph_hash_change,
        test_route_plan_only_rewrite,
        test_route_plan_only_rewrite_rejects_missing_event,
        test_route_plan_only_rewrite_rejects_missing_target,
        test_route_plan_validate_rejects_incomplete_tool_contract,
        test_route_plan_validate_rejects_tampered_profile_command,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("dry-run preflight regression passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"dry-run preflight regression failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
