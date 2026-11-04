"""Sales side: invoices, invoice and quote drafts, customers, invoice products, sales channels."""

import asyncio
import contextlib
from datetime import date, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .client import DougsClient, DougsError
from .server import DESTRUCTIVE, READ, SENDS, WRITE, compact, get_client, mcp

Limit = Annotated[int, Field(ge=1, le=100, description="Rows per page.")]
Offset = Annotated[int, Field(ge=0, description="Rows to skip, for the next page.")]
Search = Annotated[str, Field(description="Free-text search, e.g. a client name.")]
CUSTOMER_HELP = "Customer id, SIREN or name (list_customers), or any French company's SIREN."
IsoDate = Annotated[str, Field(description="ISO date 'YYYY-MM-DD'; today on creation.")]
Subject = Annotated[str, Field(description="Subject printed on the document.")]


def _day(value: str | None) -> str | None:
    """'2026-11-21T00:00:00.000Z' -> '2026-11-21'."""
    return value[:10] if value else None


def _page(rows: list[Any], limit: int, offset: int) -> dict[str, Any]:
    page: dict[str, Any] = {"rows": rows}
    if len(rows) == limit:
        page["more"] = f"call again with offset={offset + limit}"
    return page


def _client_label(data: dict[str, Any]) -> str | None:
    return (
        data.get("legalName")
        or " ".join(filter(None, [data.get("firstName"), data.get("lastName")]))
        or None
    )


# --- Invoices --------------------------------------------------------------------


def _invoice_row(i: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": i.get("id"),
            "number": i.get("reference"),
            "client": i.get("clientName"),
            "date": _day(i.get("date")),
            "dueDate": _day(i.get("dueDate")),
            "amount": i.get("amount"),
            "netAmount": i.get("netAmount"),
            "vatAmount": i.get("vatAmount"),
            "currency": i.get("currency") if i.get("currency") != "EUR" else None,
            "paymentStatus": i.get("paymentStatus"),
            "late": i.get("isLate") or None,
            "refund": i.get("isRefund") or None,
            "paidAt": _day(i.get("paidAt")),
            "source": i.get("source"),
            "filePath": i.get("filePath"),
            "operationIds": [
                (a.get("operation") or {}).get("id") for a in i.get("operationAttachments") or []
            ],
            "suggestedOperationId": (
                (i.get("operationCandidate") or {}).get("operation") or {}
            ).get("id"),
        }
    )


@mcp.tool(annotations=READ)
async def list_sales_invoices(
    payment_status: Annotated[
        Literal["waiting", "paid", "canceled"] | None,
        Field(description="Keep only invoices with this payment status."),
    ] = None,
    search: Search = "",
    limit: Limit = 20,
    offset: Offset = 0,
) -> dict[str, Any]:
    """List finalized sales invoices, newest first: number, client, amounts, payment status and
    the bank operations they are matched or suggested to. Drafts are in
    list_sales_invoice_drafts."""
    client = get_client()
    cid = await client.resolve_company_id()
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if payment_status:
        params["paymentStatus"] = payment_status
    if search:
        params["search"] = search
    invoices = await client.get(f"/companies/{cid}/sales-invoices", params=params)
    return _page([_invoice_row(i) for i in invoices or []], limit, offset)


@mcp.tool(annotations=READ)
async def get_sales_overview(
    accounting_year_id: Annotated[
        int | None,
        Field(description="Year for the revenue figures (list_accounting_years); default: active."),
    ] = None,
) -> dict[str, Any]:
    """Sales dashboard: invoice and quote counts, issued/waiting/late amounts, receivables aging,
    and the year's net revenue by month and by client."""
    client = get_client()
    cid = await client.resolve_company_id()
    year_id = accounting_year_id or await client.active_accounting_year_id(cid)
    base = f"/companies/{cid}"
    year = {"accountingYearId": year_id}
    counts, amounts, quotes, aging, by_month, by_client = await asyncio.gather(
        client.get(f"{base}/sales-invoices/counts"),
        client.get(f"{base}/sales-invoices/stats"),
        client.get(f"{base}/invoicing/quotes/counts"),
        client.get(f"{base}/invoice-stats/aging-balance"),
        client.get(f"{base}/invoice-stats/invoice-amounts-by-month", params=year),
        client.get(
            f"{base}/invoice-stats/invoice-amounts-by-client",
            params={**year, "limit": 10, "offset": 0},
        ),
    )
    return compact(
        {
            "invoiceCounts": counts,
            "invoiceAmounts": amounts,
            "quoteCounts": quotes,
            "receivablesAging": aging,
            "revenue": {
                "accountingYearId": year_id,
                "netTotal": by_month.get("totalN"),
                "previousYearNetTotal": by_month.get("totalN1"),
                "previousYearToDate": by_month.get("totalN1YearToDate"),
                "byMonth": {
                    str(m["date"])[:7]: m["value"]
                    for m in by_month.get("current") or []
                    if m["value"]
                },
                "topClients": by_client.get("items"),
            },
        }
    )


# --- Customers -------------------------------------------------------------------
#
# Registered customers live under /customers. Like the app, a document's client can also be any
# French company taken from the public directory by SIREN, without registering it.


