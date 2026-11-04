"""Purchase side: vendor (supplier) invoices and their link to bank operations."""

import asyncio
import mimetypes
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import Field

from .client import DougsClient
from .server import DESTRUCTIVE, READ, SENDS, WRITE, compact, get_client, mcp

# The list endpoint filters on these values only; "not_paid_or_partially_paid" covers both open states.
_STATUS_FILTER = {"paid": "paid", "unpaid": "not_paid_or_partially_paid"}

InvoiceId = Annotated[
    str,
    Field(description="Vendor invoice id (a uuid) from list_vendor_invoices."),
]


def _day(value: str | None) -> str | None:
    return value[:10] if value else None


def _operation(op: dict[str, Any] | None) -> dict[str, Any] | None:
    if not op:
        return None
    return {
        "id": op.get("id"),
        "date": _day(op.get("date")),
        "amount": op.get("signedAmount", op.get("amount")),
        "wording": op.get("wording"),
    }


def _row(inv: dict[str, Any]) -> dict[str, Any]:
    open_ = inv.get("paymentStatus") != "paid"
    matched = inv.get("matchedOperation") or {}
    return compact(
        {
            "id": inv.get("id"),
            "supplier": inv.get("supplierName"),
            "number": inv.get("reference"),
            "date": _day(inv.get("date")),
            "amount": inv.get("amount"),
            "vat": inv.get("vatAmount") or None,
            "currency": None if inv.get("currency") == "EUR" else inv.get("currency"),
            "status": inv.get("paymentStatus"),
            "remaining": inv.get("remainingAmountDue") if open_ else None,
            "operation_ids": [o.get("id") for o in inv.get("operations") or []],
            "suggested_operation_id": matched.get("id"),
            "file": inv.get("fileName"),
        }
    )


def _detail(inv: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": inv.get("id"),
            "type": inv.get("type"),
            "supplier": inv.get("supplierName"),
            "supplier_country": inv.get("supplierCountry"),
            "supplier_vat_number": inv.get("supplierVatNumber"),
            "number": inv.get("reference"),
            "date": _day(inv.get("date")),
            "due_date": _day(inv.get("dueDate")),
            "amount": inv.get("amount"),
            "vat": inv.get("vatAmount"),
            "vat_breakdown": [
                compact({k: v.get(k) for k in ("vatRate", "netAmount", "vatAmount")})
                for v in inv.get("vatBreakdown") or []
            ],
            "deposit": inv.get("depositAmount") or None,
            "payable": inv.get("payableAmount"),
            "remaining": inv.get("remainingAmountDue"),
            "currency": inv.get("currency"),
            "exchange_rate_to_eur": inv.get("exchangeRateToEur"),
            "status": inv.get("paymentStatus"),
            # "initialised" means Dougs' OCR is still reading the file; fields fill in afterwards.
            "ocr": inv.get("prefillStatus"),
            "source": inv.get("source"),
            "locked": inv.get("isLocked") or None,
            "refusable": (inv.get("can") or {}).get("beRefused") or None,
            "refusal_reason": inv.get("refusalReasonCode"),
            "refusal_message": inv.get("refusalMessage"),
            "e_invoice_history": [
                compact(
                    {
                        "date": _day(h.get("date")),
                        "label": h.get("label"),
                        "refusal": h.get("refusal"),
                    }
                )
                for h in inv.get("history") or []
            ],
            "label": inv.get("label"),
            "memo": inv.get("memo"),
            "operations": [_operation(o) for o in inv.get("operations") or []],
            "suggested_operation": _operation(inv.get("matchedOperation")),
            "file": inv.get("fileName"),
            "file_path": inv.get("filePath"),
            "editable_fields": inv.get("allowedUpdateFields"),
        }
    )


def _read_file(file_path: str) -> tuple[str, bytes]:
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValueError(f"no file at {path}")
    return path.name, path.read_bytes()


