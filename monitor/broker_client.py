#!/usr/bin/env python3
"""
Broker Control Client — sends JSON commands to the broker's TCP control server.

Used by the FastAPI backend to control broker gains without importing GNU Radio.
Each call opens a TCP connection, sends one JSON command, reads the response, closes.

Usage:
    client = BrokerControlClient()
    result = client.set_gain("ue1", "dl", 0.5)
    gains = client.get_gains()
    client.kill("ue2")
    client.reset()
"""

import json
import socket
import logging

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4000
TIMEOUT = 3.0


class BrokerControlClient:
    """Sends commands to the broker's TCP control server."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self._connected = False

    def _send(self, command: dict) -> dict:
        """Send a JSON command and return the JSON response."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(TIMEOUT)
            sock.connect((self.host, self.port))
            sock.sendall((json.dumps(command) + "\n").encode("utf-8"))

            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break

            sock.close()
            self._connected = True
            return json.loads(data.decode("utf-8").strip())

        except ConnectionRefusedError:
            self._connected = False
            return {"error": "broker not running (connection refused)"}
        except socket.timeout:
            self._connected = False
            return {"error": "broker timeout"}
        except Exception as e:
            self._connected = False
            return {"error": str(e)}

    def ping(self) -> dict:
        return self._send({"cmd": "ping"})

    def is_connected(self) -> bool:
        """Check if broker is reachable."""
        result = self.ping()
        return result.get("status") == "ok"

    def set_gain(self, ue: str, direction: str, value: float) -> dict:
        return self._send({
            "cmd": "set_gain",
            "ue": ue,
            "direction": direction,
            "value": value,
        })

    def kill(self, ue: str) -> dict:
        return self._send({"cmd": "kill", "ue": ue})

    def get_gains(self) -> dict:
        return self._send({"cmd": "get_gains"})

    def get_status(self) -> dict:
        return self._send({"cmd": "status"})

    def reset(self) -> dict:
        return self._send({"cmd": "reset"})