"""Public Bilibili API extractor; reuses yt-dlp's controlled transport and formats."""

from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from pydantic import AliasChoices, BaseModel, Field, ValidationError
from yt_dlp.extractor.common import InfoExtractor

from reelucid import __version__
from reelucid.domain import ReelucidError
from reelucid.network import validate_url


def compatible_media_url(primary: str, backups: list[str]) -> str | None:
    """Use an unchanged upstream URL that the source transport permits."""
    for url in (primary, *backups):
        try:
            validate_url(url)
        except ReelucidError as exc:
            if exc.code not in {"INVALID_URL", "NETWORK_POLICY_BLOCKED"}:
                raise
            continue
        return url
    return None


class BiliPage(BaseModel):
    cid: int
    part: str
    duration: int


class BiliView(BaseModel):
    bvid: str
    title: str
    pages: list[BiliPage]


class BiliStream(BaseModel):
    id: int
    url: str = Field(validation_alias=AliasChoices("baseUrl", "base_url"))
    backup_urls: list[str] = Field(
        default_factory=lambda: list[str](),
        validation_alias=AliasChoices("backupUrl", "backup_url"),
    )
    codecs: str
    width: int | None = None
    height: int | None = None
    bandwidth: int = 0


class BiliDash(BaseModel):
    video: list[BiliStream] = Field(default_factory=lambda: list[BiliStream]())
    audio: list[BiliStream] = Field(default_factory=lambda: list[BiliStream]())


class BiliProgressive(BaseModel):
    url: str
    backup_urls: list[str] = Field(
        default_factory=lambda: list[str](),
        validation_alias=AliasChoices("backup_url", "backupUrl"),
    )


class BiliPlay(BaseModel):
    dash: BiliDash | None = None
    durl: list[BiliProgressive] = Field(default_factory=lambda: list[BiliProgressive]())


class BilibiliPublicIE(InfoExtractor):
    _VALID_URL = (
        r"https?://(?:(?:www\.|m\.)?bilibili\.com/video/(?P<id>BV[\w]+|av\d+)|b23\.tv/[\w]+)"
    )

    def _real_extract(self, url: str) -> Any:
        headers = {"Referer": "https://www.bilibili.com/", "User-Agent": f"Reelucid/{__version__}"}
        if urlsplit(url).hostname == "b23.tv":
            response = self._request_webpage(url, "short-link", headers=headers)
            if response is False:
                raise ReelucidError("DOWNLOAD_FAILED", "Bilibili 短链解析失败。")
            url = response.url
        match = self._match_valid_url(url)
        if not match or not (identifier := match.group("id")):
            raise ReelucidError(
                "UNSUPPORTED_SOURCE", "仅支持 Bilibili 普通视频 BV/av 与 b23 短链。"
            )
        query = {"bvid": identifier} if identifier.startswith("BV") else {"aid": identifier[2:]}
        api_headers = {**headers, "Accept": "application/json"}
        metadata: Any = self._download_json(
            "https://api.bilibili.com/x/web-interface/view",
            identifier,
            query=query,
            headers=api_headers,
        )
        try:
            data = BiliView.model_validate(self._data(metadata))
        except ValidationError as exc:
            raise ReelucidError("EXTRACTOR_BROKEN", "Bilibili 元数据格式已改变。") from exc
        try:
            part = int(parse_qs(urlsplit(url).query).get("p", ["1"])[0])
        except ValueError as exc:
            raise ReelucidError("INVALID_URL", "Bilibili 分 P 编号无效。") from exc
        pages = data.pages
        if not 1 <= part <= len(pages):
            raise ReelucidError("SOURCE_NOT_FOUND", "指定的 Bilibili 分 P 不存在。")
        page = pages[part - 1]
        play: Any = self._download_json(
            "https://api.bilibili.com/x/player/playurl",
            identifier,
            query={
                "bvid": data.bvid,
                "cid": str(page.cid),
                "qn": "64",
                "fnval": "4048",
                "fourk": "0",
            },
            headers=api_headers,
        )
        try:
            media = BiliPlay.model_validate(self._data(play))
        except ValidationError as exc:
            raise ReelucidError("EXTRACTOR_BROKEN", "Bilibili 播放接口格式已改变。") from exc
        formats: list[dict[str, Any]] = []
        blocked_media = False
        dash = media.dash or BiliDash()
        for kind in ("video", "audio"):
            for stream in dash.video if kind == "video" else dash.audio:
                media_url = compatible_media_url(stream.url, stream.backup_urls)
                if media_url is None:
                    blocked_media = True
                    continue
                formats.append(
                    {
                        "format_id": f"{kind}-{stream.id}",
                        "url": media_url,
                        "ext": "mp4" if kind == "video" else "m4a",
                        "width": stream.width,
                        "height": stream.height,
                        "tbr": stream.bandwidth / 1000,
                        "vcodec": stream.codecs if kind == "video" else "none",
                        "acodec": "none" if kind == "video" else stream.codecs,
                        "http_headers": headers,
                    }
                )
        if not formats:
            progressive = media.durl
            if len(progressive) == 1:
                media_url = compatible_media_url(progressive[0].url, progressive[0].backup_urls)
                if media_url is None:
                    blocked_media = True
                else:
                    formats.append(
                        {
                            "url": media_url,
                            "format_id": "progressive",
                            "ext": "mp4",
                            "http_headers": headers,
                        }
                    )
        if not formats:
            if blocked_media:
                raise ReelucidError(
                    "NETWORK_POLICY_BLOCKED", "平台未提供符合来源网络策略的主备媒体地址。"
                )
            raise ReelucidError(
                "SOURCE_ACCESS_REQUIRED", "平台未提供可用视频轨，可能需要登录或访问权限。"
            )
        return {
            "id": f"{data.bvid}_p{part}",
            "title": f"{data.title} · {page.part}" if len(pages) > 1 else data.title,
            "duration": page.duration,
            "formats": formats,
            "webpage_url": url,
            "http_headers": headers,
        }

    @staticmethod
    def _data(payload: Any) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or cast(dict[str, Any], payload).get("code") != 0
            or not isinstance(cast(dict[str, Any], payload).get("data"), dict)
        ):
            raise ReelucidError(
                "SOURCE_ACCESS_REQUIRED",
                "Bilibili 拒绝请求，请检查登录态、视频权限或平台风控。",
                retryable=True,
            )
        return cast(dict[str, Any], payload)["data"]