async def _invoice(client: DougsClient, cid: int, invoice_id: str) -> dict[str, Any]:
    return await client.get(f"/companies/{cid}/vendor-invoices/{invoice_id}")


@mcp.tool(annotations=READ)
async def list_vendor_invoices(
    status: Annotated[
        Literal["paid", "unpaid"] | None,
        Field(description="'unpaid' includes partially paid invoices; omit for all."),
    ] = None,
    search: Annotated[
        str | None,
        Field(
            description="Matches supplier, invoice number, file name, memo or exact amount, "
            "e.g. 'OVH' or '132'."
        ),
    ] = None,
    date_from: Annotated[
        date | None, Field(description="Invoice date on or after, e.g. 2026-01-01.")
    ] = None,
    date_to: Annotated[date | None, Field(description="Invoice date on or before.")] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Rows per page.")] = 20,
    offset: Annotated[int, Field(ge=0, description="Rows to skip.")] = 0,
) -> dict[str, Any]:
    """List vendor (supplier) invoices, most recent first (undated ones on top), as compact rows.

    Also returns the total still to pay in EUR across all unpaid invoices. Use get_vendor_invoice
    for one invoice's detail and file path.
    """
    client = get_client()
    cid = await client.resolve_company_id()
    params: dict[str, Any] = {}
    if status:
        params["paymentStatus"] = _STATUS_FILTER[status]
    if search:
        params["search"] = search
    base = f"/companies/{cid}/vendor-invoices"

    if date_from or date_to:
        # The API has no date filter: fetch every match and filter here.
        invoices = await client.get(base, params=params)
        lo, hi = str(date_from or ""), str(date_to or "9999")
        invoices = [i for i in invoices if i.get("date") and lo <= i["date"][:10] <= hi]
        total = len(invoices)
        invoices = invoices[offset : offset + limit]
    else:
        invoices = await client.get(base, params={**params, "offset": offset, "limit": limit})
        counts = await client.get(f"{base}/counts", params={"search": search or ""})
        paid, unpaid = counts["paidInvoiceCount"], counts["paymentInProgressInvoiceCount"]
        total = {"paid": paid, "unpaid": unpaid}[status] if status else paid + unpaid

    stats = await client.get(f"{base}/stats")
    answer: dict[str, Any] = {
        "total": total,
        "to_pay_eur": round(stats["totalToPay"], 2),
        "invoices": [_row(i) for i in invoices],
    }
    if offset + len(invoices) < total:
        answer["next_offset"] = offset + len(invoices)
    return answer


@mcp.tool(annotations=READ)
async def get_vendor_invoice(invoice_id: InvoiceId) -> dict[str, Any]:
    """One vendor invoice in detail: supplier, amounts, VAT breakdown, OCR status, refusal,
    attached and suggested operations, the fields update_vendor_invoice may change, and the
    file_path to pass to get_file_url."""
    client = get_client()
    cid = await client.resolve_company_id()
    return _detail(await _invoice(client, cid, invoice_id))


@mcp.tool(annotations=WRITE)
async def upload_vendor_invoice(
    file_path: Annotated[
        str, Field(description="Local path of a PDF or image, e.g. '/tmp/facture-ovh.pdf'.")
    ],
) -> dict[str, Any]:
    """Upload a supplier invoice file; Dougs reads it by OCR within seconds.

    Returns the new invoice; call get_vendor_invoice again once `ocr` is no longer 'initialised'
    to see what it read, and fix it with update_vendor_invoice.
    """
    name, content = await asyncio.to_thread(_read_file, file_path)
    mime = mimetypes.guess_type(name)[0]
    client = get_client()
    cid = await client.resolve_company_id()
    base = f"/companies/{cid}/vendor-invoices"
    accepted = await client.get(f"{base}/file-types")
    if mime not in accepted:
        raise ValueError(f"{name} is {mime or 'of unknown type'}; Dougs accepts {accepted}")
    created = await client.post(
        f"{base}?filename={quote(name)}&type=attachment",
        files={"file": (name, content, mime)},
    )
    return _detail(created)


