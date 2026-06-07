# Colorful-U1

[中文说明](README.zh-CN.md)

Colorful-U1 is an experimental Snapmaker U1 filament-routing firmware and
web-control project based on
[decay71/multiACE](https://github.com/decay71/multiACE), which itself extends
[BlackFrogKok/SnapAce](https://github.com/BlackFrogKok/SnapAce).

The focus of this branch is explicit, testable mixed routing for a U1 equipped
with native toolheads and one or more Anycubic ACE units. The current verified
MVP supports native heads and an ACE-driven head in the same print, with Web
preflight mapping slicer tools to either native filament or ACE slots before
the file is sent to Klipper.

> Warning: this is hardware-control beta software. Wrong routing, wrong
> unload length, or stale state can damage prints and may stress hardware.
> Test small files first, keep access to power/stop controls, and do not treat
> this as production firmware.

## Project Lineage

- **SnapAce**: original Anycubic ACE Pro integration for Snapmaker U1.
- **multiACE**: multi-ACE support, Web UI, Web preflight, hardened load/unload
  and in-print swap flow.
- **Colorful-U1**: a downstream experimental branch focused on flexible U1
  mixed routing:
  - one physical head fully driven by one ACE for 4-color printing;
  - native heads and ACE heads cooperating in one G-code file;
  - persistent material/color configuration for native heads and ACE slots;
  - explicit toolhead/ACE/slot commands instead of implicit hardware guesses.

This repository keeps the GPL-3.0 license and preserves the upstream history.

## Current Status

Verified on a Snapmaker U1 with this topology:

```text
T0: native
T1: native
T2: native
T3: ACE head
ACE0 -> T3
```

The tested Web preflight mapping can resolve slicer tools like this:

```text
Slicer T0 -> native T0
Slicer T1 -> ACE0 Slot1 -> physical T3
Slicer T2 -> native T1
Slicer T3 -> native T2
```

The generated print file uses normal `T<head>` commands for native targets and
explicit ACE commands for ACE targets:

```gcode
T3
ACE_SWAP_HEAD HEAD=3 ACE=0 SLOT=1
```

## Key Features

- Dashboard toolhead topology:
  - each U1 head can be configured as `native` or `ace`;
  - each ACE device can be assigned to a specific ACE head;
  - changes are staged and applied deliberately, not auto-restarted on every
    dropdown edit.

- Web preflight for mixed routing:
  - reads slicer tools, filament colors and filament materials;
  - reads the live printer loadout;
  - resolves each slicer tool to a native head or an ACE slot;
  - supports manual mapping override when automatic matching is not enough;
  - rejects duplicate or unavailable targets before sending a print.

- Persistent material configuration:
  - ACE slot metadata persists through `slot_overrides.json`;
  - native head metadata persists through `native_overrides.json`;
  - preflight uses these persisted values instead of relying only on transient
    `print_task_config` state.

- Safety boundaries:
  - `ACE_LOAD_HEAD` and `ACE_SWAP_HEAD` require explicit `HEAD`, `ACE`, and
    `SLOT`;
  - native heads reject ACE load/swap/unload paths;
  - ghost-head checks only apply to ACE-mode heads;
  - Web backend validates direct `FEED_AUTO` native load/unload routing.

- Docker dry-run:
  - mock Moonraker service for UI and preflight testing without hardware;
  - mixed native/ACE state simulation;
  - API-level validation for preflight and manual mapping.

## Typical Workflow

1. Open the Colorful-U1 Web UI at:

   ```text
   http://<printer-ip>/multiace/
   ```

2. In Dashboard, configure:
   - which heads are `native`;
   - which head is the `ace` head;
   - which ACE is assigned to that ACE head.

3. Configure filament metadata:
   - native head cards store each native head's material/color;
   - ACE slot cards store each ACE slot's material/color.

4. Upload the slicer G-code through the Colorful-U1 Web preflight dialog.

5. Verify the mapping table:
   - native slicer tools should point to `Native Tn`;
   - ACE slicer tools should point to `ACE n Slot m -> Tn`;
   - use manual override if automatic matching is wrong.

6. Send the print only after the mapping matches the physical loadout.

## Known Limits

- Current automatic mapping is conservative; it is not yet optimized for
  minimum swap time.
- ACE swaps are functional but still slow.
- Purge, wipe and retract strategies are intentionally conservative.
- The source-graph backend is now partially implemented: it can express
  arbitrary source/head edges and preview source transitions, but the Dashboard
  UI and final print rewrite path have not yet been rebuilt around it.
- Arbitrary ACE-slot to head routing, one native head using multiple ordinary
  native feeders, and native + ACE mixed sources on the same head have not yet
  been validated on real hardware.
- This branch is tuned for active hardware experimentation, not end-user
  appliance behavior.

## Roadmap

Near-term work:

- improve mapping algorithms to reduce unnecessary swaps;
- show final generated command intent in preflight;
- make UI warnings distinguish recoverable alerts from hard blockers;
- add repeatable dry-run regression tests for native-only, ACE-only and mixed
  routing;
- connect the source-graph backend to the final print rewrite path;
- rebuild Dashboard configuration around heads, sources and edges instead of
  the old `native`/`ace` mode split;
- add Web G-code machine/dialect safety validation so non-U1 Bambu/P1S-style
  files are blocked before upload;
- improve unload/reload parameters from real failure logs.

Longer-term work:

- single native head + multiple ordinary feeders;
- arbitrary ACE slot to toolhead mapping;
- multiple ACE heads;
- multiple ACE devices in mixed routing;
- richer purge/wipe strategies.

See [native/ACE MVP plan](multiace/docs/native_ace_mvp_plan.md) for the current
engineering notes and TODO list. See
[post-MVP optimization plan](multiace/docs/post_mvp_optimization_plan.md) for
the swap optimization and slicer-integration roadmap. See
[source graph architecture](multiace/docs/source_graph_architecture.md) for the
current backend routing refactor, and
[real-printer G-code validation strategy](multiace/docs/real_printer_gcode_validation_strategy.md)
for the Web send safety plan.

## Installation

Colorful-U1 currently follows the multiACE installation model. The original
installer and deployment scripts are still in `multiace/`.

For development and dry-run testing:

```bash
docker compose -f multiace/docker-dryrun/docker-compose.yml up -d --build
```

Then open:

```text
http://127.0.0.1:7126/
```

For printer deployment, review the upstream multiACE install notes and the
changed files in this branch before copying anything to a real printer. Do not
blindly deploy to a machine that is currently printing.

## Upstream Credits

Colorful-U1 exists because of:

- [BlackFrogKok/SnapAce](https://github.com/BlackFrogKok/SnapAce)
- [decay71/multiACE](https://github.com/decay71/multiACE)
- Snapmaker U1, ACE Pro and Klipper community testing work

Please keep upstream attribution intact when redistributing this branch.
