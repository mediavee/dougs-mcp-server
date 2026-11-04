"""Sales side: invoices, invoice drafts, customers and sales channels."""

import contextlib
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from .client import DougsClient
from .server import get_client, mcp


@mcp.tool()
async def list_sales_channels(company_id: int | None = None) -> list[dict[str, Any]]:
    """List the sales channels configured for a company."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/salesChannels")


@mcp.tool()
async def list_sales_invoices(company_id: int | None = None) -> list[dict[str, Any]]:
    """List customer (sales) invoices: client, amount, VAT, due date, payment status."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/sales-invoices")


@mcp.tool()
async def get_invoices_overview(company_id: int | None = None) -> dict[str, Any]:
    """Summary of sales and vendor invoices: counts and amounts (paid, waiting, late, draft)."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    return {
        "sales": {
            "counts": await client.get(f"/companies/{cid}/sales-invoices/counts"),
            "stats": await client.get(f"/companies/{cid}/sales-invoices/stats"),
        },
        "vendor": {
            "counts": await client.get(f"/companies/{cid}/vendor-invoices/counts"),
            "stats": await client.get(f"/companies/{cid}/vendor-invoices/stats"),
        },
    }


# --- Sales invoice drafts -------------------------------------------------------
#
# Drafts live under /sales-invoices-drafts, a separate resource from finalized
# invoices (/sales-invoices). A draft is not an accounting entry and gets no
# invoice number until it is finalized, so these tools cannot alter the books.
# Finalizing and emailing are deliberately not exposed: they stay a human step.


class InvoiceLine(BaseModel):
    """One line of a sales invoice draft."""

    title: str = Field(description="Line label, e.g. 'Prestation de services'.")
    quantity: float = Field(gt=0, description="Quantity, e.g. a number of days.")
    unit_amount: float = Field(ge=0, description="Unit price, excluding VAT unless price_with_vat.")
    description: str | None = Field(None, description="Optional detail, e.g. 'Juin 2026'.")
    unit: str | None = Field(None, description="Optional unit label, e.g. 'jour', 'heure'.")
    vat_rate: float = Field(
        0.2, ge=0, le=1, description="VAT rate as a fraction: 0.2 = 20 %, 0 = exempt."
    )
    discount: float = Field(0, ge=0, le=100, description="Line discount, as a percentage.")
    price_with_vat: bool = Field(False, description="True if unit_amount includes VAT.")


def _draft_line(line: InvoiceLine) -> dict[str, Any]:
    return {
        "title": line.title,
        "description": line.description,
        "unit": line.unit or "",  # the API rejects null
        "quantity": line.quantity,
        "unitAmount": line.unit_amount,
        "vatRate": line.vat_rate,
        "discount": line.discount,
        "discountUnit": "%",
        "reference": None,
        "isPriceWithVat": line.price_with_vat,
    }


def _draft_client_data(found: dict[str, Any]) -> dict[str, Any]:
    """Map a /sales-invoices-drafts/clients search hit to a draft's clientData.

    The two shapes differ: address.zipcode -> address.zipCode, isBtoB -> isBToB.
    """
    address = found.get("address") or {}
    return {
        "siren": found.get("siren"),
        "legalName": found.get("legalName"),
        "lastName": found.get("lastName"),
        "firstName": found.get("firstName"),
        "address": {
            "street": address.get("street") or "",
            "zipCode": address.get("zipcode") or "",
            "city": address.get("city") or "",
            "country": address.get("country") or "France",
        },
        "others": [],
        "email": found.get("email") or "",
        "phone": found.get("phone") or "",
        "vatNumber": found.get("vatNumber"),
        "deliveryAddress": found.get("deliveryAddress"),
        "isBToB": found.get("isBtoB"),
        "siret": found.get("siret") or "",
        "clientId": found.get("clientId"),
    }


def _draft_summary(client: DougsClient, company_id: int, d: dict[str, Any]) -> dict[str, Any]:
    client_data = d.get("clientData") or {}
    return {
        "id": d.get("id"),
        "url": f"{client.base_url}/app/c/{company_id}/invoicing/sales-invoice/{d.get('id')}",
        "client": client_data.get("legalName")
        or " ".join(filter(None, [client_data.get("firstName"), client_data.get("lastName")]))
        or None,
        "subject": d.get("subject"),
        "date": d.get("date"),
        "dueDate": d.get("dueDate"),
        "dueDateOption": d.get("dueDateOption"),
        "lines": [
            {
                "title": line.get("title"),
                "description": line.get("description"),
                "quantity": line.get("quantity"),
                "unit": line.get("unit"),
                "unitAmount": line.get("unitAmount"),
                "vatRate": line.get("vatRate"),
                "amount": line.get("amount"),
            }
            for line in d.get("lines") or []
        ],
        "totalNetAmount": d.get("totalNetAmount"),
        "totalVatAmount": d.get("totalVatAmount"),
        "totalAmountWithVat": d.get("totalAmountWithVat"),
    }


def _iso_midnight(d: date) -> str:
    return f"{d.isoformat()}T00:00:00.000Z"


def _apply_dates(draft: dict[str, Any], invoice_date: str | None, due_in_days: int | None) -> None:
    """Set date / dueDate / dueDateOption; due date defaults to 30 days after the date."""
    if invoice_date is None and due_in_days is None:
        return
    if invoice_date is not None:
        day = date.fromisoformat(invoice_date)
    elif draft.get("date"):
        day = date.fromisoformat(str(draft["date"])[:10])
    else:
        day = date.today()
    days = 30 if due_in_days is None else due_in_days
    draft["date"] = _iso_midnight(day)
    draft["dueDate"] = _iso_midnight(day + timedelta(days=days))
    # Presets the UI offers; any other delay is a CUSTOM option with an explicit dueDate.
    draft["dueDateOption"] = (
        "IMMEDIATE" if days == 0 else f"DAYS_{days}" if days in (15, 30, 60) else "CUSTOM"
    )


async def _find_invoice_client(client: DougsClient, cid: int, customer: str) -> dict[str, Any]:
    """Resolve a registered customer by id, SIREN or name to its invoice client record."""
    customers = await client.get(f"/companies/{cid}/customers", params={"limit": 1000, "offset": 0})
    wanted = customer.strip().lower()
    matches = [
        c
        for c in customers
        if not c.get("archived")
        and (
            str(c.get("id")) == wanted
            or (c.get("siren") or "").replace(" ", "") == wanted.replace(" ", "")
            or (c.get("name") or "").lower() == wanted
        )
    ]
    if not matches:
        matches = [
            c
            for c in customers
            if not c.get("archived") and wanted in (c.get("name") or "").lower()
        ]
    if len(matches) != 1:
        names = [c.get("name") for c in matches] or "none"
        raise ValueError(
            f"customer '{customer}' must match exactly one registered customer, got: {names}. "
            "Use list_customers and pass its id or SIREN."
        )
    chosen = matches[0]
    hits = await client.get(
        f"/companies/{cid}/sales-invoices-drafts/clients",
        params={"isBtoB": str(bool(chosen.get("isBtoB"))).lower(), "name": chosen.get("name")},
    )
    siren = (chosen.get("siren") or "").replace(" ", "")
    for hit in hits or []:
        if not hit.get("clientId"):
            continue
        # With a SIREN, a same-name record of another company must not be picked.
        if siren:
            if (hit.get("siren") or "").replace(" ", "") == siren:
                return hit
        elif hit.get("name") == chosen.get("name"):
            return hit
    raise ValueError(f"no invoice client record found for customer '{chosen.get('name')}'")


@mcp.tool()
async def list_customers(
    include_archived: bool = False, company_id: int | None = None
) -> list[dict[str, Any]]:
    """List registered customers (the clients a sales invoice can be addressed to).

    Returns id, name, SIREN and whether the customer is a business (isBtoB).
    Pass the id, SIREN or exact name as `customer` to create_sales_invoice_draft.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    customers = await client.get(f"/companies/{cid}/customers", params={"limit": 1000, "offset": 0})
    return [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "siren": c.get("siren"),
            "isBtoB": c.get("isBtoB"),
            "email": c.get("email"),
            "archived": c.get("archived"),
        }
        for c in customers
        if include_archived or not c.get("archived")
    ]


