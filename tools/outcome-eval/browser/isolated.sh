#!/bin/bash
# Runs a command in a loopback-only network namespace with the install's ports bridged to the host loopback.
# Chromium aborts in-flight requests with ERR_NETWORK_CHANGED whenever a host interface changes, which a shared
# host does each time another job starts a container network. Needs passwordless sudo for `unshare -n` only;
# the command itself runs as the calling user.
#
# Usage: OUTCOME_BRIDGE_PORTS="<console port> <api port>" isolated.sh <command> [argument ...]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
ports=${OUTCOME_BRIDGE_PORTS:?set OUTCOME_BRIDGE_PORTS to the ports the browser must reach}
sockets=$(mktemp -d /tmp/outcome-bridge.XXXXXX)
bridges=()
trap 'kill "${bridges[@]}" 2>/dev/null || true; rm -rf "$sockets"' EXIT

for port in $ports; do
  python3 "$here/loopback_bridge.py" unix-to-tcp "$sockets/$port.sock" "$port" &
  bridges+=($!)
done

export -p > "$sockets/environment.sh"
printf '%q ' "$@" > "$sockets/command.sh"
cat > "$sockets/inside.sh" <<INSIDE
source "$sockets/environment.sh"
cd $(printf %q "$PWD")
for port in $ports; do
  python3 "$here/loopback_bridge.py" tcp-to-unix "\$port" "$sockets/\$port.sock" &
done
sleep 0.5
bash "$sockets/command.sh"
status=\$?
kill \$(jobs -p) 2>/dev/null
exit \$status
INSIDE
chmod 644 "$sockets"/*.sh
sleep 0.5

sudo -n unshare -n -- bash -c "ip link set lo up; exec sudo -n -u $(id -un) bash $sockets/inside.sh"