@mcp.tool(annotations=WRITE)
async def update_vendor_invoice(
    invoice_id: InvoiceId,
    supplier: Annotated[str | None, Field(description="Supplier name.")] = None,
    number: Annotated[
        str | None, Field(description="Invoice number printed by the supplier.")
    ] = None,
    invoice_date: Annotated[
        date | None, Field(description="Invoice date, e.g. 2026-10-31.")
    ] = None,
    due_date: Annotated[date | None, Field(description="Payment due date.")] = None,
    amount: Annotated[
        float | None, Field(ge=0, description="Total including VAT, in the invoice currency.")
    ] = None,
    vat_amount: Annotated[float | None, Field(ge=0, description="Total VAT amount.")] = None,
    currency: Annotated[str | None, Field(description="ISO code, e.g. 'EUR', 'USD'.")] = None,
    invoice_type: Annotated[
        Literal["Facture", "Avoir", "Rectificative", "Acompte"] | None,
        Field(description="Invoice, credit note, corrective invoice or deposit invoice."),
    ] = None,
    supplier_country: Annotated[
        str | None, Field(description="ISO country code, e.g. 'FR', 'US'.")
    ] = None,
    supplier_vat_number: Annotated[
        str | None, Field(description="Intra-EU VAT number, e.g. 'FR67824012173'.")
    ] = None,
    memo: Annotated[str | None, Field(description="Free note.")] = None,
) -> dict[str, Any]:
    """Correct the fields of a vendor invoice, typically what the OCR misread; only the fields
    given change. E-invoices received through the e-invoicing network are locked except memo."""
    fields = {
        "supplierName": supplier,
        "reference": number,
        "date": f"{invoice_date}T00:00:00.000Z" if invoice_date else None,
        "dueDate": f"{due_date}T00:00:00.000Z" if due_date else None,
        "amount": amount,
        "vatAmount": vat_amount,
        "currency": currency,
        "type": invoice_type,
        "supplierCountry": supplier_country,
        "supplierVatNumber": supplier_vat_number,
        "memo": memo,
    }
    changes = {k: v for k, v in fields.items() if v is not None}
    if not changes:
        raise ValueError("nothing to update: pass at least one field")
    client = get_client()
    cid = await client.resolve_company_id()
    allowed = (await _invoice(client, cid, invoice_id)).get("allowedUpdateFields") or []
    refused = [k for k in changes if k not in allowed]
    if refused:
        raise ValueError(
            f"Dougs does not allow changing {refused} on this invoice; allowed: {allowed}"
        )
    updated = await client.put(
        f"/companies/{cid}/vendor-invoices/{invoice_id}", json={"id": invoice_id, **changes}
    )
    return _detail(updated)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_vendor_invoice(invoice_id: InvoiceId) -> dict[str, str]:
    """Delete a vendor invoice and its file. Dougs refuses while it is attached to a validated
    operation."""
    client = get_client()
    cid = await client.resolve_company_id()
    await client.delete(f"/companies/{cid}/vendor-invoices/{invoice_id}")
    return {"deleted": invoice_id}


@mcp.tool(annotations=WRITE)
async def attach_vendor_invoice(
    invoice_id: InvoiceId,
    operation_ids: Annotated[
        list[int],
        Field(
            min_length=1,
            description="Bank operation ids from list_operations, e.g. [288071055]; several "
            "when the invoice was paid in installments.",
        ),
    ],
    detach: Annotated[bool, Field(description="True to remove the links instead.")] = False,
) -> dict[str, Any]:
    """Attach a vendor invoice to the bank operations that paid it, as their receipt, or detach it.

    Dougs refuses to detach from a validated operation: un-validate it first with
    validate_operation, then validate it again. The invoice's payment status follows a few
    seconds later, so the status in this answer is the previous one.
    """
    client = get_client()
    cid = await client.resolve_company_id()
    action = "detach-operations" if detach else "attach-operations"
    updated = await client.post(
        f"/companies/{cid}/vendor-invoices/{invoice_id}/actions/{action}",
        json={"operationIds": operation_ids},
    )
    return _detail(updated)


