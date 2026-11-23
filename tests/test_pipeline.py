import importlib.metadata
import json
import wave
from pathlib import Path

from reelucid.domain import JsonObject, PipelineOptions, Segment, SpeechRegion, Word
from reelucid.files import digest_file, digest_json
from reelucid.pipeline import run_pipeline
from reelucid.ports import Cancellation


class SpeechVad:
    def detect(self, wav_path: Path, cancelled: Cancellation) -> list[SpeechRegion]:
        return [SpeechRegion(start=0, end=16000)]


class RepeatingEngine:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def transcribe(
        self, audio: object, language: str, prompt: str, temperature: float = 0
    ) -> tuple[list[Segment], str, bool]:
        self.prompts.append(prompt)
        return (
            [
                Segment(
                    start_ms=100,
                    end_ms=800,
                    text="confirmed",
                    words=[Word(start_ms=100, end_ms=800, text="confirmed")],
                )
            ],
            "en",
            temperature == 0,
        )


def test_prompt_retry_and_only_committed_checkpoints_are_reused(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 16000)
    engine = RepeatingEngine()
    options = PipelineOptions(model_path=str(tmp_path / "model"), glossary="terms")
    saved: dict[str, str] = {}

    def progress(kind: str, payload: JsonObject) -> None:
        if kind == "chunk":
            saved[payload["path"]] = payload["sha256"]

    result = run_pipeline(
        source,
        tmp_path / "out",
        options,
        progress,
        engine=engine,
        vad=SpeechVad(),
        resume={},
        cpu_threads=1,
    )
    assert engine.prompts == ["terms", ""]
    assert result["transcript"]["segments"][0]["text"] == "confirmed"
    legacy_key = digest_json(
        {
            "source": digest_file(source),
            "options": options.model_dump(exclude={"chunk_strategy"}),
            "pipeline": 2,
            "model": {},
            "runtimes": {
                name: importlib.metadata.version(name)
                for name in ("faster-whisper", "onnxruntime", "ctranslate2")
            },
        }
    )
    assert Path(next(iter(saved))).parent.name == legacy_key
    run_pipeline(
        source,
        tmp_path / "out",
        options,
        progress,
        engine=engine,
        vad=SpeechVad(),
        resume=saved,
        cpu_threads=2,
    )
    assert len(engine.prompts) == 2
    # An empty resume map simulates a crash before the checkpoint was registered.
    run_pipeline(
        source, tmp_path / "out", options, progress, engine=engine, vad=SpeechVad(), resume={}
    )
    assert len(engine.prompts) == 4
    checkpoint = Path(next(iter(saved)))
    checkpoint.write_text(json.dumps({"segments": []}), encoding="utf-8")
    run_pipeline(source, tmp_path / "out", options, engine=engine, vad=SpeechVad(), resume=saved)
    assert len(engine.prompts) == 6