def _customer_row(c: dict[str, Any]) -> dict[str, Any]:
    address = c.get("address") or {}
    return compact(
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "business": c.get("isBtoB"),
            "siren": c.get("siren"),
            "vatNumber": c.get("vatNumber"),
            "email": c.get("email"),
            "phone": c.get("phone"),
            "address": ", ".join(
                filter(None, [address.get("street"), address.get("zipcode"), address.get("city")])
            ),
            "archived": c.get("archived") or None,
        }
    )


def _address(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Customer address (zipcode) -> document address (zipCode)."""
    if not value:
        return None
    return {
        "street": value.get("street") or "",
        "zipCode": value.get("zipcode") or "",
        "city": value.get("city") or "",
        "country": value.get("country") or "France",
    }


def _client_data(c: dict[str, Any], client_id: str | None) -> dict[str, Any]:
    """Build a document's clientData from a customer-shaped record."""
    return {
        "siren": c.get("siren") or "",
        "legalName": c.get("legalName"),
        "lastName": c.get("lastName"),
        "firstName": c.get("firstName"),
        "address": _address(c.get("address") or {"country": "France"}),
        "others": [],
        "email": c.get("email") or "",
        "phone": c.get("phone") or "",
        "vatNumber": c.get("vatNumber"),
        "deliveryAddress": _address(c.get("deliveryAddress")),
        "isBToB": bool(c.get("isBtoB")),
        "siret": c.get("siret") or "",
        "clientId": client_id,
    }


async def _registered_client_id(client: DougsClient, cid: int, c: dict[str, Any]) -> str | None:
    """The uuid documents link a registered customer by (not its numeric id)."""
    hits = await client.get(
        f"/companies/{cid}/sales-invoices-drafts/clients",
        params={"isBtoB": str(bool(c.get("isBtoB"))).lower(), "name": c.get("name")},
    )
    for hit in hits or []:
        if not hit.get("isFromPappers") and hit.get("name") == c.get("name"):
            return hit.get("clientId")
    return None


async def _resolve_client(client: DougsClient, cid: int, customer: str) -> dict[str, Any]:
    """Resolve a customer argument to a document's clientData."""
    wanted = customer.strip().lower()
    digits = wanted.replace(" ", "")
    customers = await client.get(f"/companies/{cid}/customers", params={"limit": 1000, "offset": 0})
    active = [c for c in customers or [] if not c.get("archived")]
    matches = [
        c
        for c in active
        if str(c.get("id")) == digits
        or (c.get("siren") or "").replace(" ", "") == digits
        or (c.get("name") or "").lower() == wanted
    ] or [c for c in active if wanted in (c.get("name") or "").lower()]
    if len(matches) == 1:
        chosen = matches[0]
        return _client_data(chosen, await _registered_client_id(client, cid, chosen))
    if len(matches) > 1:
        names = [f"{c.get('name')} (id {c.get('id')})" for c in matches]
        raise ValueError(f"customer '{customer}' matches several customers: {names}; pass an id")
    if digits.isdigit() and len(digits) == 9:
        company = await client.get("/company-data-from-siren", params={"siren": digits})
        hq = company.get("headquarter") or {}
        return _client_data(
            {
                "isBtoB": True,
                "legalName": company.get("legalName"),
                "siren": company.get("siren"),
                "siret": hq.get("siret"),
                "vatNumber": company.get("vatNumber"),
                "address": hq,
            },
            None,
        )
    hits = await client.get(
        f"/companies/{cid}/sales-invoices-drafts/clients",
        params={"isBtoB": "true", "name": customer.strip()},
    )
    found = [f"{h.get('name')} (SIREN {h.get('siren')})" for h in hits or [] if h.get("siren")]
    raise ValueError(
        f"customer '{customer}' is not a registered customer. Pass a SIREN"
        + (f"; companies with that name: {found[:10]}" if found else "")
        + ", or register it with save_customer."
    )


@mcp.tool(annotations=READ)
async def list_customers(
    search: Search = "",
    include_archived: Annotated[bool, Field(description="Also list archived customers.")] = False,
    limit: Limit = 50,
    offset: Offset = 0,
) -> dict[str, Any]:
    """List registered customers (the address book used for invoices and quotes)."""
    client = get_client()
    cid = await client.resolve_company_id()
    params: dict[str, Any] = {
        "limit": limit,
        "offset": offset,
        "includeArchived": str(include_archived).lower(),
    }
    if search:
        params["search"] = search
    customers = await client.get(f"/companies/{cid}/customers", params=params)
    return _page([_customer_row(c) for c in customers or []], limit, offset)


Street = Annotated[str, Field(description="Street line.")]
Zipcode = Annotated[str, Field(description="Postal code.")]
City = Annotated[str, Field(description="City.")]
Country = Annotated[str, Field(description="Country; Dougs assumes France.")]
Email = Annotated[str, Field(description="Billing email.")]
Phone = Annotated[str, Field(description="Phone number.")]
Siren = Annotated[str, Field(description="SIREN, 9 digits.")]
VatNumber = Annotated[str, Field(description="EU VAT number, e.g. 'FR54903951259'.")]


def _customer_fields(**fields: Any) -> dict[str, Any]:
    """Map tool arguments to the customer payload, leaving out those not given."""
    payload: dict[str, Any] = {}
    for key, api in [
        ("business", "isBtoB"),
        ("company_name", "legalName"),
        ("first_name", "firstName"),
        ("last_name", "lastName"),
        ("siren", "siren"),
        ("vat_number", "vatNumber"),
        ("email", "email"),
        ("phone", "phone"),
        ("archived", "archived"),
    ]:
        if fields.get(key) not in (None, ""):
            payload[api] = fields[key]
    address = {
        api: fields[key]
        for key, api in [
            ("street", "street"),
            ("zipcode", "zipcode"),
            ("city", "city"),
            ("country", "country"),
        ]
        if fields.get(key)
    }
    if address:
        payload["address"] = address
    return payload


@mcp.tool(annotations=WRITE)
async def save_customer(
    customer_id: Annotated[
        int | None, Field(description="Customer to change (list_customers); omit to create one.")
    ] = None,
    business: Annotated[
        bool | None, Field(description="Creation: True for a company, False for a person.")
    ] = None,
    company_name: Annotated[str, Field(description="Required to create a company.")] = "",
    first_name: Annotated[str, Field(description="Required to create a person.")] = "",
    last_name: Annotated[str, Field(description="Required to create a person.")] = "",
    siren: Siren = "",
    vat_number: VatNumber = "",
    street: Street = "",
    zipcode: Zipcode = "",
    city: City = "",
    country: Country = "",
    email: Email = "",
    phone: Phone = "",
    archived: Annotated[bool | None, Field(description="Archive or restore it.")] = None,
) -> dict[str, Any]:
    """Create a customer, or change one (only the arguments passed change). Documents already
    issued keep the client details they were made with."""
    fields = _customer_fields(**locals())
    client = get_client()
    cid = await client.resolve_company_id()
    if customer_id is None:
        if business is None:
            raise ValueError("creating a customer needs business=True (company) or False (person)")
        if business and not company_name:
            raise ValueError("a business customer needs company_name")
        if not business and not (first_name and last_name):
            raise ValueError("a person needs first_name and last_name")
        return _customer_row(await client.post(f"/companies/{cid}/customers", json=fields))
    path = f"/companies/{cid}/customers/{customer_id}"
    current = await client.get(path)
    if "address" in fields:
        fields["address"] = {**(current.get("address") or {}), **fields["address"]}
    return _customer_row(await client.post(path, json={**current, **fields}))


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_customer(
    customer_id: Annotated[int, Field(description="Customer id from list_customers.")],
) -> dict[str, Any]:
    """Delete a registered customer. To keep it out of lists without deleting it, archive it
    with save_customer."""
    client = get_client()
    cid = await client.resolve_company_id()
    await client.delete(f"/companies/{cid}/customers/{customer_id}")
    return {"deleted": customer_id}


# --- Invoice and quote drafts ------------------------------------------------------
#
# Drafts are not accounting entries and invoice drafts get no number until finalized, so editing
# them cannot alter the books. Finalizing, sending and the quote lifecycle are the actions below.


class InvoiceLine(BaseModel):
    title: str = Field(description="e.g. 'Développement'.")
    quantity: float = Field(gt=0)
    unit_amount: float = Field(ge=0, description="Unit price, excluding VAT.")
    description: str = ""
    unit: str = Field("unité", description="e.g. 'jour', 'heure'.")
    vat_rate: float = Field(0.2, ge=0, le=1, description="0.2 = 20 %, 0 = exempt.")
    discount: float = Field(0, ge=0, le=100, description="Percentage.")


Lines = Annotated[
    list[InvoiceLine], Field(min_length=1, description="All the lines; replaces existing ones.")
]


def _line(line: InvoiceLine) -> dict[str, Any]:
    return {
        "title": line.title,
        "description": line.description,
        "unit": line.unit,
        "quantity": line.quantity,
        "unitAmount": line.unit_amount,
        "vatRate": line.vat_rate,
        "discount": line.discount,
        "discountUnit": "%",
        "reference": None,
        "isPriceWithVat": False,
    }


def _document_row(d: dict[str, Any], kind: str) -> dict[str, Any]:
    """List row of an invoice or quote draft; kind is the app route segment."""
    return compact(
        {
            "id": d.get("id"),
            "number": d.get("reference") if kind == "quote" else None,
            "status": d.get("status") if kind == "quote" else None,
            "client": _client_label(d.get("clientData") or {}),
            "subject": d.get("subject"),
            "date": _day(d.get("date")),
            "dueDate": _day(d.get("dueDate")),
            "expirationDate": _day(d.get("expirationDate")),
            "totalNetAmount": d.get("totalNetAmount"),
            "totalAmountWithVat": d.get("totalAmountWithVat"),
        }
    )


def _document(client: DougsClient, cid: int, d: dict[str, Any], kind: str) -> dict[str, Any]:
    """A draft with its lines, VAT total and app URL."""
    lines = [
        {
            "title": line.get("title"),
            "description": line.get("description"),
            "quantity": line.get("quantity"),
            "unit": line.get("unit"),
            "unitAmount": line.get("unitAmount"),
            "vatRate": line.get("vatRate"),
            "discount": line.get("discount") or None,
            "amount": line.get("amount"),
        }
        for line in d.get("lines") or []
    ]
    return {
        **_document_row(d, kind),
        "url": f"{client.base_url}/app/c/{cid}/invoicing/{kind}/{d.get('id')}",
        "lines": compact(lines),
        **compact({"totalVatAmount": d.get("totalVatAmount")}),
    }


def _problems(problems: list[dict[str, Any]] | None) -> dict[str, Any]:
    """can-finalize answers [{field, message}]: what Dougs requires before finalizing."""
    return compact({"finalizeProblems": [p.get("message") for p in problems or []]})


def _midnight(d: date) -> str:
    return f"{d.isoformat()}T00:00:00.000Z"


def _apply_dates(
    doc: dict[str, Any], doc_date: str | None, days: int | None, end_field: str
) -> None:
    """Set the date and its end date (due or expiry) `days` later."""
    if not doc_date and days is None:
        return
    if doc_date:
        day = date.fromisoformat(doc_date)
    elif doc.get("date"):
        day = date.fromisoformat(str(doc["date"])[:10])
    else:
        day = date.today()
    if days is None and doc.get("date") and doc.get(end_field):
        # Keep the document's current delay when only the date moves.
        start = date.fromisoformat(str(doc["date"])[:10])
        days = (date.fromisoformat(str(doc[end_field])[:10]) - start).days
    days = 30 if days is None else days
    doc["date"] = _midnight(day)
    doc[end_field] = _midnight(day + timedelta(days=days))
    if end_field == "dueDate":
        # Presets the app offers; any other delay is CUSTOM with an explicit dueDate.
        doc["dueDateOption"] = (
            "IMMEDIATE" if days == 0 else f"DAYS_{days}" if days in (15, 30, 60) else "CUSTOM"
        )


DraftId = Annotated[UUID, Field(description="Invoice draft id from list_sales_invoice_drafts.")]
QuoteDate = Annotated[str, Field(description="ISO date 'YYYY-MM-DD', not after today.")]
QuoteDraftId = Annotated[UUID, Field(description="Quote draft id from list_quotes.")]


async def _edit_draft(
    client: DougsClient,
    cid: int,
    kind: Literal["invoice", "quote"],
    draft_id: object,
    client_data: dict[str, Any] | None,
    lines: list[InvoiceLine] | None,
    doc_date: str | None,
    days: int | None,
    subject: str | None,
) -> dict[str, Any]:
    """Read a draft, apply the given changes and save the whole document back."""
    path = (
        f"/companies/{cid}/sales-invoices-drafts/{draft_id}"
        if kind == "invoice"
        else f"/companies/{cid}/invoicing/quote-drafts/{draft_id}"
    )
    doc = await client.get(path)
    if str(doc.get("status")).lower() != "draft":
        raise ValueError(f"{kind} {draft_id} is {doc.get('status')}: only drafts can be edited")
    if client_data is not None:
        doc["clientData"] = client_data
    if lines is not None:
        doc["lines"] = [_line(line) for line in lines]
    if subject:
        doc["subject"] = subject
    end_field = "dueDate" if kind == "invoice" else "expirationDate"
    _apply_dates(doc, doc_date, days, end_field)

    async def save(body: dict[str, Any]) -> dict[str, Any]:
        if kind == "invoice":
            return dict(await client.post(path, json=body))
        # The app saves quote drafts with a PUT under /quotes.
        return dict(await client.put(f"/companies/{cid}/invoicing/quotes/{draft_id}", json=body))

    saved = await save(doc)
    # A new quote date makes Dougs reset the expiration to one month later: set it again.
    if saved.get(end_field) != doc.get(end_field):
        saved = await save({**saved, end_field: doc[end_field]})
    return saved


@mcp.tool(annotations=READ)
async def list_sales_invoice_drafts(
    search: Search = "", limit: Limit = 20, offset: Offset = 0
) -> dict[str, Any]:
    """List sales invoice drafts (not finalized) with client, dates and totals; read the lines
    with get_sales_invoice_draft."""
    client = get_client()
    cid = await client.resolve_company_id()
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if search:
        params["search"] = search
    drafts = await client.get(f"/companies/{cid}/sales-invoices-drafts", params=params)
    return _page([_document_row(d, "sales-invoice") for d in drafts or []], limit, offset)


@mcp.tool(annotations=READ)
async def get_sales_invoice_draft(draft_id: DraftId) -> dict[str, Any]:
    """Read a sales invoice draft: client, dates, lines, totals, its app URL and what still
    blocks finalizing it."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/sales-invoices-drafts/{draft_id}"
    draft, problems = await asyncio.gather(
        client.get(path), client.get(f"{path}/actions/can-finalize")
    )
    return {**_document(client, cid, draft, "sales-invoice"), **_problems(problems)}


@mcp.tool(annotations=WRITE)
async def save_sales_invoice_draft(
    draft_id: Annotated[
        UUID | None, Field(description="Draft to change; omit to create one.")
    ] = None,
    customer: Annotated[str, Field(description=CUSTOMER_HELP + " Required to create.")] = "",
    lines: Lines | None = None,
    invoice_date: IsoDate = "",
    due_in_days: Annotated[
        int | None, Field(ge=0, description="Payment delay in days; 30 on creation.")
    ] = None,
    subject: Subject = "",
) -> dict[str, Any]:
    """Create a sales invoice draft, or change one (only the arguments passed change; a new date
    or delay recomputes the due date). Dougs computes the totals; finalize it with
    finalize_sales_document."""
    client = get_client()
    cid = await client.resolve_company_id()
    if draft_id is not None:
        client_data = await _resolve_client(client, cid, customer) if customer else None
        saved = await _edit_draft(
            client, cid, "invoice", draft_id, client_data, lines, invoice_date, due_in_days, subject
        )
        return _document(client, cid, saved, "sales-invoice")
    if not customer or not lines:
        raise ValueError("creating a draft needs customer and lines")
    # Resolve the customer first, so a bad one leaves no empty draft behind.
    client_data = await _resolve_client(client, cid, customer)
    created = await client.post(f"/companies/{cid}/sales-invoices-drafts", json={})
    path = f"/companies/{cid}/sales-invoices-drafts/{created['id']}"
    try:
        saved = await _edit_draft(
            client,
            cid,
            "invoice",
            created["id"],
            client_data,
            lines,
            invoice_date or date.today().isoformat(),
            30 if due_in_days is None else due_in_days,
            subject,
        )
    except Exception:
        with contextlib.suppress(Exception):
            await client.delete(path)
        raise
    return _document(client, cid, saved, "sales-invoice")


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_sales_invoice_draft(draft_id: DraftId) -> dict[str, Any]:
    """Delete a sales invoice draft. Finalized invoices cannot be deleted here."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/sales-invoices-drafts/{draft_id}"
    await client.get(path)  # 404s unless this id is an existing draft
    await client.delete(path)
    return {"deleted": str(draft_id)}


@mcp.tool(annotations=READ)
async def list_quotes(
    status: Annotated[
        Literal["draft", "pending", "accepted", "refused"] | None,
        Field(description="Keep only quotes in this state; default: all."),
    ] = None,
    search: Search = "",
    limit: Limit = 20,
    offset: Offset = 0,
) -> dict[str, Any]:
    """List quotes (devis), drafts included, newest first: number, status, client, dates and
    totals. Read a draft's lines with get_quote_draft."""
    client = get_client()
    cid = await client.resolve_company_id()
    # Drafts and issued quotes are two endpoints; fetch enough of each to cut one page.
    params: dict[str, Any] = {"limit": offset + limit, "offset": 0}
    if search:
        params["search"] = search
    calls = []
    if status in (None, "draft"):
        calls.append(client.get(f"/companies/{cid}/invoicing/quote-drafts", params=params))
    if status != "draft":
        quote_params = {**params, **({"status": status.upper()} if status else {})}
        calls.append(client.get(f"/companies/{cid}/invoicing/quotes", params=quote_params))
    quotes = [q for result in await asyncio.gather(*calls) for q in result or []]
    quotes.sort(key=lambda q: q.get("date") or "", reverse=True)
    rows = [_document_row(q, "quote") for q in quotes[offset : offset + limit]]
    return _page(rows, limit, offset)


@mcp.tool(annotations=READ)
async def get_quote_draft(quote_id: QuoteDraftId) -> dict[str, Any]:
    """Read a quote draft: client, dates, lines, totals, its app URL and what still blocks
    finalizing it."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/invoicing/quote-drafts/{quote_id}"
    quote, problems = await asyncio.gather(
        client.get(path), client.get(f"{path}/actions/can-finalize")
    )
    return {**_document(client, cid, quote, "quote"), **_problems(problems)}


@mcp.tool(annotations=WRITE)
async def save_quote_draft(
    quote_id: Annotated[
        UUID | None, Field(description="Quote draft to change; omit to create one.")
    ] = None,
    customer: Annotated[str, Field(description=CUSTOMER_HELP + " Required to create.")] = "",
    lines: Lines | None = None,
    quote_date: QuoteDate = "",
    valid_for_days: Annotated[
        int | None, Field(ge=0, description="Validity in days; 30 on creation.")
    ] = None,
    subject: Subject = "",
) -> dict[str, Any]:
    """Create a quote (devis) draft, or change one (only the arguments passed change; a new date
    or validity recomputes the expiration). Dougs computes the totals; finalize it with
    finalize_sales_document."""
    client = get_client()
    cid = await client.resolve_company_id()
    if quote_id is not None:
        client_data = await _resolve_client(client, cid, customer) if customer else None
        saved = await _edit_draft(
            client, cid, "quote", quote_id, client_data, lines, quote_date, valid_for_days, subject
        )
        return _document(client, cid, saved, "quote")
    if not customer or not lines:
        raise ValueError("creating a quote draft needs customer and lines")
    client_data = await _resolve_client(client, cid, customer)
    created = await client.post(f"/companies/{cid}/invoicing/quote-drafts", json={})
    draft_path = f"/companies/{cid}/invoicing/quote-drafts/{created['id']}"
    try:
        saved = await _edit_draft(
            client,
            cid,
            "quote",
            created["id"],
            client_data,
            lines,
            quote_date or date.today().isoformat(),
            30 if valid_for_days is None else valid_for_days,
            subject,
        )
    except Exception:
        with contextlib.suppress(Exception):
            await client.delete(draft_path)
        raise
    return _document(client, cid, saved, "quote")


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_quote_draft(quote_id: QuoteDraftId) -> dict[str, Any]:
    """Delete a quote draft. Issued quotes cannot be deleted here."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/invoicing/quote-drafts/{quote_id}"
    await client.get(path)  # 404s unless this id is an existing draft
    await client.delete(path)
    return {"deleted": str(quote_id)}


# --- Invoice and quote actions -----------------------------------------------------
#
# Finalizing gives a document its legal number and freezes it; sending emails the client.

Document = Annotated[Literal["invoice", "quote"], Field(description="Sales invoice or quote.")]
InvoiceId = Annotated[UUID, Field(description="Finalized invoice id from list_sales_invoices.")]
QuoteId = Annotated[UUID, Field(description="Quote id from list_quotes.")]


def _invoice_url(client: DougsClient, cid: int, invoice: dict[str, Any]) -> dict[str, Any]:
    return {
        **_invoice_row(invoice),
        "url": f"{client.base_url}/app/c/{cid}/invoicing/sales-invoice/{invoice.get('id')}",
    }


def _quote_url(client: DougsClient, cid: int, quote: dict[str, Any]) -> dict[str, Any]:
    return {
        **_document_row(quote, "quote"),
        "url": f"{client.base_url}/app/c/{cid}/invoicing/quote/{quote.get('id')}",
    }


@mcp.tool(annotations=DESTRUCTIVE)
async def finalize_sales_document(
    document: Document,
    draft_id: Annotated[
        UUID, Field(description="Draft id from list_sales_invoice_drafts or list_quotes.")
    ],
) -> dict[str, Any]:
    """Finalize an invoice or quote draft: Dougs numbers it, issues its PDF and freezes it. Fails
    with what is missing when the draft is incomplete; send it with send_sales_document."""
    client = get_client()
    cid = await client.resolve_company_id()
    if document == "invoice":
        path = f"/companies/{cid}/sales-invoices-drafts/{draft_id}"
    else:
        path = f"/companies/{cid}/invoicing/quote-drafts/{draft_id}"
    problems = await client.get(f"{path}/actions/can-finalize")
    if problems:
        tool = "save_sales_invoice_draft" if document == "invoice" else "save_quote_draft"
        messages = [p.get("message") for p in problems]
        raise ValueError(f"the draft is not ready: {messages}; complete it with {tool}")
    if document == "invoice":
        finalized = await client.post(f"{path}/actions/finalize", json={}) or {}
        invoice_id = finalized.get("salesInvoiceId") or finalized.get("id")
        if not invoice_id:
            return {"finalized": str(draft_id), "next": "find it with list_sales_invoices"}
        invoice = await client.get(f"/companies/{cid}/sales-invoices/{invoice_id}")
        return _invoice_url(client, cid, invoice)
    await client.put(f"{path}/actions/finalize", json={})
    quote = await client.get(f"/companies/{cid}/invoicing/quotes/{draft_id}")
    return _quote_url(client, cid, quote)


def _email_message(doc: dict[str, Any], quote: bool, signer: str, company: str) -> str:
    """The message the app pre-fills."""
    kind = "devis" if quote else "facture"
    expiry = ""
    if quote and doc.get("expirationDate"):
        day = date.fromisoformat(str(doc["expirationDate"])[:10])
        expiry = f"\nDate d'expiration : {day.strftime('%d/%m/%Y')}"
    return (
        f"Bonjour,\n\nVeuillez trouver ci-joint votre {kind}.\n\n"
        f"{kind.capitalize()} : {doc.get('reference')}\nMontant : {doc.get('amount')} €{expiry}\n\n"
        "Je vous remercie pour votre confiance et vous souhaite une excellente journée ! \n\n"
        f"{signer}\n{company}"
    )


@mcp.tool(annotations=SENDS)
async def send_sales_document(
    document: Document,
    document_id: Annotated[
        UUID, Field(description="Finalized invoice (list_sales_invoices) or quote (list_quotes).")
    ],
    recipient: Annotated[str, Field(description="Email address; default: the client's.")] = "",
    cc: Annotated[list[str] | None, Field(description="Email addresses in copy.")] = None,
    copy_to_me: Annotated[bool, Field(description="Also send a copy to the user.")] = False,
    subject: Annotated[str, Field(description="Default: the app's subject.")] = "",
    message: Annotated[
        str, Field(description="Email body; default: the app's text with number and amount.")
    ] = "",
) -> dict[str, Any]:
    """Email a finalized invoice or quote to the client, with its PDF attached."""
    client = get_client()
    cid = await client.resolve_company_id()
    quote = document == "quote"
    path = (
        f"/companies/{cid}/invoicing/quotes/{document_id}"
        if quote
        else f"/companies/{cid}/sales-invoices/{document_id}"
    )
    doc = await client.get(path)
    if quote and doc.get("status") == "DRAFT":
        raise ValueError("this quote is a draft: finalize it first with finalize_sales_document")
    if not quote and (
        doc.get("source") != "dougs" or (doc.get("can") or {}).get("canBeSentByEmail") is False
    ):
        raise ValueError("Dougs cannot email this invoice (only invoices made in Dougs)")
    recipient = recipient or (doc.get("clientData") or {}).get("email") or ""
    if not recipient:
        raise ValueError("the client has no email address: pass recipient")
    if not message:
        me, company = await asyncio.gather(client.get("/users/me"), client.get(f"/companies/{cid}"))
        signer = (me.get("profile") or {}).get("fullName") or ""
        message = _email_message(doc, quote, signer, company.get("legalName") or "")
    payload = {
        "recipient": recipient,
        "copyReceivers": cc or [],
        "selfCopy": copy_to_me,
        "subject": subject
        or ("Vous avez reçu un nouveau devis" if quote else "Vous avez reçu une nouvelle facture"),
        "message": message,
    }
    sent = await client.post(f"{path}/actions/send-email", json=payload)
    row = _quote_url(client, cid, sent) if quote else _invoice_url(client, cid, sent)
    return {**row, "sentTo": recipient}


@mcp.tool(annotations=WRITE)
async def duplicate_sales_document(
    document: Document,
    document_id: Annotated[
        UUID, Field(description="Invoice, invoice draft, quote or quote draft to copy.")
    ],
) -> dict[str, Any]:
    """Copy an invoice or quote, finalized or draft, into a new draft."""
    client = get_client()
    cid = await client.resolve_company_id()
    base = f"/companies/{cid}"
    if document == "quote":
        quote = await client.get(f"{base}/invoicing/quotes/{document_id}")
        source = "quote-drafts" if quote.get("status") == "DRAFT" else "quotes"
        copy = await client.post(
            f"{base}/invoicing/{source}/{document_id}/actions/duplicate", json={}
        )
        return _document(client, cid, copy, "quote")
    try:
        await client.get(f"{base}/sales-invoices-drafts/{document_id}")
        source = "sales-invoices-drafts"
    except DougsError as e:
        if e.status != 404:
            raise
        source = "sales-invoices"
    copy = await client.post(f"{base}/{source}/{document_id}/actions/duplicate", json={})
    return _document(client, cid, copy, "sales-invoice")


@mcp.tool(annotations=DESTRUCTIVE)
async def refund_sales_invoice(invoice_id: InvoiceId) -> dict[str, Any]:
    """Create the credit note (avoir) that cancels a finalized invoice, as a draft with the
    amounts negated: adjust it with save_sales_invoice_draft, then finalize it."""
    client = get_client()
    cid = await client.resolve_company_id()
    credit_note = await client.post(
        f"/companies/{cid}/sales-invoices/{invoice_id}/actions/refund", json={}
    )
    return _document(client, cid, credit_note, "sales-invoice")


@mcp.tool(annotations=WRITE)
async def set_sales_invoice_paid(
    invoice_id: InvoiceId,
    paid: Annotated[bool, Field(description="True: paid; False: back to waiting.")],
    paid_at: Annotated[str, Field(description="ISO date 'YYYY-MM-DD'; default: today.")] = "",
) -> dict[str, Any]:
    """Mark a finalized invoice paid or unpaid by hand, when no bank operation settles it."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/sales-invoices/{invoice_id}"
    invoice = await client.get(path)
    if not (invoice.get("can") or {}).get("updatePaymentStatus"):
        raise ValueError("Dougs does not let this invoice's payment status change")
    paid_day = date.fromisoformat(paid_at) if paid_at else date.today()
    changes = {
        "paymentStatus": "paid" if paid else "waiting",
        "paidAt": _midnight(paid_day) if paid else None,
    }
    allowed = invoice.get("allowedUpdateFields") or []
    if "paymentStatus" not in allowed:
        raise ValueError("Dougs does not let this invoice's payment status change")
    updated = await client.post(path, json={k: v for k, v in changes.items() if k in allowed})
    return _invoice_row(updated)


# The API has no call that undoes a rejection.
@mcp.tool(annotations=DESTRUCTIVE)
async def review_sales_invoice_match(
    invoice_id: InvoiceId,
    action: Annotated[
        Literal["accept", "reject"],
        Field(description="accept attaches the suggested operation; reject dismisses it."),
    ],
) -> dict[str, Any]:
    """Accept or reject the bank operation Dougs suggests for a sales invoice
    (suggestedOperationId in list_sales_invoices)."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/sales-invoices/{invoice_id}"
    invoice = await client.get(path)
    if not invoice.get("operationCandidate"):
        raise ValueError("Dougs suggests no operation for this invoice")
    updated = await client.post(f"{path}/actions/{action}-matched-operation", json={})
    return _invoice_row(updated or await client.get(path))


@mcp.tool(annotations=SENDS)
async def refuse_sales_invoice(
    invoice_id: InvoiceId,
    reason_code: Annotated[
        str, Field(description="Refusal reason code, e.g. 'TRANSAC_INC'; empty lists them.")
    ],
    message: Annotated[str, Field(max_length=2000, description="Comment sent with it.")],
) -> dict[str, Any]:
    """Refuse an electronic invoice wrongly addressed to you: Dougs cancels it and reports the
    refusal to its issuer."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/sales-invoices/{invoice_id}"
    invoice, reasons = await asyncio.gather(client.get(path), client.get(f"{path}/refusal-reasons"))
    if not (invoice.get("can") or {}).get("beRefused"):
        raise ValueError("this invoice cannot be refused")
    codes = {r.get("code"): r.get("label") for r in reasons or []}
    if reason_code not in codes:
        raise ValueError(f"reason_code must be one of {codes}")
    refused = await client.post(
        path,
        json={
            "paymentStatus": "canceled",
            "refusalReasonCode": reason_code,
            "refusalMessage": message,
        },
    )
    return _invoice_row(refused)


@mcp.tool(annotations=DESTRUCTIVE)
async def set_quote_status(
    quote_id: QuoteId,
    status: Annotated[
        Literal["accepted", "refused", "draft"],
        Field(description="accepted or refused by the client; draft reopens it for editing."),
    ],
) -> dict[str, Any]:
    """Record a finalized quote as accepted or refused (final), or reopen it as a draft."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/invoicing/quotes/{quote_id}"
    current = (await client.get(path)).get("status")
    if current == "DRAFT":
        raise ValueError("this quote is a draft: finalize it first with finalize_sales_document")
    if status != "draft" and current != "PENDING":
        raise ValueError(f"only a pending quote can be {status}; this one is {current}")
    quote = await client.put(path, json={"status": status.upper()})
    return _quote_url(client, cid, quote)


@mcp.tool(annotations=DESTRUCTIVE)
async def create_invoice_from_quote(quote_id: QuoteId) -> dict[str, Any]:
    """Turn a finalized quote into an invoice draft with its lines; the quote is marked
    accepted."""
    client = get_client()
    cid = await client.resolve_company_id()
    path = f"/companies/{cid}/invoicing/quotes/{quote_id}"
    if (await client.get(path)).get("status") == "DRAFT":
        raise ValueError("this quote is a draft: finalize it first with finalize_sales_document")
    draft = await client.post(f"{path}/actions/create-invoice", json={})
    return _document(client, cid, draft, "sales-invoice")


# --- Invoice products --------------------------------------------------------------


def _product_row(p: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "id": p.get("id"),
            "title": p.get("title"),
            "description": p.get("description"),
            "reference": p.get("reference"),
            "unitAmount": p.get("amount"),
            "unit": p.get("unit"),
            "vatRate": p.get("vatRate"),
        }
    )


