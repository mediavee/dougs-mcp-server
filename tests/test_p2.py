"""P2 regressions: money, expense edits, session files, sales documents, filings, uploads."""

import copy
import json
import os
import stat
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
from pydantic import ValidationError

from dougs_mcp.accounting import (
    _declaration_row,
    _round,
    _template_labels,
    confirm_declaration,
    get_declaration,
    list_declarations,
)
from dougs_mcp.client import DougsClient, DougsError, DougsJobError
from dougs_mcp.company import list_customer_todos, resolve_customer_todo
from dougs_mcp.operations import (
    add_attachment,
    create_operation,
    set_breakdown_vat,
    update_operation,
)
from dougs_mcp.purchases import _row, list_vendor_invoices, upload_vendor_invoice
from dougs_mcp.sales import (
    InvoiceLine,
    _email_message,
    save_quote_draft,
    save_sales_invoice_draft,
    send_sales_document,
)

DRAFT = UUID("11111111-1111-1111-1111-111111111111")
QUOTE = UUID("22222222-2222-2222-2222-222222222222")
INVOICE = UUID("33333333-3333-3333-3333-333333333333")


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


class _Posts:
    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []
        self.body: dict[str, Any] | None = None

    async def resolve_company_id(self) -> int:
        return 1

    async def post(
        self, path: str, json: dict[str, Any] | None = None, files: Any = None, data: Any = None
    ) -> dict[str, Any]:
        assert json is not None
        self.posts.append((path, json))
        self.body = json
        return {
            "id": 9,
            "type": json["type"],
            "date": json["date"],
            "amount": json["amount"],
            "memo": json.get("memo"),
            "isInbound": True,
            "validated": False,
            "breakdowns": json["breakdowns"],
        }


