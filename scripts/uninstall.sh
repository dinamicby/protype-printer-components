#!/usr/bin/env bash
# Protype Printer Components — Uninstaller
#
# Removes symlinks from ~/moonraker/moonraker/components/.
# Does NOT remove config sections from moonraker.conf (safe).
#
# Copyright (C) 2026 Protype — GPLv3

set -Ee

if [[ ${UID} = "0" ]]; then
  echo "Do not run as root."
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$(dirname "${BASH_SOURCE[0]}")")" && pwd)"
MOONRAKER_DIR="${HOME}/moonraker/moonraker/components"

echo "=============================================="
echo "  Protype Printer Components — Uninstall"
echo "=============================================="
echo

# ── Stop Moonraker ─────────────────────────────────

echo "[1/3] Stopping Moonraker..."
MOONRAKER_SERVICE=$(systemctl list-units --full --all -t service --no-legend 2>/dev/null \
  | grep -oP 'moonraker\S*\.service' | head -1 || true)

if [[ -n "${MOONRAKER_SERVICE}" ]]; then
  sudo systemctl stop "${MOONRAKER_SERVICE}" 2>/dev/null || true
fi

# ── Remove symlinks ────────────────────────────────

echo "[2/3] Removing symlinks..."

COMPONENTS_DIR="${SRC_DIR}/components"
removed=0

for component in "${COMPONENTS_DIR}"/*.py; do
  [[ -f "${component}" ]] || continue
  name=$(basename "${component}")
  target="${MOONRAKER_DIR}/${name}"

  if [[ -L "${target}" ]]; then
    rm -f "${target}"
    echo "       Removed ${name}"
    removed=$((removed + 1))
  fi
done

echo "       ${removed} symlink(s) removed"

# ── Start Moonraker ────────────────────────────────

echo "[3/3] Starting Moonraker..."
if [[ -n "${MOONRAKER_SERVICE}" ]]; then
  sudo systemctl start "${MOONRAKER_SERVICE}" 2>/dev/null || true
fi

echo
echo "Uninstall complete."
echo
echo "Note: Config sections in moonraker.conf were NOT removed."
echo "Remove [bed_surface_calibration] manually if needed."
echo