ProductId = Annotated[UUID, Field(description="Product id from list_invoice_products.")]
ProductUnit = Annotated[str, Field(description="e.g. 'jour', 'heure', 'unité'.")]
ProductVat = Annotated[float | None, Field(ge=0, le=1, description="0.2 = 20 %, 0 = exempt.")]
ProductReference = Annotated[str, Field(description="Your product code.")]


@mcp.tool(annotations=READ)
async def list_invoice_products(
    search: Search = "", limit: Limit = 50, offset: Offset = 0
) -> dict[str, Any]:
    """List the company's saved products and services, with their unit price and VAT rate, to
    reuse as invoice or quote lines."""
    client = get_client()
    cid = await client.resolve_company_id()
    products = await client.get(
        f"/companies/{cid}/invoice-products",
        params={"search": search, "sortBy": "title", "limit": limit, "offset": offset},
    )
    return _page([_product_row(p) for p in products or []], limit, offset)


@mcp.tool(annotations=WRITE)
async def save_invoice_product(
    product_id: Annotated[
        UUID | None, Field(description="Product to change; omit to create one.")
    ] = None,
    title: Annotated[
        str, Field(max_length=255, description="Name printed on the line; required to create.")
    ] = "",
    unit_amount: Annotated[
        float | None, Field(ge=0, description="Unit price excl. VAT; required to create.")
    ] = None,
    unit: ProductUnit = "",
    vat_rate: Annotated[
        float | None, Field(ge=0, le=1, description="0.2 = 20 % (default), 0 = exempt.")
    ] = None,
    description: Annotated[str, Field(description="Detail printed under the title.")] = "",
    reference: ProductReference = "",
) -> dict[str, Any]:
    """Save a product or service to reuse on invoices and quotes, or change one (only the
    arguments passed change)."""
    client = get_client()
    cid = await client.resolve_company_id()
    changes = {
        "title": title,
        "amount": unit_amount,
        "unit": unit,
        "vatRate": vat_rate,
        "description": description,
        "reference": reference,
    }
    if product_id is None:
        if not title or unit_amount is None:
            raise ValueError("creating a product needs title and unit_amount")
        payload = {
            **changes,
            "unit": unit or "unité",
            "vatRate": 0.2 if vat_rate is None else vat_rate,
            "reference": reference or None,
        }
        return _product_row(await client.post(f"/companies/{cid}/invoice-products", json=payload))
    # The API has no single-product GET: find it in the list.
    products = await client.get(
        f"/companies/{cid}/invoice-products", params={"search": "", "limit": 1000}
    )
    current = next((p for p in products or [] if p.get("id") == str(product_id)), None)
    if current is None:
        raise ValueError(f"no product {product_id}; see list_invoice_products")
    current.update({k: v for k, v in changes.items() if v not in (None, "")})
    updated = await client.post(f"/companies/{cid}/invoice-products/{product_id}", json=current)
    return _product_row(updated)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_invoice_product(product_id: ProductId) -> dict[str, Any]:
    """Delete a saved product. Invoices and quotes that used it keep their lines."""
    client = get_client()
    cid = await client.resolve_company_id()
    await client.delete(f"/companies/{cid}/invoice-products/{product_id}")
    return {"deleted": str(product_id)}


# --- Sales channels ----------------------------------------------------------------


@mcp.tool(annotations=READ)
async def list_sales_channels() -> list[dict[str, Any]]:
    """List the sales channels declared to Dougs (website, marketplace, shop...) and the payment
    processors attached to each."""
    client = get_client()
    cid = await client.resolve_company_id()
    channels = await client.get(f"/companies/{cid}/salesChannels")
    return [
        compact(
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "type": c.get("type"),
                "category": c.get("category"),
                "shopUrl": c.get("shopUrl"),
                "startDate": _day(c.get("startDate")),
                "endDate": _day(c.get("endDate")),
                "sells": [
                    label
                    for key, label in [
                        ("salesServices", "services"),
                        ("sellsMerchandise", "merchandise"),
                        ("sellsFinishedProduct", "finished products"),
                    ]
                    if c.get(key)
                ],
                "paymentProcessors": [
                    (p.get("paymentsProcessor") or {}).get("name")
                    for p in c.get("paymentsProcessorsWithDate") or []
                ],
                "synchronizationStatus": c.get("synchronizationStatus"),
            }
        )
        for c in channels or []
    ]