class _Operation:
    def __init__(self, operation: dict[str, Any]) -> None:
        self._operation = operation
        self.posted: tuple[dict[str, Any], dict[str, Any] | None] | None = None

    async def resolve_company_id(self) -> int:
        return 1

    async def operation(self, company_id: int, operation_id: int) -> dict[str, Any]:
        return copy.deepcopy(self._operation)

    async def update_operation(
        self,
        company_id: int,
        operation: dict[str, Any],
        updated_breakdown: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.posted = (operation, updated_breakdown)
        return operation


class _Uploads:
    def __init__(self, root: Path | None) -> None:
        self._s = _Root(root)
        self.posts: list[str] = []
        self.gets: list[str] = []

    async def resolve_company_id(self) -> int:
        return 1

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.gets.append(path)
        if path.endswith("/file-types"):
            return ["application/pdf", "text/plain", "application/octet-stream"]
        return {"id": "inv", "amount": 1, "vatAmount": 0, "paymentStatus": "paid"}

    async def post(
        self, path: str, json: Any = None, files: Any = None, data: Any = None
    ) -> dict[str, Any]:
        self.posts.append(path)
        return {"id": 4, "sourceDocumentAttachments": []}


class _Root:
    def __init__(self, root: Path | None) -> None:
        self.dougs_upload_root = root


class _Drafts:
    base_url = "https://app.dougs.fr"

    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.doc = doc
        self.posts: list[tuple[str, Any]] = []

    async def resolve_company_id(self) -> int:
        return 1

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.doc is None:
            raise AssertionError(path)
        return copy.deepcopy(self.doc)

    async def post(
        self, path: str, json: Any = None, files: Any = None, data: Any = None
    ) -> dict[str, Any]:
        self.posts.append((path, json))
        if not json:
            return {"id": "created", "status": "draft", "lines": []}
        return dict(json)

    async def put(self, path: str, json: Any = None) -> dict[str, Any]:
        self.posts.append((path, json))
        return dict(json or {})

    async def delete(self, path: str) -> None:
        return None


class _Mail:
    base_url = "https://app.dougs.fr"

    def __init__(self, doc: dict[str, Any], quote: bool) -> None:
        self.doc = doc
        self.quote = quote
        self.sent: dict[str, Any] | None = None

    async def resolve_company_id(self) -> int:
        return 1

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "/users/me":
            return {"profile": {"fullName": "Ada"}}
        if path == "/companies/1":
            return {"legalName": "Acme"}
        return copy.deepcopy(self.doc)

    async def post(
        self, path: str, json: Any = None, files: Any = None, data: Any = None
    ) -> dict[str, Any]:
        assert isinstance(json, dict)
        self.sent = json
        return dict(self.doc)


class MoneyTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_operation_rounds_half_up(self) -> None:
        fake = _Posts()
        with patch("dougs_mcp.operations.get_client", return_value=fake):
            await create_operation("cashPayment", "2026-06-01", 2.675, 100)
        assert fake.body is not None
        self.assertEqual(_dec(fake.body["amount"]), Decimal("2.68"))
        self.assertEqual(_dec(fake.body["breakdowns"][0]["amount"]), Decimal("2.68"))

    async def test_forced_vat_rounds_half_up(self) -> None:
        operation = {
            "id": 4,
            "type": "bank",
            "amount": 10,
            "isInbound": False,
            "validated": False,
            "breakdowns": [{"id": 8, "amount": 10, "isVatAmountEditable": True, "vatAmount": 2}],
        }
        fake = _Operation(operation)
        with patch("dougs_mcp.operations.get_client", return_value=fake):
            await set_breakdown_vat(4, 8, vat_amount=1.005)
            assert fake.posted is not None
            first = fake.posted[1]
            assert first is not None
            self.assertEqual(_dec(first["manualVatAmount"]), Decimal("1.01"))
            await set_breakdown_vat(4, 8, vat_amount=10.125)
            assert fake.posted is not None
            second = fake.posted[1]
            assert second is not None
            self.assertEqual(_dec(second["manualVatAmount"]), Decimal("10.13"))

    def test_displayed_amounts_round_half_up(self) -> None:
        self.assertEqual(_dec(_round(10.125)), Decimal("10.13"))
        self.assertEqual(_dec(_round(1.005)), Decimal("1.01"))
        self.assertEqual(_dec(_round(-1.005)), Decimal("-1.01"))
        self.assertEqual(_round(10), 10)

    async def test_vendor_total_and_declaration_boxes_round_half_up(self) -> None:
        class _Vendor:
            async def resolve_company_id(self) -> int:
                return 1

            async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
                if path.endswith("/counts"):
                    return {"paidInvoiceCount": 0, "paymentInProgressInvoiceCount": 0}
                if path.endswith("/stats"):
                    return {"totalToPay": 1.005}
                return []

        vendor = _Vendor()
        with patch("dougs_mcp.purchases.get_client", return_value=vendor):
            listed = await list_vendor_invoices()
        self.assertEqual(_dec(listed["to_pay_eur"]), Decimal("1.01"))

        class _Filing:
            async def resolve_company_id(self) -> int:
                return 3

            async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
                if path == "/declaration-templates/P2FORM":
                    return {
                        "items": [
                            {"name": "AA", "label": "Box A", "isGroup": False, "isInternal": False}
                        ]
                    }
                if path == "/companies/3/declarations/4":
                    return {
                        "id": 4,
                        "type": "P2FORM",
                        "label": "TVA",
                        "summaryAmount": 10.125,
                        "form": {"AA": 1.005},
                        "confirmedAt": "2026-01-01T00:00:00.000Z",
                        "messages": {"slots": {}},
                    }
                raise AssertionError(path)

        _template_labels.pop("P2FORM", None)
        try:
            with patch("dougs_mcp.accounting.get_client", return_value=_Filing()):
                filing = await get_declaration(4)
        finally:
            _template_labels.pop("P2FORM", None)
        self.assertEqual(_dec(filing["amount"]), Decimal("10.13"))
        boxes = filing["boxes"]
        self.assertIsInstance(boxes, list)
        assert isinstance(boxes, list)
        self.assertEqual(_dec(boxes[0]["value"]), Decimal("1.01"))


class ExpenseUpdateTests(unittest.IsolatedAsyncioTestCase):
    def _expense(self, breakdowns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "id": 15,
            "type": "expense",
            "date": "2026-01-15T00:00:00.000Z",
            "amount": 10,
            "isInbound": False,
            "validated": False,
            "breakdowns": breakdowns
            or [
                {
                    "id": 1,
                    "amount": 10,
                    "categoryId": 5,
                    "vatAmount": 1.67,
                    "manualVatAmount": 1.67,
                },
                {"id": 2, "amount": 0, "isCounterpart": True, "categoryId": -1},
            ],
        }

    async def test_amount_and_date_follow_the_line(self) -> None:
        fake = _Operation(self._expense())
        with patch("dougs_mcp.operations.get_client", return_value=fake):
            await update_operation(15, date="2026-06-01", amount=2.675)
        assert fake.posted is not None
        body, updated = fake.posted
        self.assertEqual(_dec(body["amount"]), Decimal("2.68"))
        self.assertEqual(body["date"], "2026-06-01T00:00:00.000Z")
        line = body["breakdowns"][0]
        self.assertEqual(_dec(line["amount"]), Decimal("2.68"))
        self.assertNotIn("vatAmount", line)
        self.assertNotIn("manualVatAmount", line)
        self.assertEqual(updated, line)
        self.assertEqual(body["breakdowns"][1]["amount"], 0)

    async def test_date_only_keeps_the_line(self) -> None:
        fake = _Operation(self._expense())
        with patch("dougs_mcp.operations.get_client", return_value=fake):
            await update_operation(15, date="2026-06-01")
        assert fake.posted is not None
        body, updated = fake.posted
        self.assertEqual(body["date"], "2026-06-01T00:00:00.000Z")
        self.assertEqual(body["breakdowns"][0]["amount"], 10)
        self.assertIsNone(updated)

    async def test_several_lines_are_refused(self) -> None:
        fake = _Operation(self._expense([{"id": 1, "amount": 6}, {"id": 2, "amount": 4}]))
        with (
            patch("dougs_mcp.operations.get_client", return_value=fake),
            self.assertRaises(ValueError) as caught,
        ):
            await update_operation(15, amount=5)
        self.assertIn("exactly one categorizable line", str(caught.exception))
        self.assertIsNone(fake.posted)

    async def test_fee_that_breaks_the_sum_is_refused(self) -> None:
        fake = _Operation(
            self._expense(
                [
                    {"id": 1, "amount": 10, "categoryId": 5},
                    {"id": 2, "amount": 1.5, "isFee": True},
                    {"id": 3, "amount": 0, "isCounterpart": True},
                ]
            )
        )
        with (
            patch("dougs_mcp.operations.get_client", return_value=fake),
            self.assertRaises(ValueError) as caught,
        ):
            await update_operation(15, amount=2.675)
        self.assertIn("lines sum to", str(caught.exception))
        self.assertIsNone(fake.posted)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, path: Path) -> tuple[DougsClient, httpx.AsyncClient]:
        client = DougsClient.__new__(DougsClient)
        http = httpx.AsyncClient()
        client._http = http
        client._session_path = path
        http.cookies.set("sid", "session-value", domain="app.dougs.fr", path="/")
        return client, http

    async def test_cookie_is_created_private_and_the_directory_is_too(self) -> None:
        previous = os.umask(0o022)
        try:
            with self._tmp() as root:
                client, http = self._client(root / "dougs-mcp" / "session.json")
                try:
                    with patch.object(Path, "write_text", side_effect=AssertionError("write_text")):
                        client._save_session()
                    saved = client._session_path
                    self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(saved.parent.stat().st_mode), 0o700)
                    self.assertEqual(json.loads(saved.read_text())[0]["value"], "session-value")
                finally:
                    await http.aclose()
        finally:
            os.umask(previous)

    async def test_failed_replace_removes_the_temp_file(self) -> None:
        with self._tmp() as root:
            client, http = self._client(root / "dougs-mcp" / "session.json")
            try:
                with (
                    patch.object(Path, "replace", side_effect=OSError("replace failed")),
                    self.assertRaises(OSError),
                ):
                    client._save_session()
                self.assertEqual(list((root / "dougs-mcp").iterdir()), [])
            finally:
                await http.aclose()

    async def test_unwritable_cache_is_not_swallowed(self) -> None:
        with self._tmp() as root:
            blocker = root / "blocked"
            blocker.write_text("x")
            client, http = self._client(blocker / "session.json")
            try:
                with self.assertRaises(OSError):
                    client._save_session()
            finally:
                await http.aclose()

    def _tmp(self) -> Any:
        return _TempDir()