@mcp.tool(annotations=DESTRUCTIVE)
async def review_vendor_invoice_match(
    invoice_id: InvoiceId,
    action: Annotated[
        Literal["accept", "reject"],
        Field(
            description="'accept' attaches the suggested operation; 'reject' dismisses the "
            "suggestion."
        ),
    ],
) -> dict[str, Any]:
    """Accept or reject the bank operation Dougs suggests as the payment of a vendor invoice
    (suggested_operation in get_vendor_invoice). To attach another operation, use
    attach_vendor_invoice."""
    client = get_client()
    cid = await client.resolve_company_id()
    invoice = await _invoice(client, cid, invoice_id)
    if not invoice.get("matchedOperation"):
        raise ValueError(
            f"vendor invoice {invoice_id} has no suggested operation; attach one with "
            "attach_vendor_invoice"
        )
    updated = await client.post(
        f"/companies/{cid}/vendor-invoices/{invoice_id}/actions/{action}-matched-operation",
        json={},
    )
    return _detail(updated)


@mcp.tool(annotations=SENDS)
async def set_vendor_invoice_status(
    invoice_id: InvoiceId,
    status: Annotated[
        Literal["paid", "not_paid", "refused"],
        Field(
            description="'refused' is final and only for refusable e-invoices "
            "(get_vendor_invoice's `refusable`)."
        ),
    ],
    refusal_reason: Annotated[
        str | None,
        Field(
            description="Refusal code, e.g. 'DOUBLON'; omit it with status='refused' to get "
            "the list."
        ),
    ] = None,
    refusal_message: Annotated[
        str | None, Field(max_length=2000, description="Refusal comment sent to the supplier.")
    ] = None,
) -> dict[str, Any]:
    """Mark a vendor invoice paid or unpaid, or refuse an e-invoice. On an e-invoice the
    status reaches the supplier through the e-invoicing network ('Paiement transmis', or the
    refusal with its reason)."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/vendor-invoices/{invoice_id}"
    can = (await _invoice(client, cid, invoice_id)).get("can") or {}
    body: dict[str, Any] = {"paymentStatus": status}
    if status == "refused":
        if not can.get("beRefused"):
            raise ValueError(
                f"vendor invoice {invoice_id} cannot be refused: only e-invoices not yet "
                "attached to an operation or paid can"
            )
        if not refusal_reason or not refusal_message:
            reasons = await client.get(f"{path}/refusal-reasons")
            codes = {r.get("code"): r.get("label") for r in reasons}
            raise ValueError(
                f"pass refusal_reason, one of {codes}, and a refusal_message for the supplier"
            )
        body |= {"refusalReasonCode": refusal_reason, "refusalMessage": refusal_message}
    elif not can.get("updatePaymentStatus"):
        raise ValueError(f"Dougs does not allow changing the status of vendor invoice {invoice_id}")
    return _detail(await client.put(path, json=body))


@mcp.tool(annotations=DESTRUCTIVE)
async def move_vendor_invoice_to_sales(invoice_id: InvoiceId) -> dict[str, Any]:
    """Move an uploaded document filed by mistake under purchases to the sales invoices; it
    gets a new id there. Dougs refuses to move it back."""
    client = get_client()
    cid = await client.resolve_company_id()
    moved = await client.post(
        f"/companies/{cid}/invoices-switch/{invoice_id}/actions/move-to-sales-invoices", json={}
    )
    return compact(
        {
            "sales_invoice_id": moved.get("id"),
            "client": moved.get("clientName"),
            "number": moved.get("reference"),
            "date": _day(moved.get("date")),
            "amount": moved.get("amount"),
        }
    )
