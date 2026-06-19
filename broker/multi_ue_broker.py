#!/usr/bin/env python3
###############################################################################
# multi_ue_broker.py — GNU Radio ZMQ Broker for 1 eNB ↔ 3 UEs
#
# Based on the GRC broker architecture from:
#   https://docs.srsran.com/projects/4g/en/rfsoc/app_notes/source/handover/source/
#   https://docs.srsran.com/projects/4g/en/rfsoc/app_notes/source/zeromq/source/
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
# Per-UE gain controls are exposed as variables that can be modified
# at runtime for fault injection. Default gain = 1.0 (passthrough).
#
# The handover app note uses ZMQ REQ Source / REP Sink block types.
# Data type is complex float (gr_complex) at base_srate = 23.04 MHz.
###############################################################################

import signal
import sys
import threading

from gnuradio import gr
from gnuradio import blocks
from gnuradio import zeromq


class MultiUEBroker(gr.top_block):
    """
    GNU Radio flowgraph that brokers ZMQ IQ streams between
    one srsENB and three srsUE instances.
    """

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

        # Pull DL IQ from eNB's TX port
        self.enb_dl_source = zeromq.req_source(
            gr.sizeof_gr_complex, 1,
            "tcp://localhost:3101",
            zmq_timeout, False, -1
        )

        # Per-UE gain control (multiply by constant)
        self.gain_ue1_dl_block = blocks.multiply_const_cc(self._gain_ue1_dl)
        self.gain_ue2_dl_block = blocks.multiply_const_cc(self._gain_ue2_dl)
        self.gain_ue3_dl_block = blocks.multiply_const_cc(self._gain_ue3_dl)

        # Push DL IQ to each UE's RX port
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

        # Pull UL IQ from each UE's TX port
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

        # Per-UE UL gain control
        self.gain_ue1_ul_block = blocks.multiply_const_cc(self._gain_ue1_ul)
        self.gain_ue2_ul_block = blocks.multiply_const_cc(self._gain_ue2_ul)
        self.gain_ue3_ul_block = blocks.multiply_const_cc(self._gain_ue3_ul)

        # Sum all 3 UE uplinks into one composite signal
        self.ul_adder = blocks.add_cc(1)

        # Push combined UL to eNB's RX port
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
    # Call these to change per-UE gains while running.
    # gain=0.0 simulates link dropout.
    # gain<1.0 simulates attenuation/fading.
    # gain>1.0 is possible but not recommended.
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
        """Return current gains as a dict for status reporting."""
        return {
            "ue1_dl": self._gain_ue1_dl, "ue1_ul": self._gain_ue1_ul,
            "ue2_dl": self._gain_ue2_dl, "ue2_ul": self._gain_ue2_ul,
            "ue3_dl": self._gain_ue3_dl, "ue3_ul": self._gain_ue3_ul,
        }


def main():
    print("=" * 60)
    print("  Multi-UE ZMQ Broker")
    print("  1 eNB (3101/3100) ↔ 3 UEs (3000-3021)")
    print("=" * 60)
    print()
    print("  Port map:")
    print("    eNB  TX: 3101  →  broker  →  UE1 RX: 3000")
    print("                      broker  →  UE2 RX: 3010")
    print("                      broker  →  UE3 RX: 3020")
    print("    UE1  TX: 3001  →  broker")
    print("    UE2  TX: 3011  →  broker  →  eNB RX: 3100")
    print("    UE3  TX: 3021  →  broker")
    print()
    print("  All gains set to 1.0 (passthrough)")
    print("  Press Ctrl+C to stop")
    print("=" * 60)

    tb = MultiUEBroker()

    def signal_handler(sig, frame):
        print("\nStopping broker...")
        tb.stop()
        tb.wait()
        print("Broker stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    tb.start()
    print("\nBroker running.\n")

    # Block main thread until flowgraph stops
    try:
        tb.wait()
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    main()