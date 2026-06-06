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
    assert_true(route_plan.get("source_graph_hash") == graph_meta.get("hash"),
                f"route plan graph hash mismatch: {route_plan}")
    events = route_plan.get("events") or []
    assert_true(events, f"route plan missing events: {route_plan}")
    for event in events:
        assert_true(event.get("source"), f"route event missing source: {event}")
        assert_true(str(event.get("head") or "").startswith("head:"),
                    f"route event missing graph head id: {event}")
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


def test_route_plan_only_rewrite() -> None:
    pp = load_postprocessor()
    route_plan = {
        "version": 1,
        "tool_map": {
            "0": {
                "source": "native:1",
                "head": "head:1",
                "target": {"kind": "native", "head": 1, "source": "native:1"},
            },
            "1": {
                "source": "ace:0:1",
                "head": "head:0",
                "target": {
                    "kind": "ace", "head": 0, "source": "ace:0:1",
                    "ace": 0, "slot": 1,
                },
            },
        },
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
    assert_true(active == 1, f"route-plan rewrite active swap mismatch: {active}")


def main() -> int:
    tests = [
        test_mixed_native_ace_print,
        test_native_only_print,
        test_single_ace_head_print,
        test_unmapped_tool_is_not_feasible,
        test_manual_mapping_can_reuse_source_edge,
        test_wrong_feed_auto_channel_rejected,
        test_stale_head_source_cleared_on_print_start,
        test_ghost_head_refuses_swap,
        test_source_graph_edge_required_for_ace_swap,
        test_route_plan_only_rewrite,
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
