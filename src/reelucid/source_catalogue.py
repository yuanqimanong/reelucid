"""An upstream inventory is discoverability, never a platform success guarantee."""

from importlib.metadata import version

from reelucid.domain import JsonObject


def source_catalogue() -> JsonObject:
    from yt_dlp.extractor import list_extractor_classes

    extractors = list(list_extractor_classes())
    return {
        "engine": "yt-dlp",
        "version": version("yt-dlp"),
        "scope": "single_vod_only",
        "notice": "上游提取器目录不代表实测成功；可用性取决于 URL、网络、登录、DRM 和下载协议。",
        "extractor_count": len(extractors),
        "extractors": [
            {"id": ie.ie_key(), "name": ie.IE_NAME, "upstream_working": ie.working()}
            for ie in extractors
        ],
        "built_in": ["upload", "http_media", "yt_dlp", "bilibili"],
        "optional": [
            {
                "id": "dtk",
                "platforms": ["douyin", "tiktok", "bilibili"],
                "requires": "admin-configured DTK v5 endpoint",
            },
            {
                "id": "xhs",
                "platforms": ["xiaohongshu", "rednote"],
                "requires": "bundled XHS parser (Docker) or admin-configured XHS-Downloader endpoint",
            },
        ],
    }
