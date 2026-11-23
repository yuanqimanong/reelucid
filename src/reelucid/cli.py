from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from reelucid.domain import (
    CHUNK_POLICIES,
    JsonObject,
    PipelineOptions,
    ReelucidError,
    choose_chunk_strategy,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="reelucid", description="帧闻录：本地音视频转录服务")
    sub = parser.add_subparsers(dest="command", required=True)
    from reelucid.component_manager.commands import register

    register(sub.add_parser("components", help="在宿主管理可选 DTK 和小红书服务"))
    local = sub.add_parser("transcribe", help="离线处理本地音视频")
    local.add_argument("file", type=Path)
    local.add_argument("--output", type=Path, default=Path("output"))
    local.add_argument("--model", default="small")
    local.add_argument("--model-path", type=Path)
    local.add_argument("--language", default="auto")
    local.add_argument("--glossary", default="")
    local.add_argument("--chunk-strategy", choices=CHUNK_POLICIES)
    local.add_argument("--no-vad", action="store_true")
    model = sub.add_parser("models", help="管理本地模型")
    model.add_argument("action", choices=["list", "install"])
    model.add_argument("model", nargs="?", default="small")
    serve = sub.add_parser("serve", help="运行 Web/API")
    serve.add_argument("--host", default=os.environ.get("REELUCID_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("REELUCID_PORT", "8000")))
    sub.add_parser("init", help="生成本地目录与部署凭据，打印凭据文件位置")
    sub.add_parser("doctor", help="打印不含凭据的运行环境")
    sub.add_parser("healthcheck", help="检查本地 API 就绪状态")
    args = parser.parse_args()
    try:
        _execute(args)
    except ReelucidError as exc:
        print(json.dumps({"error": exc.public()}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def _execute(args: argparse.Namespace) -> None:
    if args.command == "components":
        from reelucid.component_manager.commands import execute

        execute(args)
        return
    from reelucid.models import catalog, install_model, model_path
    from reelucid.settings import AppConfig

    config = AppConfig()
    config.prepare()
    if args.command == "models":
        if args.action == "install" and config.offline:
            raise ReelucidError("OFFLINE_MODE", "离线模式不允许下载模型。")
        value = (
            install_model(config.models_dir, args.model)
            if args.action == "install"
            else catalog(config.models_dir)
        )
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "transcribe":
        from reelucid.pipeline import run_pipeline

        options = PipelineOptions(
            model_path=str(args.model_path or model_path(config.models_dir, args.model)),
            model_id=args.model,
            language=args.language,
            glossary=args.glossary,
            chunk_strategy=choose_chunk_strategy(
                args.chunk_strategy or config.asr_chunk_policy, args.language, args.model
            ),
            vad=not args.no_vad,
        )

        def progress(event: str, payload: JsonObject) -> None:
            if event != "chunk":
                print(json.dumps({"event": event, **payload}, ensure_ascii=False), file=sys.stderr)

        result = run_pipeline(args.file.resolve(), args.output.resolve(), options, progress)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "serve":
        import uvicorn

        uvicorn.run(
            "reelucid.api:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            access_log=False,
            timeout_graceful_shutdown=10,
        )
    elif args.command == "init":
        from reelucid.security import Security

        Security(config)
        print(
            f"Admin token file: {config.admin_token_file}\nData: {config.data_dir}\nModels: {config.models_dir}"
        )
    elif args.command == "doctor":
        import shutil

        print(
            json.dumps(
                {
                    "python": sys.version,
                    "versions": {
                        name: importlib.metadata.version(name)
                        for name in (
                            "reelucid",
                            "faster-whisper",
                            "ctranslate2",
                            "onnxruntime",
                            "av",
                        )
                    },
                    "ffmpeg": shutil.which("ffmpeg"),
                    "ffprobe": shutil.which("ffprobe"),
                    "models": catalog(config.models_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "healthcheck":
        from urllib.error import URLError
        from urllib.request import urlopen

        try:
            with urlopen(
                f"http://127.0.0.1:{os.environ.get('REELUCID_PORT', '8000')}/api/v1/health/ready",
                timeout=4,
            ) as response:
                if response.status != 200:
                    raise SystemExit(1)
        except URLError, TimeoutError:
            raise SystemExit(1) from None
