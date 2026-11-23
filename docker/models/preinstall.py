"""Prepare the one bundled ASR model from the pinned upstream files."""

import hashlib
import json
import shutil
import sys
from pathlib import Path

from huggingface_hub import snapshot_download


def main() -> None:
    support = Path(__file__).parent
    manifest = json.loads((support / "small.json").read_text(encoding="utf-8"))
    target = Path(sys.argv[1])
    files = manifest["files"]
    source = support / manifest["id"]
    if not source.is_dir():
        source = Path(
            snapshot_download(
                repo_id=manifest["repository"],
                revision=manifest["revision"],
                allow_patterns=list(files),
            )
        )
    target.mkdir(parents=True, exist_ok=False)
    for name, digest in files.items():
        path = target / name
        shutil.copyfile(source / name, path)
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"Bundled model file failed SHA-256 verification: {name}")
    (target / "reelucid-model.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
