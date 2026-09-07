"""Fill the database with sample data so the tools have something real to work
against. Safe to run more than once."""

from datetime import datetime, timezone

from src.core.config import settings
from src.core.database import get_connection, initialise_database
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

NOW = datetime.now(timezone.utc).isoformat()

CUSTOMERS = [
    ("CUST-10001", "Amara Osei", "amara.osei@example.com", "pro", "active"),
    ("CUST-10002", "Ben Carter", "ben.carter@example.com", "starter", "active"),
    ("CUST-10003", "Chen Wei", "chen.wei@example.com", "enterprise", "active"),
    ("CUST-10004", "Diana Ruiz", "diana.ruiz@example.com", "pro", "suspended"),
    ("CUST-10005", "Ethan Blake", "ethan.blake@example.com", "starter", "closed"),
]

# The first two scale with the deployment. The third is deliberately small in
# every environment, because the rate limiting demo needs a budget that can be
# exhausted in a handful of requests.
TENANTS = [
    ("tk_live_acme_9f2b", "Acme Corp", settings.seed_token_limit),
    ("tk_live_globex_4d7a", "Globex", settings.seed_token_limit),
    ("tk_live_tiny_1c3e", "Tiny Startup", 2_000),
]


def seed() -> None:
    initialise_database()
    with get_connection() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO customers "
            "(customer_id, name, email, plan, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(*row, NOW) for row in CUSTOMERS],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO tenants "
            "(api_key, tenant_name, token_limit_per_minute, created_at) "
            "VALUES (?, ?, ?, ?)",
            [(*row, NOW) for row in TENANTS],
        )
    logger.info(
        "seeded %d customers and %d tenants into %s",
        len(CUSTOMERS),
        len(TENANTS),
        settings.database_path,
    )


if __name__ == "__main__":
    seed()
