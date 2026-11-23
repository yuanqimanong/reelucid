from reelucid.domain import (
    Chunk,
    Segment,
    SpeechRegion,
    Transcript,
    Word,
    choose_chunk_strategy,
    make_chunks,
    own_segments,
)
from reelucid.exports import export_text, timestamp


def test_automatic_chunk_policy_applies_to_explicit_english_small() -> None:
    assert choose_chunk_strategy("auto", "en", "small") == "adjacent"
    assert choose_chunk_strategy("auto", "zh", "small") == "silence"
    assert choose_chunk_strategy("auto", "auto", "small") == "silence"
    assert choose_chunk_strategy("auto", "en", "tiny") == "silence"
    assert choose_chunk_strategy("adjacent", "zh", "small") == "adjacent"
    assert choose_chunk_strategy("silence", "en", "small") == "silence"


def test_silence_cuts_have_no_borrowed_context() -> None:
    chunks = make_chunks(
        [SpeechRegion(start=16000, end=32000), SpeechRegion(start=48000, end=64000)], 80000
    )
    assert [(c.context_start, c.context_end) for c in chunks] == [(16000, 32000), (48000, 64000)]
    assert all(c.cut_kind == "silence" for c in chunks)


def test_adjacent_speech_keeps_pause_on_original_timeline_and_limits_window() -> None:
    regions = [
        SpeechRegion(start=16000, end=48000),
        SpeechRegion(start=57600, end=64000),
        SpeechRegion(start=96000, end=224000),
        SpeechRegion(start=240000, end=432000),
    ]
    chunks = make_chunks(regions, 480000, strategy="adjacent")
    assert [(c.context_start, c.context_end) for c in chunks] == [
        (16000, 64000),
        (96000, 224000),
        (240000, 432000),
    ]
    assert all(c.cut_kind == "silence" and c.core_start == c.context_start for c in chunks)
    after_pause = Segment(start_ms=2700, end_ms=2800, text="next")
    assert own_segments([after_pause], chunks[0])[0].start_ms == 3700


def test_hard_cut_core_ownership_does_not_duplicate_boundary_word() -> None:
    first, second = make_chunks(
        [SpeechRegion(start=0, end=40 * 16000)], 40 * 16000, strategy="adjacent"
    )
    assert first.context_end == 30 * 16000 + 8000
    assert second.context_start == 30 * 16000 - 8000
    left = Segment(
        start_ms=29900,
        end_ms=30100,
        text="boundary",
        words=[Word(start_ms=29900, end_ms=30100, text="boundary")],
    )
    right = left.model_copy(update={"words": [Word(start_ms=400, end_ms=600, text="boundary")]})
    assert own_segments([left], first) == []
    assert own_segments([right], second)[0].text == "boundary"


def test_context_offset_is_in_source_milliseconds() -> None:
    chunk = Chunk(
        index=1,
        core_start=160000,
        core_end=320000,
        context_start=152000,
        context_end=320000,
        cut_kind="hard",
    )
    result = own_segments(
        [
            Segment(
                start_ms=1000,
                end_ms=1500,
                text="word",
                words=[Word(start_ms=1000, end_ms=1500, text="word")],
            )
        ],
        chunk,
    )
    assert (result[0].start_ms, result[0].end_ms) == (10500, 11000)


def test_subtitles_keep_milliseconds_past_one_hour() -> None:
    transcript = Transcript(
        version_id="tr_test", segments=[Segment(start_ms=3661123, end_ms=3662789, text="a --> b")]
    )
    assert "01:01:01,123 --> 01:01:02,789" in export_text(transcript, "srt")
    assert "a → b" in export_text(transcript, "srt")
    assert export_text(transcript, "vtt").startswith("WEBVTT\n\n")
    assert timestamp(3661123, web=True) == "01:01:01.123"
