#!/bin/bash
# Installs the Songmaker boot-autostart systemd unit (issue #256).
#
# `restart: unless-stopped` only revives a container that has already
# started at least once. A container docker compose merely *created* but
# never started (e.g. a failed `docker compose up` after a driver mismatch)
# is ignored by dockerd on every daemon start and survives any number of
# reboots on its own. This unit closes that gap: it runs
# `docker compose up -d` (no `--build`) once per boot, after docker.service
# is up, which starts every container regardless of the state it was left
# in — running, exited, or created.
#
# The OPERATOR runs this script, not an agent. It only touches
# /etc/systemd/system/ and the systemd unit cache; it never starts, stops,
# or rebuilds any Songmaker container itself.
#
# Idempotent: rerunning it re-copies the (identical) unit file and re-applies
# enable/start, both no-ops if already applied.
#
# Usage:
#   ./scripts/install-autostart.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SOURCE="$SCRIPT_DIR/songmaker.service"
UNIT_TARGET="/etc/systemd/system/songmaker.service"

if [ ! -f "$UNIT_SOURCE" ]; then
    echo "ERROR: $UNIT_SOURCE not found." >&2
    exit 1
fi

echo "Installing $UNIT_TARGET from $UNIT_SOURCE..."
sudo cp "$UNIT_SOURCE" "$UNIT_TARGET"

echo "Reloading systemd unit files..."
sudo systemctl daemon-reload

echo "Enabling songmaker.service to run on every boot, and starting it now..."
sudo systemctl enable --now songmaker.service

echo
echo "Done. Verify with:"
echo "  systemctl status songmaker.service"
echo "  docker compose -f /home/felix-hummert/git/songmaker/docker-compose.yml ps -a"
