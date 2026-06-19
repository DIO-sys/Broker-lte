# Project Context — srsRAN 4G ZMQ Broker Network

## What This Is

A private 4G LTE network running entirely in software on one machine. One eNB serves three UEs through a GNU Radio ZMQ broker that fans out the downlink and sums the uplinks. No physical radio hardware. This is the substrate for a four-layer intelligent control system (RF classifier, anomaly detector, agentic controller, visualizer) described in the project spec.

## What Was Built in This Session

### Network Stack (3000-Series Broker Topology)
- srsEPC — core network (MME + HSS + SPGW)
- srsENB — single cell, ZMQ RF on ports 3101/3100
- GRC Broker — Python GNU Radio flowgraph (`multi_ue_broker.py`) doing 1-to-3 DL fanout and 3-to-1 UL summation
- 3 × srsUE — separate processes, separate configs, separate network namespaces

### Monitoring
- FastAPI backend (`monitor/backend.py`) reading srsRAN CSV metrics via file tail, serving REST + WebSocket
- HTML frontend (`monitor/frontend.html`) showing live per-UE topology, RF metrics, throughput, CPU, memory, SNR sparklines
- Metric watcher script (`scripts/watch_metrics.sh`) for terminal-based monitoring

### Traffic Generation
- Bidirectional G.711 voice-profile traffic via iperf3 (`--bidir -u -b 64K -l 160`)
- Differentiated traffic profiles: voice on UE1/UE2, keepalive ping on UE3

---

## Machine Specs & Constraints

- Ubuntu 24.04, 16 GB RAM, 16 CPU cores
- Python 3.13 (requires `python3.13-venv` for venv creation)
- **50 PRB (23.04 MHz) is too heavy** — each srsue process uses ~970 MB resident memory. With 3 UEs + eNB + broker + EPC + VS Code, total exceeds 16 GB and the OS force-kills processes. **Switched to 25 PRB (11.52 MHz)** which halves memory per process to ~500 MB.

---

## Port Map

| Element | TX Port | RX Port |
|---------|---------|---------|
| eNB     | 3101    | 3100    |
| UE1     | 3001    | 3000    |
| UE2     | 3011    | 3010    |
| UE3     | 3021    | 3020    |

---

## Config Parameters That Must Match Everywhere

| Parameter  | Value   | Where                          |
|------------|---------|--------------------------------|
| dl_earfcn  | 2850    | enb.conf, rr.conf, ue*.conf   |
| base_srate | 11.52e6 | enb.conf, ue*.conf, broker.py |
| n_prb      | 25      | enb.conf                       |
| mcc        | 901     | epc.conf, enb.conf             |
| mnc        | 70      | epc.conf, enb.conf             |
| tac        | 0x0007  | epc.conf, rr.conf              |

If ANY of these mismatch between files, things fail silently or with cryptic errors.

---

## Startup Order (Strict)

1. `netns-up` — create network namespaces
2. `epc` — wait for `SP-GW Initialized.`
3. `enb` — wait for `==== eNodeB started ===`
4. `ue1` (own terminal) — wait for `Attaching UE...`
5. `ue2` (own terminal)
6. `ue3` (own terminal)
7. `broker` — this is what triggers all 3 UEs to connect and get IPs
8. `monitor` (optional) — must be in venv

Each component MUST be in its own VS Code terminal. You need 6-8 terminals open simultaneously.

## Teardown Order (Also Strict)

Per the srsRAN docs: "eNB and UE can only run once; after UE detach, restart eNB."

1. Kill UEs first
2. Kill broker
3. Kill eNB
4. Kill EPC
5. `netns-down`

Or just `killnet` then `netns-down`.

After teardown, you MUST restart everything fresh. You cannot re-attach a UE to a running eNB that previously had UEs attached and detached. This is a known srsRAN ZMQ limitation.

---

## Shell Aliases (in ~/.bashrc)

