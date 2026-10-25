"""Relays TCP connections from one address to another until killed.

    forward.py LISTEN_HOST LISTEN_PORT TARGET_HOST TARGET_PORT

What `socat TCP-LISTEN,fork` does, so the lab needs no package that the end-to-end
job does not already have.
"""
import asyncio
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def relay(client_reader, client_writer, target):
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(*target)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        pipe(client_reader, upstream_writer),
        pipe(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def main(listen, target):
    server = await asyncio.start_server(
        lambda r, w: relay(r, w, target), *listen, reuse_address=True
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    host, port, target_host, target_port = sys.argv[1:5]
    asyncio.run(main((host, int(port)), (target_host, int(target_port))))
