# multiACE Docker dry-run

Dry-run harness for testing the multiACE Web UI and command routing without
printer hardware.

It starts two services:

- `moonraker`: a small FastAPI Moonraker mock on `http://localhost:7125`
- `multiace-web`: the real multiACE Web backend/frontend on
  `http://localhost:7126`

The mock exposes one virtual ACE with four ready slots and records every
G-code command sent by the UI.

## Start

From this directory:

```sh
docker compose up --build
```

Open:

```text
http://localhost:7126/
```

The first run copies `multiace/config/extended/ace.cfg` into the Docker
volume as `/data/ace.cfg`. Later UI saves only modify the Docker volume copy,
not the repository file.

## Single-head ACE routing test

1. Open the Dashboard tab.
2. Set the target toolhead `Source` to `ACE`.
3. Set all other toolheads to `Native`.
4. Set `ACE 1` `Target` to the ACE toolhead.
5. Wait for the dry-run Klipper restart to complete.
6. Confirm all four ACE slots show `-> T<target>`.
7. Click `Load` on any slot.

Expected G-code examples:

```text
ACE_LOAD_HEAD HEAD=0 ACE=0 SLOT=0
ACE_UNLOAD_HEAD HEAD=0
ACE_LOAD_HEAD HEAD=0 ACE=0 SLOT=1
```

Read the captured command log:

```sh
curl http://localhost:7125/dry-run/gcode-log
```

Reset the virtual printer state:

```sh
curl -X POST http://localhost:7125/dry-run/reset
```

## Preflight regression

With the dry-run stack running, execute:

```sh
python3 multiace/docker-dryrun/regression_preflight.py
```

The script uploads a small mixed native/ACE G-code through the real Web
preflight API, verifies the persisted source map, sends the print to the mock
Moonraker upload endpoint, and checks that the rewritten file contains both a
native tool command and an explicit `ACE_SWAP_HEAD`.

## Limits

This validates UI configuration, state parsing, command generation, and
snapshot command planning. It does not validate U1 motor motion, sensors,
ACE serial protocol timing, PTFE routing, load lengths, retraction lengths,
temperature behavior, jams, or feed-assist behavior.
