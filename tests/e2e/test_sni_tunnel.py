"""Tests for the host-side SNI-pinned tunnel (anti-cheat method A).

The tunnel lets a quarantine container reach ONE Cloudflare-fronted LLM
endpoint (e.g. api.kimi.com) while the same Cloudflare IP range stays
CIDR-blocked for the package registry that also rides it (npm). Safety rests
entirely on the forwarder refusing to relay any TLS ClientHello whose SNI is
not the pinned endpoint — so these tests hammer the SNI parser and the
accept/reject decision with real ClientHello bytes and adversarial inputs.
"""

import socket
import ssl
import threading
import time

import pytest

from harness.e2e.sni_tunnel import (
    SNI_TUNNELABLE_DOMAINS,
    SniTunnel,
    build_tunnel_from_args,
    parse_sni,
    sni_allowed,
    tunnel_plan,
)


def _real_client_hello(hostname: str) -> bytes:
    """A genuine TLS ClientHello OpenSSL emits for `hostname`.

    Driven through ssl.MemoryBIO so the bytes are exactly what a real client
    (curl, claude-code) would put on the wire — ground truth, not a hand-rolled
    record that could share a bug with the parser.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    inbio = ssl.MemoryBIO()
    outbio = ssl.MemoryBIO()
    sslobj = ctx.wrap_bio(inbio, outbio, server_hostname=hostname)
    try:
        sslobj.do_handshake()
    except ssl.SSLWantReadError:
        pass  # handshake blocks waiting for ServerHello; ClientHello is already out
    return outbio.read()


@pytest.mark.parametrize("hostname", ["api.kimi.com", "registry.npmjs.org", "a.io"])
def test_parse_sni_extracts_hostname_from_real_client_hello(hostname):
    assert parse_sni(_real_client_hello(hostname)) == hostname


def test_parse_sni_returns_none_on_non_tls_garbage():
    assert parse_sni(b"GET / HTTP/1.1\r\nHost: evil\r\n\r\n") is None


def test_parse_sni_returns_none_on_empty():
    assert parse_sni(b"") is None


def test_parse_sni_does_not_crash_on_truncated_client_hello():
    full = _real_client_hello("api.kimi.com")
    # Every prefix must parse to either the right host or None — never raise.
    for cut in range(1, len(full)):
        result = parse_sni(full[:cut])
        assert result in (None, "api.kimi.com")


def test_sni_allowed_accepts_pinned_host():
    assert sni_allowed("api.kimi.com", {"api.kimi.com"}) is True


def test_sni_allowed_rejects_registry():
    assert sni_allowed("registry.npmjs.org", {"api.kimi.com"}) is False


def test_sni_allowed_rejects_none():
    assert sni_allowed(None, {"api.kimi.com"}) is False


def test_sni_allowed_is_case_insensitive():
    # DNS is case-insensitive; an attacker must not slip past with API.KIMI.COM.
    assert sni_allowed("API.KIMI.COM", {"api.kimi.com"}) is True


def test_sni_allowed_rejects_suffix_lookalike():
    # api.kimi.com.evil.test must NOT be treated as the pinned host.
    assert sni_allowed("api.kimi.com.evil.test", {"api.kimi.com"}) is False


# --- Forwarder splice behavior (the live security boundary) -----------------


class _FakeUpstream:
    """A plain TCP server that records the first bytes any client sends."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.host, self.port = self._sock.getsockname()
        self.received = b""
        self._got = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.settimeout(2)
            try:
                data = conn.recv(4096)
                if data:
                    self.received += data
                    self._got.set()
            except OSError:
                pass
            finally:
                conn.close()

    def wait_for_bytes(self, timeout: float) -> bool:
        return self._got.wait(timeout)

    def close(self):
        self._sock.close()


def _send_client_hello_to(host: str, port: int, sni_hostname: str) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    inbio, outbio = ssl.MemoryBIO(), ssl.MemoryBIO()
    sslobj = ctx.wrap_bio(inbio, outbio, server_hostname=sni_hostname)
    try:
        sslobj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    hello = outbio.read()
    s = socket.create_connection((host, port), timeout=2)
    try:
        s.sendall(hello)
        time.sleep(0.3)  # let the tunnel peek + (maybe) relay
    finally:
        s.close()


