"""Native model adapters. Construct these only in the computing process."""

from __future__ import annotations

import importlib.resources
import wave
from pathlib import Path
from typing import Any, cast

from reelucid.domain import Cancelled, PipelineOptions, ReelucidError, Segment, SpeechRegion, Word
from reelucid.ports import Cancellation


class SileroOnnx:
    def __init__(self, model_path: str | None = None) -> None:
        import onnxruntime as ort  # pyright: ignore[reportMissingTypeStubs]

        cast(Any, ort).disable_telemetry_events()

        bundled = importlib.resources.files("faster_whisper").joinpath("assets/silero_vad_v6.onnx")
        path = Path(model_path) if model_path else Path(str(bundled))
        if not path.is_file():
            raise ReelucidError("VAD_MODEL_NOT_READY", "未找到锁定的 Silero VAD ONNX 模型。")
        options: Any = cast(Any, ort).SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session: Any = ort.InferenceSession(
            str(path), options, providers=["CPUExecutionProvider"]
        )
        if {x.name for x in self.session.get_inputs()} != {"input", "h", "c"}:
            raise ReelucidError(
                "VAD_ABI_MISMATCH", "VAD 模型接口不匹配，要求锁定的 v6 input/h/c 模型。"
            )

    def detect(self, wav_path: Path, cancelled: Cancellation) -> list[SpeechRegion]:
        import numpy as np

        h = np.zeros((1, 1, 128), dtype=np.float32)
        c = np.zeros_like(h)
        context = np.zeros(64, dtype=np.float32)
        regions: list[SpeechRegion] = []
        speech_start: int | None = None
        silence_start: int | None = None
        offset = 0
        with wave.open(str(wav_path)) as stream:
            total = stream.getnframes()
            while offset < total:
                if cancelled():
                    raise Cancelled()
                samples = (
                    np.frombuffer(stream.readframes(512), dtype=np.int16).astype(np.float32) / 32768
                )
                if len(samples) < 512:
                    samples = np.pad(samples, (0, 512 - len(samples)))
                batch = np.concatenate((context, samples))[None, :]
                prediction, h, c = self.session.run(None, {"input": batch, "h": h, "c": c})
                probability = float(prediction[0])
                context = samples[-64:]
                if probability >= 0.5:
                    if speech_start is None:
                        speech_start = max(0, offset - 2400)
                    silence_start = None
                elif probability < 0.35 and speech_start is not None:
                    if silence_start is None:
                        silence_start = offset
                    if offset - silence_start >= 6400:
                        end = min(total, silence_start + 2400)
                        if end - speech_start >= 4000:
                            regions.append(SpeechRegion(start=speech_start, end=end))
                        speech_start = None
                        silence_start = None
                offset += 512
            if speech_start is not None and total - speech_start >= 4000:
                regions.append(SpeechRegion(start=speech_start, end=total))
        return regions


class FasterWhisperEngine:
    def __init__(self, options: PipelineOptions) -> None:
        from faster_whisper import WhisperModel  # pyright: ignore[reportMissingTypeStubs]

        if not (Path(options.model_path) / "model.bin").is_file():
            raise ReelucidError("MODEL_NOT_READY", "本地 ASR 模型未安装，请先在设置中安装模型。")
        self.model: Any = WhisperModel(
            options.model_path,
            device=options.device,
            compute_type=options.compute_type,
            cpu_threads=options.cpu_threads,
            num_workers=1,
            local_files_only=True,
        )

    def transcribe(
        self, audio: object, language: str, prompt: str, temperature: float = 0
    ) -> tuple[list[Segment], str, bool]:
        prompt_tokens = self.model.hf_tokenizer.encode(prompt).ids[:128] if prompt else None
        raw, info = self.model.transcribe(
            cast(Any, audio),
            language=None if language == "auto" else language,
            word_timestamps=True,
            vad_filter=False,
            initial_prompt=prompt_tokens,
            condition_on_previous_text=False,
            temperature=temperature,
            beam_size=5 if temperature == 0 else 1,
        )
        result: list[Segment] = []
        suspicious = False
        for segment in raw:
            words: list[Word] = [
                Word(
                    start_ms=max(0, round(w.start * 1000)),
                    end_ms=max(0, round(w.end * 1000)),
                    text=w.word,
                )
                for w in cast(list[Any], segment.words or [])
            ]
            repeat = segment.compression_ratio > 2.4
            # Tiny/short utterances can echo a prompt with collapsed word timings.
            repeat = repeat or (
                len(words) >= 4 and sum(w.start_ms == w.end_ms for w in words) / len(words) > 0.3
            )
            tokens = segment.text.split()
            if len(tokens) >= 12:
                grams = [tuple(tokens[i : i + 4]) for i in range(len(tokens) - 3)]
                repeat = repeat or any(grams.count(g) >= 3 for g in set(grams))
            suspicious = suspicious or repeat
            if segment.text.strip():
                result.append(
                    Segment(
                        start_ms=max(0, round(segment.start * 1000)),
                        end_ms=max(0, round(segment.end * 1000)),
                        text=segment.text.strip(),
                        words=words,
                        needs_review=repeat,
                    )
                )
        return result, str(info.language), suspicious
