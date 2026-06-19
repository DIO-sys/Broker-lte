#!/bin/bash
# Live metric watcher — shows latest line from each UE every second
while true; do
  clear
  echo "=== UE Metrics ($(date +%H:%M:%S)) ==="
  echo ""
  for i in 1 2 3; do
    LINE=$(tail -2 /tmp/ue${i}_metrics.csv 2>/dev/null | head -1)
    if [ -n "$LINE" ]; then
      BRATE_DL=$(echo "$LINE" | cut -d';' -f14)
      BRATE_UL=$(echo "$LINE" | cut -d';' -f21)
      SNR=$(echo "$LINE" | cut -d';' -f12)
      MCS_DL=$(echo "$LINE" | cut -d';' -f11)
      BLER_DL=$(echo "$LINE" | cut -d';' -f15)
      ATTACHED=$(echo "$LINE" | cut -d';' -f26)
      printf "  UE%d  attached=%-3s  snr=%-6s  mcs=%-4s  bler=%-5s  dl=%-12s  ul=%-12s\n" \
        "$i" "$ATTACHED" "$SNR" "$MCS_DL" "$BLER_DL" "$BRATE_DL" "$BRATE_UL"
    else
      printf "  UE%d  no data\n" "$i"
    fi
  done
  sleep 1
done