@mcp.tool()
async def list_sales_invoice_drafts(company_id: int | None = None) -> list[dict[str, Any]]:
    """List sales invoice drafts (not yet finalized), with client, lines and totals."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    drafts = await client.get(f"/companies/{cid}/sales-invoices-drafts")
    return [_draft_summary(client, cid, d) for d in drafts or []]


@mcp.tool()
async def get_sales_invoice_draft(draft_id: UUID, company_id: int | None = None) -> dict[str, Any]:
    """Read one sales invoice draft: client, dates, lines and server-computed totals."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    draft = await client.get(f"/companies/{cid}/sales-invoices-drafts/{draft_id}")
    return _draft_summary(client, cid, draft)


@mcp.tool()
async def create_sales_invoice_draft(
    customer: str,
    lines: list[InvoiceLine],
    invoice_date: str | None = None,
    due_in_days: int = 30,
    subject: str | None = None,
    company_id: int | None = None,
) -> dict[str, Any]:
    """Create a sales invoice DRAFT for a registered customer.

    customer: the customer's id, SIREN or name (see list_customers).
    invoice_date: ISO 'YYYY-MM-DD', defaults to today; the due date is
    invoice_date + due_in_days. Totals are computed by Dougs.

    The draft is never finalized nor sent: it has no invoice number and is not
    an accounting entry. Review and finalize it in the Dougs app (returned url).
    """
    if not lines:
        raise ValueError("a draft needs at least one line")
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    # Resolve the customer before creating anything, so a bad name leaves no empty draft.
    found = await _find_invoice_client(client, cid, customer)
    created = await client.post(f"/companies/{cid}/sales-invoices-drafts", json={})
    path = f"/companies/{cid}/sales-invoices-drafts/{created['id']}"
    try:
        draft = await client.get(path)
        draft["clientData"] = _draft_client_data(found)
        draft["subject"] = subject
        draft["lines"] = [_draft_line(line) for line in lines]
        _apply_dates(draft, invoice_date or date.today().isoformat(), due_in_days)
        saved = await client.post(path, json=draft)
    except Exception:
        # Don't leave the empty draft behind; the original error is the one to report.
        with contextlib.suppress(Exception):
            await client.delete(path)
        raise
    return _draft_summary(client, cid, saved)


