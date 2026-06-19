# srsRAN 4G ZMQ — 3-UE Broker Topology

One eNB, three UEs, connected via a GNU Radio ZMQ broker on 3000-series ports.
No physical radio hardware required.

## Architecture

```
srsEPC (127.0.1.100)
   │ S1
srsENB (TX:3101, RX:3100)
   │ ZMQ IQ
GRC Broker (multi_ue_broker.py)
   ├── UE1 (TX:3001, RX:3000)  netns:ue1  IMSI:901700123456789
   ├── UE2 (TX:3011, RX:3010)  netns:ue2  IMSI:901700123456790
   └── UE3 (TX:3021, RX:3020)  netns:ue3  IMSI:901700123456791
```

## Quick Start

```bash
# 1. Install everything (run once)
chmod +x scripts/*.sh broker/*.py
./scripts/install.sh

# 2. Start the network
./scripts/start_network.sh

# 3. Verify (wait ~30s for all UEs to attach)
./scripts/verify_network.sh

# 4. Stop
./scripts/stop_network.sh
```

## Running Manually (VS Code — 5 terminals)

If you prefer to run each component in its own VS Code terminal:

**Terminal 1 — EPC:**
```bash
sudo srsepc configs/epc.conf
```

**Terminal 2 — eNB:**
```bash
sudo srsenb configs/enb.conf --enb_files.rr_config=configs/rr.conf
```

**Terminal 3 — Broker:**
```bash
python3 broker/multi_ue_broker.py
```

**Terminal 4, 5, 6 — UEs (start each ~10s apart):**
```bash
sudo srsue configs/ue1.conf
sudo srsue configs/ue2.conf
sudo srsue configs/ue3.conf
```

**Terminal 7 — Verify:**
```bash
sudo ip netns exec ue1 ping 172.16.0.1
sudo ip netns exec ue2 ping 172.16.0.1
sudo ip netns exec ue3 ping 172.16.0.1
```

## What Success Looks Like

**EPC console** — 3 attach sequences:
```
Initial UE message: LIBLTE_MME_MSG_TYPE_ATTACH_REQUEST
...
SPGW Allocated IP 172.16.0.2 to IMSI 901700123456789
...
SPGW Allocated IP 172.16.0.3 to IMSI 901700123456790
...
SPGW Allocated IP 172.16.0.4 to IMSI 901700123456791
```

**eNB console** — 3 RACH events:
```
RACH:  tti=..., cc=0, preamble=..., offset=0, temp_crnti=0x46
User 0x46 connected
RACH:  tti=..., cc=0, preamble=..., offset=0, temp_crnti=0x47
User 0x47 connected
RACH:  tti=..., cc=0, preamble=..., offset=0, temp_crnti=0x48
User 0x48 connected
```

**Each UE console:**
```
Found Cell:  Mode=FDD, PCI=1, PRB=25, Ports=1, ...
Random Access Complete.     c-rnti=0x4X, ta=0
RRC Connected
Network attach successful. IP: 172.16.0.X
```

## Port Map

| Element | TX Port | RX Port |
|---------|---------|---------|
| eNB     | 3101    | 3100    |
| UE1     | 3001    | 3000    |
| UE2     | 3011    | 3010    |
| UE3     | 3021    | 3020    |

## Config Parameters That Must Match

| Parameter   | Value    | Files                          |
|-------------|----------|--------------------------------|
| dl_earfcn   | 2850     | enb.conf, rr.conf, ue*.conf    |
| base_srate  | 11.52e6  | enb.conf, ue*.conf, broker     |
| n_prb       | 25       | enb.conf                       |
| mcc         | 901      | epc.conf, enb.conf             |
| mnc         | 70       | epc.conf, enb.conf             |
| tac         | 0x0007   | epc.conf, rr.conf              |

## Memory & PRB Sizing

Each srsue process allocates memory proportional to the sample rate. At 50 PRB (23.04 MHz), each UE uses ~970 MB resident memory. With 3 UEs + eNB + broker + EPC, total system memory exceeds 4.5 GB for srsRAN alone. On a 16 GB machine this can exhaust RAM and force-quit VS Code or the desktop environment.

Use 25 PRB (11.52 MHz) to halve memory per process (~500 MB each). To switch between them:

