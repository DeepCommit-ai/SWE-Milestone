"""Host-side SNI-pinned TLS tunnel for the quarantine anti-cheat (method A).

Some LLM endpoints (api.kimi.com, api.moonshot.ai) sit behind Cloudflare, on
the SAME IP range a repo's package registry rides (npm → Cloudflare). A
quarantine repo (element-web) must CIDR-block that whole range to stop the
agent fetching its own target-version source from npm — which also blocks the
LLM endpoint. This module resolves that conflict WITHOUT weakening the block:

  * The container CIDR-blocks Cloudflare as before, but /etc/hosts maps the ONE
    LLM endpoint to the Docker bridge gateway, and iptables lets the gateway
    through.
  * A host-side forwarder (this module) listens on the gateway, peeks the TLS
    ClientHello SNI, and relays the connection to the real endpoint ONLY when
    the SNI is the pinned host. It never decrypts — TLS stays end-to-end
    between the container and the real endpoint, so the certificate matches.

The agent runs inside the container and cannot touch this forwarder. The three
detour paths all fail:
  * direct to Cloudflare (any SNI)         → iptables CIDR-DROP
  * via tunnel with SNI=registry           → forwarder rejects (SNI not pinned)
  * via tunnel with SNI=endpoint, Host=reg → Cloudflare answers 403 (SNI≠Host)

See docs/quarantine.md.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The ONLY hosts the SNI tunnel may ever relay: Cloudflare-fronted LLM
# endpoints. This is a CODE-LEVEL fact (like quarantine.FIREWALL_EXEMPTABLE_DOMAINS),
# not a per-repo self-declaration — it is the explicit, reviewed control that
# keeps the tunnel from ever becoming an answer-fetch channel. A package
# registry must NEVER appear here: the tunnel deliberately punches one host
# through a deny_cidr, so listing a registry would hand the agent its own
# target-version source. New endpoints are added only by a reviewed edit here.
SNI_TUNNELABLE_DOMAINS: frozenset[str] = frozenset({
    "api.kimi.com",
    "api.moonshot.ai",
})

# Cap on how much we buffer while looking for the ClientHello. A ClientHello is
# a single TLS record (max 16 KiB); anything larger without a valid record is a
# non-TLS client we drop.
_MAX_HELLO_BYTES = 16 * 1024 + 5


def parse_sni(data: bytes) -> str | None:
    """Extract the SNI host_name from a TLS ClientHello record.

    Returns the lowercased server name, or None if `data` is not a ClientHello
    that carries an SNI extension (including truncated/garbage input — the
    forwarder must treat "can't find a pinned SNI" as reject, never crash).
    """
    try:
        pos = 0
        n = len(data)

        def need(count: int) -> int:
            # Return the current offset and require `count` more bytes past it.
            nonlocal pos
            if pos + count > n:
                raise _Truncated
            start = pos
            pos += count
            return start

        # --- TLS record header: type(1) version(2) length(2) ---
        rec = need(5)
        if data[rec] != 0x16:  # 0x16 = handshake
            return None

        # --- Handshake header: msg_type(1) length(3) ---
        hs = need(4)
        if data[hs] != 0x01:  # 0x01 = ClientHello
            return None

        # --- ClientHello: client_version(2) random(32) ---
        need(2 + 32)
        # session_id: len(1) + bytes
        sid = need(1)
        need(data[sid])
        # cipher_suites: len(2) + bytes
        cs = need(2)
        need((data[cs] << 8) | data[cs + 1])
        # compression_methods: len(1) + bytes
        cm = need(1)
        need(data[cm])
        # extensions: len(2) + bytes
        ext_hdr = need(2)
        ext_total = (data[ext_hdr] << 8) | data[ext_hdr + 1]
        ext_end = pos + ext_total
        if ext_end > n:
            raise _Truncated

        # --- Walk extensions looking for server_name (type 0x0000) ---
        while pos < ext_end:
            eh = need(4)  # ext_type(2) ext_len(2)
            etype = (data[eh] << 8) | data[eh + 1]
            elen = (data[eh + 2] << 8) | data[eh + 3]
            ebody = need(elen)
            if etype != 0x0000:
                continue
            # server_name_list: list_len(2), then name_type(1) name_len(2) name
            j = ebody
            eend = ebody + elen
            if j + 2 > eend:
                return None
            j += 2  # skip server_name_list length
            if j + 3 > eend:
                return None
            name_type = data[j]
            name_len = (data[j + 1] << 8) | data[j + 2]
            j += 3
            if name_type != 0x00 or j + name_len > eend:
                return None
            return data[j:j + name_len].decode("ascii", "ignore").lower() or None
        return None
    except _Truncated:
        return None
    except Exception:
        return None


class _Truncated(Exception):
    """Internal: raised when a length field runs past the buffer."""


def sni_allowed(sni: str | None, allowed: set[str]) -> bool:
    """True only if `sni` is an exact (case-insensitive) member of `allowed`.

    Exact match, never suffix/substring: `api.kimi.com.evil.test` must not be
    accepted for a pinned `api.kimi.com`.
    """
    if sni is None:
        return False
    return sni.lower() in {a.lower() for a in allowed}


def _default_resolver(host: str) -> list[str]:
    try:
        return [sa[0] for *_head, sa in socket.getaddrinfo(host, None, socket.AF_INET)]
    except socket.gaierror:
        return []


def tunnel_plan(base_url: str, deny_cidrs, resolver=_default_resolver) -> str | None:
    """Return the host that must be SNI-tunneled for this trial, or None.

    Activates ONLY when the LLM endpoint host is both:
      (a) in the code-level SNI_TUNNELABLE_DOMAINS allowlist, and
      (b) resolving into one of `deny_cidrs` — i.e. this repo's quarantine
          would otherwise CIDR-block the endpoint along with the registry that
          shares its CDN range.
    Absent (b) the endpoint is reachable normally and no tunnel is set up, so
    the mechanism stays dormant for every repo/model that doesn't need it.

    `resolver(host) -> list[str]` is injected for testing.
    """
    if not base_url:
        return None
    host = (urlparse(base_url).hostname or "").lower()
    if host not in SNI_TUNNELABLE_DOMAINS:
        return None
    nets = []
    for c in deny_cidrs or []:
        try:
            nets.append(ipaddress.ip_network(str(c).strip(), strict=False))
        except ValueError:
            continue
    if not nets:
        return None
    for ip in resolver(host):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr in net for net in nets):
            return host
    return None


def _read_client_hello(sock: socket.socket) -> bytes:
    """Read exactly one TLS ClientHello record from `sock`, or what arrives.

    Reads the 5-byte record header, then the declared record body, so a
    ClientHello split across TCP segments is still fully buffered before the
    SNI parse. Returns whatever was read (possibly partial) on early close or
    a non-TLS first byte — parse_sni turns anything unparseable into a reject.
    """
    buf = b""
    # Record header first: type(1) version(2) length(2).
    while len(buf) < 5:
        chunk = sock.recv(5 - len(buf))
        if not chunk:
            return buf
        buf += chunk
    if buf[0] != 0x16:  # not a handshake record — not TLS; let caller reject
        return buf
    record_len = (buf[3] << 8) | buf[4]
    target = min(5 + record_len, _MAX_HELLO_BYTES)
    while len(buf) < target:
        chunk = sock.recv(target - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


class SniTunnel:
    """Host-side TCP forwarder that relays only pinned-SNI TLS connections.

    Binds a listener, and for each inbound connection reads the ClientHello,
    checks its SNI against `pinned_sni`, and — only on a match — opens a
    connection to the upstream endpoint, replays the buffered ClientHello, and
    splices the two sockets. It never decrypts; TLS stays end-to-end between
    the caller and the real upstream, so the certificate validates.

    Non-matching SNI (or non-TLS bytes) => the inbound socket is closed and no
    upstream connection is ever made. That is the whole anti-cheat guarantee.
    """

    def __init__(
        self,
        pinned_sni: str,
        upstream_host: str,
        upstream_port: int,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        handshake_timeout: float = 10.0,
    ):
        self.pinned_sni = pinned_sni.lower()
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        # Timeout for reading the initial ClientHello only — a non-TLS or idle
        # client is dropped fast. It must NOT bound the relay itself, or a
        # streaming reply that pauses longer than this truncates.
        self.handshake_timeout = handshake_timeout
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> int:
        """Bind + spawn the accept loop. Returns the actual bound port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.listen_host, self.listen_port))
        sock.listen(16)
        self.listen_port = sock.getsockname()[1]
        self._sock = sock
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        logger.info(
            "SNI tunnel listening on %s:%d, pinned to %s -> %s:%d",
            self.listen_host, self.listen_port, self.pinned_sni,
            self.upstream_host, self.upstream_port,
        )
        return self.listen_port

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(client,), daemon=True
            ).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(self.handshake_timeout)
            hello = _read_client_hello(client)
            sni = parse_sni(hello)
            if not sni_allowed(sni, {self.pinned_sni}):
                logger.warning(
                    "SNI tunnel dropped connection: sni=%r not pinned (%s)",
                    sni, self.pinned_sni,
                )
                return
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port),
                timeout=self.handshake_timeout,
            )
            upstream.sendall(hello)
            # Clear the handshake read timeout on BOTH sockets before relaying:
            # a long streaming reply may pause (model thinking) far longer than
            # it, and any leftover timeout would sever the stream mid-response.
            client.settimeout(None)
            upstream.settimeout(None)
            self._splice(client, upstream)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def serve_forever(self) -> None:
        """Block until interrupted (sidecar entrypoint)."""
        self.start()
        try:
            self._stop.wait()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    @staticmethod
    def _splice(a: socket.socket, b: socket.socket) -> None:
        """Bidirectionally forward bytes until BOTH directions reach EOF.

        Each direction pumps independently; when one EOFs it half-closes only
        the peer's write side (SHUT_WR) so the peer sees end-of-request without
        tearing down the OTHER direction. This is what lets a streaming response
        finish after the client has half-closed its request side — tearing both
        sockets down on the first EOF truncates the reply ("Connection closed
        mid-response").
        """

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t1 = threading.Thread(target=pump, args=(a, b), daemon=True)
        t2 = threading.Thread(target=pump, args=(b, a), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def _split_host_port(value: str, default_port: int | None = None) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host:  # no colon
        if default_port is None:
            raise ValueError(f"expected host:port, got {value!r}")
        return value, default_port
    return host, int(port)


def build_tunnel_from_args(argv: list[str]) -> SniTunnel:
    """Build (do not start) an SniTunnel from sidecar CLI args.

    Enforces that --pin is in SNI_TUNNELABLE_DOMAINS so a sidecar can never be
    hand-pointed at a registry — the same code-level guarantee as the harness.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="sni_tunnel")
    parser.add_argument("--pin", required=True, help="the only SNI to relay")
    parser.add_argument("--listen", required=True, help="listen host:port")
    parser.add_argument("--upstream", required=True, help="upstream host:port")
    ns = parser.parse_args(argv)

    if ns.pin.lower() not in SNI_TUNNELABLE_DOMAINS:
        raise ValueError(
            f"--pin {ns.pin!r} is not an allowed SNI-tunnelable endpoint "
            f"({sorted(SNI_TUNNELABLE_DOMAINS)})"
        )
    listen_host, listen_port = _split_host_port(ns.listen)
    upstream_host, upstream_port = _split_host_port(ns.upstream, 443)
    return SniTunnel(
        pinned_sni=ns.pin,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        listen_host=listen_host,
        listen_port=listen_port,
    )


def main(argv: list[str] | None = None) -> int:
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    tunnel = build_tunnel_from_args(sys.argv[1:] if argv is None else argv)
    tunnel.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
