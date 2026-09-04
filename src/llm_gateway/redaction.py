"""Streaming redaction of sensitive values.

The problem this solves is that a model streams its answer in small pieces, and
a value like an email address is routinely split across two of them. Checking
each piece on its own finds nothing, and the address reaches the user intact.

Waiting for the whole answer would find it, at the cost of the user staring at
a blank screen and the gateway holding every in flight response in memory. So
this class does neither. It keeps a short tail of text that might still be the
start of something sensitive, and releases everything before that immediately.

Nothing here knows about HTTP or SSE. It takes text and returns text, so it can
be tested with no network involved.
"""

import re
import string
from dataclasses import dataclass, field

REDACTION_PLACEHOLDER = "[REDACTED]"

# Ceiling on how much text may be held back. Without it, a stream containing no
# break characters would buffer without limit, which is the memory problem this
# design exists to avoid. Comfortably longer than any pattern below.
MAX_HOLDBACK = 48

EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)

# The dashed form only. Nine bare digits are just as likely to be an order
# number, and redacting those would damage ordinary answers. Recorded in
# docs/decisions.md.
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# A candidate card number: 13 to 19 digits, optionally grouped by spaces or
# dashes. Every candidate is confirmed with the Luhn check before it is touched.
CARD_CANDIDATE_PATTERN = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# Characters that can appear inside one of the patterns above. A character
# outside this set is a point where no match can span, and therefore a safe
# place to cut the buffer.
TOKEN_CHARACTERS = frozenset(string.ascii_letters + string.digits + "@._%+-")


def luhn_check(digits: str) -> bool:
    """The checksum every real card number satisfies.

    Confirming a candidate before redacting it is what stops a long invoice or
    tracking number from being blanked out of an otherwise correct answer.
    """
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, character in enumerate(reversed(digits)):
        value = int(character)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _redact_card(match: re.Match[str]) -> str:
    candidate = match.group(0)
    digits = re.sub(r"[^\d]", "", candidate)
    if luhn_check(digits):
        return REDACTION_PLACEHOLDER
    return candidate


@dataclass
class RedactionCounts:
    """How many of each kind were removed. Never the values themselves."""

    email: int = 0
    ssn: int = 0
    credit_card: int = 0

    @property
    def total(self) -> int:
        return self.email + self.ssn + self.credit_card

    def as_dict(self) -> dict[str, int]:
        return {
            "email": self.email,
            "ssn": self.ssn,
            "credit_card": self.credit_card,
            "total": self.total,
        }


@dataclass
class StreamRedactor:
    """Feed text in, get safe text out. Call flush when the stream ends."""

    _buffer: str = ""
    counts: RedactionCounts = field(default_factory=RedactionCounts)

    def _apply_patterns(self, text: str) -> str:
        text, email_hits = EMAIL_PATTERN.subn(REDACTION_PLACEHOLDER, text)
        self.counts.email += email_hits

        text, ssn_hits = SSN_PATTERN.subn(REDACTION_PLACEHOLDER, text)
        self.counts.ssn += ssn_hits

        confirmed = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal confirmed
            result = _redact_card(match)
            if result == REDACTION_PLACEHOLDER:
                confirmed += 1
            return result

        text = CARD_CANDIDATE_PATTERN.sub(replace, text)
        self.counts.credit_card += confirmed

        return text

    def _holdback_length(self, text: str) -> int:
        """How many characters at the end might still be part of a match.

        Walks backwards until it reaches a character that cannot appear inside
        any pattern. In ordinary prose that is the very first step, so nothing
        is held and the text goes straight out.
        """
        limit = min(len(text), MAX_HOLDBACK)
        held = 0
        while held < limit:
            position = len(text) - held - 1
            character = text[position]

            if character in TOKEN_CHARACTERS:
                held += 1
                continue

            # A space is only risky when it might be grouping a card number,
            # which means the character before it is a digit. After a letter a
            # space is a clean break, so ordinary text is never delayed.
            if character == " " and position > 0 and text[position - 1].isdigit():
                held += 1
                continue

            break
        return held

    def feed(self, text: str) -> str:
        """Take the next piece of the stream and return what is safe to emit.

        The order here is the whole correctness argument. What to hold is
        decided first, on raw text, and only the released part is matched
        against. Matching the held tail as well would fire a pattern on a value
        that has not finished arriving: an address ending .co matches the email
        pattern before its final m has been received, and that m then survives
        as a leftover character after the placeholder.
        """
        if not text:
            return ""

        self._buffer += text

        held = self._holdback_length(self._buffer)
        split_at = len(self._buffer) - held
        safe = self._buffer[:split_at]
        self._buffer = self._buffer[split_at:]

        if not safe:
            return ""
        return self._apply_patterns(safe)

    def flush(self) -> str:
        """Release the tail at end of stream, after one last check.

        Without this the final words of every response would be silently lost.
        """
        if not self._buffer:
            return ""
        remaining = self._apply_patterns(self._buffer)
        self._buffer = ""
        return remaining

    @property
    def buffered_characters(self) -> int:
        """Exposed so tests can assert the buffer never grows with the response."""
        return len(self._buffer)


def redact_text(text: str) -> tuple[str, RedactionCounts]:
    """Redact a complete string in one go. For non streaming use and tests."""
    redactor = StreamRedactor()
    result = redactor.feed(text) + redactor.flush()
    return result, redactor.counts
