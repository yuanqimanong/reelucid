import pytest

from reelucid import ai
from reelucid.domain import JsonObject, ReelucidError, Segment, Transcript
from reelucid.settings import ModelBinding, Provider


@pytest.mark.parametrize(
    "reply,valid",
    [
        ('{"claims":[{"text":"30 seconds","refs":["S1"]}]}', True),
        ('{"claims":[{"text":"30 seconds","refs":["S999"]}]}', False),
    ],
)
async def test_evidence_is_assembled_from_verified_anchors(
    monkeypatch: pytest.MonkeyPatch, reply: str, valid: bool
) -> None:
    async def chat(
        _provider: Provider,
        _binding: ModelBinding,
        _secret: str | None,
        _messages: list[JsonObject],
    ) -> str:
        return reply

    monkeypatch.setattr(ai, "chat", chat)
    transcript = Transcript(
        version_id="tr_test",
        segments=[
            Segment(
                segment_id="seg_actual",
                start_ms=1200,
                end_ms=2400,
                text="The timeout is 30 seconds.",
            )
        ],
    )
    provider = Provider(id="local", base_url="http://localhost:1234/v1")
    binding = ModelBinding(provider_id="local", model="test")
    if valid:
        result = await ai.analyze(transcript, {"type": "summarize"}, provider, binding, None)
        assert result["evidence"][0]["segment_ids"] == ["seg_actual"]
        assert result["evidence"][0]["start_ms"] == 1200
        assert result["evidence"][0]["quotes"] == ["The timeout is 30 seconds."]
    else:
        with pytest.raises(ReelucidError, match="引用"):
            await ai.analyze(transcript, {"type": "summarize"}, provider, binding, None)
