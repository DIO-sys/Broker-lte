#!/usr/bin/env python3
"""Minimal pass-through broker — 1 UE, REQ/REP."""
from gnuradio import gr, blocks, zeromq

class TestBroker(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "Test Broker")
        
        zmq_timeout = 100
        hwm = 100
        
        # DL: eNB → UE1 (REQ/REP)
        self.dl_source = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://localhost:3101", zmq_timeout, False, hwm)
        self.dl_sink = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://*:3000", zmq_timeout, False, hwm)
        
        # UL: UE1 → eNB (REQ/REP)
        self.ul_source = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://localhost:3001", zmq_timeout, False, hwm)
        self.ul_sink = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://*:3100", zmq_timeout, False, hwm)
        
        self.connect(self.dl_source, self.dl_sink)
        self.connect(self.ul_source, self.ul_sink)

tb = TestBroker()
tb.start()
print("Test broker running — 1 UE pass-through, REQ/REP")
tb.wait()
