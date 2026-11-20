#!/bin/sh
# Installs the autorun service on one Pi. Run as root from the repo folder:
#   sudo sh deploy/install.sh            install + enable service (no reboot)
#   sudo sh deploy/install.sh --disable-radios   also disable Wi-Fi/Bluetooth (needs reboot)
# Does not start the service and never reboots. Not tested on a board.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ -x "$REPO/.venv/bin/python" ] || { echo "missing $REPO/.venv (see README install steps)" >&2; exit 1; }
CMDLINE=/boot/firmware/cmdline.txt
if grep -q 'g_serial' "$CMDLINE" 2>/dev/null; then
  echo "cmdline.txt loads g_serial (USB console). The single USB port cannot also host a stick." >&2
  echo "Revert that console setup first (keep a backup of cmdline.txt/config.txt)." >&2
  exit 1
fi
if [ ! -f /etc/pt-radio.env ]; then
  cat > /etc/pt-radio.env <<ENV
# PT_RATE must match on both Pis: 250k or 2m
PT_RATE=
# Owner's attestation that wiring/power/antenna checks in hardware.md were done
PT_HARDWARE_CHECKED=
PT_PYTHON=$REPO/.venv/bin/python
PT_EXTRA_ARGS=--batch
ENV
  chmod 644 /etc/pt-radio.env
  echo "Created /etc/pt-radio.env: fill PT_RATE and PT_HARDWARE_CHECKED=yes"
fi
mkdir -p /var/lib/pt-radio /mnt/pt-usb
sed "s#@REPO@#$REPO#g" "$REPO/deploy/pt-radio.service" > /etc/systemd/system/pt-radio.service
systemctl daemon-reload
systemctl enable pt-radio.service
systemctl enable pigpiod.service 2>/dev/null || echo "enable pigpiod yourself (local-only)" >&2
if [ "${1:-}" = "--disable-radios" ]; then
  CFG=/boot/firmware/config.txt
  cp -n "$CFG" "$CFG.pt-backup"
  printf '[all]\n' >> "$CFG"
  for line in dtoverlay=disable-wifi dtoverlay=disable-bt; do
    grep -qx "$line" "$CFG" || printf '%s\n' "$line" >> "$CFG"
  done
  systemctl disable hciuart.service bluetooth.service 2>/dev/null || true
  echo "Wi-Fi/Bluetooth disabled after reboot. SSH over Wi-Fi will stop working."
fi
echo "Installed. Start now with: systemctl start pt-radio  (or reboot)."
