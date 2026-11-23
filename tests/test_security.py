import socket
from pathlib import Path
from typing import Any

import pytest

from reelucid.domain import ReelucidError
from reelucid.files import contained
from reelucid.network import checked_connection, public_ip, validate_url
from reelucid.security import Security
from reelucid.settings import AppConfig


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1"],
)
def test_non_public_addresses_blocked(address: str) -> None:
    assert not public_ip(address)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@example.com/a",
        "http://example.com:11434/",
        "gopher://example.com",
    ],
)
def test_invalid_source_urls_rejected(url: str) -> None:
    with pytest.raises(ReelucidError):
        validate_url(url)


def test_mixed_dns_answers_fail_before_opening_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(ReelucidError, match="内网"):
        checked_connection(("mixed.example", 443))


def test_secret_restart_and_tamper_detection(tmp_path: Path) -> None:
    config = AppConfig(tmp_path)
    original = Security(config)
    encrypted = original.encrypt("private-api-key")
    restored = Security(config)
    assert restored.decrypt(encrypted) == "private-api-key"
    assert restored.valid_token(original.token)
    assert restored.valid_session(original.session())
    assert not restored.valid_session("tampered-session")
    with pytest.raises(ReelucidError):
        restored.decrypt(encrypted[:-8] + "AAAAAAAA")


def test_artifact_paths_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(ReelucidError):
        contained(tmp_path / "artifacts", tmp_path / "secrets/master_key")
