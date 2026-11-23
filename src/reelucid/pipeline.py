"""Local media -> canonical transcript. Independent of HTTP and databases."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

from reelucid.domain import (
    Cancelled,
    JsonObject,
    PipelineOptions,
    ReelucidError,
    Segment,
    SpeechRegion,
    Transcript,
    make_chunks,
    own_segments,
)
from reelucid.exports import write_exports
from reelucid.files import digest_file, digest_json, write_json
from reelucid.media import normalize, probe, read_samples, sample_count
from reelucid.ports import AsrEngine, Cancellation, Progress, VadEngine


def run_pipeline(
    source: Path,
    output: Path,
    options: PipelineOptions,
    progress: Progress = lambda _event, _payload: None,
    cancelled: Cancellation = lambda: False,
    engine: AsrEngine | None = None,
    vad: VadEngine | None = None,
    resume: dict[str, str] | None = None,
    *,
    cpu_threads: int | None = None,
) -> JsonObject:
    # A retry may have a different admission budget but reuse the same media checkpoints.
    threads = min(options.cpu_threads, cpu_threads or options.cpu_threads)

    def stage(name: str, **payload: object) -> None:
        if cancelled():
            raise Cancelled()
        progress("stage", {"stage": name, **payload})

    output.mkdir(parents=True, exist_ok=True)
    stage("inspect")
    media = probe(source, cancelled)
    if not media.has_audio:
        raise ReelucidError("NO_AUDIO_TRACK", "媒体没有音轨，不能执行语音转录。")
    if media.duration_ms > options.max_duration_seconds * 1000:
        raise ReelucidError("MEDIA_DURATION_LIMIT", "媒体超过实例允许的时长。")
    model = Path(options.model_path)
    # Retrying a job created before adjacent grouping must find its original checkpoints.
    legacy_chunks = options.chunk_strategy == "silence"
    key = digest_json(
        {
            "source": digest_file(source),
            "options": options.model_dump(exclude={"chunk_strategy"} if legacy_chunks else None),
            "pipeline": 2 if legacy_chunks else 3,
            "model": {
                name: digest_file(model / name)
                for name in ("model.bin", "config.json", "tokenizer.json")
                if (model / name).is_file()
            },
            "runtimes": {
                name: importlib.metadata.version(name)
                for name in ("faster-whisper", "onnxruntime", "ctranslate2")
            },
        }
    )
    work = output / "checkpoints" / key
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "normalized.wav"
    normalized_meta = work / "normalization.json"
    stage("normalize")
    if (
        not wav.exists()
        or not normalized_meta.exists()
        or json.loads(normalized_meta.read_text())["sha256"] != digest_file(wav)
    ):
        normalize(source, wav, cancelled, cpu_threads=threads)
        write_json(
            normalized_meta,
            {
                "policy": "container_pts_async1_v1",
                "media": media.model_dump(),
                "sha256": digest_file(wav),
                "sample_count": sample_count(wav),
            },
        )
    total = sample_count(wav)
    stage("vad")
    if options.vad:
        if vad is None:
            from reelucid.engines import SileroOnnx

            vad = SileroOnnx(options.vad_model_path)
        regions = vad.detect(wav, cancelled)
    else:
        regions = [SpeechRegion(start=0, end=total)]
    chunks = make_chunks(regions, total, options.chunk_seconds, strategy=options.chunk_strategy)
    write_json(work / "chunks.json", [c.model_dump() for c in chunks])
    segments: list[Segment] = []
    warnings: list[str] = []
    language = options.language
    history = ""
    previous_end = 0
    if chunks and engine is None:
        from reelucid.engines import FasterWhisperEngine

        engine = FasterWhisperEngine(options.model_copy(update={"cpu_threads": threads}))
    stage("asr", completed_ms=0, total_ms=round(total / 16))
    for chunk in chunks:
        if cancelled():
            raise Cancelled()
        if chunk.core_start - previous_end > 5 * 16000:
            history = ""
        prompt = " ".join(
            x for x in (options.glossary, history[-100:] if options.context_prompt else "") if x
        )
        chunk_key = digest_json({"pipeline": key, "chunk": chunk.model_dump(), "prompt": prompt})
        cache = work / f"chunk-{chunk.index:06d}-{chunk_key}.json"
        expected_hash = (
            resume.get(str(cache))
            if resume is not None
            else (digest_file(cache) if cache.is_file() else None)
        )
        if expected_hash and cache.is_file() and digest_file(cache) == expected_hash:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            owned = [Segment.model_validate(s) for s in saved["segments"]]
            language = saved["language"]
            suspicious = saved["suspicious"]
        else:
            assert engine is not None
            samples = read_samples(wav, chunk.context_start, chunk.context_end)
            parts, detected, suspicious = engine.transcribe(samples, options.language, prompt)
            if suspicious:
                parts, detected, suspicious = engine.transcribe(samples, options.language, "", 0.2)
            owned = own_segments(parts, chunk)
            if suspicious:
                owned = [s.model_copy(update={"needs_review": True}) for s in owned]
            language = detected
            write_json(
                cache,
                {
                    "segments": [s.model_dump() for s in owned],
                    "language": language,
                    "suspicious": suspicious,
                },
            )
        progress(
            "chunk",
            {
                "index": chunk.index,
                "cache_key": chunk_key,
                "path": str(cache),
                "sha256": digest_file(cache),
                "chunk": chunk.model_dump(),
            },
        )
        segments.extend(owned)
        history = "" if suspicious else " ".join(s.text for s in owned if not s.needs_review)[-100:]
        previous_end = chunk.core_end
        progress(
            "progress",
            {
                "stage": "asr",
                "completed_ms": round(chunk.core_end / 16),
                "total_ms": round(total / 16),
            },
        )
    if not segments:
        warnings.append("no_speech")
    if any(s.needs_review for s in segments):
        warnings.append("needs_review")
    ordered = sorted(segments, key=lambda s: (s.start_ms, s.end_ms))
    ordered = [
        s.model_copy(update={"segment_id": f"seg_{i:06d}"}) for i, s in enumerate(ordered, 1)
    ]
    transcript = Transcript(
        version_id=f"tr_{key[:24]}",
        language=language,
        duration_ms=media.duration_ms,
        segments=ordered,
        warnings=warnings,
    )
    stage("export")
    exported = write_exports(transcript, output, options.formats)
    return {
        "schema_version": "1.0",
        "transcript": transcript.model_dump(),
        "media": media.model_dump(),
        "artifacts": exported,
        "warnings": warnings,
    }