```bash
alias epc='sudo srsepc /home/timodagoat/code/Broker-lte/configs/epc.conf'
alias enb='sudo srsenb /home/timodagoat/code/Broker-lte/configs/enb.conf'
alias ue1='sudo stdbuf -oL srsue /home/timodagoat/code/Broker-lte/configs/ue1.conf'
alias ue2='sudo stdbuf -oL srsue /home/timodagoat/code/Broker-lte/configs/ue2.conf'
alias ue3='sudo stdbuf -oL srsue /home/timodagoat/code/Broker-lte/configs/ue3.conf'
alias broker='cd ~/code/Broker-lte && python3 broker/multi_ue_broker.py'
alias monitor='cd ~/code/Broker-lte/monitor && source venv/bin/activate && python3 backend.py'
alias netns-up='sudo ip netns add ue1 2>/dev/null; sudo ip netns add ue2 2>/dev/null; sudo ip netns add ue3 2>/dev/null; echo "namespaces ready"'
alias netns-down='sudo ip netns delete ue1 2>/dev/null; sudo ip netns delete ue2 2>/dev/null; sudo ip netns delete ue3 2>/dev/null; echo "namespaces removed"'
alias ping1='sudo ip netns exec ue1 ping 172.16.0.1'
alias ping2='sudo ip netns exec ue2 ping 172.16.0.1'
alias ping3='sudo ip netns exec ue3 ping 172.16.0.1'
alias pingall='sudo ip netns exec ue1 ping 172.16.0.1 & sudo ip netns exec ue2 ping 172.16.0.1 & sudo ip netns exec ue3 ping 172.16.0.1 &'
alias killnet='sudo pkill -f srsue; sleep 1; pkill -f multi_ue_broker; sleep 1; sudo pkill -f srsenb; sleep 1; sudo pkill -f srsepc; echo "network stopped"'
alias killiperf='sudo pkill -9 iperf3; echo "iperf stopped"'
alias watchmetrics='~/code/Broker-lte/scripts/watch_metrics.sh'
```

IMPORTANT: After adding aliases, run `source ~/.bashrc` in EVERY open terminal. VS Code terminals don't auto-reload — new aliases won't exist until sourced or a new terminal is opened.

---

## Networking Lessons Learned (The Hard Way)

### sudo changes the working directory context
Config files with relative paths (like `db_file = user_db.csv`) break under sudo because sudo resolves paths from root's home, not yours. **Always use absolute paths in config files.** We hit this with:
- `epc.conf` → `db_file` needed absolute path to `user_db.csv`
- `enb.conf` → `rr_config` needed absolute path to `rr.conf`

### Network namespaces + backgrounding don't mix
`sudo ip netns exec ue1 iperf3 ... &` does NOT reliably background inside a namespace. The `sudo` + `netns` + `&` combination sometimes silently fails — the process appears to start but never actually connects. **Always run netns commands in their own dedicated terminal**, never backgrounded.

Similarly, `pingall` (which backgrounds 3 pings with `&`) works but can be flaky. For reliable verification, run `ping1`, `ping2`, `ping3` in separate terminals.

### srsEPC SPGW does not hairpin UE-to-UE traffic
`sudo ip netns exec ue1 ping 172.16.0.3` (UE1 → UE2) returns 100% packet loss. This is a known srsEPC limitation — the SPGW doesn't route traffic between GTP tunnels. All traffic must go UE → EPC (172.16.0.1). For voice simulation, this doesn't matter — the scheduler allocates PRBs the same regardless of destination.

We tried enabling forwarding:
```bash
sudo sysctl net.ipv4.ip_forward=1
sudo iptables -A FORWARD -i srs_spgw_sgi -o srs_spgw_sgi -j ACCEPT
```
This did not fix it. The limitation is in the SPGW's GTP tunnel handling, not IP forwarding.

### UEs detach if there's no traffic
If you start UEs and don't send any traffic (ping or iperf), they will eventually detach. Always start pings immediately after attach. The `pingall` alias exists for this reason.

