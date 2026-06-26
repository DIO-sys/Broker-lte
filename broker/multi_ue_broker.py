#!/usr/bin/env python3
###############################################################################
# multi_ue_broker.py — GNU Radio ZMQ Broker for 1 eNB ↔ 3 UEs
#
# Topology:
#   eNB TX (3101) ──→ REQ Source ──┬──→ Multiply(gain_ue1) ──→ REP Sink (3000) → UE1 RX
#                                  ├──→ Multiply(gain_ue2) ──→ REP Sink (3010) → UE2 RX
#                                  └──→ Multiply(gain_ue3) ──→ REP Sink (3020) → UE3 RX
#
#   UE1 TX (3001) ──→ REQ Source ──┐
#   UE2 TX (3011) ──→ REQ Source ──┼──→ Add ──→ REP Sink (3100) → eNB RX
#   UE3 TX (3021) ──→ REQ Source ──┘
#
# Runtime API:
#   gain ue1 dl 0.6  — adjust UE1 downlink gain
#   kill ue2         — zero gain on both DL and UL (permanent for session)
#   status           — show all gains
#   reset            — all gains back to 1.0 (won't recover a killed UE)
###############################################################################

import signal
import sys
import threading

from gnuradio import gr
from gnuradio import blocks
from gnuradio import zeromq


class MultiUEBroker(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self, "Multi-UE ZMQ Broker")

        ##################################################
        # Variables — per-UE gain (modifiable at runtime)
        ##################################################
        self._gain_ue1_dl = 1.0
        self._gain_ue2_dl = 1.0
        self._gain_ue3_dl = 1.0
        self._gain_ue1_ul = 1.0
        self._gain_ue2_ul = 1.0
        self._gain_ue3_ul = 1.0

        ##################################################
        # Parameters
        ##################################################
        samp_rate = 11.52e6  # Must match base_srate in all configs
        zmq_timeout = 100    # ms

        ##################################################
        # DOWNLINK PATH: eNB TX → fan-out to 3 UE RX
        ##################################################

        self.enb_dl_source = zeromq.req_source(
            gr.sizeof_gr_complex, 1,
            "tcp://localhost:3101",
            zmq_timeout, False, -1
        )

        self.gain_ue1_dl_block = blocks.multiply_const_cc(self._gain_ue1_dl)
        self.gain_ue2_dl_block = blocks.multiply_const_cc(self._gain_ue2_dl)
        self.gain_ue3_dl_block = blocks.multiply_const_cc(self._gain_ue3_dl)

        self.ue1_dl_sink = zeromq.rep_sink(
            gr.sizeof_gr_complex, 1,
            "tcp://*:3000",
            zmq_timeout, False, -1
        )
        self.ue2_dl_sink = zeromq.rep_sink(
            gr.sizeof_gr_complex, 1,
            "tcp://*:3010",
            zmq_timeout, False, -1
        )
        self.ue3_dl_sink = zeromq.rep_sink(
            gr.sizeof_gr_complex, 1,
            "tcp://*:3020",
            zmq_timeout, False, -1
        )

        ##################################################
        # UPLINK PATH: 3 UE TX → sum → eNB RX
        ##################################################

        self.ue1_ul_source = zeromq.req_source(
            gr.sizeof_gr_complex, 1,
            "tcp://localhost:3001",
            zmq_timeout, False, -1
        )
        self.ue2_ul_source = zeromq.req_source(
            gr.sizeof_gr_complex, 1,
            "tcp://localhost:3011",
            zmq_timeout, False, -1
        )
        self.ue3_ul_source = zeromq.req_source(
            gr.sizeof_gr_complex, 1,
            "tcp://localhost:3021",
            zmq_timeout, False, -1
        )

        self.gain_ue1_ul_block = blocks.multiply_const_cc(self._gain_ue1_ul)
        self.gain_ue2_ul_block = blocks.multiply_const_cc(self._gain_ue2_ul)
        self.gain_ue3_ul_block = blocks.multiply_const_cc(self._gain_ue3_ul)

        self.ul_adder = blocks.add_cc(1)

        self.enb_ul_sink = zeromq.rep_sink(
            gr.sizeof_gr_complex, 1,
            "tcp://*:3100",
            zmq_timeout, False, -1
        )

        ##################################################
        # Connections
        ##################################################

        # DL: eNB TX → gain → each UE RX
        self.connect(self.enb_dl_source, self.gain_ue1_dl_block, self.ue1_dl_sink)
        self.connect(self.enb_dl_source, self.gain_ue2_dl_block, self.ue2_dl_sink)
        self.connect(self.enb_dl_source, self.gain_ue3_dl_block, self.ue3_dl_sink)

        # UL: each UE TX → gain → adder → eNB RX
        self.connect(self.ue1_ul_source, self.gain_ue1_ul_block, (self.ul_adder, 0))
        self.connect(self.ue2_ul_source, self.gain_ue2_ul_block, (self.ul_adder, 1))
        self.connect(self.ue3_ul_source, self.gain_ue3_ul_block, (self.ul_adder, 2))
        self.connect(self.ul_adder, self.enb_ul_sink)

    ##################################################
    # Runtime gain control API
    ##################################################

    def set_gain_ue1_dl(self, gain):
        self._gain_ue1_dl = gain
        self.gain_ue1_dl_block.set_k(gain)

    def set_gain_ue2_dl(self, gain):
        self._gain_ue2_dl = gain
        self.gain_ue2_dl_block.set_k(gain)

    def set_gain_ue3_dl(self, gain):
        self._gain_ue3_dl = gain
        self.gain_ue3_dl_block.set_k(gain)

    def set_gain_ue1_ul(self, gain):
        self._gain_ue1_ul = gain
        self.gain_ue1_ul_block.set_k(gain)

    def set_gain_ue2_ul(self, gain):
        self._gain_ue2_ul = gain
        self.gain_ue2_ul_block.set_k(gain)

    def set_gain_ue3_ul(self, gain):
        self._gain_ue3_ul = gain
        self.gain_ue3_ul_block.set_k(gain)

    def get_gains(self):
        return {
            "ue1_dl": self._gain_ue1_dl, "ue1_ul": self._gain_ue1_ul,
            "ue2_dl": self._gain_ue2_dl, "ue2_ul": self._gain_ue2_ul,
            "ue3_dl": self._gain_ue3_dl, "ue3_ul": self._gain_ue3_ul,
        }