@mcp.tool()
async def update_sales_invoice_draft(
    draft_id: UUID,
    lines: list[InvoiceLine] | None = None,
    invoice_date: str | None = None,
    due_in_days: int | None = None,
    subject: str | None = None,
    company_id: int | None = None,
) -> dict[str, Any]:
    """Update a sales invoice draft. Only the arguments you pass are changed.

    lines replaces all the draft's lines. invoice_date is ISO 'YYYY-MM-DD';
    changing it or due_in_days recomputes the due date. Returns the draft with
    the totals recomputed by Dougs. Finalized invoices cannot be edited here.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    path = f"/companies/{cid}/sales-invoices-drafts/{draft_id}"
    draft = await client.get(path)
    if lines is not None:
        if not lines:
            raise ValueError("a draft needs at least one line")
        draft["lines"] = [_draft_line(line) for line in lines]
    if subject is not None:
        draft["subject"] = subject
    _apply_dates(draft, invoice_date, due_in_days)
    saved = await client.post(path, json=draft)
    return _draft_summary(client, cid, saved)


@mcp.tool()
async def delete_sales_invoice_draft(
    draft_id: UUID, company_id: int | None = None
) -> dict[str, Any]:
    """Delete a sales invoice draft. Finalized invoices are out of reach of this tool."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    path = f"/companies/{cid}/sales-invoices-drafts/{draft_id}"
    await client.get(path)  # 404s unless this id is an existing draft
    await client.delete(path)
    return {"deleted": str(draft_id)}


@mcp.tool()
async def get_product_catalog(company_id: int | None = None) -> Any:
    """Return the company's product/service catalog."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/product-catalog")
