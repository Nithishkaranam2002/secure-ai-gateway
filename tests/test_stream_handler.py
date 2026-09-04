"""Tests for the SSE layer that joins the provider to the redactor."""

import json

import pytest

from src.llm_gateway.stream_handler import redacted_stream
from tests.fault_injection.providers import ScriptedProvider

REQUEST = {"messages": [{"role": "user", "content": "hello"}]}


async def collect(provider: ScriptedProvider) -> list[str]:
    return [line async for line in redacted_stream(provider, REQUEST, "test-actor", "resp-1")]


def text_of(lines: list[str]) -> str:
    """Reassemble the content the client would have displayed."""
    output = ""
    for line in lines:
        body = line.removeprefix("data: ").strip()
        if not body or body == "[DONE]":
            continue
        payload = json.loads(body)
        for choice in payload.get("choices", []):
            output += choice.get("delta", {}).get("content", "")
    return output


class TestSSEShape:
    async def test_every_line_is_a_data_event(self) -> None:
        lines = await collect(ScriptedProvider(chunks=["a", "b"]))
        assert all(line.startswith("data: ") for line in lines)
        assert all(line.endswith("\n\n") for line in lines)

    async def test_the_stream_terminates_with_done(self) -> None:
        """Clients wait for this marker. Without it they hang."""
        lines = await collect(ScriptedProvider(chunks=["a"]))
        assert lines[-1] == "data: [DONE]\n\n"

    async def test_a_finish_reason_is_sent_before_done(self) -> None:
        lines = await collect(ScriptedProvider(chunks=["a"]))
        payload = json.loads(lines[-2].removeprefix("data: "))
        assert payload["choices"][0]["finish_reason"] == "stop"

    async def test_the_model_name_is_reported(self) -> None:
        lines = await collect(ScriptedProvider(model="some-model", chunks=["a"]))
        payload = json.loads(lines[0].removeprefix("data: "))
        assert payload["model"] == "some-model"


class TestRedactionThroughTheStream:
    async def test_a_value_split_across_chunks_is_removed(self) -> None:
        provider = ScriptedProvider(chunks=["Write to nith", "ish@gm", "ail.com now"])
        assert "@" not in text_of(await collect(provider))

    async def test_clean_text_arrives_unchanged(self) -> None:
        provider = ScriptedProvider(chunks=["The report ", "is ready ", "for review."])
        assert text_of(await collect(provider)) == "The report is ready for review."

    async def test_nothing_is_lost_at_the_end_of_the_stream(self) -> None:
        """Without a flush, the tail held back for inspection is silently
        dropped and every answer loses its last few words."""
        provider = ScriptedProvider(chunks=["The final answer is fortytwo"])
        assert text_of(await collect(provider)) == "The final answer is fortytwo"

    async def test_a_card_number_is_removed_mid_stream(self) -> None:
        provider = ScriptedProvider(chunks=["Card 4111 ", "1111 1111 ", "1111 on file"])
        assert "4111" not in text_of(await collect(provider))


class TestMidStreamFailure:
    async def test_a_failure_after_the_first_chunk_is_reported_in_band(self) -> None:
        """Once a chunk has been sent the status line is already 200 and cannot
        be taken back, so the only honest way to report a failure is inside the
        stream."""

        class FailsPartWay(ScriptedProvider):
            async def stream(self, request):
                yield "The answer begins "
                raise RuntimeError("upstream connection reset at /opt/internal/x.py")

        lines = await collect(FailsPartWay(chunks=[]))
        blob = "".join(lines)
        assert "upstream_error" in blob
        assert lines[-1] == "data: [DONE]\n\n"

    async def test_the_in_band_error_leaks_nothing(self) -> None:
        class FailsPartWay(ScriptedProvider):
            async def stream(self, request):
                yield "The answer begins "
                raise RuntimeError("connection reset key sk-proj-secret at /opt/internal/x.py")

        blob = "".join(await collect(FailsPartWay(chunks=[])))
        assert "sk-proj-secret" not in blob
        assert "/opt/internal" not in blob

    async def test_unflushed_text_is_dropped_rather_than_released(self) -> None:
        """The tail held back has not been checked yet.

        Releasing it on the way out of an error path could emit half of the
        value the redactor was waiting to complete.
        """

        class FailsHoldingATail(ScriptedProvider):
            async def stream(self, request):
                yield "Contact nithish@gmail"
                raise RuntimeError("dropped")

        blob = "".join(await collect(FailsHoldingATail(chunks=[])))
        assert "nithish@gmail" not in blob
