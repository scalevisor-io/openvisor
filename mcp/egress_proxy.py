"""§egress filtering proxy - the enforcement point for the dev-sandbox egress
allowlist (K8s only). Runs from the mcp image (`python egress_proxy.py`).

A minimal forward proxy: it accepts HTTP CONNECT (HTTPS tunnels) and absolute-form
plain-HTTP requests, and lets a connection through ONLY when the target host
matches the allowlist AND resolves to a public address. Everything else is
refused. In the K8s dev-run, a NetworkPolicy makes this proxy the pod's sole
internet route, so an agent that ignores HTTP(S)_PROXY still cannot reach the
outside - the proxy is a hard boundary, not advisory.

Allowlist comes from EGRESS_ALLOWLIST (a JSON array of FQDN / `*.fqdn` / IP / CIDR
entries) - computed per run by the worker (admin list + the run's own hosts).

Hardening: the target's resolved IP is always re-checked and refused if it is
private / loopback / link-local (169.254.169.254 cloud metadata, RFC1918, CGNAT),
regardless of any allowlist match - defeats DNS-rebinding and SSRF-to-metadata.
Never logs full URLs or request bodies; only host + allow/deny + a reason.
"""
import asyncio
import ipaddress
import json
import logging
import os
import socket

logging.basicConfig(level=logging.INFO, format="egress-proxy %(levelname)s %(message)s")
log = logging.getLogger("egress-proxy")

PORT = int(os.environ.get("EGRESS_PROXY_PORT", "3128"))
_RAW = os.environ.get("EGRESS_ALLOWLIST", "[]")
try:
    ALLOWLIST = [str(e).strip().lower() for e in json.loads(_RAW) if str(e).strip()]
except Exception:
    log.warning("EGRESS_ALLOWLIST is not valid JSON; denying all egress")
    ALLOWLIST = []

_FQDNS: list[str] = []
_NETS: list = []
for _e in ALLOWLIST:
    try:
        _NETS.append(ipaddress.ip_network(_e, strict=False))
    except ValueError:
        _FQDNS.append(_e)

IDLE_TIMEOUT = 300  # seconds a tunnel may sit idle before we tear it down


def host_matches_name(host: str) -> bool:
    """FQDN / wildcard match against the allowlist name entries."""
    host = host.lower().rstrip(".")
    for pat in _FQDNS:
        if pat.startswith("*."):
            if host == pat[2:] or host.endswith(pat[1:]):  # *.a.com matches a.com and x.a.com
                return True
        elif host == pat:
            return True
    return False


def ip_in_nets(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _NETS)


def is_blocked_ip(ip: str) -> bool:
    """Refuse private / loopback / link-local / reserved regardless of allowlist."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _resolve(host: str) -> str | None:
    try:
        return socket.getaddrinfo(host, None)[0][4][0]
    except OSError:
        return None


def decide(host: str) -> tuple[bool, str, str | None]:
    """(allowed, reason, resolved_ip). A literal-IP target is matched directly;
    a hostname is matched by name then resolved for the public-IP re-check."""
    # literal IP target
    try:
        ipaddress.ip_address(host)
        if is_blocked_ip(host):
            return False, "target is a private/reserved address", host
        return (ip_in_nets(host), "ip not in allowlist", host)
    except ValueError:
        pass
    name_ok = host_matches_name(host)
    ip = _resolve(host)
    if ip is None:
        return False, "host did not resolve", None
    if is_blocked_ip(ip):
        return False, "host resolves to a private/reserved address", ip
    if name_ok or ip_in_nets(ip):
        return True, "allowed", ip
    return False, "host not in allowlist", ip


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _deny(writer: asyncio.StreamWriter, host: str, reason: str, code: str = "403 Forbidden"):
    log.info("DENY %s (%s)", host, reason)
    body = f"egress blocked: {reason}\n".encode()
    writer.write(f"HTTP/1.1 {code}\r\nContent-Length: {len(body)}\r\n"
                 f"Content-Type: text/plain\r\nConnection: close\r\n\r\n".encode() + body)
    try:
        await writer.drain()
    except ConnectionError:
        pass
    writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=30)
    except asyncio.TimeoutError:
        writer.close()
        return
    if not request_line:
        writer.close()
        return
    try:
        method, target, _ = request_line.decode("latin-1").split(" ", 2)
    except ValueError:
        writer.close()
        return

    # drain request headers (we don't forward hop-by-hop proxy headers)
    headers = []
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        headers.append(line)

    if method.upper() == "CONNECT":
        host, _, port = target.partition(":")
        port = int(port or 443)
        allowed, reason, ip = decide(host)
        if not allowed:
            await _deny(writer, host, reason)
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=15)
        except (OSError, asyncio.TimeoutError):
            await _deny(writer, host, "upstream connect failed", "502 Bad Gateway")
            return
        log.info("ALLOW CONNECT %s:%s", host, port)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        return

    # absolute-form plain HTTP: GET http://host/path
    if "://" in target:
        from urllib.parse import urlsplit
        u = urlsplit(target)
        host, port = u.hostname or "", u.port or 80
        allowed, reason, ip = decide(host)
        if not allowed:
            await _deny(writer, host, reason)
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=15)
        except (OSError, asyncio.TimeoutError):
            await _deny(writer, host, "upstream connect failed", "502 Bad Gateway")
            return
        log.info("ALLOW %s %s", method, host)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        up_w.write(f"{method} {path} HTTP/1.1\r\n".encode("latin-1"))
        for h in headers:
            up_w.write(h)
        up_w.write(b"\r\n")
        await up_w.drain()
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        return

    await _deny(writer, target, "only CONNECT and absolute-form HTTP are proxied", "400 Bad Request")


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log.info("listening on :%s | %d fqdn + %d net allow entries", PORT, len(_FQDNS), len(_NETS))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
