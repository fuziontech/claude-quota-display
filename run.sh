#!/bin/bash
# Launcher for the Claude quota display. Runs the pygame kiosk app.
cd "$(dirname "$0")" || exit 1
export PYGAME_HIDE_SUPPORT_PROMPT=1
exec python3 -u quota_display.py
