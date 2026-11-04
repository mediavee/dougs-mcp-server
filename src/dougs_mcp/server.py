"""FastMCP server instance and the plumbing shared by the tool modules."""

from collections.abc import Sequence
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import DougsClient
from .config import load_settings

mcp = FastMCP("dougs")

# Hints clients use to decide which calls need a confirmation.
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def compact(value: Any) -> Any:
    """Drop None, empty strings and empty containers recursively: they cost tokens and say nothing."""
    if isinstance(value, dict):
        pruned = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def _lean_schema(schema: Any) -> Any:
    """Shorten a pydantic JSON schema without changing what it accepts in practice.

    An optional argument becomes its plain type (`anyOf [T, null]` with a null default),
    and `title` keys, which repeat the property names, go.
    """
    if isinstance(schema, list):
        return [_lean_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    # A string `title` is an annotation; a dict one is a property named "title".
    schema = {
        k: _lean_schema(v) for k, v in schema.items() if not (k == "title" and isinstance(v, str))
    }
    variants = schema.get("anyOf")
    if (
        isinstance(variants, list)
        and {"type": "null"} in variants
        and "default" in schema
        and schema["default"] is None
    ):
        rest = [v for v in variants if v != {"type": "null"}]
        if len(rest) == 1:
            extra = {k: v for k, v in schema.items() if k not in ("anyOf", "default")}
            return {**rest[0], **extra}
    return schema


class LeanTools(Transform):
    """Serve every tool with a lean input schema and no output schema.

    The definitions sit in the agent's context for the whole session; the output schemas
    are generic objects that add nothing but a duplicate structured copy of each answer.
    """

    @staticmethod
    def _lean(tool: Tool) -> Tool:
        return tool.model_copy(
            update={"parameters": _lean_schema(tool.parameters), "output_schema": None}
        )

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._lean(t) for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return self._lean(tool) if tool else None


mcp.add_transform(LeanTools())

_client: DougsClient | None = None


def get_client() -> DougsClient:
    global _client
    if _client is None:
        _client = DougsClient(load_settings())  # raises if DOUGS_EMAIL/DOUGS_PASSWORD missing
    return _client


@mcp.tool(annotations=READ)
async def get_file_url(
    path: Annotated[
        str,
        Field(description="A Dougs file path ('/files/...') taken from another tool's answer."),
    ],
) -> dict[str, str]:
    """Turn a Dougs file path into a signed download URL (public, short-lived)."""
    return {"url": await get_client().resolve_file_url(path)}


@mcp.tool(annotations=READ)
async def raw_get(
    path: Annotated[
        str, Field(description="API path, e.g. '/companies/12345/vat-configurations'.")
    ],
    params: Annotated[dict[str, Any] | None, Field(description="Query parameters.")] = None,
) -> Any:
    """Authenticated GET on any Dougs API path, raw JSON back: for what no tool covers yet."""
    if not path.startswith("/"):
        path = "/" + path
    return await get_client().get(path, params=params)
