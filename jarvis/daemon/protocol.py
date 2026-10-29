"""Newline-delimited JSON framing for the daemon socket."""
from __future__ import annotations

import json
import socket
from typing import Any, Iterator


def send(sock: socket.socket, obj: dict[str, Any]) -> None:
    sock.sendall((json.dumps(obj, default=str) + "\n").encode())


class Reader:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""

    def messages(self) -> Iterator[dict]:
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
            try:
                chunk = self.sock.recv(65536)
            except (ConnectionResetError, OSError):
                return
            if not chunk:
                return
            self.buf += chunk

    def push_back(self, msg: dict) -> None:
        """Put a message back at the front, for whoever reads next."""
        self.buf = (json.dumps(msg, default=str) + "\n").encode() + self.buf

    def next_message(self, timeout: float | None = None) -> dict | None:
        self.sock.settimeout(timeout)
        try:
            for m in self.messages():
                return m
        except (socket.timeout, TimeoutError):
            return None
        finally:
            self.sock.settimeout(None)
        return None
