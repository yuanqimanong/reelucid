from collections.abc import Sequence
from pathlib import Path

from reelucid.domain import JsonObject, ReelucidError, Transcript
from reelucid.files import write_text


def timestamp(milliseconds: int, *, web: bool = False) -> str:
    seconds, millis = divmod(max(0, milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{'.' if web else ','}{millis:03d}"


def export_text(transcript: Transcript, format_name: str) -> str:
    if format_name == "json":
        return transcript.model_dump_json(indent=2)
    if format_name in {"srt", "vtt"}:
        web = format_name == "vtt"
        entries: list[str] = []
        for index, segment in enumerate(transcript.segments, 1):
            text = segment.text.replace("-->", "→").strip()
            entries.append(
                f"{index}\n{timestamp(segment.start_ms, web=web)} --> {timestamp(segment.end_ms, web=web)}\n{text}\n"
            )
        return ("WEBVTT\n\n" if web else "") + "\n".join(entries)
    if format_name == "md":
        return (
            "# 转录文稿\n\n"
            + "\n\n".join(
                f"[{timestamp(s.start_ms, web=True)}] {s.text}" for s in transcript.segments
            )
            + "\n"
        )
    return "\n".join(s.text for s in transcript.segments) + "\n"


def write_exports(
    transcript: Transcript, directory: Path, formats: Sequence[str]
) -> list[JsonObject]:
    exported: list[JsonObject] = []
    for format_name in dict.fromkeys(["json", *formats]):
        if format_name not in {"json", "srt", "vtt", "txt", "md"}:
            raise ReelucidError("INVALID_ARTIFACT", "不支持此导出格式。")
        path = directory / f"transcript.{format_name}"
        write_text(path, export_text(transcript, format_name))
        kind = (
            "transcript"
            if format_name == "json"
            else "subtitle"
            if format_name in {"srt", "vtt"}
            else "document"
        )
        exported.append({"path": str(path), "format": format_name, "kind": kind})
    return exported
