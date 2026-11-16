#!/usr/bin/env python3
"""Bridge a loopback port across a network namespace boundary over a unix socket.

host side:       loopback_bridge.py unix-to-tcp <socket> <port>
namespace side:  loopback_bridge.py tcp-to-unix <port> <socket>
"""
import asyncio
import contextlib
import sys

CHUNK_BYTES = 65536


async def copy(reader, writer):
    with contextlib.suppress(OSError):
        while chunk := await reader.read(CHUNK_BYTES):
            writer.write(chunk)
            await writer.drain()
    with contextlib.suppress(OSError):
        writer.close()


def relay_to(connect):
    async def relay(reader, writer):
        try:
            upstream_reader, upstream_writer = await connect()
        except OSError:
            writer.close()
            return
        await asyncio.gather(copy(reader, upstream_writer), copy(upstream_reader, writer))
    return relay


async def serve(mode, first, second):
    if mode == "unix-to-tcp":
        relay = relay_to(lambda: asyncio.open_connection("127.0.0.1", int(second)))
        server = await asyncio.start_unix_server(relay, path=first)
    elif mode == "tcp-to-unix":
        relay = relay_to(lambda: asyncio.open_unix_connection(second))
        server = await asyncio.start_server(relay, "127.0.0.1", int(first))
    else:
        raise SystemExit(__doc__)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    asyncio.run(serve(*sys.argv[1:]))
