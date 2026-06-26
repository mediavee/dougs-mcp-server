import logging

from .server import mcp


def main() -> None:
    """Run the Dougs MCP server over stdio."""
    # Keep stderr quiet; httpx logs every request at INFO by default.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


__all__ = ["main", "mcp"]
