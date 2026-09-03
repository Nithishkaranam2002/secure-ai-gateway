"""Shared test setup.

The environment is redirected to a throwaway database before anything from src
is imported, because src.core.config reads its settings at import time. Nothing
in the test suite may touch the real data/gateway.db.
"""

import os
import tempfile
from pathlib import Path

TEST_DIR = Path(tempfile.mkdtemp(prefix="secure-ai-gateway-tests-"))

os.environ["DATABASE_PATH"] = str(TEST_DIR / "test_gateway.db")
os.environ["JWT_SECRET"] = "test-secret-not-used-in-production"
os.environ["LOG_LEVEL"] = "WARNING"

import pytest  # noqa: E402

from scripts.seed_database import seed  # noqa: E402
from src.core.database import initialise_database  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def prepared_database() -> None:
    initialise_database()
    seed()
