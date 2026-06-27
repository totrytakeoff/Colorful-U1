#!/usr/bin/env bash
set -euo pipefail

HOST="192.168.1.38"
USER_NAME="root"
MOONRAKER_PORT="7125"
REMOTE_KLIPPER="/home/lava/klipper/klippy"
REMOTE_WEB="/home/lava/multiace_web"
REMOTE_TOOLS="/home/lava/printer_data/config/tools"
REMOTE_MULTIACE_CFG="/home/lava/printer_data/config/extended/multiace"
DEPLOY_WEB=1
RESTART_KLIPPER=1
RESTART_WEB=1

usage() {
  cat <<'EOF'
Install Colorful-U1 files to a Snapmaker U1 printer.

Usage:
  multiace/tools/install_to_printer.sh [options]

Options:
  --host IP              Printer IP. Default: 192.168.1.38
  --user USER            SSH user. Default: root
  --moonraker-port PORT  Moonraker port. Default: 7125
  --remote-web PATH      Remote Web directory. Default: /home/lava/multiace_web
  --klipper-only         Upload Klipper files only, skip Web files.
  --no-restart           Upload only, do not restart Klipper or Web service.
  --no-web-restart       Upload Web files but do not restart Web service.
  -h, --help             Show this help.

Notes:
  - Run from the repository root.
  - The script backs up remote files before overwriting them.
  - It does not send any motion, load, unload, heat, print, or homing command.
  - Password auth is interactive unless your SSH keys are configured.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:?missing value for --host}"
      shift 2
      ;;
    --user)
      USER_NAME="${2:?missing value for --user}"
      shift 2
      ;;
    --moonraker-port)
      MOONRAKER_PORT="${2:?missing value for --moonraker-port}"
      shift 2
      ;;
    --remote-web)
      REMOTE_WEB="${2:?missing value for --remote-web}"
      shift 2
      ;;
    --klipper-only)
      DEPLOY_WEB=0
      shift
      ;;
    --no-restart)
      RESTART_KLIPPER=0
      RESTART_WEB=0
      shift
      ;;
    --no-web-restart)
      RESTART_WEB=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "multiace/klipper/extras/ace.py" ]]; then
  echo "Run this script from the repository root." >&2
  exit 1
fi

SSH_TARGET="${USER_NAME}@${HOST}"
CONTROL_PATH="/tmp/colorful-u1-ssh-${USER_NAME}-${HOST}.sock"
SSH_OPTS=(
  -F /dev/null
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=10
  -o ControlMaster=auto
  -o ControlPersist=120
  -o ControlPath="${CONTROL_PATH}"
)
TS="$(date +%Y%m%d_%H%M%S)"
REMOTE_BACKUP="${REMOTE_KLIPPER}/colorful_u1_install_backup_${TS}"

