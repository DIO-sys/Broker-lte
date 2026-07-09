#!/usr/bin/env python3
"""
Broker Control Client — speaks the v7 broker's TCP JSON-lines protocol.

v7 control protocol (127.0.0.1:4000, one JSON object per line):
    {"cmd":"set_gain","ue":1,"dir":"dl","value":0.6}   # value 0.0-1.0, dir dl|ul
    {"cmd":"set_noise","ue":2,"dir":"dl","value":300}  # sigma 0.0-2000.0
    {"cmd":"kill","ue":3}                               # dl=ul gain 0.0
    {"cmd":"reset"}                                     # gains->1.0, noise->0.0
    {"cmd":"status"}                                    # per-UE gains/noise/backlog/late_dropped

Replies: {"ok":true, ...}  /  {"ok":false,"err":"..."}

Notes vs the old client:
  - `ue` is an INTEGER (1/2/3), not "ue1".
  - the key is "dir", not "direction".
  - success is signalled by "ok":true, not "status":"ok".
  - there is NO ping/get_gains command — use status.
"""

import json
import socket
import logging

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4000
TIMEOUT = 3.0


def ue_num(ue) -> int:
    """Accept 'ue1' / 'UE1' / 1 / '1' -> 1."""
    if isinstance(ue, int):
        return ue
    s = str(ue).lower().replace("ue", "").strip()
    return int(s)


class BrokerControlClient:
    """Per-call TCP connection to the broker's control server."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self._connected = False

    def _send(self, command: dict) -> dict:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(TIMEOUT)
            sock.connect((self.host, self.port))
            sock.sendall((json.dumps(command) + "\n").encode("utf-8"))

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            sock.close()
            self._connected = True
            reply = json.loads(data.decode("utf-8").strip())
            # Normalise: broker uses ok/err; surface err as error too for the API layer.
            if reply.get("ok") is False and "error" not in reply:
                reply["error"] = reply.get("err", "broker rejected command")
            return reply
        except ConnectionRefusedError:
            self._connected = False
            return {"ok": False, "error": "broker not running (connection refused)"}
        except socket.timeout:
            self._connected = False
            return {"ok": False, "error": "broker timeout"}
        except Exception as e:
            self._connected = False
            return {"ok": False, "error": str(e)}

    # --- commands (1:1 with the protocol) -------------------------------

    def set_gain(self, ue, direction: str, value: float) -> dict:
        return self._send({"cmd": "set_gain", "ue": ue_num(ue),
                           "dir": direction, "value": value})

    def set_noise(self, ue, direction: str, value: float) -> dict:
        return self._send({"cmd": "set_noise", "ue": ue_num(ue),
                           "dir": direction, "value": value})

    def kill(self, ue) -> dict:
        return self._send({"cmd": "kill", "ue": ue_num(ue)})

    def reset(self) -> dict:
        return self._send({"cmd": "reset"})

    def get_status(self) -> dict:
        return self._send({"cmd": "status"})

    # --- convenience ----------------------------------------------------

    def is_connected(self) -> bool:
        return self.get_status().get("ok", False) is True

    def ue_state(self, ue) -> dict:
        """Pull one UE's gains/noise out of the status reply (v7 array shape)."""
        st = self.get_status()
        n = ue_num(ue)
        blank = {"dl_gain": 1.0, "ul_gain": 1.0, "dl_noise": 0.0,
                 "ul_noise": 0.0, "backlog": None, "late_dropped": None}
        if not st.get("ok"):
            return blank

        # Exact v7 shape (broker.cpp status handler):
        #   {"ok":true,"ues":[{"ue":1,"dl_gain":..,"ul_gain":..,
        #                      "dl_noise":..,"ul_noise":..,
        #                      "started":bool,"backlog":N,"late_dropped":N}, ...]}
        for entry in st.get("ues", []):
            if entry.get("ue") == n:
                return {
                    "dl_gain": entry.get("dl_gain", 1.0),
                    "ul_gain": entry.get("ul_gain", 1.0),
                    "dl_noise": entry.get("dl_noise", 0.0),
                    "ul_noise": entry.get("ul_noise", 0.0),
                    "backlog": entry.get("backlog"),
                    "late_dropped": entry.get("late_dropped"),
                    "started": entry.get("started"),
                }
        return blank