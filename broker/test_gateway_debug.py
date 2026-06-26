#!/usr/bin/env python3
import zmq
import threading
import time

def pull_enb_tx():
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.connect("tcp://localhost:3101")
    pub = ctx.socket(zmq.PUB)
    pub.bind("tcp://*:5101")
    count = 0
    while True:
        sock.send(b"")
        msg = sock.recv()
        pub.send(msg)
        count += 1
        if count <= 3 or count % 100 == 0:
            print(f"  [enb_tx] Pulled {len(msg)} bytes (#{count})")

def serve_enb_rx():
    ctx = zmq.Context()
    rep = ctx.socket(zmq.REP)
    rep.bind("tcp://*:3100")
    count = 0
    while True:
        rep.recv()
        rep.send(b"\x00" * 11520 * 8)
        count += 1
        if count <= 3 or count % 100 == 0:
            print(f"  [enb_rx] Served {count} UL chunks to eNB")

def serve_ue1_rx():
    ctx = zmq.Context()
    rep = ctx.socket(zmq.REP)
    rep.bind("tcp://*:3000")
    count = 0
    while True:
        rep.recv()
        rep.send(b"\x00" * 11520 * 8)
        count += 1
        if count <= 3 or count % 100 == 0:
            print(f"  [ue1_rx] Served {count} DL chunks to UE1")

def pull_ue1_tx():
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.connect("tcp://localhost:3001")
    count = 0
    while True:
        sock.send(b"")
        msg = sock.recv()
        count += 1
        if count <= 3 or count % 100 == 0:
            print(f"  [ue1_tx] Pulled {len(msg)} bytes from UE1 (#{count})")

print("=" * 60)
print("  Gateway Debug — all 4 paths for eNB + UE1")
print("  Start: EPC -> this script -> eNB -> UE1")
print("=" * 60)

for fn in [pull_enb_tx, serve_enb_rx, serve_ue1_rx, pull_ue1_tx]:
    threading.Thread(target=fn, daemon=True).start()

try:
    threading.Event().wait()
except KeyboardInterrupt:
    print("\nDone.")
