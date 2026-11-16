"""raw_get must not reach a mutating GET, and must not claim to be read-only."""

import ast
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastmcp import Client

from dougs_mcp import mcp
from dougs_mcp.server import raw_get

_ROOT = Path(__file__).resolve().parents[1]
_ACTION_NEEDLES = (
    "synchronize-new-providers",
    "can-finalize",
    "-assistant",
)


def _call_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{expr}")
        return "".join(parts)
    return None


def _action_get_templates() -> list[str]:
    """Every client.get argument in src whose path contains /actions/."""
    found: list[str] = []
    for path in sorted((_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "get" or not node.args:
                continue
            text = _call_text(node.args[0])
            if text is not None and "/actions/" in text:
                found.append(text)
    return found


def _concrete(template: str) -> str:
    path = template.replace("{expr}", "1")
    return path if path.startswith("/") else "/" + path


class _Recorder:
    def __init__(self) -> None:
        self.gets: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, str]:
        self.gets.append((path, params))
        return {"ok": path}


class RawGetTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_action_get_in_the_repo_stays_out_of_reach(self) -> None:
        templates = _action_get_templates()
        joined = "\n".join(templates)
        for needle in _ACTION_NEEDLES:
            self.assertIn(needle, joined, templates)
        self.assertTrue(any("/operations/" in item and "/actions/" in item for item in templates))

        paths = [_concrete(item) for item in templates]
        paths.extend(
            [
                "/companies/1/operations/2/actions/autocategorize-matching-operations",
                "/companies/1/operations/2/actions/auto-associate-matching-operations",
                "/companies/1/connections/3/actions/synchronize-new-providers",
                "/companies/1/actions/vat-assistant",
                "/companies/1/actions/is-assistant",
                "companies/1/actions/vat-assistant",
                "/companies/1/Actions/Vat-Assistant",
                "/companies/1/operations/2/actions%2Fautocategorize-matching-operations",
                "/companies/1/actions%252Fvat-assistant",
            ]
        )
        recorder = _Recorder()
        call = _as_awaitable(raw_get)
        with patch("dougs_mcp.server.get_client", return_value=recorder):
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        await call(path)
                    self.assertEqual(recorder.gets, [])

    async def test_ordinary_path_is_forwarded(self) -> None:
        recorder = _Recorder()
        call = _as_awaitable(raw_get)
        with patch("dougs_mcp.server.get_client", return_value=recorder):
            result = await call("companies/12345/vat-configurations", {"limit": 1})
            self.assertEqual(recorder.gets, [("/companies/12345/vat-configurations", {"limit": 1})])
            self.assertEqual(result, {"ok": "/companies/12345/vat-configurations"})
            # A path that merely contains the letters, not the /actions/ segment, stays open.
            await call("/companies/1/actions")
            await call("/companies/1/transactions")
        self.assertEqual(
            [item[0] for item in recorder.gets],
            [
                "/companies/12345/vat-configurations",
                "/companies/1/actions",
                "/companies/1/transactions",
            ],
        )

    async def test_raw_get_is_not_marked_read_only(self) -> None:
        async with Client(mcp) as client:
            tools = await client.list_tools()
        tool = next(item for item in tools if item.name == "raw_get")
        hints = tool.annotations
        self.assertIsNotNone(hints)
        assert hints is not None
        self.assertIsNot(hints.read_only_hint, True)
        self.assertIsNot(hints.idempotent_hint, True)


def _as_awaitable(fn: object) -> Callable[..., Awaitable[Any]]:
    if not callable(fn):
        raise AssertionError("raw_get is not callable")
    return fn
