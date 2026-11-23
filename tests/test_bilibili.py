from typing import Any

import pytest

from reelucid.bilibili import BilibiliPublicIE
from reelucid.domain import ReelucidError
from reelucid.network import validate_url


def test_part_selection_uses_selected_cid_and_source_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[dict[str, str]] = []
    extractor = BilibiliPublicIE()

    def download(
        url: str, _identifier: str, *, query: dict[str, str], headers: dict[str, str]
    ) -> Any:
        queries.append(query)
        assert headers["Referer"] == "https://www.bilibili.com/"
        if url.endswith("/view"):
            return {
                "code": 0,
                "data": {
                    "bvid": "BVfixture",
                    "title": "Fixture",
                    "pages": [
                        {"cid": 1, "part": "one", "duration": 9},
                        {"cid": 2, "part": "two", "duration": 15},
                    ],
                },
            }
        return {
            "code": 0,
            "data": {
                "dash": {
                    "video": [
                        {
                            "id": 64,
                            "baseUrl": "https://cdn.example/video",
                            "codecs": "avc1",
                            "height": 720,
                        }
                    ],
                    "audio": [{"id": 1, "base_url": "https://cdn.example/audio", "codecs": "mp4a"}],
                }
            },
        }

    monkeypatch.setattr(extractor, "_download_json", download)
    info = extractor._real_extract("https://www.bilibili.com/video/BVfixture?p=2")  # pyright: ignore[reportPrivateUsage]
    assert queries[1]["cid"] == "2"
    assert info["duration"] == 15 and info["id"] == "BVfixture_p2"
    assert info["formats"][0]["acodec"] == "none"
    assert info["formats"][1]["vcodec"] == "none"
    extractor._real_extract("https://www.bilibili.com/video/av123?p=2")  # pyright: ignore[reportPrivateUsage]
    assert queries[-2] == {"aid": "123"}
    assert queries[-1]["bvid"] == "BVfixture" and "aid" not in queries[-1]
    with pytest.raises(ReelucidError) as error:
        extractor._real_extract("https://www.bilibili.com/video/BVfixture?p=3")  # pyright: ignore[reportPrivateUsage]
    assert error.value.code == "SOURCE_NOT_FOUND"


@pytest.mark.parametrize("backup_key", ["backupUrl", "backup_url"])
def test_pcdn_nonstandard_port_uses_unmodified_standard_https_backup(
    monkeypatch: pytest.MonkeyPatch,
    backup_key: str,
) -> None:
    extractor = BilibiliPublicIE()
    video_url = "https://upos.example/video.m4s?deadline=123&sign=preserved%2Bsignature"
    audio_url = "https://upos.example:443/audio.m4s?sign=preserved"

    def download(url: str, *_args: Any, **_kwargs: Any) -> Any:
        if url.endswith("/view"):
            return {
                "code": 0,
                "data": {
                    "bvid": "BVfixture",
                    "title": "Fixture",
                    "pages": [{"cid": 1, "part": "one", "duration": 1290}],
                },
            }
        return {
            "code": 0,
            "data": {
                "dash": {
                    "video": [
                        {
                            "id": 80,
                            "baseUrl": "https://pcdn.example:4483/blocked",
                            "codecs": "avc1",
                            "height": 1080,
                        },
                        {
                            "id": 32,
                            "baseUrl": "https://pcdn.example:8082/video",
                            backup_key: ["https://pcdn.example:4483/also-blocked", video_url],
                            "codecs": "avc1",
                            "height": 480,
                        },
                    ],
                    "audio": [
                        {
                            "id": 30232,
                            "baseUrl": "https://pcdn.example:8082/audio",
                            backup_key: [audio_url],
                            "codecs": "mp4a",
                        }
                    ],
                }
            },
        }

    monkeypatch.setattr(extractor, "_download_json", download)
    info = extractor._real_extract(  # pyright: ignore[reportPrivateUsage]
        "https://www.bilibili.com/video/BVfixture/?trackid=tracking&spm_id_from=home"
    )
    assert [item["url"] for item in info["formats"]] == [video_url, audio_url]
    assert info["duration"] == 1290
    with pytest.raises(ReelucidError) as error:
        validate_url("https://pcdn.example:8082/video")
    assert error.value.code == "NETWORK_POLICY_BLOCKED"


@pytest.mark.parametrize("has_backup", [True, False])
def test_progressive_backup_and_no_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    has_backup: bool,
) -> None:
    extractor = BilibiliPublicIE()

    def download(url: str, *_args: Any, **_kwargs: Any) -> Any:
        if url.endswith("/view"):
            return {
                "code": 0,
                "data": {
                    "bvid": "BVfixture",
                    "title": "Fixture",
                    "pages": [{"cid": 1, "part": "one", "duration": 10}],
                },
            }
        return {
            "code": 0,
            "data": {
                "durl": [
                    {
                        "url": "https://pcdn.example:8082/video",
                        "backup_url": ["https://upos.example/video?sign=unchanged"]
                        if has_backup
                        else ["file:///etc/passwd", "https://pcdn.example:4483/video"],
                    }
                ]
            },
        }

    monkeypatch.setattr(extractor, "_download_json", download)
    if has_backup:
        info = extractor._real_extract("https://www.bilibili.com/video/BVfixture")  # pyright: ignore[reportPrivateUsage]
        assert info["formats"][0]["url"] == "https://upos.example/video?sign=unchanged"
    else:
        with pytest.raises(ReelucidError) as error:
            extractor._real_extract("https://www.bilibili.com/video/BVfixture")  # pyright: ignore[reportPrivateUsage]
        assert error.value.code == "NETWORK_POLICY_BLOCKED"