cleanup() {
  ssh -O exit "${SSH_OPTS[@]}" "${SSH_TARGET}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== Colorful-U1 install =="
echo "Target: ${SSH_TARGET}"
echo "Branch: $(git branch --show-current 2>/dev/null || echo unknown)"
echo "Commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo

echo "== Checking Moonraker =="
if curl -fsS --max-time 5 "http://${HOST}:${MOONRAKER_PORT}/printer/info" >/tmp/colorful_u1_printer_info.json; then
  cat /tmp/colorful_u1_printer_info.json
  echo
else
  echo "Warning: Moonraker is not reachable at http://${HOST}:${MOONRAKER_PORT}" >&2
fi

echo "== Creating remote backup =="
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "
  set -e
  mkdir -p '${REMOTE_BACKUP}/extras' '${REMOTE_BACKUP}/kinematics' '${REMOTE_BACKUP}/config'
  cp '${REMOTE_KLIPPER}/extras/ace.py' '${REMOTE_BACKUP}/extras/ace.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/extras/ace_protocol.py' '${REMOTE_BACKUP}/extras/ace_protocol.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/extras/ace_protocol_v1.py' '${REMOTE_BACKUP}/extras/ace_protocol_v1.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/extras/ace_protocol_v2.py' '${REMOTE_BACKUP}/extras/ace_protocol_v2.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/extras/filament_feed_ace.py' '${REMOTE_BACKUP}/extras/filament_feed_ace.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/extras/filament_feed.py' '${REMOTE_BACKUP}/extras/filament_feed.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/extras/filament_switch_sensor_ace.py' '${REMOTE_BACKUP}/extras/filament_switch_sensor_ace.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/extras/filament_switch_sensor.py' '${REMOTE_BACKUP}/extras/filament_switch_sensor.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/kinematics/extruder_ace.py' '${REMOTE_BACKUP}/kinematics/extruder_ace.py' 2>/dev/null || true
  cp '${REMOTE_KLIPPER}/kinematics/extruder.py' '${REMOTE_BACKUP}/kinematics/extruder.py' 2>/dev/null || true
  cp '${REMOTE_MULTIACE_CFG}/ace_mode_switch.sh' '${REMOTE_BACKUP}/config/ace_mode_switch.sh' 2>/dev/null || true
  cp '${REMOTE_TOOLS}/post_process_virtual_toolheads.py' '${REMOTE_BACKUP}/config/post_process_virtual_toolheads.py' 2>/dev/null || true
  if [ -d '${REMOTE_WEB}' ]; then
    mkdir -p '${REMOTE_BACKUP}/web'
    cp -a '${REMOTE_WEB}/.' '${REMOTE_BACKUP}/web/' 2>/dev/null || true
  fi
  echo '${REMOTE_BACKUP}'
"

echo "== Uploading Klipper files =="
scp "${SSH_OPTS[@]}" multiace/klipper/extras/ace.py \
  "${SSH_TARGET}:${REMOTE_KLIPPER}/extras/ace.py"
scp "${SSH_OPTS[@]}" multiace/klipper/extras/ace_protocol.py \
  "${SSH_TARGET}:${REMOTE_KLIPPER}/extras/ace_protocol.py"
scp "${SSH_OPTS[@]}" multiace/klipper/extras/ace_protocol_v1.py \
  "${SSH_TARGET}:${REMOTE_KLIPPER}/extras/ace_protocol_v1.py"
scp "${SSH_OPTS[@]}" multiace/klipper/extras/ace_protocol_v2.py \
  "${SSH_TARGET}:${REMOTE_KLIPPER}/extras/ace_protocol_v2.py"
scp "${SSH_OPTS[@]}" multiace/klipper/extras/filament_feed_ace.py \
  "${SSH_TARGET}:${REMOTE_KLIPPER}/extras/filament_feed_ace.py"
scp "${SSH_OPTS[@]}" multiace/klipper/extras/filament_switch_sensor_ace.py \
  "${SSH_TARGET}:${REMOTE_KLIPPER}/extras/filament_switch_sensor_ace.py"
scp "${SSH_OPTS[@]}" multiace/klipper/kinematics/extruder_ace.py \
  "${SSH_TARGET}:${REMOTE_KLIPPER}/kinematics/extruder_ace.py"
scp "${SSH_OPTS[@]}" multiace/config/extended/multiace/ace_mode_switch.sh \
  "${SSH_TARGET}:${REMOTE_MULTIACE_CFG}/ace_mode_switch.sh"
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "mkdir -p '${REMOTE_TOOLS}'"
scp "${SSH_OPTS[@]}" multiace/tools/post_process_virtual_toolheads.py \
  "${SSH_TARGET}:${REMOTE_TOOLS}/post_process_virtual_toolheads.py"

echo "== Activating Colorful-U1 Klipper modules =="
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "
  set -e
  chmod 644 \
    '${REMOTE_KLIPPER}/extras/ace.py' \
    '${REMOTE_KLIPPER}/extras/ace_protocol.py' \
    '${REMOTE_KLIPPER}/extras/ace_protocol_v1.py' \
    '${REMOTE_KLIPPER}/extras/ace_protocol_v2.py' \
    '${REMOTE_KLIPPER}/extras/filament_feed_ace.py' \
    '${REMOTE_KLIPPER}/extras/filament_switch_sensor_ace.py' \
    '${REMOTE_KLIPPER}/kinematics/extruder_ace.py'
  chmod 755 '${REMOTE_MULTIACE_CFG}/ace_mode_switch.sh'
  chmod 644 '${REMOTE_TOOLS}/post_process_virtual_toolheads.py'
  cp '${REMOTE_KLIPPER}/extras/filament_feed_ace.py' '${REMOTE_KLIPPER}/extras/filament_feed.py'
  cp '${REMOTE_KLIPPER}/extras/filament_switch_sensor_ace.py' '${REMOTE_KLIPPER}/extras/filament_switch_sensor.py'
  cp '${REMOTE_KLIPPER}/kinematics/extruder_ace.py' '${REMOTE_KLIPPER}/kinematics/extruder.py'
  chmod 644 \
    '${REMOTE_KLIPPER}/extras/filament_feed.py' \
    '${REMOTE_KLIPPER}/extras/filament_switch_sensor.py' \
    '${REMOTE_KLIPPER}/kinematics/extruder.py'
  rm -rf '${REMOTE_KLIPPER}/extras/__pycache__' '${REMOTE_KLIPPER}/kinematics/__pycache__' '${REMOTE_TOOLS}/__pycache__'
"

if [[ "${DEPLOY_WEB}" -eq 1 ]]; then
  echo "== Uploading Web files =="
  tar \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    -C multiace/web \
    -cf - backend frontend \
    | ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "
        set -e
        mkdir -p '${REMOTE_WEB}'
        rm -rf '${REMOTE_WEB}/backend' '${REMOTE_WEB}/frontend'
        tar -xf - -C '${REMOTE_WEB}'
        rm -rf '${REMOTE_WEB}/backend/__pycache__'
      "
  scp "${SSH_OPTS[@]}" multiace/web/deploy/S98multiace-web \
    "${SSH_TARGET}:/etc/init.d/S98multiace-web" || \
    echo "Warning: failed to upload init script; continuing." >&2
fi

if [[ "${RESTART_WEB}" -eq 1 && "${DEPLOY_WEB}" -eq 1 ]]; then
  echo "== Restarting Web service =="
  ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "
    if [ -x /etc/init.d/S98multiace-web ]; then
      /etc/init.d/S98multiace-web restart
    elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files | grep -q multiace; then
      systemctl restart multiace-web || true
    else
      echo 'No known multiACE web service restart command found; skip.'
    fi
  "
fi

if [[ "${RESTART_KLIPPER}" -eq 1 ]]; then
  echo "== Restarting Klipper =="
  if ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "
    if [ -x /etc/init.d/S60klipper ]; then
      /etc/init.d/S60klipper restart
    elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files | grep -q klipper; then
      systemctl restart klipper
    else
      exit 127
    fi
  "; then
    echo "Klipper host process restarted."
  else
    echo "Warning: failed to restart Klipper host process; falling back to Moonraker printer restart." >&2
    curl -fsS --max-time 10 -X POST "http://${HOST}:${MOONRAKER_PORT}/printer/restart" || true
  fi
  echo "Waiting for Klipper to become ready..."
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "http://${HOST}:${MOONRAKER_PORT}/printer/info" \
      | grep -q '"state": "ready"'; then
      echo "Klipper is ready."
      break
    fi
    sleep 2
  done
fi

echo "== Verifying deployed Klipper files =="
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "
  set -e
  cmp -s '${REMOTE_KLIPPER}/extras/filament_feed_ace.py' '${REMOTE_KLIPPER}/extras/filament_feed.py'
  cmp -s '${REMOTE_KLIPPER}/extras/filament_switch_sensor_ace.py' '${REMOTE_KLIPPER}/extras/filament_switch_sensor.py'
  cmp -s '${REMOTE_KLIPPER}/kinematics/extruder_ace.py' '${REMOTE_KLIPPER}/kinematics/extruder.py'
  grep -q 'COLORFUL_U1_ROUTE_SELECT' '${REMOTE_KLIPPER}/extras/ace.py'
  grep -q 'COLORFUL_U1_ROUTE_SELECT' '${REMOTE_TOOLS}/post_process_virtual_toolheads.py'
  grep -q 'not ace_routed' '${REMOTE_KLIPPER}/extras/filament_feed.py'
  grep -q '_pending_load_source' '${REMOTE_KLIPPER}/extras/filament_feed.py'
  echo 'Active Klipper module files match Colorful-U1 ACE files.'
"

echo "== Final state =="
curl -fsS --max-time 8 "http://${HOST}:${MOONRAKER_PORT}/printer/info" || true
echo
echo "Backup: ${REMOTE_BACKUP}"
echo "Done."
