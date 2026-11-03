#!/bin/sh
# Installs/starts the listener service on this VM. The VM is replaced
# periodically and /etc does not persist, so re-run this after a replacement.
set -e
sudo -n cp "$(dirname "$0")/../lead-signal-listener.service" /etc/systemd/system/lead-signal-listener.service
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now lead-signal-listener.service
