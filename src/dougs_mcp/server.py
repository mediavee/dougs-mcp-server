"""FastMCP server instance and the plumbing shared by the tool modules."""

from typing import Annotated, Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import DougsClient
from .config import load_settings

mcp = FastMCP("dougs")

# Hints clients use to decide which calls need a confirmation.
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

CompanyId = Annotated[
    int | None, Field(description="Company id; defaults to the account's preferred company.")
]


def compact(value: Any) -> Any:
    """Drop None, empty strings and empty containers recursively: they cost tokens and say nothing."""
    if isinstance(value, dict):
        pruned = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


_client: DougsClient | None = None


def get_client() -> DougsClient:
    global _client
    if _client is None:
        _client = DougsClient(load_settings())  # raises if DOUGS_EMAIL/DOUGS_PASSWORD missing
    return _client


@mcp.tool()
async def get_file_url(path: str) -> dict[str, str]:
    """Resolve a Dougs file path to a directly-downloadable URL.

    Pass a path like '/files/...' taken from an operation's
    sourceDocumentAttachments[].sourceDocument.file.url, or a sales/vendor
    invoice filePath. Returns the signed S3 URL (publicly downloadable, expires).
    """
    url = await get_client().resolve_file_url(path)
    return {"url": url}


@mcp.tool()
async def raw_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Low-level authenticated GET on the Dougs API (read-only).

    Use for endpoints not yet wrapped, e.g. '/companies/12345/invoices'.
    Path is relative to the API base URL.
    """
    if not path.startswith("/"):
        path = "/" + path
    return await get_client().get(path, params=params)
