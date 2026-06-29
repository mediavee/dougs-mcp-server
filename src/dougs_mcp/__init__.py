import logging

from .server import mcp


def main() -> None:
    """Run the Dougs MCP server over stdio."""
    # Keep stderr quiet; httpx logs every request at INFO by default.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # show_banner=False keeps stdout clean for the JSON-RPC stream.
    mcp.run(transport="stdio", show_banner=False)


__all__ = ["main", "mcp"]
