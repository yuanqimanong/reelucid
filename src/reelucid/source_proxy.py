"""Administrator-owned HTTP CONNECT proxy for checked source connections."""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode, urlsplit

from reelucid.domain import ReelucidError
from reelucid.network import public_ip

DNS_HOST = "cloudflare-dns.com"
DNS_ADDRESS = "1.1.1.1"
MAX_DNS_BYTES = 64 * 1024
# These translation prefixes can embed private IPv4 destinations.
NAT64 = (ipaddress.IPv6Network("64:ff9b::/96"), ipaddress.IPv6Network("64:ff9b:1::/48"))


def allowed_destination(address: str) -> bool:
    if "%" in address or not public_ip(address):
        return False
    parsed = ipaddress.ip_address(address)
    return not any(parsed in network for network in NAT64)


@dataclass(frozen=True)
class SourceProxy:
    host: str
    port: int

    @classmethod
    def configured(cls, value: str) -> SourceProxy | None:
        if not value:
            return None
        try:
            endpoint = urlsplit(value)
            if (
                endpoint.scheme != "http"
                or not endpoint.hostname
                or not endpoint.port
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.path not in {"", "/"}
                or endpoint.query
                or endpoint.fragment
                or any(character.isspace() for character in value)
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                "REELUCID_SOURCE_PROXY must be an HTTP proxy address with an explicit port"
            ) from None
        return cls(endpoint.hostname, endpoint.port)

    def _tunnel(self, address: str, port: int, timeout: float) -> socket.socket:
        # The proxy receives a checked numeric address. It never resolves a source host name.
        if not allowed_destination(address) or port not in {80, 443}:
            raise ReelucidError("NETWORK_POLICY_BLOCKED", "代理仅允许连接公网标准端口。")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        connection.set_tunnel(address, port)
        try:
            connection.connect()
            connected = connection.sock
            assert connected is not None
            connection.sock = None
            return connected
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise ReelucidError(
                "SOURCE_PROXY_FAILED",
                "视频源代理无法建立连接，请检查代理地址和出站规则。",
                retryable=True,
            ) from exc

    def _dns_answers(self, name: str, record_type: str, timeout: float) -> list[str]:
        # Use an authenticated DNS endpoint over the configured proxy so Clash fake-IP
        # responses from the system resolver never become CONNECT destinations.
        query = urlencode({"name": name, "type": record_type})
        with self._tunnel(DNS_ADDRESS, 443, timeout) as raw:
            try:
                with ssl.create_default_context().wrap_socket(raw, server_hostname=DNS_HOST) as tls:
                    request = (
                        f"GET /dns-query?{query} HTTP/1.1\r\n"
                        f"Host: {DNS_HOST}\r\nAccept: application/dns-json\r\nConnection: close\r\n\r\n"
                    )
                    tls.sendall(request.encode("ascii"))
                    with http.client.HTTPResponse(tls) as response:
                        response.begin()
                        body = response.read(MAX_DNS_BYTES + 1)
                        if response.status != 200 or len(body) > MAX_DNS_BYTES:
                            raise ValueError
                data: object = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError
                payload = cast(dict[str, object], data)
                if payload.get("Status") != 0:
                    raise ValueError
                answers = payload.get("Answer", [])
                if not isinstance(answers, list):
                    raise ValueError
                addresses: list[str] = []
                for answer in cast(list[object], answers):
                    if not isinstance(answer, dict):
                        raise ValueError
                    record = cast(dict[str, object], answer)
                    if record.get("type") in (1, 28):
                        address = record.get("data")
                        if not isinstance(address, str) or "%" in address:
                            raise ValueError
                        addresses.append(str(ipaddress.ip_address(address)))
                return addresses
            except (OSError, http.client.HTTPException, ValueError) as exc:
                raise ReelucidError(
                    "SOURCE_DNS_FAILED", "无法通过代理完成视频源的公网 DNS 查询。", retryable=True
                ) from exc

    def connect(self, host: str, port: int, timeout: float) -> socket.socket:
        if port not in {80, 443} or "%" in host:
            raise ReelucidError("NETWORK_POLICY_BLOCKED", "代理仅允许连接公网标准端口。")
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise ReelucidError("DOWNLOAD_TIMEOUT", "视频源连接超时。", retryable=True)
            return seconds

        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                name = host.rstrip(".").encode("idna").decode("ascii").lower()
            except UnicodeError:
                raise ReelucidError("INVALID_URL", "视频源域名格式无效。") from None
            if (
                len(name) > 253
                or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", name)
                or name.endswith((".localhost", ".local", ".internal", ".home.arpa"))
            ):
                raise ReelucidError(
                    "NETWORK_POLICY_BLOCKED", "代理仅支持公网视频源域名。"
                ) from None
            addresses = self._dns_answers(name, "A", remaining())
            addresses.extend(self._dns_answers(name, "AAAA", remaining()))
        if not addresses:
            raise ReelucidError(
                "SOURCE_DNS_FAILED", "视频源域名没有公网 DNS 记录。", retryable=True
            )
        if any(not allowed_destination(address) for address in addresses):
            raise ReelucidError(
                "NETWORK_POLICY_BLOCKED", "视频源解析到非公网地址，代理连接已阻止。"
            )
        # DNS may supply multiple alternatives, but a source cannot multiply the time budget.
        last_error: ReelucidError | None = None
        for address in list(dict.fromkeys(addresses))[:8]:
            try:
                return self._tunnel(address, port, remaining())
            except ReelucidError as exc:
                if exc.code != "SOURCE_PROXY_FAILED":
                    raise
                last_error = exc
        assert last_error is not None
        raise last_error