@pytest.fixture
def upstream():
    up = _FakeUpstream()
    yield up
    up.close()


def test_tunnel_relays_connection_with_pinned_sni(upstream):
    tunnel = SniTunnel(
        pinned_sni="api.kimi.com",
        upstream_host=upstream.host,
        upstream_port=upstream.port,
    )
    port = tunnel.start()
    try:
        _send_client_hello_to("127.0.0.1", port, "api.kimi.com")
        assert upstream.wait_for_bytes(2.0), "pinned-SNI connection was not relayed"
        assert parse_sni(upstream.received) == "api.kimi.com"
    finally:
        tunnel.stop()


# --- Activation decision (tunnel_plan) --------------------------------------

# A resolver stub: kimi rides a Cloudflare IP; a non-tunnelable host does not.
def _fake_resolver(host):
    return {
        "api.kimi.com": ["104.18.20.246"],
        "api.moonshot.ai": ["104.18.28.136"],
        "api.anthropic.com": ["160.79.104.10"],  # Anthropic ASN, not Cloudflare
    }.get(host, [])


def test_tunnel_plan_activates_for_cidr_blocked_llm_endpoint():
    # element blocks Cloudflare (104.16.0.0/12); kimi rides it → needs a tunnel.
    assert tunnel_plan(
        "https://api.kimi.com/coding",
        deny_cidrs=["104.16.0.0/12"],
        resolver=_fake_resolver,
    ) == "api.kimi.com"


def test_tunnel_plan_none_when_endpoint_not_cidr_blocked():
    # No deny_cidrs overlap kimi's IP → kimi is reachable normally, no tunnel.
    assert tunnel_plan(
        "https://api.kimi.com/coding",
        deny_cidrs=["151.101.0.0/16"],  # Fastly, unrelated
        resolver=_fake_resolver,
    ) is None


def test_tunnel_plan_none_for_host_not_in_allowlist():
    # A registry must NEVER be tunnelable, even if it's CIDR-blocked.
    assert tunnel_plan(
        "https://registry.npmjs.org",
        deny_cidrs=["104.16.0.0/12"],
        resolver=lambda h: ["104.16.8.34"],
    ) is None


def test_tunnel_plan_none_for_first_party_anthropic():
    # api.anthropic.com isn't Cloudflare-fronted and isn't in the allowlist.
    assert tunnel_plan(
        "https://api.anthropic.com",
        deny_cidrs=["104.16.0.0/12"],
        resolver=_fake_resolver,
    ) is None


def test_tunnel_plan_none_for_empty_base_url():
    assert tunnel_plan("", deny_cidrs=["104.16.0.0/12"], resolver=_fake_resolver) is None


def test_build_tunnel_from_args_parses_sidecar_invocation():
    # The sidecar container runs: sni_tunnel.py --pin H --listen 0.0.0.0:443 --upstream H:443
    t = build_tunnel_from_args(
        ["--pin", "api.kimi.com", "--listen", "0.0.0.0:443",
         "--upstream", "api.kimi.com:443"]
    )
    assert t.pinned_sni == "api.kimi.com"
    assert t.listen_host == "0.0.0.0"
    assert t.listen_port == 443
    assert t.upstream_host == "api.kimi.com"
    assert t.upstream_port == 443


def test_build_tunnel_from_args_rejects_non_allowlisted_pin():
    # A sidecar must never be pinned to a registry, even by hand.
    with pytest.raises(ValueError, match="not an allowed"):
        build_tunnel_from_args(
            ["--pin", "registry.npmjs.org", "--listen", "0.0.0.0:443",
             "--upstream", "registry.npmjs.org:443"]
        )


def test_allowlist_contains_only_llm_endpoints_never_registries():
    registries = {
        "registry.npmjs.org", "registry.yarnpkg.com", "pypi.org",
        "files.pythonhosted.org", "crates.io", "static.crates.io",
        "repo1.maven.org", "proxy.golang.org",
    }
    assert SNI_TUNNELABLE_DOMAINS.isdisjoint(registries)


