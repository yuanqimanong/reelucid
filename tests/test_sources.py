from pathlib import Path

import pytest

from reelucid.domain import ReelucidError
from reelucid.sources import HttpMediaAdapter, SourceRegistry, normalize_source_url


def test_trusted_mounted_adapter_can_be_selected_without_changing_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "mounted_adapter.py").write_text(
        "from reelucid.sources import HttpMediaAdapter\n\n"
        "class MountedAdapter(HttpMediaAdapter):\n"
        '    id = "mounted"\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))  # pyright: ignore[reportUnknownMemberType]
    registry = SourceRegistry(plugins=("mounted_adapter:MountedAdapter",))
    assert registry.select("https://media.example.com/file.mp4", "mounted").id == "mounted"


def test_adapter_collision_and_invalid_entry_are_rejected() -> None:
    with pytest.raises(ReelucidError):
        SourceRegistry([HttpMediaAdapter(), HttpMediaAdapter()])
    with pytest.raises(ReelucidError):
        SourceRegistry(plugins=("../../arbitrary_file",))


def test_share_text_and_ambiguous_links() -> None:
    assert (
        normalize_source_url("分享视频 https://v.douyin.com/abc/，复制后打开")
        == "https://v.douyin.com/abc/"
    )
    assert (
        normalize_source_url("分享视频 https://v.douyin.com/abc/ 。") == "https://v.douyin.com/abc/"
    )
    with pytest.raises(ValueError):
        normalize_source_url("https://example.com/a https://example.com/b")
