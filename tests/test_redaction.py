"""Tests for the streaming redaction engine.

The central claim being tested is that the result does not depend on how the
text was chopped up on its way in. A value split across chunk boundaries must
be caught exactly as if it had arrived whole.
"""

import pytest

from src.llm_gateway.redaction import (
    MAX_HOLDBACK,
    REDACTION_PLACEHOLDER,
    StreamRedactor,
    luhn_check,
    redact_text,
)

VALID_CARDS = [
    "4111111111111111",
    "5500005555555559",
    "378282246310005",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
]

INVALID_DIGIT_RUNS = [
    "1234567890123456",   # fails the checksum
    "1111111111111111",   # fails the checksum
    "12345678901",        # too short to be a card
]


def stream(text: str, size: int) -> str:
    """Feed text through the redactor in fixed size pieces."""
    redactor = StreamRedactor()
    parts = [text[i : i + size] for i in range(0, len(text), size)]
    return "".join(redactor.feed(part) for part in parts) + redactor.flush()


class TestLuhn:
    @pytest.mark.parametrize("number", ["4111111111111111", "5500005555555559"])
    def test_accepts_real_card_numbers(self, number: str) -> None:
        assert luhn_check(number) is True

    @pytest.mark.parametrize("number", ["1234567890123456", "4111111111111112"])
    def test_rejects_numbers_that_fail_the_checksum(self, number: str) -> None:
        assert luhn_check(number) is False

    @pytest.mark.parametrize("number", ["123", "12345678901", "1" * 20, "abcd", ""])
    def test_rejects_anything_the_wrong_shape(self, number: str) -> None:
        assert luhn_check(number) is False


class TestWholeStringRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "Write to nithish@gmail.com today",
            "Two addresses: a@b.co and c.d+tag@example.org here",
            "Contact first.last@sub.domain.co.uk please",
        ],
    )
    def test_emails_are_removed(self, text: str) -> None:
        result, counts = redact_text(text)
        assert "@" not in result
        assert REDACTION_PLACEHOLDER in result
        assert counts.email >= 1

    def test_ssn_is_removed(self) -> None:
        result, counts = redact_text("His SSN is 123-45-6789 on file.")
        assert "123-45-6789" not in result
        assert counts.ssn == 1

    @pytest.mark.parametrize("card", VALID_CARDS)
    def test_valid_cards_are_removed(self, card: str) -> None:
        result, counts = redact_text(f"Card on file: {card} expires soon")
        assert card not in result
        assert counts.credit_card == 1

    @pytest.mark.parametrize("number", INVALID_DIGIT_RUNS)
    def test_digit_runs_that_are_not_cards_are_left_alone(self, number: str) -> None:
        """False positives damage ordinary answers.

        An order or tracking number that happens to be long must survive, which
        is what the Luhn check buys us.
        """
        text = f"Order {number} has shipped"
        result, counts = redact_text(text)
        assert result == text
        assert counts.credit_card == 0

    @pytest.mark.parametrize(
        "text",
        [
            "The quick brown fox jumps over the lazy dog.",
            "Prices are 49.99 and 1200 for the year 2026.",
            "Email me later, ok? Meeting at 10:30 on 12-05-2026.",
            "",
            "   ",
            "One\ntwo\nthree\n",
        ],
    )
    def test_clean_text_passes_through_byte_for_byte(self, text: str) -> None:
        result, counts = redact_text(text)
        assert result == text
        assert counts.total == 0


