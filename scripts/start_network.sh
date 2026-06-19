#!/bin/bash
###############################################################################
# start_network.sh — Launch the full 3000-series broker topology
#
# Startup order (from srsRAN ZMQ app note):
#   1. Create network namespaces
#   2. srsEPC
#   3. srsENB
#   4. GRC Broker (UE won't connect until broker links UL/DL)
#   5. srsUE x3 (staggered ~10s apart)
#
# Each component runs in its own terminal via gnome-terminal.
# If you don't have gnome-terminal, switch to xterm or run manually.
#
# Known issues from the docs:
#   - For clean teardown: terminate UEs first, then eNB, then EPC
#   - eNB and UE can only run once; after UE detach, restart eNB
#   - UE won't connect until the GRC broker is running
###############################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="${SCRIPT_DIR}/../configs"
BROKER_DIR="${SCRIPT_DIR}/../broker"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[NET]${NC} $*"; }
warn() { echo -e "${YELLOW}[NET]${NC} $*"; }
info() { echo -e "${CYAN}[NET]${NC} $*"; }

###############################################################################
# Preflight checks
###############################################################################
for cmd in srsepc srsenb srsue python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}[ERROR]${NC} $cmd not found in PATH. Run install.sh first."
        exit 1
    fi
done

# Verify GNU Radio ZMQ module
if ! python3 -c "from gnuradio import zeromq" 2>/dev/null; then
    echo -e "${RED}[ERROR]${NC} GNU Radio ZMQ blocks not available."
    exit 1
fi

# Verify config files exist
for f in epc.conf enb.conf rr.conf user_db.csv ue1.conf ue2.conf ue3.conf; do
    if [ ! -f "${CONF_DIR}/${f}" ]; then
        echo -e "${RED}[ERROR]${NC} Missing config: ${CONF_DIR}/${f}"
        exit 1
    fi
done

###############################################################################
# 1. Create network namespaces
###############################################################################
log "Creating network namespaces..."
for ns in ue1 ue2 ue3; do
    if ! sudo ip netns list | grep -qw "$ns"; then
        sudo ip netns add "$ns"
        log "  Created netns: $ns"
    else
        info "  netns $ns already exists"
    fi
done

###############################################################################
# 2. Launch srsEPC
###############################################################################
log "Starting srsEPC..."
info "  Config: ${CONF_DIR}/epc.conf"
info "  User DB: ${CONF_DIR}/user_db.csv"

# Run in background, log to file
sudo srsepc "${CONF_DIR}/epc.conf" > /tmp/epc_console.log 2>&1 &
EPC_PID=$!
log "  srsEPC started (PID: ${EPC_PID})"
sleep 2

# Verify EPC is running
if ! kill -0 "$EPC_PID" 2>/dev/null; then
    echo -e "${RED}[ERROR]${NC} srsEPC failed to start. Check /tmp/epc.log and /tmp/epc_console.log"
    exit 1
fi

###############################################################################
# 3. Launch srsENB
###############################################################################
log "Starting srsENB..."
info "  Config: ${CONF_DIR}/enb.conf"
info "  RR config: ${CONF_DIR}/rr.conf"
info "  ZMQ ports: TX=3101, RX=3100"

sudo srsenb "${CONF_DIR}/enb.conf" \
    --enb_files.rr_config="${CONF_DIR}/rr.conf" \
    > /tmp/enb_console.log 2>&1 &
ENB_PID=$!
log "  srsENB started (PID: ${ENB_PID})"
sleep 3

if ! kill -0 "$ENB_PID" 2>/dev/null; then
    echo -e "${RED}[ERROR]${NC} srsENB failed to start. Check /tmp/enb.log and /tmp/enb_console.log"
    sudo kill "$EPC_PID" 2>/dev/null || true
    exit 1
fi

###############################################################################
# 4. Launch GRC Broker
#    From the ZMQ app note: "the UE will not connect to the eNB until the
#    broker has been started, as the UL and DL channels are not directly
#    connected between the UE and eNB."
###############################################################################
log "Starting GRC broker..."
info "  eNB DL fan-out → UE1(3000), UE2(3010), UE3(3020)"
info "  UE1(3001) + UE2(3011) + UE3(3021) → eNB UL(3100)"

python3 "${BROKER_DIR}/multi_ue_broker.py" > /tmp/broker_console.log 2>&1 &
BROKER_PID=$!
log "  Broker started (PID: ${BROKER_PID})"
sleep 3

if ! kill -0 "$BROKER_PID" 2>/dev/null; then
    echo -e "${RED}[ERROR]${NC} Broker failed to start. Check /tmp/broker_console.log"
    sudo kill "$ENB_PID" "$EPC_PID" 2>/dev/null || true
    exit 1
fi

###############################################################################
# 5. Launch UEs (staggered ~10s apart to avoid RACH collision)
#    From the handover app note: UEs connect sequentially.
###############################################################################
UE_PIDS=()

for i in 1 2 3; do
    log "Starting srsUE ${i}..."
    info "  Config: ${CONF_DIR}/ue${i}.conf"
    info "  Netns: ue${i}"

    sudo srsue "${CONF_DIR}/ue${i}.conf" \
        > "/tmp/ue${i}_console.log" 2>&1 &
    UE_PIDS+=($!)
    log "  srsUE${i} started (PID: ${UE_PIDS[-1]})"

    if [ "$i" -lt 3 ]; then
        info "  Waiting 10s before next UE (stagger for RACH)..."
        sleep 10
    fi
done

###############################################################################
# Summary
###############################################################################
echo ""
log "============================================"
log "NETWORK STARTED"
log "============================================"
echo ""
info "  PIDs:"
info "    EPC:    ${EPC_PID}"
info "    eNB:    ${ENB_PID}"
info "    Broker: ${BROKER_PID}"
info "    UE1:    ${UE_PIDS[0]}"
info "    UE2:    ${UE_PIDS[1]}"
info "    UE3:    ${UE_PIDS[2]}"
echo ""
info "  Logs:"
info "    EPC:    /tmp/epc.log       (console: /tmp/epc_console.log)"
info "    eNB:    /tmp/enb.log       (console: /tmp/enb_console.log)"
info "    Broker: /tmp/broker_console.log"
info "    UE1:    /tmp/ue1.log       (console: /tmp/ue1_console.log)"
info "    UE2:    /tmp/ue2.log       (console: /tmp/ue2_console.log)"
info "    UE3:    /tmp/ue3.log       (console: /tmp/ue3_console.log)"
echo ""
info "  Verify connections:"
info "    sudo ip netns exec ue1 ping 172.16.0.1"
info "    sudo ip netns exec ue2 ping 172.16.0.1"
info "    sudo ip netns exec ue3 ping 172.16.0.1"
echo ""
info "  To stop: run ./stop_network.sh"
echo ""

# Save PIDs for stop script
cat > /tmp/srsran_pids.txt << EOF
EPC_PID=${EPC_PID}
ENB_PID=${ENB_PID}
BROKER_PID=${BROKER_PID}
UE1_PID=${UE_PIDS[0]}
UE2_PID=${UE_PIDS[1]}
UE3_PID=${UE_PIDS[2]}
EOF

# Wait for any child to exit
wait