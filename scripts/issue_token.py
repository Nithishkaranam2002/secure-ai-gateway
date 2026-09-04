"""Issue a signed token for local testing and demos.

Usage:
    python -m scripts.issue_token admin
    python -m scripts.issue_token viewer --subject agent-7 --minutes 120
"""

import argparse
from datetime import datetime, timedelta, timezone

import jwt

from src.core.config import settings


def issue(subject: str, role: str, minutes: int, tenant: str | None) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, object] = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    if tenant:
        claims["tenant"] = tenant
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue a gateway token")
    parser.add_argument("role", choices=["admin", "viewer"])
    parser.add_argument("--subject", default=None)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--tenant", default="tk_live_acme_9f2b")
    arguments = parser.parse_args()

    subject = arguments.subject or f"{arguments.role}-local"
    token = issue(subject, arguments.role, arguments.minutes, arguments.tenant)

    # This is a command line tool whose entire output is the token, so the token
    # goes to stdout. Nothing in this script runs inside the MCP server process.
    import sys

    sys.stdout.write(token + "\n")


if __name__ == "__main__":
    main()
