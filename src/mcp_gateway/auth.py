"""Bearer token authentication for the MCP gateway.

Nothing here imports a web framework. Identity is a rule, not a routing
concern, so this module takes a header string and returns a principal, and can
be tested with no server running.
"""

from dataclasses import dataclass

import jwt

from src.core.config import settings
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

ALGORITHM = "HS256"
VALID_ROLES = frozenset({"admin", "viewer"})

# Every authentication failure returns this one sentence. A message that names
# the specific problem helps a caller correct their token, and an attacker
# probing the gateway is also a caller.
PUBLIC_AUTH_MESSAGE = "Invalid or missing credentials."


@dataclass(frozen=True)
class Principal:
    """Who is making the request, once the token has been verified."""

    subject: str
    role: str
    tenant: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthError(Exception):
    """Authentication failed.

    `reason` is for the log and the audit trail. It never reaches the caller.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.public_message = PUBLIC_AUTH_MESSAGE


def extract_bearer_token(authorization_header: str | None) -> str:
    if not authorization_header:
        raise AuthError("authorization header absent")

    parts = authorization_header.split()
    if len(parts) != 2:
        raise AuthError("authorization header is not two space separated parts")

    scheme, token = parts
    if scheme.lower() != "bearer":
        raise AuthError(f"unsupported authorization scheme: {scheme}")
    if not token.strip():
        raise AuthError("bearer token is empty")

    return token


def verify_token(token: str) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            # Pinned deliberately. Accepting the algorithm named inside the
            # token lets an attacker present one signed with "none".
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub", "role"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidSignatureError as exc:
        raise AuthError("token signature does not verify") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise AuthError(f"token missing required claim: {exc.claim}") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"token rejected: {type(exc).__name__}") from exc

    role = claims.get("role")
    if not isinstance(role, str):
        raise AuthError("role claim is not a string")

    normalised_role = role.strip().lower()
    if normalised_role not in VALID_ROLES:
        # An unrecognised role is refused rather than treated as the least
        # privileged one. A typo in a role name must not become a silent grant.
        raise AuthError(f"unrecognised role: {role!r}")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthError("sub claim is not a usable string")

    tenant = claims.get("tenant")
    if tenant is not None and not isinstance(tenant, str):
        raise AuthError("tenant claim is not a string")

    return Principal(subject=subject, role=normalised_role, tenant=tenant)


def authenticate(authorization_header: str | None) -> Principal:
    """Header in, verified principal out. Raises AuthError on any failure."""
    token = extract_bearer_token(authorization_header)
    principal = verify_token(token)
    logger.info(
        "authenticated subject=%s role=%s", principal.subject, principal.role
    )
    return principal
