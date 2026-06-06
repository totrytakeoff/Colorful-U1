#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


WEB = "http://127.0.0.1:7126/api"
MOONRAKER = "http://127.0.0.1:7125"


GCODE = """; Colorful-U1 dry-run mixed preflight regression
; filament_type = PLA;PETG
; filament_colour = #dc2828;#1e78dc
T0
G92 E0
; Change Tool 0 -> Tool 1
T1
G1 X10 Y10 E1
; Change Tool 1 -> Tool 0
T0
G1 X20 Y10 E1
"""


def request(method: str, url: str, data: bytes | None = None,
            headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}")
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def multipart_upload(path: Path) -> dict:
    boundary = "----colorful-u1-dryrun"
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
    )


def assert_true(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def main() -> int:
    request("POST", f"{MOONRAKER}/dry-run/reset")
    with tempfile.TemporaryDirectory() as td:
        gpath = Path(td) / "colorful_u1_mixed_regression.gcode"
        gpath.write_text(GCODE, encoding="utf-8")
        report = multipart_upload(gpath)

    source_map = report.get("source_map") or {}
    entries = source_map.get("entries") or []
    assert_true(len(entries) == 2, f"expected 2 source-map entries, got {len(entries)}")
    targets = {int(e["slicer_tool"]): e.get("target") for e in entries}
    commands = {int(e["slicer_tool"]): e.get("commands") or [] for e in entries}
    assert_true(targets[0]["kind"] == "native" and targets[0]["head"] == 1,
                f"T0 should map to native T1, got {targets[0]}")
    assert_true(targets[1]["kind"] == "ace"
                and targets[1]["head"] == 0
                and targets[1]["ace"] == 0
                and targets[1]["slot"] == 1,
                f"T1 should map to ACE0 slot1 -> T0, got {targets[1]}")
    assert_true(commands[0] == ["T1"], f"T0 command preview mismatch: {commands[0]}")
    assert_true(commands[1] == ["T0", "ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1"],
                f"T1 command preview mismatch: {commands[1]}")
    stats = source_map.get("swap_stats") or {}
    assert_true(stats.get("tool_events") == 2,
                f"expected 2 tool events, got {stats}")
    assert_true(stats.get("active_ace_swaps") == 1,
                f"expected 1 active ACE swap, got {stats}")
    assert_true(stats.get("skipped_same_ace") == 0,
                f"expected 0 same-source skips, got {stats}")
    assert_true(stats.get("estimated_swap_seconds_min") == 120,
                f"unexpected min swap estimate: {stats}")
    assert_true(stats.get("estimated_swap_seconds_max") == 240,
                f"unexpected max swap estimate: {stats}")
    suggestion = source_map.get("optimization_suggestion") or {}
    assert_true("feasible" in suggestion,
                f"optimization suggestion missing feasibility: {suggestion}")
    assert_true("current" in suggestion,
                f"optimization suggestion missing current stats: {suggestion}")
    if suggestion.get("feasible"):
        assert_true("suggested" in suggestion and "tool_targets" in suggestion,
                    f"feasible suggestion missing suggested mapping: {suggestion}")

    token = report["token"]
    saved_map = request("GET", f"{WEB}/preflight/source-map?token={token}")
    assert_true(saved_map.get("entries") == entries,
                "saved source map differs from preflight response")

    print_req = json.dumps({
        "token": token,
        "mode": "slicer",
        "tool_targets": report.get("tool_targets") or {},
    }).encode("utf-8")
    started = request(
        "POST",
        f"{WEB}/preflight/print",
        print_req,
        {"Content-Type": "application/json"},
    )
    job_id = started["job_id"]
    status = {}
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
    final_stats = ((status.get("source_map") or {}).get("swap_stats") or {})
    assert_true(final_stats.get("active_ace_swaps") == 1,
                f"final source map lost swap stats: {final_stats}")

    uploaded = request(
        "GET",
        f"{MOONRAKER}/dry-run/uploaded/{urllib.parse.quote(started['filename'])}",
    )
    content = uploaded.get("content") or ""
    assert_true("T1" in content, "rewritten upload missing native T1")
    assert_true("ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1" in content,
                "rewritten upload missing ACE slot1 swap")

    print("dry-run preflight regression passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"dry-run preflight regression failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
