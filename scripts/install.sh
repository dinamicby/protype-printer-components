#!/usr/bin/env bash
# Protype Printer Components — Installer
#
# Symlinks Moonraker components into ~/moonraker/moonraker/components/
# and adds config sections to moonraker.conf.
#
# Usage:
#   make install
#   # or directly:
#   ./scripts/install.sh [-c /path/to/config]
#
# Copyright (C) 2026 Protype — GPLv3

set -Ee

if [[ ${UID} = "0" ]]; then
  echo "Do not run as root. You will be prompted for sudo if needed."
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$(dirname "${BASH_SOURCE[0]}")")" && pwd)"
MOONRAKER_DIR="${HOME}/moonraker/moonraker/components"
CONFIG_DIR=""

# Parse arguments
while getopts "c:" opt; do
  case ${opt} in
    c) CONFIG_DIR="${OPTARG}" ;;
    *) echo "Usage: $0 [-c config_dir]"; exit 1 ;;
  esac
done

# Auto-detect config directory
if [[ -z "${CONFIG_DIR}" ]]; then
  if [[ -d "${HOME}/printer_data/config" ]]; then
    CONFIG_DIR="${HOME}/printer_data/config"
  elif [[ -d "${HOME}/klipper_config" ]]; then
    CONFIG_DIR="${HOME}/klipper_config"
  else
    echo "ERROR: Cannot find config directory."
    echo "  Tried: ~/printer_data/config, ~/klipper_config"
    echo "  Specify with: $0 -c /path/to/config"
    exit 1
  fi
fi

MOONRAKER_CONF="${CONFIG_DIR}/moonraker.conf"

echo "=============================================="
echo "  Protype Printer Components — Install"
echo "=============================================="
echo
echo "  Source:     ${SRC_DIR}"
echo "  Moonraker:  ${MOONRAKER_DIR}"
echo "  Config:     ${CONFIG_DIR}"
echo

# ── Check dependencies ─────────────────────────────

if [[ ! -d "${HOME}/moonraker" ]]; then
  echo "ERROR: Moonraker not found at ~/moonraker"
  exit 1
fi

if [[ ! -d "${MOONRAKER_DIR}" ]]; then
  echo "ERROR: Moonraker components dir not found: ${MOONRAKER_DIR}"
  exit 1
fi

if [[ ! -f "${MOONRAKER_CONF}" ]]; then
  echo "ERROR: moonraker.conf not found: ${MOONRAKER_CONF}"
  exit 1
fi

# ── Stop Moonraker ─────────────────────────────────

echo "[1/4] Stopping Moonraker..."
MOONRAKER_SERVICE=$(systemctl list-units --full --all -t service --no-legend 2>/dev/null \
  | grep -oP 'moonraker\S*\.service' | head -1 || true)

if [[ -n "${MOONRAKER_SERVICE}" ]]; then
  sudo systemctl stop "${MOONRAKER_SERVICE}" 2>/dev/null || true
  echo "       Stopped ${MOONRAKER_SERVICE}"
else
  echo "       Moonraker service not found (manual restart may be needed)"
fi

# ── Symlink components ─────────────────────────────

echo "[2/4] Linking components..."

COMPONENTS_DIR="${SRC_DIR}/components"
linked=0

for component in "${COMPONENTS_DIR}"/*.py; do
  [[ -f "${component}" ]] || continue
  name=$(basename "${component}")
  target="${MOONRAKER_DIR}/${name}"

  if ln -sf "${component}" "${target}" 2>/dev/null; then
    echo "       ${name} -> OK"
    linked=$((linked + 1))
  else
    echo "       ${name} -> FAILED"
  fi
done

echo "       ${linked} component(s) linked"

# ── Add config sections ────────────────────────────

echo "[3/4] Updating moonraker.conf..."

# bed_surface_calibration
if ! grep -q '^\[bed_surface_calibration\]' "${MOONRAKER_CONF}" 2>/dev/null; then
  cat >> "${MOONRAKER_CONF}" <<'CONFIG'

# ─── Protype: Bed Surface Calibration ────────────────────
[bed_surface_calibration]
stabilize_time: 600
samples_per_point: 10
bed_heater: heater_bed
chamber_heater: Active_Chamber
surface_sensor: bed_glass
tolerance: 2.0
CONFIG
  echo "       Added [bed_surface_calibration]"
else
  echo "       [bed_surface_calibration] already exists"
fi

# ── Start Moonraker ────────────────────────────────

echo "[4/4] Starting Moonraker..."
if [[ -n "${MOONRAKER_SERVICE}" ]]; then
  sudo systemctl start "${MOONRAKER_SERVICE}" 2>/dev/null || true
  echo "       Started ${MOONRAKER_SERVICE}"
else
  echo "       Start Moonraker manually"
fi

echo
echo "=============================================="
echo "  Installation complete!"
echo "=============================================="
echo
echo "  Verify:"
echo "    curl http://localhost:7125/server/calibration/status"
echo
echo "  Update:"
echo "    cd ${SRC_DIR} && git pull && make install"
echo