### Zombie processes hold ZMQ ports
If a process dies uncleanly (OOM kill, Ctrl+C at the wrong time, machine reboot), the ZMQ ports can remain bound. Symptoms: eNB or broker starts but UEs never connect, or you get "Address already in use." Fix:
```bash
sudo pkill -9 -f srsue
sudo pkill -9 -f srsenb
sudo pkill -9 -f srsepc
pkill -9 -f multi_ue_broker
pkill -9 -f python3  # careful — kills all python
sleep 2
```
Then verify nothing is holding ports: `ps aux | grep srs`

### The broker MUST start last
From the srsRAN ZMQ app note: UEs cannot reach the eNB without the broker. The UEs start and sit at "Attaching UE..." until the broker links the ZMQ channels. Starting the broker is what triggers the actual RF connection.

### CSV metrics arrive in bursts, not per-second
srsRAN writes metrics to CSV files but the OS buffers file writes. You get ~13 lines flushed every ~13 seconds instead of 1 line per second. This is OS-level file buffer behavior (buffer fills at ~4KB, each line is ~300 bytes). There is no srsRAN config to change this. We added `stdbuf -oL` to the UE aliases which helps with stdout but doesn't fully fix file writes. The watcher script and backend handle this by reading whatever's available.

Implication: when you inject a fault or change traffic, expect ~10 seconds before seeing it reflected in metrics. This is acceptable for the anomaly detector (which uses 30-60 second sliding windows) but can be confusing during manual testing.

### iperf3 needs --bidir for realistic voice traffic
Without `--bidir`, iperf3 sends traffic only client→server (uplink only). DL throughput shows 0. With `--bidir`, both directions are active simultaneously — matching a real phone call. Also, `Ctrl+C` on backgrounded iperf doesn't always kill it cleanly. Use `killiperf` (which does `sudo pkill -9 iperf3`).

### You can't re-attach UEs without restarting everything
This is a fundamental srsRAN ZMQ limitation documented in their app note. Once a UE detaches (or the broker goes down), you must tear down and restart the entire stack: EPC, eNB, all UEs, broker. There is no "reconnect." `killnet` + fresh start is the only path.

---

## Config File Gotchas We Hit

