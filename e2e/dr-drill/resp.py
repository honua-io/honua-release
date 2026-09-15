#!/usr/bin/env python3
"""Minimal binary-safe RESP client.

The drill has to take and replay a *logical* backup of the Redis-backed substrates, which
means `DUMP`/`RESTORE` payloads: opaque binary blobs that `redis-cli` cannot round-trip
through a shell pipeline without corrupting them. Speaking RESP over the published loopback
port is the real substrate surface and keeps every byte intact.
"""
from __future__ import annotations

import socket


class RespError(RuntimeError):
    pass


class Resp:
    def __init__(self, host: str, port: int, timeout: float = 30.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Resp":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def call(self, *args):
        parts = [b"*%d\r\n" % len(args)]
        for arg in args:
            raw = arg if isinstance(arg, (bytes, bytearray)) else str(arg).encode("utf-8")
            parts.append(b"$%d\r\n%s\r\n" % (len(raw), raw))
        self._sock.sendall(b"".join(parts))
        return self._read()

    def _fill(self, n: int) -> None:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RespError("connection closed by the Redis substrate")
            self._buf += chunk

    def _line(self) -> bytes:
        while b"\r\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RespError("connection closed by the Redis substrate")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _read(self):
        line = self._line()
        kind, payload = line[:1], line[1:]
        if kind == b"+":
            return payload
        if kind == b"-":
            raise RespError(payload.decode("utf-8", "replace"))
        if kind == b":":
            return int(payload)
        if kind == b"$":
            length = int(payload)
            if length == -1:
                return None
            self._fill(length + 2)
            value, self._buf = self._buf[:length], self._buf[length + 2:]
            return value
        if kind == b"*":
            count = int(payload)
            if count == -1:
                return None
            return [self._read() for _ in range(count)]
        raise RespError(f"unsupported RESP type {kind!r}")

    def scan_keys(self, pattern: str) -> list[bytes]:
        cursor, keys = b"0", []
        while True:
            cursor, batch = self.call("SCAN", cursor, "MATCH", pattern, "COUNT", 500)
            keys.extend(batch)
            if cursor == b"0":
                break
        return sorted(set(keys))
