import io
import os
import sys
import time
import urllib.request
from email.message import Message
from pathlib import Path
from urllib.response import addinfourl

import pytest

from reelucid.domain import Cancelled, ReelucidError
from reelucid.network import RedirectHandler
from reelucid.xhs_local import XHS_HOSTS, fetch_page, parse_page


def helper(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "resolve.py"
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "resolve.cmd"
        launcher.write_text(f'@echo off\n"{sys.executable}" "{script}"\n', encoding="utf-8")
        return launcher
    script.chmod(0o755)
    return script


def test_subprocess_reads_page_without_service_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELUCID_ADMIN_TOKEN", "do-not-inherit")
    command = helper(
        tmp_path,
        "import json, os, sys\n"
        "assert 'REELUCID_ADMIN_TOKEN' not in os.environ\n"
        "page = json.load(sys.stdin.buffer)\n"
        "assert page['html'] == '<article>笔记</article>'\n"
        "print(json.dumps({'data': page['url']}))",
    )
    url = "https://www.xiaohongshu.com/explore/example"
    result = parse_page(
        command,
        {"html": "<article>笔记</article>", "url": url},
        time.monotonic() + 10,
        lambda: False,
    )
    assert result == {"data": url}


def test_oversized_offline_response_is_rejected(tmp_path: Path) -> None:
    command = helper(tmp_path, "print('x' * (2 * 1024**2 + 1))")
    with pytest.raises(ReelucidError, match="超过上限"):
        parse_page(command, {}, time.monotonic() + 10, lambda: False)


def test_cancelled_page_never_starts_a_parser(tmp_path: Path) -> None:
    with pytest.raises(Cancelled):
        parse_page(tmp_path / "missing", {}, time.monotonic() + 10, lambda: True)


def test_share_redirect_stays_within_site() -> None:
    redirect = RedirectHandler(XHS_HOSTS)
    req = urllib.request.Request("https://xhslink.com/o/example")
    with pytest.raises(ReelucidError, match="其他网站"):
        redirect.redirect_request(req, None, 302, "Found", {}, "https://example.org/")


def test_login_page_is_reported_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    response = addinfourl(io.BytesIO(b""), Message(), "https://www.xiaohongshu.com/login", 200)

    def redirect_to_login(*args: object, **kwargs: object) -> addinfourl:
        return response

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", redirect_to_login)
    with pytest.raises(ReelucidError, match="登录页") as caught:
        fetch_page("https://xhslink.com/o/example", None, time.monotonic() + 10, lambda: False)
    assert caught.value.code == "SOURCE_ACCESS_REQUIRED" and response.closed