### user_db.csv format is fragile
- No header row allowed (srsRAN's parser treats `ue_name,auth,...` as a data row and chokes)
- Must NOT have a `ue_name,auth,imsi,...` column header line
- OPc hex must be lowercase to match the default format
- Opening/editing in Excel or VS Code CSV mode can add stray quotes that corrupt the file
- We debugged this with `hexdump -C configs/user_db.csv | head -20` to find hidden characters

### rr.conf format varies by srsRAN version
The rr.conf we generated initially didn't match the installed version's parser. Key differences:
- `sched_request_cnfg` requires `nof_prb` field in newer versions
- `cqi_report_cnfg` requires `m_ri` field
- `meas_report_desc` uses different syntax (`eventA = 3` vs nested objects)
- `nr_cell_list` section required even if empty
- **Solution: always base your rr.conf on the default at `/root/.config/srsran/rr.conf`**, don't write from scratch

### ue.conf structure differs from documentation
- `dl_earfcn` goes under `[rat.eutra]`, NOT `[rf]` — this is version-specific
- `[pcap]` section options vary — `mac_filename` doesn't exist in some versions, just `filename`
- **Solution: check `/root/.config/srsran/ue.conf` for the actual format your installed version expects**

### enb.conf pcap section
- `mac_filename` and `mac_nr_filename` don't exist in all versions
- Some versions only accept `enable` and `filename`
- If you get `unrecognised option` errors, strip the pcap section down to just `enable = false` and `filename = /tmp/enb_mac.pcap`

---

## File Structure

```
~/code/Broker-lte/
├── configs/
│   ├── epc.conf              # All paths must be absolute
│   ├── enb.conf              # n_prb=25, base_srate=11.52e6
│   ├── rr.conf               # Based on system default, dl_earfcn=2850
│   ├── user_db.csv           # 3 entries, NO header row, lowercase hex
│   ├── ue1.conf              # IMSI ...789, ports 3001/3000
│   ├── ue2.conf              # IMSI ...790, ports 3011/3010
│   └── ue3.conf              # IMSI ...791, ports 3021/3020
├── broker/
│   └── multi_ue_broker.py    # GNU Radio flowgraph, per-UE gain API exposed
├── monitor/
│   ├── backend.py            # FastAPI, reads CSVs, WebSocket push
│   ├── frontend.html         # Topology dashboard, live metrics
│   ├── requirements.txt      # fastapi, uvicorn
│   └── venv/                 # Python venv (don't use system python for fastapi)
├── scripts/
│   ├── install.sh            # Full build from source
│   ├── start_network.sh      # Automated startup (alternative to aliases)
│   ├── stop_network.sh       # Clean teardown
│   ├── verify_network.sh     # Check all UEs attached
│   └── watch_metrics.sh      # Terminal metric watcher
└── README.md
```

---

## Software Versions

- Ubuntu 24.04
- Python 3.13
- srsRAN 4G — commit 6bcbd9e5b, branch master
- GNU Radio 3.10.12.0
- libzmq — built from source (required for srsRAN ZMQ plugin)
- czmq — built from source
- FastAPI 0.137.2
- uvicorn 0.49.0

---

## What's Ready for Next Session

### Broker Gain API
The `multi_ue_broker.py` exposes per-UE gain controls:
```python
tb.set_gain_ue1_dl(gain)  # 0.0 = dropout, 0.3 = weak, 1.0 = full
tb.set_gain_ue2_dl(gain)
tb.set_gain_ue3_dl(gain)
tb.set_gain_ue1_ul(gain)  # same for uplink
# etc.
```
These are not yet wired to any external control. Next step: expose via a control socket or REST endpoint so the Markov fault engine and the dashboard can manipulate them.

### Per-UE Distinction Strategy
Three methods identified to maximize per-UE metric divergence:
1. **Asymmetric broker gains** — different DL gains per UE → different MCS/BLER
2. **Differentiated traffic** — voice (64K bidir) vs video (1M bidir) vs ping
3. **QCI differentiation** — change QCI values in user_db.csv per UE

### Next Layers (from project spec)
1. Wire broker gains to a control API (REST or ZMQ PUB/SUB)
2. Markov fault engine driving the gain API
3. Manual fault injection from the dashboard
4. RF signal classifier (CNN on IQ spectrograms)
5. Anomaly detector (LSTM autoencoder on InfluxDB metrics)
6. Agentic controller (LLM-based, Anthropic API)
7. Full visualizer with operator control panel

### Monitor Dashboard
Running at http://localhost:8080 when `monitor` alias is used. Shows per-UE RF metrics, throughput, normalized CPU, memory, topology visualization. WebSocket updates every second (displayed data may lag ~10s due to CSV buffering).

---

## Quick Reference Commands

```bash
# Full startup
netns-up && epc    # terminal 1
enb                # terminal 2
ue1                # terminal 3
ue2                # terminal 4
ue3                # terminal 5
broker             # terminal 6
monitor            # terminal 7
pingall            # terminal 8

# Voice traffic
iperf3 -s -p 5201 &
iperf3 -s -p 5202 &
sudo ip netns exec ue1 iperf3 -c 172.16.0.1 -u -b 64K -l 160 -t 300 --bidir -p 5201  # own terminal
sudo ip netns exec ue2 iperf3 -c 172.16.0.1 -u -b 1M -t 300 --bidir -p 5202           # own terminal

# Monitoring
watchmetrics       # terminal view
http://localhost:8080  # browser view

# Teardown
killiperf
killnet
netns-down

# Nuclear option (if killnet doesn't work)
sudo pkill -9 -f srsue
sudo pkill -9 -f srsenb
sudo pkill -9 -f srsepc
pkill -9 -f multi_ue_broker
pkill -9 -f iperf3
```