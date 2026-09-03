import logging
import sys

from src.core.config import settings

_configured = False


def configure_logging(level: str | None = None) -> None:
    """Send every log record to stderr.

    stdout is reserved for JSON-RPC traffic in src/mcp_server, so nothing in
    this project may ever write log output there.
    """
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel((level or settings.log_level).upper())
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
