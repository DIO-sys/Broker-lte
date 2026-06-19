#!/bin/bash
###############################################################################
# verify_network.sh — Check that all 3 UEs are attached and reachable
###############################################################################

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0

check() {
    local desc="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} ${desc}"
        ((PASS++))
    else
        echo -e "  ${RED}✗${NC} ${desc}"
        ((FAIL++))
    fi
}

echo ""
echo "=== Process checks ==="
check "srsEPC running"      pgrep -f srsepc
check "srsENB running"      pgrep -f srsenb
check "Broker running"      pgrep -f multi_ue_broker
check "srsUE1 running"      pgrep -f "ue1.conf"
check "srsUE2 running"      pgrep -f "ue2.conf"
check "srsUE3 running"      pgrep -f "ue3.conf"

echo ""
echo "=== Namespace checks ==="
check "netns ue1 exists"    sudo ip netns list | grep -qw ue1
check "netns ue2 exists"    sudo ip netns list | grep -qw ue2
check "netns ue3 exists"    sudo ip netns list | grep -qw ue3

echo ""
echo "=== Connectivity checks (3 pings each) ==="
check "UE1 → EPC ping"     sudo ip netns exec ue1 ping -c 3 -W 5 172.16.0.1
check "UE2 → EPC ping"     sudo ip netns exec ue2 ping -c 3 -W 5 172.16.0.1
check "UE3 → EPC ping"     sudo ip netns exec ue3 ping -c 3 -W 5 172.16.0.1

echo ""
echo "=== EPC attach log check ==="
if [ -f /tmp/epc_console.log ]; then
    ATTACH_COUNT=$(grep -c "Network attach successful\|Attach Request\|Sending EMM Information" /tmp/epc_console.log 2>/dev/null || echo "0")
    echo -e "  Attach-related log entries: ${ATTACH_COUNT}"
fi

echo ""
echo "=== Summary ==="
echo -e "  Passed: ${GREEN}${PASS}${NC}  Failed: ${RED}${FAIL}${NC}"

if [ "$FAIL" -eq 0 ]; then
    echo -e "\n  ${GREEN}All checks passed. Network is operational.${NC}\n"
else
    echo -e "\n  ${YELLOW}Some checks failed. Check logs in /tmp/ for details.${NC}"
    echo -e "  ${YELLOW}Common issues:${NC}"
    echo -e "    - UEs not attached yet: wait 30s after start and retry"
    echo -e "    - IMSI mismatch: compare user_db.csv with ue*.conf"
    echo -e "    - base_srate mismatch: must be 23.04e6 everywhere"
    echo -e "    - dl_earfcn mismatch: must be 2850 everywhere"
    echo ""
fi