def main():
    print("=" * 60)
    print("  Multi-UE ZMQ Broker (REQ/REP)")
    print("  1 eNB (3101/3100) <-> 3 UEs (3000-3021)")
    print("=" * 60)
    print("  Commands: status, kill ue1/2/3,")
    print("            gain ue1/2/3 dl/ul <value>, reset, quit")
    print("=" * 60)

    tb = MultiUEBroker()

    def signal_handler(sig, frame):
        print("\nStopping broker...")
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    tb.start()
    print("\nBroker running.\n")

    threading.Thread(target=tb.wait, daemon=True).start()

    while True:
        try:
            cmd = input("broker> ").strip().lower()
        except EOFError:
            break

        if not cmd:
            continue
        elif cmd == "status":
            gains = tb.get_gains()
            for k, v in gains.items():
                print(f"  {k}: {v:.2f}")
        elif cmd == "kill ue1":
            tb.set_gain_ue1_dl(0.0)
            tb.set_gain_ue1_ul(0.0)
            print("  UE1 soft killed — gain zeroed (permanent)")
        elif cmd == "kill ue2":
            tb.set_gain_ue2_dl(0.0)
            tb.set_gain_ue2_ul(0.0)
            print("  UE2 soft killed — gain zeroed (permanent)")
        elif cmd == "kill ue3":
            tb.set_gain_ue3_dl(0.0)
            tb.set_gain_ue3_ul(0.0)
            print("  UE3 soft killed — gain zeroed (permanent)")
        elif cmd.startswith("gain "):
            parts = cmd.split()
            if len(parts) == 4:
                ue, direction, val = parts[1], parts[2], parts[3]
                try:
                    gain_val = float(val)
                    func = getattr(tb, f"set_gain_{ue}_{direction}", None)
                    if func:
                        func(gain_val)
                        print(f"  {ue} {direction} gain -> {gain_val:.2f}")
                    else:
                        print(f"  Unknown: {ue} {direction}")
                except ValueError:
                    print("  Usage: gain ue1/2/3 dl/ul <value>")
            else:
                print("  Usage: gain ue1/2/3 dl/ul <value>")
        elif cmd == "reset":
            for ue in ["ue1", "ue2", "ue3"]:
                for d in ["dl", "ul"]:
                    getattr(tb, f"set_gain_{ue}_{d}")(1.0)
            print("  All gains reset to 1.0")
        elif cmd == "quit":
            tb.stop()
            break
        else:
            print("  Commands: status, kill ue1/2/3,")
            print("            gain ue1/2/3 dl/ul <value>, reset, quit")


if __name__ == "__main__":
    main()