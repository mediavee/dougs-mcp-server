"""split_operation must refuse an unbalanced sum and must not keep a stale VAT."""

import copy
import unittest
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

from fastmcp import Client

from dougs_mcp import mcp
from dougs_mcp.operations import SplitLine, split_operation


def _operation(
    *,
    amount: float = 120.0,
    inbound: bool = False,
    main_amount: float = 120.0,
    fee: float | None = 1.5,
) -> dict[str, Any]:
    breakdowns: list[dict[str, Any]] = [
        {
            "id": 10,
            "amount": main_amount,
            "isInbound": inbound,
            "categoryId": 50,
            "categoryWording": "Old",
            "vatAmount": 20.0,
            "manualVatAmount": 5.0,
            "section": "main",
        }
    ]
    if fee is not None:
        breakdowns.append(
            {
                "id": 11,
                "amount": fee,
                "isInbound": False,
                "isFee": True,
                "vatAmount": 0.25,
                "section": "main",
            }
        )
    breakdowns.append(
        {
            "id": 12,
            "amount": amount,
            "isInbound": not inbound,
            "isCounterpart": True,
            "section": "counterpart",
        }
    )
    return {
        "id": 7,
        "amount": amount,
        "isInbound": inbound,
        "allowAddingBreakdown": True,
        "type": "bank",
        "breakdowns": breakdowns,
    }


