#!/bin/bash
###############################################################################
# stop_network.sh — Clean teardown of the 3000-series broker topology
#
# From the srsRAN ZMQ known issues:
#   "For a clean tear down, the UE needs to be terminated first, then the eNB."
###############################################################################

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log() { echo -e "${GREEN}[STOP]${NC} $*"; }

# Load saved PIDs if available
if [ -f /tmp/srsran_pids.txt ]; then
    source /tmp/srsran_pids.txt
fi

# Step 1: Kill UEs first (per docs)
log "Stopping UEs..."
sudo pkill -f "srsue.*ue[123].conf" 2>/dev/null || true
sleep 2

# Step 2: Kill broker
log "Stopping broker..."
pkill -f "multi_ue_broker.py" 2>/dev/null || true
sleep 1

# Step 3: Kill eNB
log "Stopping eNB..."
sudo pkill -f "srsenb" 2>/dev/null || true
sleep 1

# Step 4: Kill EPC
log "Stopping EPC..."
sudo pkill -f "srsepc" 2>/dev/null || true
sleep 1

# Step 5: Clean up namespaces
log "Removing network namespaces..."
for ns in ue1 ue2 ue3; do
    if sudo ip netns list | grep -qw "$ns"; then
        sudo ip netns delete "$ns"
        log "  Deleted netns: $ns"
    fi
done

# Clean PID file
rm -f /tmp/srsran_pids.txt

log "Network stopped."