class _TempDir:
    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._directory.name)

    def __exit__(self, *exc: object) -> None:
        self._directory.cleanup()


class SalesTests(unittest.IsolatedAsyncioTestCase):
    def test_invoice_email_uses_the_currency(self) -> None:
        text = _email_message(
            {"reference": "F-1", "amount": 100, "currency": "USD"}, False, "Ada", "Acme"
        )
        self.assertIn("Montant : 100 USD", text)
        self.assertNotIn("€", text)

    def test_quote_email_uses_the_quote_total(self) -> None:
        text = _email_message({"reference": "D-1", "totalAmountWithVat": 50}, True, "Ada", "Acme")
        self.assertIn("Montant : 50 €", text)
        self.assertNotIn("None", text)

    def test_missing_amount_refuses_the_default_text(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _email_message({"reference": "D-1"}, True, "Ada", "Acme")
        self.assertIn("amount", str(caught.exception))

    async def test_send_posts_the_currency_and_refuses_a_missing_amount(self) -> None:
        usd = _Mail(
            {
                "id": str(INVOICE),
                "source": "dougs",
                "reference": "F-1",
                "amount": 100,
                "currency": "USD",
                "clientData": {"email": "a@b.c"},
            },
            quote=False,
        )
        with patch("dougs_mcp.sales.get_client", return_value=usd):
            await send_sales_document("invoice", INVOICE)
        assert usd.sent is not None
        self.assertIn("100 USD", usd.sent["message"])
        self.assertNotIn("€", usd.sent["message"])

        missing = _Mail(
            {
                "id": str(QUOTE),
                "status": "PENDING",
                "reference": "D-1",
                "clientData": {"email": "a@b.c"},
            },
            quote=True,
        )
        with (
            patch("dougs_mcp.sales.get_client", return_value=missing),
            self.assertRaises(ValueError),
        ):
            await send_sales_document("quote", QUOTE)
        self.assertIsNone(missing.sent)

    def test_a_credit_line_can_be_negative_on_one_side_only(self) -> None:
        self.assertEqual(InvoiceLine(title="Dev", quantity=1, unit_amount=-600).unit_amount, -600)
        self.assertEqual(InvoiceLine(title="Dev", quantity=-1, unit_amount=600).quantity, -1)
        with self.assertRaises(ValidationError) as caught:
            InvoiceLine(title="Dev", quantity=-1, unit_amount=-600)
        self.assertIn("not both", str(caught.exception))

    async def test_credit_note_accepts_a_negative_line_and_refuses_a_positive_one(self) -> None:
        fake = _Drafts(_credit_doc())
        with patch("dougs_mcp.sales.get_client", return_value=fake):
            await save_sales_invoice_draft(
                draft_id=DRAFT, lines=[InvoiceLine(title="Dev", quantity=1, unit_amount=-500)]
            )
        self.assertEqual(fake.posts[-1][1]["lines"][0]["unitAmount"], -500)

        flagged = _Drafts(_credit_doc(is_refund=True, unit_amount=600))
        with patch("dougs_mcp.sales.get_client", return_value=flagged):
            await save_sales_invoice_draft(
                draft_id=DRAFT, lines=[InvoiceLine(title="Dev", quantity=-1, unit_amount=600)]
            )
        self.assertEqual(flagged.posts[-1][1]["lines"][0]["quantity"], -1)

        positive = _Drafts(_credit_doc())
        with (
            patch("dougs_mcp.sales.get_client", return_value=positive),
            self.assertRaises(ValueError) as caught,
        ):
            await save_sales_invoice_draft(
                draft_id=DRAFT, lines=[InvoiceLine(title="Dev", quantity=1, unit_amount=600)]
            )
        self.assertIn("stays negative", str(caught.exception))
        self.assertEqual(positive.posts, [])

    async def test_subject_edit_keeps_the_negative_lines(self) -> None:
        fake = _Drafts(_credit_doc())
        with patch("dougs_mcp.sales.get_client", return_value=fake):
            await save_sales_invoice_draft(draft_id=DRAFT, subject="Avoir corrigé")
        body = fake.posts[-1][1]
        self.assertEqual(body["subject"], "Avoir corrigé")
        self.assertEqual(body["lines"][0]["unitAmount"], -600)

    async def test_mixed_signs_and_new_drafts_and_quotes_are_refused(self) -> None:
        mixed = _Drafts(_credit_doc())
        with (
            patch("dougs_mcp.sales.get_client", return_value=mixed),
            self.assertRaises(ValueError) as caught,
        ):
            await save_sales_invoice_draft(
                draft_id=DRAFT,
                lines=[
                    InvoiceLine(title="A", quantity=1, unit_amount=-10),
                    InvoiceLine(title="B", quantity=1, unit_amount=10),
                ],
            )
        self.assertIn("mix", str(caught.exception))
        self.assertEqual(mixed.posts, [])

        created = _Drafts(None)
        with (
            patch("dougs_mcp.sales.get_client", return_value=created),
            self.assertRaises(ValueError) as caught,
        ):
            await save_sales_invoice_draft(
                customer="acme", lines=[InvoiceLine(title="A", quantity=1, unit_amount=-1)]
            )
        self.assertIn("credit-note", str(caught.exception))
        self.assertEqual(created.posts, [])

        quote = _Drafts(
            {
                "id": str(QUOTE),
                "status": "draft",
                "date": "2026-01-15T00:00:00.000Z",
                "expirationDate": "2026-02-14T00:00:00.000Z",
                "lines": [{"title": "Dev", "quantity": 1, "unitAmount": 600}],
            }
        )
        with (
            patch("dougs_mcp.sales.get_client", return_value=quote),
            self.assertRaises(ValueError) as caught,
        ):
            await save_quote_draft(
                quote_id=QUOTE, lines=[InvoiceLine(title="Dev", quantity=1, unit_amount=-5)]
            )
        self.assertIn("quote", str(caught.exception))
        self.assertEqual(quote.posts, [])


def _credit_doc(is_refund: bool = False, unit_amount: float = -600) -> dict[str, Any]:
    return {
        "id": str(DRAFT),
        "status": "draft",
        "isRefund": is_refund,
        "date": "2026-01-15T00:00:00.000Z",
        "dueDate": "2026-02-14T00:00:00.000Z",
        "subject": "Avoir",
        "lines": [
            {
                "title": "Dev",
                "description": "",
                "unit": "jour",
                "quantity": 1,
                "unitAmount": unit_amount,
                "vatRate": 0.2,
                "discount": 0,
            }
        ],
    }


class VendorVatTests(unittest.TestCase):
    def test_zero_vat_stays_visible(self) -> None:
        row = _row({"id": "inv", "amount": 100, "vatAmount": 0, "paymentStatus": "paid"})
        self.assertEqual(row.get("vat"), 0)


class DeclarationTests(unittest.IsolatedAsyncioTestCase):
    def test_direction_is_explicit(self) -> None:
        paid = _declaration_row({"id": 1, "label": "TVA", "summaryAmount": -1500}, "to_complete")
        self.assertEqual(paid["direction"], "pay")
        credit = _declaration_row({"id": 2, "label": "TVA", "summaryAmount": 80}, None)
        self.assertEqual(credit["direction"], "credit")
        zero = _declaration_row({"id": 3, "label": "TVA", "summaryAmount": 0}, None)
        self.assertIsNone(zero["direction"])
        for tool in (list_declarations, get_declaration):
            text = tool.__doc__ or ""
            self.assertIn("to pay", text)
            self.assertIn("credit", text)

    async def test_list_shows_the_direction(self) -> None:
        class _List:
            def __init__(self, amount: int) -> None:
                self.amount = amount

            async def resolve_company_id(self) -> int:
                return 3

            async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
                assert params is not None
                if params.get("status") != "ready_to_complete":
                    return []
                return [
                    {
                        "id": 9,
                        "label": "TVA",
                        "type": "CA3-2026",
                        "summaryAmount": self.amount,
                        "dueDate": "2026-03-24",
                    }
                ]

        with patch("dougs_mcp.accounting.get_client", return_value=_List(-1500)):
            listed = await list_declarations()
        self.assertEqual(listed["declarations"][0]["direction"], "pay")
        self.assertEqual(listed["declarations"][0]["amount"], -1500)

    async def test_job_read_failure_is_not_pending(self) -> None:
        failed = _Confirm(DougsError("GET /jobs/job-1 -> 500"))
        with patch("dougs_mcp.accounting.get_client", return_value=failed):
            result = await confirm_declaration(9, True)
        self.assertEqual(failed.posts, ["/companies/3/declarations/9/actions/confirm"])
        self.assertNotIn("pending", result)
        self.assertIn("500", str(result["trackingFailed"]))
        self.assertIn("do not call confirm_declaration", str(result["next"]))

        offline = _Confirm(httpx.ConnectError("network down"))
        with patch("dougs_mcp.accounting.get_client", return_value=offline):
            result = await confirm_declaration(9, True)
        self.assertNotIn("pending", result)
        self.assertIn("trackingFailed", result)
        self.assertIn("do not call confirm_declaration", str(result["next"]))

        waiting = _Confirm(None)
        with patch("dougs_mcp.accounting.get_client", return_value=waiting):
            result = await confirm_declaration(9, True)
        self.assertNotIn("trackingFailed", result)
        self.assertIn("do not call confirm_declaration", str(result["pending"]))

        broken = _Confirm(DougsJobError("job job-1 failed: rejected"))
        with (
            patch("dougs_mcp.accounting.get_client", return_value=broken),
            self.assertRaises(DougsJobError),
        ):
            await confirm_declaration(9, True)


class _Confirm:
    def __init__(self, outcome: Exception | None) -> None:
        self.outcome = outcome
        self.posts: list[str] = []

    async def resolve_company_id(self) -> int:
        return 3

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if path != "/companies/3/declarations/9":
            raise AssertionError(path)
        return {
            "id": 9,
            "type": "CFE",
            "label": "CFE",
            "confirmedAt": None,
            "isReadOnly": False,
            "messages": {"slots": {}, "errorCount": 0},
            "metadata": {},
            "summaryAmount": -1500,
        }

    async def post(
        self, path: str, json: Any = None, files: Any = None, data: Any = None
    ) -> dict[str, str]:
        self.posts.append(path)
        return {"uuid": "job-1"}

    async def wait_job(self, job: dict[str, Any], max_wait: float = 60) -> dict[str, Any] | None:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return None


class TodoTests(unittest.IsolatedAsyncioTestCase):
    async def test_tasks_the_resolver_cannot_close_are_marked(self) -> None:
        class _Tasks:
            async def resolve_company_id(self) -> int:
                return 1

            async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
                if path.endswith("/customer-todo"):
                    return [
                        {
                            "id": 1,
                            "code": "customer:attach",
                            "title": "Attach",
                            "description": "Receipt",
                        }
                    ]
                if path.endswith("/customer-tasks"):
                    return [
                        {"id": 1, "dueDate": "2026-01-01", "overdue": True},
                        {
                            "id": 2,
                            "code": "customer:other",
                            "title": "Other",
                            "dueDate": "2026-02-01",
                            "overdue": True,
                        },
                    ]
                if path.endswith("/customer-tip"):
                    return [{"id": 3, "code": "tip", "title": "Tip"}]
                raise AssertionError(path)

            async def post(
                self, path: str, json: Any = None, files: Any = None, data: Any = None
            ) -> dict[str, Any]:
                raise AssertionError(path)

        text = list_customer_todos.__doc__ or ""
        self.assertIn("actionable", text)
        with patch("dougs_mcp.company.get_client", return_value=_Tasks()):
            rows = await list_customer_todos()
            with self.assertRaises(ValueError):
                await resolve_customer_todo(2, "complete")
        by_id = {row["id"]: row for row in rows}
        self.assertNotIn("actionable", by_id[1])
        self.assertIs(by_id[2]["actionable"], False)
        self.assertEqual(by_id[2]["dueDate"], "2026-02-01")
        self.assertIs(by_id[3]["tip"], True)
        self.assertNotIn("actionable", by_id[3])


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_stay_inside_the_root(self) -> None:
        with _TempDir() as root:
            allowed = root / "receipts"
            allowed.mkdir()
            receipt = allowed / "receipt.pdf"
            receipt.write_bytes(b"%PDF")
            outside = root / "secret.txt"
            outside.write_text("password")
            env = allowed / ".env"
            env.write_text("DOUGS_PASSWORD=nope")
            linked = allowed / "link.pdf"
            linked.symlink_to(outside)
            cache = root / "cache"
            session = cache / "dougs-mcp" / "session-abc.json"
            session.parent.mkdir(parents=True)
            session.write_text('[{"name":"sid","value":"cookie"}]')
            previous = os.environ.get("XDG_CACHE_HOME")
            os.environ["XDG_CACHE_HOME"] = str(cache)
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                fake = _Uploads(allowed)
                with (
                    patch("dougs_mcp.operations.get_client", return_value=fake),
                    patch("dougs_mcp.purchases.get_client", return_value=fake),
                ):
                    await add_attachment(4, "receipt.pdf")
                    self.assertEqual(
                        fake.posts,
                        [
                            "/companies/1/operations/4/source-document-attachments/actions/create-from-formdata"
                        ],
                    )
                    for path in (str(outside), str(env), str(linked), "../secret.txt"):
                        fake.posts.clear()
                        with (
                            patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
                            self.assertRaises(ValueError),
                        ):
                            await add_attachment(4, path)
                        self.assertEqual(fake.posts, [])
                    wide = _Uploads(root)
                    with (
                        patch("dougs_mcp.operations.get_client", return_value=wide),
                        patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
                        self.assertRaises(ValueError) as caught_cache,
                    ):
                        await add_attachment(4, str(session))
                    self.assertIn("session-cache", str(caught_cache.exception))
                    self.assertEqual(wide.posts, [])
                    unset = _Uploads(None)
                    with (
                        patch("dougs_mcp.operations.get_client", return_value=unset),
                        patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
                        self.assertRaises(ValueError) as caught,
                    ):
                        await add_attachment(4, str(receipt))
                    self.assertIn("DOUGS_UPLOAD_ROOT", str(caught.exception))
                    purchases = _Uploads(allowed)
                    with (
                        patch("dougs_mcp.purchases.get_client", return_value=purchases),
                        patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
                        self.assertRaises(ValueError),
                    ):
                        await upload_vendor_invoice(str(env))
                    self.assertEqual(purchases.gets, [])
                    self.assertEqual(purchases.posts, [])
            finally:
                os.chdir(previous_cwd)
                if previous is None:
                    os.environ.pop("XDG_CACHE_HOME", None)
                else:
                    os.environ["XDG_CACHE_HOME"] = previous
