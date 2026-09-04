"""Tests for bearer token authentication."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from src.core.config import settings
from src.mcp_gateway.auth import (
    PUBLIC_AUTH_MESSAGE,
    AuthError,
    authenticate,
    extract_bearer_token,
    verify_token,
)


def make_token(
    role: str = "viewer",
    subject: str = "agent-1",
    minutes: int = 60,
    secret: str | None = None,
    include_exp: bool = True,
    include_role: bool = True,
    algorithm: str = "HS256",
) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, object] = {"sub": subject, "iat": now}
    if include_role:
        claims["role"] = role
    if include_exp:
        claims["exp"] = now + timedelta(minutes=minutes)
    key = secret if secret is not None else settings.jwt_secret
    return jwt.encode(claims, key, algorithm=algorithm)


class TestHeaderParsing:
    def test_extracts_a_well_formed_header(self) -> None:
        assert extract_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"

    def test_scheme_is_case_insensitive(self) -> None:
        assert extract_bearer_token("bearer abc.def.ghi") == "abc.def.ghi"

    @pytest.mark.parametrize(
        "header",
        [
            None,                    # no header at all
            "",                      # empty header
            "Bearer",                # scheme with no token
            "abc.def.ghi",           # token with no scheme
            "Basic dXNlcjpwYXNz",    # wrong scheme
            "Bearer a b",            # too many parts
        ],
    )
    def test_rejects_malformed_headers(self, header: str | None) -> None:
        with pytest.raises(AuthError):
            extract_bearer_token(header)


class TestTokenVerification:
    def test_accepts_a_valid_viewer_token(self) -> None:
        principal = verify_token(make_token(role="viewer"))
        assert principal.role == "viewer"
        assert principal.is_admin is False

    def test_accepts_a_valid_admin_token(self) -> None:
        principal = verify_token(make_token(role="admin", subject="ops-2"))
        assert principal.is_admin is True
        assert principal.subject == "ops-2"

    def test_normalises_role_case(self) -> None:
        assert verify_token(make_token(role="ADMIN")).is_admin is True

    def test_rejects_a_token_signed_with_another_secret(self) -> None:
        with pytest.raises(AuthError):
            verify_token(make_token(secret="a-different-secret-entirely"))

    def test_rejects_an_expired_token(self) -> None:
        with pytest.raises(AuthError):
            verify_token(make_token(minutes=-5))

    def test_rejects_the_none_algorithm(self) -> None:
        """The classic JWT attack.

        A token declaring alg "none" is unsigned. Because verify_token pins the
        accepted algorithm list, the declared one is never honoured.
        """
        now = datetime.now(timezone.utc)
        unsigned = jwt.encode(
            {
                "sub": "attacker",
                "role": "admin",
                "iat": now,
                "exp": now + timedelta(minutes=60),
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(AuthError):
            verify_token(unsigned)

    def test_rejects_an_unrecognised_role(self) -> None:
        # An unknown role is refused, not quietly downgraded to viewer.
        with pytest.raises(AuthError):
            verify_token(make_token(role="superadmin"))

    def test_rejects_a_token_with_no_role(self) -> None:
        with pytest.raises(AuthError):
            verify_token(make_token(include_role=False))

    def test_rejects_a_token_with_no_expiry(self) -> None:
        with pytest.raises(AuthError):
            verify_token(make_token(include_exp=False))

    def test_rejects_rubbish(self) -> None:
        with pytest.raises(AuthError):
            verify_token("not-a-token-at-all")


class TestFailuresLookIdentical:
    @pytest.mark.parametrize(
        "header",
        [
            None,
            "Basic dXNlcjpwYXNz",
            "Bearer not-a-token",
            f"Bearer {make_token(minutes=-5)}",
            f"Bearer {make_token(secret='another-secret')}",
            f"Bearer {make_token(role='superadmin')}",
        ],
    )
    def test_every_failure_gives_the_same_public_message(
        self, header: str | None
    ) -> None:
        """A caller probing the gateway must not learn why it failed.

        The specific reason is kept for the log and the audit trail.
        """
        with pytest.raises(AuthError) as excinfo:
            authenticate(header)
        assert excinfo.value.public_message == PUBLIC_AUTH_MESSAGE

    def test_the_internal_reason_is_still_recorded(self) -> None:
        with pytest.raises(AuthError) as excinfo:
            authenticate(None)
        assert excinfo.value.reason
        assert excinfo.value.reason != excinfo.value.public_message
