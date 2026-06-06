#!/usr/bin/env python3
from __future__ import annotations

import json
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
    post_json(f"{MOONRAKER}/dry-run/scenario", {
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
    })


def set_scenario(payload: dict[str, Any]) -> None:
    post_json(f"{MOONRAKER}/dry-run/scenario", payload)


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


def commands_for(report: dict, tool: int) -> list[str]:
    return entries_by_tool(report)[tool].get("commands") or []


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


def assert_source_map_shape(report: dict) -> None:
    source_map = report.get("source_map") or {}
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
    assert_true(len(entries_by_tool(report)) == 2, "mixed case should map two tools")
    t0 = target_for(report, 0)
    t1 = target_for(report, 1)
    assert_true(t0 == {"kind": "native", "head": 1},
                f"T0 should map to native T1, got {t0}")
    assert_true(t1 == {"kind": "ace", "head": 0, "ace": 0, "slot": 1},
                f"T1 should map to ACE0 slot1 -> T0, got {t1}")
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
    started = start_print(report)
    status = wait_job(started["job_id"])
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
    assert_true(target_for(report, 0) == {"kind": "native", "head": 0},
                f"T0 native-only mapping mismatch: {target_for(report, 0)}")
    assert_true(target_for(report, 1) == {"kind": "native", "head": 1},
                f"T1 native-only mapping mismatch: {target_for(report, 1)}")
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
    assert_true(target_for(report, 0) == {"kind": "ace", "head": 0, "ace": 0, "slot": 0},
                f"T0 single ACE mapping mismatch: {target_for(report, 0)}")
    assert_true(target_for(report, 1) == {"kind": "ace", "head": 0, "ace": 0, "slot": 1},
                f"T1 single ACE mapping mismatch: {target_for(report, 1)}")
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


def test_duplicate_manual_mapping_rejected() -> None:
    reset_default()
    report = multipart_upload(
        "duplicate_manual.gcode",
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
    native_target = {"kind": "native", "head": 1}
    err = start_print(
        report,
        tool_targets={"0": native_target, "1": native_target},
        expect_status=409,
    )
    detail = str(err.get("detail") or err)
    assert_true("reuses the same target" in detail,
                f"duplicate manual mapping error mismatch: {err}")


def test_wrong_feed_auto_channel_rejected() -> None:
    set_scenario({
        "head_modes": {"0": "native", "1": "native", "2": "native", "3": "native"},
        "ace_targets": {"0": None},
    })
    err = post_json(
        f"{MOONRAKER}/printer/gcode/script",
        {"script": "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=0 LOAD=1"},
        expect_status=400,
    )
    assert_true("route mismatch" in str(err),
                f"wrong FEED_AUTO channel should be rejected: {err}")


def main() -> int:
    tests = [
        test_mixed_native_ace_print,
        test_native_only_print,
        test_single_ace_head_print,
        test_unmapped_tool_is_not_feasible,
        test_duplicate_manual_mapping_rejected,
        test_wrong_feed_auto_channel_rejected,
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
