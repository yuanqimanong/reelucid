from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from reelucid.domain import Cancelled, MediaInfo, ReelucidError
from reelucid.ports import Cancellation


def tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise ReelucidError("MISSING_TOOL", f"未安装 {name}，请先安装 FFmpeg/ffprobe。")
    return executable


def run_tool(arguments: list[str], cancelled: Cancellation, timeout: float = 3600) -> bytes:
    if cancelled():
        raise Cancelled()
    with tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdout:
        process = subprocess.Popen(
            arguments, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr
        )
        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancelled():
                    raise Cancelled()
                if time.monotonic() - started > timeout:
                    raise ReelucidError("MEDIA_TIMEOUT", "媒体工具执行超时。", retryable=True)
                time.sleep(0.05)
            stdout.seek(0)
            result = stdout.read(4 * 1024**2)
            if process.returncode:
                raise ReelucidError("INVALID_MEDIA", "媒体无法解码，请检查文件内容、格式和完整性。")
            return result
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()


def probe(path: Path, cancelled: Cancellation = lambda: False) -> MediaInfo:
    result = json.loads(
        run_tool(
            [
                tool("ffprobe"),
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-threads",
                "1",
                "-show_entries",
                "format=duration,start_time,format_name:stream=codec_type,start_time,duration",
                "-of",
                "json",
                str(path),
            ],
            cancelled,
            timeout=30,
        )
    )
    info = result.get("format", {})
    streams = result.get("streams", [])
    format_name = str(info.get("format_name", ""))
    if any(x in format_name for x in ("hls", "dash", "concat")):
        raise ReelucidError("UNSUPPORTED_MEDIA", "上传应为独立音视频文件，不能是播放清单。")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(
        info.get("duration") or max((float(s.get("duration") or 0) for s in streams), default=0)
    )
    if duration <= 0:
        raise ReelucidError("INVALID_MEDIA", "无法确定媒体时长。")
    return MediaInfo(
        duration_ms=round(duration * 1000),
        has_audio=audio is not None,
        has_video=any(s.get("codec_type") == "video" for s in streams),
        format_name=format_name,
        source_origin_seconds=float(info.get("start_time") or 0),
        audio_start_seconds=float(audio.get("start_time") or 0) if audio else 0,
    )


def normalize(
    source: Path, destination: Path, cancelled: Cancellation, *, cpu_threads: int = 1
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".pending.wav")
    try:
        run_tool(
            [
                tool("ffmpeg"),
                "-y",
                "-v",
                "error",
                "-nostdin",
                "-filter_threads",
                "1",
                "-protocol_whitelist",
                "file,pipe",
                "-copyts",
                "-start_at_zero",
                "-threads",
                str(cpu_threads),
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                "aresample=16000:async=1:first_pts=0",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                "-threads",
                "1",
                str(temporary),
            ],
            cancelled,
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def sample_count(path: Path) -> int:
    with wave.open(str(path)) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ReelucidError("INVALID_AUDIO", "规范化音频必须为 16kHz mono PCM16。")
        return audio.getnframes()


def read_samples(path: Path, start: int, end: int) -> object:
    import numpy as np

    with wave.open(str(path)) as audio:
        audio.setpos(start)
        return (
            np.frombuffer(audio.readframes(max(0, end - start)), dtype=np.int16).astype(np.float32)
            / 32768
        )
