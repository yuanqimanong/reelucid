import wave
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import av
import numpy as np
import pytest

from reelucid.engines import SileroOnnx
from reelucid.media import normalize, run_tool, sample_count, tool


def test_delayed_audio_track_keeps_container_zero(tmp_path: Path) -> None:
    source, output = tmp_path / "offset.mkv", tmp_path / "normalized.wav"
    run_tool(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=16x16:r=1:d=5",
            "-itsoffset",
            "2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=16000:duration=1",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        lambda: False,
    )
    normalize(source, output, lambda: False)
    assert abs(sample_count(output) - 48000) <= 2
    with wave.open(str(output)) as stream:
        assert set(stream.readframes(31000)) == {0}
        stream.setpos(33000)
        assert len(set(stream.readframes(1000))) > 1


def test_decode_fills_packet_gap_without_compressing_time(tmp_path: Path) -> None:
    source, output = tmp_path / "gap.mka", tmp_path / "gap.wav"
    # Explicit packet PTS avoids depending on the fixture generator's aselect filter.
    with av.open(str(source), "w", format="matroska") as native_container:
        # PyAV's packet generic and **kwargs are incomplete in its distributed stubs.
        container = cast(Any, native_container)
        stream = container.add_stream("pcm_s16le", rate=16000)
        samples = (np.sin(np.arange(16000) * 2 * np.pi / 16) * 4000).astype(np.int16)[None, :]
        for pts in (0, 48000):
            frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
            frame.sample_rate, frame.time_base, frame.pts = 16000, Fraction(1, 16000), pts
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    normalize(source, output, lambda: False)
    assert abs(sample_count(output) - 64000) < 160
    with wave.open(str(output)) as stream:
        stream.setpos(24000)
        assert set(stream.readframes(16000)) == {0}


@pytest.mark.model
def test_real_vad_rejects_silence(tmp_path: Path) -> None:
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 16000 * 10)
    assert SileroOnnx().detect(path, lambda: False) == []
