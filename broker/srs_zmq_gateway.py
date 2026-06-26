#!/usr/bin/env python3
"""
ZMQ Gateway — translates between srsRAN's REQ/REP and GNU Radio's PUB/SUB.

srsRAN socket behavior:
  - TX ports (eNB:3101, UE:3001/3011/3021): srsRAN BINDS as REP, waits for REQ
  - RX ports (eNB:3100, UE:3000/3010/3020): srsRAN CONNECTS as REQ to a REP

Gateway must:
  - CONNECT REQ to srsRAN TX ports to pull samples (srsRAN binds REP)
  - BIND REP on srsRAN RX ports to serve samples (srsRAN connects REQ)
"""

import zmq
import threading
import signal
import sys

# "pull" = gateway REQ connects to srsRAN REP, pulls samples, publishes to GNU Radio
# "serve" = gateway REP binds, srsRAN REQ connects, gateway serves samples from GNU Radio
CONFIG = {
    # eNB TX: pull IQ from eNB, publish to broker
    "enb_tx":  {"srs_port": 3101, "gr_port": 5101, "mode": "pull"},
    # eNB RX: broker publishes composite UL, gateway serves to eNB
    "enb_rx":  {"srs_port": 3100, "gr_port": 5100, "mode": "serve"},
    # UE1 RX: broker publishes DL, gateway serves to UE1
    "ue1_rx":  {"srs_port": 3000, "gr_port": 5000, "mode": "serve"},
    # UE1 TX: pull IQ from UE1, publish to broker
    "ue1_tx":  {"srs_port": 3001, "gr_port": 5001, "mode": "pull"},
    # UE2
    "ue2_rx":  {"srs_port": 3010, "gr_port": 5010, "mode": "serve"},
    "ue2_tx":  {"srs_port": 3011, "gr_port": 5011, "mode": "pull"},
    # UE3
    "ue3_rx":  {"srs_port": 3020, "gr_port": 5020, "mode": "serve"},
    "ue3_tx":  {"srs_port": 3021, "gr_port": 5021, "mode": "pull"},
}


def handle_pull(name, srs_port, gr_port):
    """
    Pull samples FROM srsRAN (eNB TX or UE TX).
    
    srsRAN binds REP on srs_port.
    Gateway connects REQ to srs_port, requests samples.
    Gateway publishes samples on gr_port for GNU Radio broker to SUB.
    """
    ctx = zmq.Context()

    # Connect to srsRAN's bound REP socket
    srs_sock = ctx.socket(zmq.REQ)
    srs_sock.connect(f"tcp://localhost:{srs_port}")

    # Publish to GNU Radio broker
    gr_sock = ctx.socket(zmq.PUB)
    gr_sock.bind(f"tcp://*:{gr_port}")

    print(f"  [{name}] PULL: srsRAN(:{srs_port}) --> GNU Radio(:{gr_port})")

    while True:
        try:
            srs_sock.send(b"")            # Request samples from srsRAN
            msg = srs_sock.recv()          # Receive IQ samples
            gr_sock.send(msg)             # Publish to GNU Radio
        except zmq.ZMQError as e:
            print(f"  [{name}] ZMQ error: {e}")
            break


def handle_serve(name, srs_port, gr_port):
    """
    Serve samples TO srsRAN (eNB RX or UE RX).
    
    Gateway binds REP on srs_port.
    srsRAN connects REQ to srs_port, requests samples.
    Gateway gets freshest sample from GNU Radio broker's PUB on gr_port.
    """
    ctx = zmq.Context()

    # Bind REP for srsRAN to connect to
    srs_sock = ctx.socket(zmq.REP)
    srs_sock.bind(f"tcp://*:{srs_port}")

    # Subscribe to GNU Radio broker
    gr_sock = ctx.socket(zmq.SUB)
    gr_sock.connect(f"tcp://localhost:{gr_port}")
    gr_sock.setsockopt(zmq.SUBSCRIBE, b"")
    gr_sock.setsockopt(zmq.RCVTIMEO, 200)  # 200ms timeout instead of NOBLOCK 

    print(f"  [{name}] SERVE: GNU Radio(:{gr_port}) --> srsRAN(:{srs_port})")

    while True:
        try:
            srs_sock.recv()               # srsRAN requests data
            try:
                payload = gr_sock.recv(flags=zmq.NOBLOCK)
            except zmq.Again:
                payload = b"\x00" * 4096  # Zero padding if broker is late
            srs_sock.send(payload)
        except zmq.ZMQError as e:
            print(f"  [{name}] ZMQ error: {e}")
            break


def main():
    def signal_handler(sig, frame):
        print("\nShutting down gateway...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    print("=" * 60)
    print("  ZMQ Gateway — srsRAN (REQ/REP) <-> GNU Radio (PUB/SUB)")
    print("=" * 60)

    threads = []
    for name, cfg in CONFIG.items():
        if cfg["mode"] == "pull":
            t = threading.Thread(
                target=handle_pull,
                args=(name, cfg["srs_port"], cfg["gr_port"]),
                daemon=True,
            )
        else:
            t = threading.Thread(
                target=handle_serve,
                args=(name, cfg["srs_port"], cfg["gr_port"]),
                daemon=True,
            )
        t.start()
        threads.append(t)

    print("=" * 60)
    print("  Gateway active. Start broker next, then eNB, then UEs.")
    print("=" * 60)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nExiting gateway...")


if __name__ == "__main__":
    main()