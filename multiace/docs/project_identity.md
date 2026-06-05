# Colorful-U1 project identity

Date: 2026-06-05

## Name

The downstream project name is **Colorful-U1**.

The name is intentionally separate from `multiACE` because the branch now carries
large behavior changes around U1 native/ACE mixed routing. It should be
maintained as an independent experimental branch rather than a small patch set.

## Upstream lineage

```text
BlackFrogKok/SnapAce
  -> decay71/multiACE
    -> Colorful-U1
```

### SnapAce

SnapAce is the original Snapmaker U1 + Anycubic ACE Pro integration. It focused
on connecting one ACE Pro to the U1 and installing the Klipper modules needed
for ACE-driven filament handling.

### multiACE

multiACE extends SnapAce with multi-ACE support, hardened load/unload paths,
ACE switching, Web UI, Web preflight, post-processing and in-print color-swap
workflows.

### Colorful-U1

Colorful-U1 keeps the multiACE foundation but changes the project goal toward
explicit U1 source routing:

- native U1 heads and ACE-driven heads can cooperate in one print;
- slicer tools are resolved to a concrete target before printing;
- native heads keep persistent material/color metadata;
- ACE load/swap commands require explicit `HEAD`, `ACE`, and `SLOT`;
- UI configuration and preflight behavior are designed around testable physical
  routing rather than legacy normal/multi mode switching.

## Current verified MVP

The verified MVP is:

```text
T0/T1/T2: native
T3: ACE head
ACE0 -> T3
```

The Web preflight can map a single G-code file to both native heads and ACE
slots. Native targets remain normal U1 tool changes; ACE targets emit explicit
`ACE_SWAP_HEAD` commands.

## Maintenance policy

- Keep upstream attribution and GPL-3.0 license intact.
- Prefer direct, explicit routing state over implicit guesses.
- Do not add a feature if it can accidentally drive the wrong head or slot.
- Treat real-printer test notes as part of the engineering record.
- Keep Docker dry-run paths working before deploying risky UI/preflight changes.

## Public project summary

Short summary:

> Colorful-U1 is an experimental Snapmaker U1 firmware/Web branch for explicit
> native + ACE mixed color routing, based on multiACE and SnapAce.

Long summary:

> Colorful-U1 turns a Snapmaker U1 with native feeders and Anycubic ACE units
> into a manually configurable mixed-source color printer. The Web preflight
> resolves each slicer tool to a concrete native head or ACE slot before
> printing, stores native and ACE material metadata persistently, and emits only
> explicit ACE load/swap commands so hardware routing remains auditable.
