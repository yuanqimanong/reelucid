from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from reelucid.domain import JsonObject, Segment, SpeechRegion

type Progress = Callable[[str, JsonObject], None]
type Cancellation = Callable[[], bool]


class AsrEngine(Protocol):
    def transcribe(
        self, audio: object, language: str, prompt: str, temperature: float = 0
    ) -> tuple[list[Segment], str, bool]: ...


class VadEngine(Protocol):
    def detect(self, wav_path: Path, cancelled: Cancellation) -> list[SpeechRegion]: ...
