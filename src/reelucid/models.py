"""Explicit, administrator-initiated model installation with pinned revisions."""

import json
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from reelucid.domain import JsonObject, ReelucidError
from reelucid.files import digest_file, write_json

MODEL_IDS = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
BUNDLED_SMALL = Path("/opt/reelucid-models/small")
REPOSITORIES = {name: f"Systran/faster-whisper-{name}" for name in MODEL_IDS}
REPOSITORIES["large-v3-turbo"] = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"


def model_path(root: Path, model_id: str) -> Path:
    if model_id not in MODEL_IDS:
        raise ReelucidError("UNKNOWN_MODEL", "不支持此模型标识。")
    installed = root / "asr" / model_id
    if model_id == "small" and not model_ready(installed) and model_ready(BUNDLED_SMALL):
        return BUNDLED_SMALL
    return installed


def model_ready(path: Path) -> bool:
    return all((path / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json"))


def catalog(root: Path) -> list[JsonObject]:
    return [
        {
            "id": name,
            "engine": "faster_whisper",
            "state": "ready" if model_ready(model_path(root, name)) else "not_installed",
        }
        for name in MODEL_IDS
    ]


@dataclass(frozen=True, slots=True)
class PreparedModel:
    manifest: JsonObject
    publish: Callable[[], None]


@contextmanager
def prepare_model(
    root: Path, model_id: str, *, check_cancelled: Callable[[], None] = lambda: None
) -> Generator[PreparedModel]:
    """Hold the install lock until publication and its caller's commit both finish."""
    from filelock import FileLock, Timeout

    check_cancelled()
    path = model_path(root, model_id)
    with ExitStack() as resources:
        if path != BUNDLED_SMALL:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                resources.enter_context(FileLock(str(path.parent / f".{model_id}.lock"), timeout=0))
            except Timeout as exc:
                raise ReelucidError(
                    "MODEL_INSTALL_BUSY", "该模型已有安装任务。", retryable=True
                ) from exc
        if model_ready(path):
            yield PreparedModel(_installed_manifest(path, model_id), check_cancelled)
            return
        staging = root / ".downloads" / model_id
        manifest = _download_model(staging, model_id, check_cancelled)

        with ExitStack() as rollback:

            def publish() -> None:
                check_cancelled()
                if path.exists():
                    # Keep an incomplete manual import on success and restore it on failure.
                    previous = path.with_name(f".{model_id}.replaced-{uuid4().hex}")
                    path.replace(previous)
                    rollback.callback(previous.replace, path)
                staging.replace(path)
                rollback.callback(path.replace, staging)

            yield PreparedModel(manifest, publish)
            rollback.pop_all()


def install_model(root: Path, model_id: str) -> JsonObject:
    with prepare_model(root, model_id) as prepared:
        prepared.publish()
        return prepared.manifest


def _installed_manifest(path: Path, model_id: str) -> JsonObject:
    manifest = path / "reelucid-model.json"
    return (
        json.loads(manifest.read_text(encoding="utf-8"))
        if manifest.is_file()
        else {"id": model_id, "revision": "offline-import"}
    )


def _download_model(
    staging: Path, model_id: str, check_cancelled: Callable[[], None]
) -> JsonObject:
    import httpx2
    from huggingface_hub import HfApi, snapshot_download  # pyright: ignore[reportUnknownVariableType]

    staging.mkdir(parents=True, exist_ok=True)
    repository = REPOSITORIES[model_id]
    try:
        revision = HfApi().model_info(repository).sha
        if not revision:
            raise ReelucidError("MODEL_REVISION_UNAVAILABLE", "无法确认模型版本。")
        check_cancelled()
        snapshot_download(
            repo_id=repository,
            revision=revision,
            local_dir=str(staging),
            allow_patterns=["*.json", "*.bin", "*.txt", "*.model"],
        )
    except ReelucidError:
        raise
    except (OSError, RuntimeError, ValueError, httpx2.HTTPError) as exc:
        raise ReelucidError(
            "MODEL_DOWNLOAD_FAILED", "模型下载失败，请检查网络或离线导入完整模型。", retryable=True
        ) from exc
    check_cancelled()
    if not model_ready(staging):
        raise ReelucidError("MODEL_INCOMPLETE", "模型缺少必要文件。")
    manifest = {
        "id": model_id,
        "repository": repository,
        "revision": revision,
        "files": {
            name.name: digest_file(name)
            for name in staging.iterdir()
            if name.is_file() and name.name != "reelucid-model.json"
        },
    }
    write_json(staging / "reelucid-model.json", manifest)
    return manifest
