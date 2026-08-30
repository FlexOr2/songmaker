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
# /etc/systemd/system/ and the systemd unit cache. `systemctl enable` takes
# effect on the NEXT boot only — it does not touch the running stack.
# Starting the unit immediately (see the final message this script prints)
# runs `docker compose up -d` against whatever is running right now: if
# .env or code changed since the containers were last started, compose may
# recreate them (and re-run the migrate container), which can kill an
# in-flight generation. Do that deliberately, in a maintenance window —
# this script does not do it for you.
#
# The project root and the user the unit runs as are derived from where
# this script lives, not hardcoded, so running it from a worktree installs
# a unit pointing at that worktree rather than silently at the main
# checkout.
#
# Idempotent for `enable`: rerunning it re-copies the unit file and
# re-applies enable, both no-ops if already applied. If the unit file
# content changed since the last install (e.g. this script itself is a
# newer version), `daemon-reload` picks up the new file immediately, but a
# unit that is already active (RemainAfterExit=yes) only picks up a new
# ExecStart on the next boot or an explicit `systemctl restart` — see
# docs/acestep.md.
#
# Usage:
#   ./scripts/install-autostart.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
INSTALL_USER="${SUDO_USER:-$(id -un)}"
UNIT_SOURCE="$SCRIPT_DIR/songmaker.service"
UNIT_TARGET="/etc/systemd/system/songmaker.service"

if [ ! -f "$UNIT_SOURCE" ]; then
    echo "ERROR: $UNIT_SOURCE not found." >&2
    exit 1
fi

echo "Installing $UNIT_TARGET (WorkingDirectory=$PROJECT_ROOT, User=$INSTALL_USER)..."
sed -e "s#^WorkingDirectory=.*#WorkingDirectory=$PROJECT_ROOT#" \
    -e "s#^User=.*#User=$INSTALL_USER#" \
    "$UNIT_SOURCE" | sudo tee "$UNIT_TARGET" > /dev/null

echo "Reloading systemd unit files..."
sudo systemctl daemon-reload

echo "Enabling songmaker.service to run on every future boot..."
sudo systemctl enable songmaker.service

echo
echo "Done. songmaker.service will run 'docker compose up -d' starting with the NEXT boot."
echo "It has NOT touched the currently running stack."
echo
echo "To verify what's installed:"
echo "  systemctl status songmaker.service"
echo "  docker compose -f $PROJECT_ROOT/docker-compose.yml ps -a"
echo
echo "To apply it right now instead of waiting for a reboot (touches the live"
echo "stack — may recreate containers and kill an in-flight generation if .env"
echo "or code changed since the last start; do this in a maintenance window):"
echo "  sudo systemctl start songmaker.service"
