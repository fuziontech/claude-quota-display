#!/bin/bash
# Installer for the Claude Quota Display on Raspberry Pi OS (labwc / Wayland).
#
# Idempotent: safe to run more than once. It will
#   1. install python3-pygame if missing,
#   2. enable autohide on the wf-panel-pi top bar,
#   3. add the app to the labwc autostart (auto-restart via lwrespawn),
#   4. optionally start it right now without a reboot.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"

echo "==> Claude Quota Display installer"
echo "    repo: $REPO_DIR"

# 1. dependencies -----------------------------------------------------------
if ! python3 -c 'import pygame' 2>/dev/null; then
  echo "==> Installing python3-pygame (sudo)"
  sudo apt-get update
  sudo apt-get install -y python3-pygame
else
  echo "==> pygame already present"
fi

# 2. hide the top bar -------------------------------------------------------
echo "==> Enabling panel autohide"
mkdir -p "$CONFIG/wf-panel-pi"
cat > "$CONFIG/wf-panel-pi/wf-panel-pi.ini" <<'EOF'
[panel]
autohide=true
autohide_duration=300
EOF

# 3. autostart --------------------------------------------------------------
echo "==> Wiring up labwc autostart"
mkdir -p "$CONFIG/labwc"
AUTOSTART="$CONFIG/labwc/autostart"
LAUNCH="/usr/bin/lwrespawn $REPO_DIR/run.sh &"

# Seed a user autostart the first time. labwc runs the user file *instead of*
# the system default, so we must reproduce the desktop's own startup entries —
# otherwise the panel, desktop and output config would never launch.
if [ ! -f "$AUTOSTART" ]; then
  if [ -f /etc/xdg/labwc/autostart ]; then
    cp /etc/xdg/labwc/autostart "$AUTOSTART"
  else
    echo "    no /etc/xdg/labwc/autostart found — writing the standard defaults"
    cat > "$AUTOSTART" <<'EOF'
/usr/bin/lwrespawn /usr/bin/pcmanfm-pi &
/usr/bin/lwrespawn /usr/bin/wf-panel-pi &
/usr/bin/kanshi &
/usr/bin/lxsession-xdg-autostart
EOF
  fi
fi
touch "$AUTOSTART"

if grep -qF "$REPO_DIR/run.sh" "$AUTOSTART"; then
  echo "    already in autostart"
else
  printf '\n# Claude quota display\n%s\n' "$LAUNCH" >> "$AUTOSTART"
  echo "    added launch line"
fi

chmod +x "$REPO_DIR/run.sh"

# 4. start now (optional) ---------------------------------------------------
echo
read -r -p "Start the display now (no reboot needed)? [Y/n] " ans
ans="${ans:-Y}"
if [[ "$ans" =~ ^[Yy] ]]; then
  if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "    WAYLAND_DISPLAY not set — run this from the Pi's desktop session,"
    echo "    or just reboot. Skipping live start."
  else
    systemctl --user reset-failed claude-quota.service 2>/dev/null || true
    systemd-run --user --unit=claude-quota \
      --setenv=WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
      --setenv=XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
      --setenv=PYGAME_HIDE_SUPPORT_PROMPT=1 \
      "$REPO_DIR/run.sh"
    echo "    started as user service 'claude-quota'"
  fi
fi

echo
echo "==> Done. It will appear on every boot. Push the mouse to the top edge"
echo "    to reveal the (now autohidden) taskbar."
