from pathlib import Path

import pytest

from reelucid.files import digest_file
from reelucid.sources import HttpMediaAdapter, YtDlpAdapter


@pytest.mark.network
def test_public_media_download_through_both_source_transports(tmp_path: Path) -> None:
    url = "https://www.w3schools.com/html/mov_bbb.mp4"
    direct = HttpMediaAdapter().acquire(
        url, tmp_path / "direct", 20 * 1024**2, True, lambda _event, _payload: None, lambda: False
    )
    extracted = YtDlpAdapter().acquire(
        url,
        tmp_path / "extractor",
        20 * 1024**2,
        True,
        lambda _event, _payload: None,
        lambda: False,
    )
    assert direct["sha256"] == extracted["sha256"]
    assert digest_file(Path(direct["path"])) == direct["sha256"]
