from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import socket
from collections.abc import Iterable


TARGET_HOST = "api.telegram.org"
TARGET_PORT = 443
MAX_HEADER_BYTES = 8192
CONNECT_TIMEOUT_SECONDS = 15.0
DEFAULT_ALLOWED_SUBNETS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

log = logging.getLogger("clientplatform.telegram_ipv6_relay")


def _allowed_peer(
    peer_host: str,
    allowed_subnets: Iterable[ipaddress.IPv4Network],
) -> bool:
    try:
        address = ipaddress.ip_address(peer_host)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and any(
        address in subnet for subnet in allowed_subnets
    )


def _valid_connect_request(header: bytes) -> bool:
    if len(header) > MAX_HEADER_BYTES:
        return False
    try:
        request_line = header.split(b"\r\n", 1)[0].decode("ascii", "strict")
    except UnicodeDecodeError:
        return False
    parts = request_line.split()
    if len(parts) != 3:
        return False
    method, authority, version = parts
    return (
        method.upper() == "CONNECT"
        and authority.lower() == f"{TARGET_HOST}:{TARGET_PORT}"
        and version in {"HTTP/1.0", "HTTP/1.1"}
    )


async def _open_ipv6_target() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo(
        TARGET_HOST,
        TARGET_PORT,
        family=socket.AF_INET6,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    last_error: OSError | None = None
    for _family, _socktype, _proto, _canonname, sockaddr in addresses:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    host=sockaddr[0],
                    port=sockaddr[1],
                    family=socket.AF_INET6,
                ),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            last_error = exc
        except TimeoutError:
            last_error = OSError("ipv6_connect_timeout")
    if last_error is not None:
        raise last_error
    raise OSError("telegram_ipv6_address_missing")


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.write_eof()
        except (AttributeError, OSError, RuntimeError):
            pass


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    allowed_subnets: tuple[ipaddress.IPv4Network, ...],
) -> None:
    peer = writer.get_extra_info("peername")
    peer_host = str(peer[0]) if isinstance(peer, tuple) and peer else ""
    upstream_writer: asyncio.StreamWriter | None = None

    try:
        if not _allowed_peer(peer_host, allowed_subnets):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        try:
            header = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=10.0,
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        if not _valid_connect_request(header):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        upstream_reader, upstream_writer = await _open_ipv6_target()
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async with asyncio.TaskGroup() as group:
            group.create_task(_pipe(reader, upstream_writer))
            group.create_task(_pipe(upstream_reader, writer))
    except (OSError, ConnectionError, ExceptionGroup) as exc:
        log.warning("Telegram IPv6 relay connection failed: %s", type(exc).__name__)
        if not writer.is_closing():
            try:
                writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            except (ConnectionError, OSError, RuntimeError):
                pass
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except (ConnectionError, OSError, RuntimeError):
                pass
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass


async def _serve(listen_host: str, listen_port: int) -> None:
    allowed_subnets = tuple(
        ipaddress.ip_network(value) for value in DEFAULT_ALLOWED_SUBNETS
    )
    server = await asyncio.start_server(
        lambda reader, writer: _handle_client(
            reader,
            writer,
            allowed_subnets=allowed_subnets,
        ),
        host=listen_host,
        port=listen_port,
        family=socket.AF_INET,
        limit=MAX_HEADER_BYTES,
        start_serving=True,
    )
    sockets = server.sockets or []
    bound = ",".join(str(sock.getsockname()) for sock in sockets)
    log.info("Telegram IPv6 CONNECT relay listening on %s", bound)
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restricted IPv4-to-IPv6 CONNECT relay for Telegram Bot API"
    )
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=3128)
    args = parser.parse_args()
    if not 1024 <= args.listen_port <= 65535:
        parser.error("listen port must be between 1024 and 65535")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        asyncio.run(_serve(args.listen_host, args.listen_port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
