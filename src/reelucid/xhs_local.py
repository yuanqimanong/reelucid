"""Fetch a single public note and pass its HTML to the packaged XHS parser."""

import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from reelucid.domain import Cancelled, JsonObject, ReelucidError
from reelucid.network import source_opener, validate_url
from reelucid.ports import Cancellation
from reelucid.source_proxy import SourceProxy

XHS_HOSTS = frozenset(
    {"www.xiaohongshu.com", "xiaohongshu.com", "www.rednote.com", "xhslink.com", "xhslink.cn"}
)
MAX_PAGE_BYTES = 4 * 1024**2
MAX_RESULT_BYTES = 2 * 1024**2


def remaining(deadline: float, cancelled: Cancellation) -> float:
    if cancelled():
        raise Cancelled()
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise ReelucidError("RESOLVER_TIMEOUT", "小红书解析超时。", retryable=True)
    return seconds


def fetch_page(
    url: str,
    cookies: CookieJar | None,
    deadline: float,
    cancelled: Cancellation,
    *,
    proxy: SourceProxy | None = None,
) -> JsonObject:
    validate_url(url)
    if urlsplit(url).hostname not in XHS_HOSTS:
        raise ReelucidError("UNSUPPORTED_SOURCE", "请输入小红书作品或分享链接。")
    opener = source_opener(cookies, redirect_hosts=XHS_HOSTS, proxy=proxy)
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/html", "Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with opener.open(request, timeout=min(15, remaining(deadline, cancelled))) as response:
            final_url = urlsplit(str(response.geturl()))
            if final_url.path.rstrip("/") == "/login":
                raise ReelucidError(
                    "SOURCE_ACCESS_REQUIRED",
                    "小红书将作品链接转到了登录页。可在新任务中选择有权访问作品的站点凭据。",
                )
            if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                raise ReelucidError("SOURCE_ACCESS_REQUIRED", "该链接未返回小红书作品页面。")
            page = bytearray()
            while len(page) <= MAX_PAGE_BYTES:
                remaining(deadline, cancelled)
                block = response.read1(min(64 * 1024, MAX_PAGE_BYTES + 1 - len(page)))
                if not block:
                    break
                page.extend(block)
            if len(page) > MAX_PAGE_BYTES:
                raise ReelucidError("SIZE_LIMIT_EXCEEDED", "小红书作品页面超过大小上限。")
            # The offline parser needs the note path, not the signed sharing parameters.
            url_without_query = final_url._replace(query="", fragment="").geturl()
            return {"url": url_without_query, "html": page.decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ReelucidError(
            "SOURCE_ACCESS_REQUIRED",
            "小红书未提供作品页面，请检查链接和访问权限。",
            retryable=exc.code == 429 or exc.code >= 500,
        ) from exc
    except (urllib.error.URLError, HTTPException, OSError) as exc:
        raise ReelucidError("DOWNLOAD_FAILED", "读取小红书作品页面失败。", retryable=True) from exc


def parse_page(
    command: Path, page: JsonObject, deadline: float, cancelled: Cancellation
) -> JsonObject:
    remaining(deadline, cancelled)
    with tempfile.TemporaryDirectory(prefix="xhs-") as work:
        with tempfile.TemporaryFile(dir=work) as source, tempfile.TemporaryFile(dir=work) as output:
            source.write(json.dumps(page, ensure_ascii=False).encode("utf-8"))
            source.seek(0)
            # The packaged parser needs no service credentials, proxy or Python path from the API.
            env = {"HOME": work, "TMPDIR": work, "LANG": "C.UTF-8"}
            if os.name == "nt":
                env["SystemRoot"] = os.environ["SystemRoot"]
            try:
                child = subprocess.Popen(
                    [command],
                    stdin=source,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    cwd=work,
                    start_new_session=os.name == "posix",
                )
            except OSError as exc:
                raise ReelucidError("RESOLVER_UNAVAILABLE", "无法启动镜像中的小红书插件。") from exc
            try:
                while child.poll() is None:
                    seconds = remaining(deadline, cancelled)
                    if os.fstat(output.fileno()).st_size > MAX_RESULT_BYTES:
                        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "小红书插件输出超过上限。")
                    try:
                        child.wait(timeout=min(0.15, seconds))
                    except subprocess.TimeoutExpired:
                        continue
                remaining(deadline, cancelled)
                if child.returncode:
                    raise ReelucidError("RESOLVER_FAILED", "小红书本地插件解析失败。")
                output.seek(0)
                raw = output.read(MAX_RESULT_BYTES + 1)
            finally:
                if child.poll() is None:
                    if os.name == "posix":
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        child.kill()
                    child.wait()
    if len(raw) > MAX_RESULT_BYTES:
        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "小红书插件输出超过上限。")
    try:
        payload: Any = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "小红书插件未返回有效 JSON。") from exc
    if not isinstance(payload, dict) or "data" not in payload:
        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "小红书插件响应缺少 data。")
    return cast(JsonObject, payload)
