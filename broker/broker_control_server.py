#!/usr/bin/env python3
"""
Broker Control Server — TCP JSON command interface for the GNU Radio broker.

Runs as a daemon thread inside the broker process. Listens on TCP port 4000
for JSON commands and calls the broker's gain API directly (same process,
no IPC overhead — just a socket for cross-process access).

Protocol:
  Client sends a JSON line, server responds with a JSON line.

Commands:
  {"cmd": "set_gain", "ue": "ue1", "direction": "dl", "value": 0.5}
  {"cmd": "get_gains"}
  {"cmd": "kill", "ue": "ue2"}
  {"cmd": "reset"}
  {"cmd": "status"}
  {"cmd": "ping"}
"""

import json
import socket
import threading
import time
import logging

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4000


class BrokerControlServer:
    """TCP server exposing the broker's gain API over a socket."""

    def __init__(self, broker_top_block, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.tb = broker_top_block
        self.host = host
        self.port = port
        self._server_socket = None
        self._running = False
        self._killed_ues = set()  # track permanently killed UEs

    def start(self):
        """Start the control server in a daemon thread."""
        self._running = True
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()
        logger.info(f"Broker control server listening on {self.host}:{self.port}")
        print(f"  Control server on tcp://{self.host}:{self.port}")

    def stop(self):
        self._running = False
        if self._server_socket:
            self._server_socket.close()

    def _serve(self):
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.settimeout(1.0)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(5)

        while self._running:
            try:
                conn, addr = self._server_socket.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, conn):
        """Handle a single client connection. One command per connection."""
        try:
            conn.settimeout(5.0)
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break

            if not data:
                return

            request = json.loads(data.decode("utf-8").strip())
            response = self._dispatch(request)
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

        except json.JSONDecodeError:
            resp = {"error": "invalid JSON"}
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except Exception as e:
            logger.error(f"Client handler error: {e}")
        finally:
            conn.close()

    def _dispatch(self, request):
        """Route a command to the appropriate broker method."""
        cmd = request.get("cmd")

        if cmd == "ping":
            return {"status": "ok", "timestamp": time.time()}

        elif cmd == "get_gains":
            gains = self.tb.get_gains()
            gains["killed"] = list(self._killed_ues)
            return {"status": "ok", "gains": gains}

        elif cmd == "status":
            gains = self.tb.get_gains()
            return {
                "status": "ok",
                "gains": gains,
                "killed": list(self._killed_ues),
                "timestamp": time.time(),
            }

        elif cmd == "set_gain":
            ue = request.get("ue")
            direction = request.get("direction")
            value = request.get("value")

            if not all([ue, direction, value is not None]):
                return {"error": "requires ue, direction, value"}

            if ue in self._killed_ues:
                return {"error": f"{ue} is permanently killed"}

            try:
                value = float(value)
            except (ValueError, TypeError):
                return {"error": "value must be a number"}

            if value < 0.0 or value > 1.0:
                return {"error": "value must be 0.0-1.0"}

            func_name = f"set_gain_{ue}_{direction}"
            func = getattr(self.tb, func_name, None)
            if not func:
                return {"error": f"unknown target: {ue} {direction}"}

            func(value)
            return {
                "status": "ok",
                "ue": ue,
                "direction": direction,
                "value": value,
            }

        elif cmd == "kill":
            ue = request.get("ue")
            if not ue:
                return {"error": "requires ue"}

            if ue in self._killed_ues:
                return {"error": f"{ue} already killed"}

            # Zero both DL and UL
            for direction in ["dl", "ul"]:
                func = getattr(self.tb, f"set_gain_{ue}_{direction}", None)
                if func:
                    func(0.0)

            self._killed_ues.add(ue)
            return {"status": "ok", "ue": ue, "action": "killed"}

        elif cmd == "reset":
            # Reset all gains to 1.0 — does NOT recover killed UEs
            for ue in ["ue1", "ue2", "ue3"]:
                if ue in self._killed_ues:
                    continue
                for direction in ["dl", "ul"]:
                    func = getattr(self.tb, f"set_gain_{ue}_{direction}", None)
                    if func:
                        func(1.0)

            return {
                "status": "ok",
                "action": "reset",
                "skipped_killed": list(self._killed_ues),
            }

        else:
            return {"error": f"unknown command: {cmd}"}