class _StreamingUpstream:
    """A TCP server that, after the initial bytes, streams a long reply in
    chunks (with a configurable gap) then closes — models an SSE/streaming LLM
    response, including a mid-stream pause while the model 'thinks'."""

    def __init__(self, total_chunks=50, chunk=b"x" * 1024, gap=0.005):
        self.expected = chunk * total_chunks
        self._chunk = chunk
        self._n = total_chunks
        self._gap = gap
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.host, self.port = self._sock.getsockname()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.recv(4096)  # consume the relayed ClientHello
            for _ in range(self._n):
                conn.sendall(self._chunk)
                time.sleep(self._gap)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self._sock.close()


def test_tunnel_relays_full_streaming_response_after_client_half_close(upstream):
    # Reproduces "Connection closed mid-response": a client sends its request
    # then half-closes its write side and reads a long streamed reply. The
    # forwarder must NOT tear down the response direction when the request
    # direction EOFs.
    up = _StreamingUpstream()
    tunnel = SniTunnel(
        pinned_sni="api.kimi.com",
        upstream_host=up.host,
        upstream_port=up.port,
    )
    port = tunnel.start()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        inbio, outbio = ssl.MemoryBIO(), ssl.MemoryBIO()
        obj = ctx.wrap_bio(inbio, outbio, server_hostname="api.kimi.com")
        try:
            obj.do_handshake()
        except ssl.SSLWantReadError:
            pass
        hello = outbio.read()

        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(hello)
        s.shutdown(socket.SHUT_WR)  # client done sending; awaits the long reply
        s.settimeout(3)
        received = b""
        while True:
            try:
                data = s.recv(65536)
            except OSError:
                break
            if not data:
                break
            received += data
        s.close()
        assert received == up.expected, (
            f"streaming response truncated: got {len(received)} of "
            f"{len(up.expected)} bytes"
        )
    finally:
        tunnel.stop()
        up.close()


def test_tunnel_survives_stream_gap_longer_than_handshake_timeout():
    # The short timeout used to fast-drop a non-TLS/idle client at ClientHello
    # time must NOT leak into the relay: a streaming reply that pauses (model
    # thinking) longer than that timeout must still arrive in full. Otherwise
    # long LLM responses truncate as "Connection closed mid-response".
    up = _StreamingUpstream(total_chunks=6, gap=0.4)  # 0.4s > handshake_timeout
    tunnel = SniTunnel(
        pinned_sni="api.kimi.com",
        upstream_host=up.host,
        upstream_port=up.port,
        handshake_timeout=0.15,
    )
    port = tunnel.start()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        inbio, outbio = ssl.MemoryBIO(), ssl.MemoryBIO()
        obj = ctx.wrap_bio(inbio, outbio, server_hostname="api.kimi.com")
        try:
            obj.do_handshake()
        except ssl.SSLWantReadError:
            pass
        hello = outbio.read()

        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(hello)
        s.settimeout(5)
        received = b""
        while True:
            try:
                data = s.recv(65536)
            except OSError:
                break
            if not data:
                break
            received += data
        s.close()
        assert received == up.expected, (
            f"stream truncated across a {0.4}s gap: got {len(received)} of "
            f"{len(up.expected)} bytes — handshake timeout leaked into the relay"
        )
    finally:
        tunnel.stop()
        up.close()


def test_tunnel_drops_connection_with_unpinned_sni(upstream):
    tunnel = SniTunnel(
        pinned_sni="api.kimi.com",
        upstream_host=upstream.host,
        upstream_port=upstream.port,
    )
    port = tunnel.start()
    try:
        # An agent trying to reach npm through the tunnel by SNI-spoofing.
        _send_client_hello_to("127.0.0.1", port, "registry.npmjs.org")
        assert not upstream.wait_for_bytes(1.0), (
            "unpinned-SNI (npm) connection reached upstream — detour is OPEN"
        )
    finally:
        tunnel.stop()
