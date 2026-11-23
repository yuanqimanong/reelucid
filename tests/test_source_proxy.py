from __future__ import annotations

import socketserver
import threading
from typing import ClassVar

import pytest

from reelucid.domain import ReelucidError
from reelucid.network import source_opener
from reelucid.source_proxy import SourceProxy


@pytest.mark.parametrize(
    "endpoint",
    [
        "socks5://proxy.example:7890",
        "http://u:p@proxy.example:7890",
        "http://proxy.example",
        "http://proxy.example:7890/path",
        "http://proxy.example:7890?mode=direct",
    ],
)
def test_proxy_config_requires_fixed_http_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError, match="REELUCID_SOURCE_PROXY"):
        SourceProxy.configured(endpoint)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "169.254.169.254", "198.18.0.1", "64:ff9b::7f00:1"]
)
def test_one_nonpublic_answer_blocks_before_tunnelling(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    def answers(*_args: object) -> list[str]:
        return ["8.8.8.8", address]

    def tunnel(*_args: object) -> object:
        pytest.fail("The proxy must not receive a destination with a nonpublic DNS answer")

    monkeypatch.setattr(SourceProxy, "_dns_answers", answers)
    monkeypatch.setattr(SourceProxy, "_tunnel", tunnel)
    with pytest.raises(ReelucidError) as caught:
        SourceProxy("localhost", 7890).connect("example.com", 443, 3)
    assert caught.value.code == "NETWORK_POLICY_BLOCKED"


def test_http_proxy_dials_checked_address_and_rejects_private_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Proxy(socketserver.StreamRequestHandler):
        requests: ClassVar[list[bytes]] = []

        def request_headers(self) -> bytes:
            first_line = self.rfile.readline()
            while (line := self.rfile.readline()) not in {b"\r\n", b""}:
                first_line += line
            return first_line

        def handle(self) -> None:
            self.connection.settimeout(2)
            self.requests.append(self.request_headers())
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.requests.append(self.request_headers())
            self.wfile.write(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )

    def answers(_: SourceProxy, host: str, record_type: str, timeout: float) -> list[str]:
        assert host == "example.com" and timeout > 0
        return ["8.8.8.8"] if record_type == "A" else []

    monkeypatch.setattr(SourceProxy, "_dns_answers", answers)
    with socketserver.TCPServer(("127.0.0.1", 0), Proxy) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = int(server.server_address[1])
            opener = source_opener(proxy=SourceProxy("127.0.0.1", port))
            with pytest.raises(ReelucidError) as caught:
                opener.open("http://example.com/media.mp4", timeout=3)
            assert caught.value.code == "NETWORK_POLICY_BLOCKED"
            assert len(Proxy.requests) == 2
            assert Proxy.requests[0].startswith(b"CONNECT 8.8.8.8:80 ")
            assert b"GET /media.mp4 HTTP/" in Proxy.requests[1]
            assert b"host: example.com" in Proxy.requests[1].lower()
        finally:
            server.shutdown()
            thread.join(timeout=3)
