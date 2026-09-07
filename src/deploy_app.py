"""Both gateways in one process, for single container deployment.

Locally the two services run separately, which is how they are meant to be
deployed: they scale differently and fail independently. A single container is
a deployment constraint, not a design preference, and it comes from the two
services sharing one SQLite file. Two containers would mean two disks and two
divergent copies of the usage and audit tables.

The apps themselves are untouched. The MCP gateway is mounted under /mcp-gateway
and the LLM gateway serves everything else, so the console and the completions
endpoints keep the paths they already have.
"""

import os

from fastapi import FastAPI

from src.core.database import initialise_database  # noqa: E402
from src.core.logging_setup import get_logger  # noqa: E402
from src.llm_gateway.app import app as llm_app  # noqa: E402
from src.mcp_gateway.app import app as mcp_app  # noqa: E402
from src.mcp_gateway.stdio_bridge import bridge  # noqa: E402

logger = get_logger(__name__)

# The LLM gateway calls the MCP gateway. In one process that would be a loopback
# to this same server, which would require knowing the port and mount path
# before anything is imported. The console calls the mounted app in memory
# instead, which this flag selects.
#
# Set before the app modules are imported below, because configuration is read
# at import time.
os.environ["SINGLE_PROCESS"] = "1"

app = FastAPI(
    title="Secure AI Gateway",
    description=(
        "MCP security gateway and LLM gateway in a single deployment. "
        "The demo site is at /console."
    ),
    version="1.0.0",
)


@app.on_event("startup")
async def startup() -> None:
    initialise_database()
    try:
        from scripts.seed_database import seed

        seed()
    except Exception:
        logger.exception("seeding failed at startup")
    try:
        await bridge.ensure_started()
    except Exception:
        logger.exception("downstream MCP server did not start at boot")


@app.on_event("shutdown")
async def shutdown() -> None:
    await bridge.stop()


# Mounted first so its routes take precedence over the LLM gateway's root mount.
app.mount("/mcp-gateway", mcp_app)
app.mount("/", llm_app)