class TestChunkBoundaries:
    """The reason this class exists is the whole reason the task is hard."""

    SENTENCE = (
        "Please contact nithish.karanam@example.com about the charge on card "
        "4111 1111 1111 1111, reference SSN 123-45-6789, before Friday."
    )

    def test_character_by_character_matches_whole_string(self) -> None:
        assert stream(self.SENTENCE, 1) == redact_text(self.SENTENCE)[0]

    @pytest.mark.parametrize("size", range(1, 40))
    def test_the_result_is_the_same_at_every_chunk_size(self, size: int) -> None:
        """Stronger than testing a few hand picked splits.

        If any chunk size produced a different answer, the redactor would be
        depending on how the provider happened to slice the response, which is
        not something a gateway may depend on.
        """
        expected = redact_text(self.SENTENCE)[0]
        assert stream(self.SENTENCE, size) == expected

    @pytest.mark.parametrize("size", range(1, 20))
    def test_no_sensitive_value_survives_any_split(self, size: int) -> None:
        result = stream(self.SENTENCE, size)
        assert "@" not in result
        assert "4111" not in result
        assert "123-45-6789" not in result

    def test_the_awkward_split_that_broke_the_first_version(self) -> None:
        """The .co / m case.

        An address ending .co matches the email pattern before its final m has
        arrived. Matching the held tail rather than only the released text left
        that m stranded after the placeholder.
        """
        redactor = StreamRedactor()
        emitted = "".join(
            redactor.feed(part)
            for part in ["Reach me at nith", "ish@gm", "ail.co", "m anytime."]
        )
        emitted += redactor.flush()
        assert emitted == f"Reach me at {REDACTION_PLACEHOLDER} anytime."

    def test_a_card_split_mid_number_is_still_caught(self) -> None:
        redactor = StreamRedactor()
        emitted = "".join(
            redactor.feed(part)
            for part in ["Card 4111 ", "1111 11", "11 1111 on file"]
        )
        emitted += redactor.flush()
        assert "4111" not in emitted
        assert REDACTION_PLACEHOLDER in emitted


class TestFlush:
    def test_flush_releases_a_trailing_tail(self) -> None:
        """Without flush, the last words of every answer vanish silently."""
        redactor = StreamRedactor()
        emitted = redactor.feed("The final word is truncated")
        emitted += redactor.flush()
        assert emitted == "The final word is truncated"

    def test_a_value_at_the_very_end_is_still_redacted(self) -> None:
        redactor = StreamRedactor()
        emitted = redactor.feed("My address is nithish@gmail.com")
        emitted += redactor.flush()
        assert "@" not in emitted
        assert emitted.endswith(REDACTION_PLACEHOLDER)

    def test_flush_on_an_empty_stream_returns_nothing(self) -> None:
        assert StreamRedactor().flush() == ""


class TestMemoryBehaviour:
    def test_the_buffer_does_not_grow_with_the_response(self) -> None:
        """The requirement the brief states twice.

        Buffering the whole response would pass every correctness test above
        and fail the task. The buffer must stay bounded no matter how long the
        answer runs.
        """
        redactor = StreamRedactor()
        for _ in range(2000):
            redactor.feed("This is an ordinary sentence with nothing private. ")
            assert redactor.buffered_characters <= MAX_HOLDBACK

    def test_a_long_unbroken_run_is_still_capped(self) -> None:
        """A stream with no break characters must not buffer without limit."""
        redactor = StreamRedactor()
        for _ in range(500):
            redactor.feed("abcdefghijklmnopqrstuvwxyz")
            assert redactor.buffered_characters <= MAX_HOLDBACK

    def test_most_text_is_released_immediately(self) -> None:
        """Latency, not just memory.

        Ordinary prose ends chunks at spaces and full stops, so almost all of
        it must go straight out rather than waiting for the next chunk.
        """
        redactor = StreamRedactor()
        emitted = redactor.feed("The report is ready for your review today. ")
        assert len(emitted) >= 40
        assert redactor.buffered_characters == 0


class TestCounting:
    def test_counts_each_kind_separately(self) -> None:
        text = (
            "a@b.com and c@d.org, SSN 123-45-6789, card 4111 1111 1111 1111"
        )
        _, counts = redact_text(text)
        assert counts.email == 2
        assert counts.ssn == 1
        assert counts.credit_card == 1
        assert counts.total == 4

    def test_counts_survive_being_streamed(self) -> None:
        text = "a@b.com and SSN 123-45-6789 here"
        redactor = StreamRedactor()
        for character in text:
            redactor.feed(character)
        redactor.flush()
        assert redactor.counts.email == 1
        assert redactor.counts.ssn == 1