class _FakeClient:
    def __init__(self, operation: dict[str, Any]) -> None:
        self._operation = operation
        self.posts: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    async def resolve_company_id(self) -> int:
        return 1

    async def operation(self, company_id: int, operation_id: int) -> dict[str, Any]:
        cloned = copy.deepcopy(self._operation)
        assert isinstance(cloned, dict)
        return cloned

    async def category(self, company_id: int, category_id: int) -> dict[str, Any]:
        return {"id": category_id, "wording": f"Category {category_id}"}

    async def update_operation(
        self,
        company_id: int,
        operation: dict[str, Any],
        updated_breakdown: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        posted = copy.deepcopy(operation)
        updated = copy.deepcopy(updated_breakdown)
        assert isinstance(posted, dict)
        assert updated is None or isinstance(updated, dict)
        self.posts.append((posted, updated))
        return operation


def _call() -> Callable[..., Awaitable[Any]]:
    if not callable(split_operation):
        raise AssertionError("split_operation is not callable")
    return split_operation


class SplitOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._patch = patch("dougs_mcp.operations.get_client")
        self.get_client = self._patch.start()
        self.addCleanup(self._patch.stop)

    def _use(self, operation: dict[str, Any]) -> _FakeClient:
        fake = _FakeClient(operation)
        self.get_client.return_value = fake
        return fake

    async def test_unbalanced_lines_do_not_post(self) -> None:
        fake = self._use(_operation())
        with self.assertRaises(ValueError) as raised:
            await _call()(
                7,
                [
                    SplitLine(amount=70, category_id=100, breakdown_id=10),
                    SplitLine(amount=40, category_id=200),
                ],
            )
        self.assertEqual(fake.posts, [])
        self.assertIn("-111.50", str(raised.exception))
        self.assertIn("-120.00", str(raised.exception))

    async def test_fee_is_part_of_the_signed_sum(self) -> None:
        # 70 + 50 matches the operation only when the kept 1.50 fee is ignored.
        fake = self._use(_operation())
        with self.assertRaises(ValueError) as raised:
            await _call()(
                7,
                [
                    SplitLine(amount=70, category_id=100, breakdown_id=10),
                    SplitLine(amount=50, category_id=200),
                ],
            )
        self.assertEqual(fake.posts, [])
        self.assertIn("-121.50", str(raised.exception))
        self.assertIn("-120.00", str(raised.exception))

    async def test_opposite_sign_is_not_the_operation_amount(self) -> None:
        fake = self._use(_operation(fee=None))
        with self.assertRaises(ValueError) as raised:
            await _call()(7, [SplitLine(amount=120, is_inbound=True, breakdown_id=10)])
        self.assertEqual(fake.posts, [])
        self.assertIn("signed sum of the lines is 120.00", str(raised.exception))
        self.assertIn("operation amount is -120.00", str(raised.exception))

    async def test_two_cents_off_is_refused(self) -> None:
        fake = self._use(_operation(fee=None))
        with self.assertRaises(ValueError):
            await _call()(
                7,
                [
                    SplitLine(amount=70, breakdown_id=10),
                    SplitLine(amount=50.02),
                ],
            )
        self.assertEqual(fake.posts, [])

    async def test_one_cent_is_accepted(self) -> None:
        fake = self._use(_operation(fee=None))
        await _call()(
            7,
            [
                SplitLine(amount=70, breakdown_id=10),
                SplitLine(amount=50.01),
            ],
        )
        self.assertEqual(len(fake.posts), 1)

    async def test_partial_refund_signs_can_balance(self) -> None:
        fake = self._use(_operation(amount=100, main_amount=100, fee=None))
        await _call()(
            7,
            [
                SplitLine(amount=130, is_inbound=False),
                SplitLine(amount=30, is_inbound=True),
            ],
        )
        self.assertEqual(len(fake.posts), 1)
        posted, updated = fake.posts[0]
        self.assertIsInstance(updated, dict)
        self.assertNotIsInstance(updated, list)
        self.assertTrue(any(line.get("isCounterpart") for line in posted["breakdowns"]))

    async def test_changed_line_drops_carried_vat(self) -> None:
        # 70 + 48.50 + the kept 1.50 fee = 120. The reused line changes amount and category.
        fake = self._use(_operation())
        await _call()(
            7,
            [
                SplitLine(amount=70, category_id=100, breakdown_id=10),
                SplitLine(amount=48.50, category_id=200),
            ],
        )
        self.assertEqual(len(fake.posts), 1)
        posted, updated = fake.posts[0]
        reused = next(line for line in posted["breakdowns"] if line.get("id") == 10)
        self.assertEqual(reused.get("amount"), 70)
        self.assertEqual(reused.get("categoryId"), 100)
        self.assertNotIn("vatAmount", reused)
        self.assertNotIn("manualVatAmount", reused)
        fresh = next(line for line in posted["breakdowns"] if line.get("id") is None)
        self.assertEqual(fresh.get("amount"), 48.5)
        self.assertEqual(fresh.get("categoryId"), 200)
        self.assertNotIn("vatAmount", fresh)
        self.assertNotIn("manualVatAmount", fresh)
        fee = next(line for line in posted["breakdowns"] if line.get("isFee"))
        self.assertEqual(fee.get("amount"), 1.5)
        self.assertEqual(fee.get("vatAmount"), 0.25)
        self.assertTrue(any(line.get("isCounterpart") for line in posted["breakdowns"]))
        self.assertIsInstance(updated, dict)
        assert isinstance(updated, dict)
        self.assertEqual(updated.get("amount"), 48.5)
        self.assertNotIsInstance(updated, list)

    async def test_amount_change_alone_drops_carried_vat(self) -> None:
        fake = self._use(_operation(amount=120, main_amount=120, fee=None))
        await _call()(
            7,
            [
                SplitLine(amount=70, breakdown_id=10),
                SplitLine(amount=50),
            ],
        )
        posted, _updated = fake.posts[0]
        reused = next(line for line in posted["breakdowns"] if line.get("id") == 10)
        self.assertEqual(reused.get("amount"), 70)
        self.assertEqual(reused.get("categoryId"), 50)
        self.assertNotIn("vatAmount", reused)
        self.assertNotIn("manualVatAmount", reused)

    async def test_category_change_alone_drops_carried_vat(self) -> None:
        fake = self._use(_operation(amount=120, main_amount=120, fee=None))
        await _call()(7, [SplitLine(amount=120, category_id=100, breakdown_id=10)])
        posted, updated = fake.posts[0]
        reused = next(line for line in posted["breakdowns"] if line.get("id") == 10)
        self.assertEqual(reused.get("amount"), 120)
        self.assertEqual(reused.get("categoryId"), 100)
        self.assertNotIn("vatAmount", reused)
        self.assertNotIn("manualVatAmount", reused)
        assert isinstance(updated, dict)
        self.assertNotIn("vatAmount", updated)
        self.assertNotIn("manualVatAmount", updated)

    async def test_unchanged_line_keeps_its_vat(self) -> None:
        fake = self._use(_operation(amount=100, main_amount=100, fee=None))
        await _call()(
            7,
            [SplitLine(amount=100, category_id=50, breakdown_id=10, wording="note")],
        )
        posted, _updated = fake.posts[0]
        reused = next(line for line in posted["breakdowns"] if line.get("id") == 10)
        self.assertEqual(reused.get("vatAmount"), 20.0)
        self.assertEqual(reused.get("manualVatAmount"), 5.0)
        self.assertEqual(reused.get("wording"), "note")

    async def test_description_says_fees_stay_and_the_sum_is_checked(self) -> None:
        async with Client(mcp) as client:
            tools = await client.list_tools()
        tool = next(item for item in tools if item.name == "split_operation")
        description = tool.description or ""
        lines = tool.input_schema["properties"]["lines"]["description"]
        self.assertIn("fee", description.lower())
        self.assertIn("cent", description.lower())
        self.assertIn("fee", lines.lower())
        self.assertIn("counterpart", lines.lower())