```bash
# Switch to 25 PRB (recommended for 16 GB machines)
sed -i 's/n_prb = 50/n_prb = 25/' configs/enb.conf
sed -i 's/23.04e6/11.52e6/g' configs/enb.conf configs/ue*.conf broker/multi_ue_broker.py

# Switch back to 50 PRB (32 GB+ machines)
sed -i 's/n_prb = 25/n_prb = 50/' configs/enb.conf
sed -i 's/11.52e6/23.04e6/g' configs/enb.conf configs/ue*.conf broker/multi_ue_broker.py
```

Adding swap as a safety net is also recommended:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

Reducing log verbosity saves ~50 MB per process:

```bash
sed -i 's/all_level = info/all_level = warning/' configs/*.conf
```

## Per-UE Distinction

Per-UE scheduling divergence comes from asymmetric channel conditions and differentiated traffic, not PRB count. Three UEs all running identical pings look identical to the scheduler at any bandwidth. To produce genuinely different per-UE metrics for the anomaly detector and RF classifier:

### Asymmetric Broker Gains

Set different DL gains so the eNB sees different channel quality per UE and assigns different MCS. The broker exposes a runtime API:

```python
tb.set_gain_ue1_dl(1.0)   # strong signal  → high MCS, low BLER
tb.set_gain_ue2_dl(0.6)   # moderate signal → mid MCS
tb.set_gain_ue3_dl(0.3)   # weak signal    → low MCS, higher BLER
```

This alone produces three visibly distinct UE signatures from one cell: different MCS, different BLER, different retransmission rates.

### Differentiated Traffic Profiles

Instead of identical pings, run different loads per UE so the scheduler makes real allocation decisions. Use `--bidir` for realistic bidirectional voice traffic (both UL and DL active simultaneously):

```bash
# Kill any old iperf sessions
sudo pkill -9 iperf3
sleep 1

# Start iperf servers on the EPC side
iperf3 -s -p 5201 &
iperf3 -s -p 5202 &

# UE1: bidirectional G.711 voice call (64 kbps each direction, 160-byte RTP payloads)
sudo ip netns exec ue1 iperf3 -c 172.16.0.1 -u -b 64K -l 160 -t 300 --bidir -p 5201 &

# UE2: bidirectional voice call
sudo ip netns exec ue2 iperf3 -c 172.16.0.1 -u -b 64K -l 160 -t 300 --bidir -p 5202 &

# UE3: keepalive ping only — minimal scheduler allocation
sudo ip netns exec ue3 ping 172.16.0.1 &
```

Without `--bidir`, iperf3 sends client→server only (uplink), so DL throughput shows 0. With `--bidir`, both directions are active — exactly like a real phone call. UE1 and UE2 will show ~64 kbps on both DL and UL, while UE3 shows just ping-level traffic.

Note: srsEPC's SPGW does not hairpin traffic between UEs (UE1 cannot ping UE2 directly). All traffic goes UE→EPC. For the scheduler this doesn't matter — per-UE PRB allocation and MCS selection behave identically regardless of the traffic destination.

### QCI-Based Priority

Different QCI values in `user_db.csv` make the scheduler prioritize bearers differently:

```
ue1,mil,...,7,dynamic    # voice priority
ue2,mil,...,9,dynamic    # default best-effort
ue3,mil,...,5,dynamic    # IMS signaling priority
```

### Why This Matters

The Markov fault engine, anomaly detector, and RF classifier don't care about absolute throughput — they care about changes from baseline. A BLER spike from 0% to 15% looks the same at 25 or 50 PRB. But per-UE asymmetry means a fault on UE2 produces a distinct signature from a fault on UE1, which is what enables fault localization rather than cell-wide alerting.

## Live Monitor

A FastAPI backend reads the srsRAN UE CSV metrics and serves a live topology dashboard:

```bash
cd monitor
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn[standard]
python3 backend.py
```

Open http://localhost:8080. Shows per-UE RF metrics (RSRP, SNR, MCS, BLER), throughput, normalized CPU usage, memory, topology visualization with animated links, and SNR sparklines. Data updates every second via WebSocket.

## Shell Aliases

Add these to `~/.bashrc` for quick startup:

```bash
alias epc='sudo srsepc /path/to/configs/epc.conf'
alias enb='sudo srsenb /path/to/configs/enb.conf'
alias ue1='sudo srsue /path/to/configs/ue1.conf'
alias ue2='sudo srsue /path/to/configs/ue2.conf'
alias ue3='sudo srsue /path/to/configs/ue3.conf'
alias broker='cd /path/to/project && python3 broker/multi_ue_broker.py'
alias monitor='cd /path/to/monitor && source venv/bin/activate && python3 backend.py'
alias netns-up='sudo ip netns add ue1 2>/dev/null; sudo ip netns add ue2 2>/dev/null; sudo ip netns add ue3 2>/dev/null'
alias netns-down='sudo ip netns delete ue1 2>/dev/null; sudo ip netns delete ue2 2>/dev/null; sudo ip netns delete ue3 2>/dev/null'
alias ping1='sudo ip netns exec ue1 ping 172.16.0.1'
alias ping2='sudo ip netns exec ue2 ping 172.16.0.1'
alias ping3='sudo ip netns exec ue3 ping 172.16.0.1'
alias killnet='sudo pkill -f srsue; sleep 1; pkill -f multi_ue_broker; sleep 1; sudo pkill -f srsenb; sleep 1; sudo pkill -f srsepc'
```

Startup order: `netns-up` → `epc` → `enb` → `ue1` → `ue2` → `ue3` → `broker` → `monitor`

## Troubleshooting

**UE won't attach:**
- Check the broker is running (UE can't reach eNB without it)
- Verify IMSI in ue*.conf matches user_db.csv exactly
- Check EPC log for auth failures: `cat /tmp/epc.log | grep -i error`

**"Late" warnings flooding console:**
- CPU can't keep up. Close other applications.
- Try: `sudo chrt -f 99 srsenb configs/enb.conf ...`

**System runs out of memory / VS Code force-quits:**
- Switch to 25 PRB: `sed -i 's/n_prb = 50/n_prb = 25/' configs/enb.conf` and change base_srate to 11.52e6 in all configs + broker
- Add 4 GB swap: `sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`
- Close browsers and unnecessary apps before starting the network
- 50 PRB with 3 UEs requires 32 GB+ RAM

**UE attaches then immediately detaches:**
- Keep traffic flowing: `sudo ip netns exec ue1 ping 172.16.0.1`
- Check dl_earfcn matches across all configs

**Only 1 or 2 UEs attach:**
- Stagger launches more (15s instead of 10s)
- Check each UE has a unique IMSI and unique ZMQ ports

**Clean teardown order:**
1. Stop UEs first
2. Stop broker
3. Stop eNB
4. Stop EPC

## File Structure

```
srsran-zmq-network/
├── configs/
│   ├── epc.conf          # Core network config
│   ├── enb.conf          # eNB with ZMQ on 3000-series ports
│   ├── rr.conf           # Radio resources — single cell
│   ├── user_db.csv       # 3 subscriber entries
│   ├── ue1.conf          # UE1: IMSI ...789, ports 3001/3000
│   ├── ue2.conf          # UE2: IMSI ...790, ports 3011/3010
│   └── ue3.conf          # UE3: IMSI ...791, ports 3021/3020
├── broker/
│   └── multi_ue_broker.py  # GNU Radio ZMQ broker (no GUI needed)
├── monitor/
│   ├── backend.py        # FastAPI metrics server + WebSocket
│   ├── frontend.html     # Live topology dashboard
│   └── requirements.txt  # Python deps (fastapi, uvicorn)
├── scripts/
│   ├── install.sh        # Full dependency + build script
│   ├── start_network.sh  # Launch everything in order
│   ├── stop_network.sh   # Clean teardown
│   └── verify_network.sh # Check all UEs are attached
└── README.md
```

## References

- [srsRAN ZMQ App Note](https://docs.srsran.com/projects/4g/en/rfsoc/app_notes/source/zeromq/source/index.html)
- [srsRAN Handover App Note](https://docs.srsran.com/projects/4g/en/rfsoc/app_notes/source/handover/source/)
- [srsRAN Installation Guide](https://docs.srsran.com/projects/4g/en/rfsoc/general/source/1_installation.html)