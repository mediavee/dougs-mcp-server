"""FastMCP server exposing read-only Dougs accounting tools."""

import asyncio
import mimetypes
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .client import MAX_PAGE, DougsClient
from .config import load_settings

mcp = FastMCP("dougs")

# Accounting stats exposed under /companies/{id}/stats/{type} (all take accountingYearId).
STAT_TYPES = (
    "chiffre-d-affaires",
    "compte-de-resultat",
    "resultat-d-exploitation",
    "charges-d-exploitation",
    "repartition-des-charges",
    "tresorerie-compte-treso",
    "flux-de-tresorerie-compte-treso",
    "suivi-tva",
    "suivi-impots-societes",
    "suivi-cfe",
    "remunerations",
    "remunerations-for-accounting-year",
    "tns-social-charges",
    "fonds-propres",
    "comptes-d-associes",
    "autres-reserves-reports-a-nouveau",
)

_client: DougsClient | None = None


def _get_client() -> DougsClient:
    global _client
    if _client is None:
        _client = DougsClient(load_settings())  # raises if DOUGS_EMAIL/DOUGS_PASSWORD missing
    return _client


@mcp.tool()
async def get_me() -> dict[str, Any]:
    """Return the authenticated Dougs user profile (id, email, role, company ids)."""
    return await _get_client().get("/users/me")


@mcp.tool()
async def list_companies() -> list[dict[str, Any]]:
    """List the companies accessible to the authenticated user."""
    return await _get_client().get("/users/me/companies")


@mcp.tool()
async def get_company(company_id: int | None = None) -> dict[str, Any]:
    """Return a company's detailed accounting configuration (VAT, legal form, regimes).

    Defaults to the user's preferred company when company_id is omitted.
    """
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}")


def _iso_to_fr(d: str) -> str:
    """'2026-06-01' -> '01/06/2026' (the date format the Dougs API expects)."""
    parts = d.split("-")
    if len(parts) != 3:
        raise ValueError(f"date must be ISO 'YYYY-MM-DD', got '{d}'")
    year, month, day = parts
    return f"{day}/{month}/{year}"


def _date_range(date_from: str | None, date_to: str | None) -> str | None:
    if not date_from and not date_to:
        return None
    lo = _iso_to_fr(date_from) if date_from else "01/01/1970"
    hi = _iso_to_fr(date_to) if date_to else "31/12/2099"
    return f"{lo}-{hi}"


def _operation_filters(
    query: str | None,
    amount: str | None,
    date_from: str | None,
    date_to: str | None,
    inbound: bool | None,
    operation_type: str | None,
    treasury_account_id: str | None,
    validated: bool | None,
    needs_attention: bool | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if query:
        params["q"] = query
    if amount:
        params["q-amount"] = amount
    date = _date_range(date_from, date_to)
    if date:
        params["q-date"] = date
    if inbound is not None:
        params["q-isInbound"] = str(inbound).lower()
    if operation_type:
        params["type"] = operation_type
    if treasury_account_id:
        params["q-treasuryAccountId"] = treasury_account_id
    if validated is not None:
        params["validated"] = str(validated).lower()
    if needs_attention is not None:
        params["needsAttention"] = str(needs_attention).lower()
    return params


async def _attach_categories(
    client: DougsClient, company_id: int, operations: list[dict[str, Any]]
) -> None:
    """Mutate operations in place: attach a resolved `category` to each breakdown."""
    ids = {
        b["resolvedCategoryId"]
        for op in operations
        for b in (op.get("breakdowns") or [])
        if b.get("resolvedCategoryId") is not None
    }
    if not ids:
        return
    id_list = list(ids)
    cats = await asyncio.gather(
        *(client.category(company_id, i) for i in id_list), return_exceptions=True
    )
    labels: dict[int, dict[str, Any]] = {}
    for req_id, cat in zip(id_list, cats, strict=True):
        if isinstance(cat, dict):
            labels[req_id] = {
                "id": req_id,
                "wording": cat.get("wording"),
                "group": (cat.get("group") or {}).get("name"),
                "accountingNumber": cat.get("accountingNumber"),
            }
    for op in operations:
        for b in op.get("breakdowns") or []:
            label = labels.get(b.get("resolvedCategoryId"))
            if label:
                b["category"] = label


@mcp.tool()
async def list_operations(
    query: str | None = None,
    amount: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    inbound: bool | None = None,
    operation_type: str | None = None,
    treasury_account_id: str | None = None,
    validated: bool | None = None,
    needs_attention: bool | None = None,
    resolve_categories: bool = True,
    limit: int = 40,
    offset: int = 0,
    company_id: int | None = None,
) -> list[dict[str, Any]]:
    """List and search bank operations (transactions), mirroring the Dougs UI.

    Filters:
      - query: full-text search over wording / memo / amount.
      - amount: amount filter using operators — ">1000", "<500", or a range
        "1000-2000". (Use a range for a between; the "&&" UI syntax is not
        API-safe.)
      - date_from / date_to: ISO dates (YYYY-MM-DD); either or both. Filters on
        the operation date.
      - inbound: True = incoming money (entrées), False = outgoing (sorties).
      - operation_type: exact match on the operation's technical type (its
        nature, not the accounting category). Observed values: "bank" (most),
        "dispatch:ecommerce:salesChannel", "dispatch:ecommerce",
        "miscellaneous:manual", "expense".
      - treasury_account_id: restrict to a bank account, e.g.
        "synchronizedAccount:131937" (see list_bank_accounts -> filterValue).
      - validated: only validated (True) or pending (False) operations.
      - needs_attention: only operations flagged as needing attention.

    Paginate with limit (1-500, the server-side cap) and offset. Defaults to
    the preferred company.

    By default each breakdown is enriched with a resolved `category`
    (wording / group / accountingNumber); set resolve_categories=False for the
    raw, lighter payload. Attachments are under
    sourceDocumentAttachments[].sourceDocument.file.url — pass that path to
    get_file_url to obtain a downloadable link.
    """
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    params = _operation_filters(
        query,
        amount,
        date_from,
        date_to,
        inbound,
        operation_type,
        treasury_account_id,
        validated,
        needs_attention,
    )
    params["limit"] = max(1, min(limit, MAX_PAGE))
    params["offset"] = offset
    operations = await client.get(f"/companies/{cid}/operations", params=params)
    if resolve_categories:
        await _attach_categories(client, cid, operations)
    return operations


@mcp.tool()
async def get_categories(
    category_ids: list[int],
    company_id: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve accounting category ids to human-readable labels.

    Operations carry their accounting categorization as ids in
    breakdowns[].resolvedCategoryId (and the hierarchy in resolvedCategoryPath).
    Pass those ids here to get each category's wording (label), accounting
    group, accounting number and description — e.g. 24 -> "Agios, intérêts des
    emprunts" (group "Banque & Assurance", account 661100).
    """
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    cats = await asyncio.gather(*(client.category(cid, i) for i in category_ids))
    return [
        {
            "id": c.get("id"),
            "wording": c.get("wording"),
            "group": (c.get("group") or {}).get("name"),
            "accountingNumber": c.get("accountingNumber"),
            "description": c.get("description"),
        }
        for c in cats
    ]


@mcp.tool()
async def list_sales_channels(company_id: int | None = None) -> list[dict[str, Any]]:
    """List the sales channels configured for a company."""
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/salesChannels")


@mcp.tool()
async def get_active_accounting_year(company_id: int | None = None) -> dict[str, Any]:
    """Return the company's currently active accounting year (id, opening/closing dates)."""
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/accounting-years/active")


@mcp.tool()
async def list_accounting_years(company_id: int | None = None) -> list[dict[str, Any]]:
    """List all accounting years (fiscal years) for a company."""
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/accounting-years")


@mcp.tool()
async def get_accounting_stat(
    stat_type: str,
    show_previous: bool = False,
    accounting_year_id: int | None = None,
    company_id: int | None = None,
) -> dict[str, Any]:
    """Return an aggregated accounting statistic for a fiscal year.

    stat_type is one of:
      chiffre-d-affaires (revenue), compte-de-resultat (income statement),
      resultat-d-exploitation, charges-d-exploitation, repartition-des-charges,
      tresorerie-compte-treso (cash), flux-de-tresorerie-compte-treso,
      suivi-tva (VAT tracking), suivi-impots-societes (corporate tax),
      suivi-cfe, remunerations, remunerations-for-accounting-year,
      tns-social-charges, fonds-propres, comptes-d-associes,
      autres-reserves-reports-a-nouveau.

    Defaults to the active accounting year. show_previous adds N-1 comparison
    (supported by revenue / result / charges stats).
    """
    if stat_type not in STAT_TYPES:
        raise ValueError(f"unknown stat_type '{stat_type}'. Valid: {', '.join(STAT_TYPES)}")
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    year_id = accounting_year_id or await client.active_accounting_year_id(cid)
    params: dict[str, Any] = {"accountingYearId": year_id}
    if show_previous:
        params["showPreviousAccountingYear"] = "true"
    return await client.get(f"/companies/{cid}/stats/{stat_type}", params=params)


@mcp.tool()
async def list_sales_invoices(company_id: int | None = None) -> list[dict[str, Any]]:
    """List customer (sales) invoices: client, amount, VAT, due date, payment status."""
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/sales-invoices")


@mcp.tool()
async def list_vendor_invoices(company_id: int | None = None) -> list[dict[str, Any]]:
    """List vendor (supplier) invoices: supplier, amount, VAT, payable amount, status."""
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/vendor-invoices")


@mcp.tool()
async def get_invoices_overview(company_id: int | None = None) -> dict[str, Any]:
    """Summary of sales and vendor invoices: counts and amounts (paid, waiting, late, draft)."""
    client = _get_client()
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


@mcp.tool()
async def get_product_catalog(company_id: int | None = None) -> Any:
    """Return the company's product/service catalog."""
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/product-catalog")


@mcp.tool()
async def list_partners(company_id: int | None = None) -> Any:
    """List the company's partners (customers and suppliers)."""
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/partners")


@mcp.tool()
async def list_bank_accounts(
    include_hidden: bool = False,
    company_id: int | None = None,
) -> list[dict[str, Any]]:
    """List the company's bank / treasury accounts (Qonto, PayPal, Stripe, …).

    Returns each account's id, name, type, balance, etc. Each entry also gets a
    `filterValue` (e.g. "synchronizedAccount:131937") ready to pass to
    list_operations(treasury_account_id=...). Hidden accounts are excluded
    unless include_hidden=True.
    """
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    accounts = await client.get(f"/companies/{cid}/treasury-accounts")
    result = []
    for a in accounts:
        if a.get("hidden") and not include_hidden:
            continue
        result.append({**a, "filterValue": f"{a.get('type')}:{a.get('id')}"})
    return result


@mcp.tool()
async def get_file_url(path: str) -> dict[str, str]:
    """Resolve a Dougs file path to a directly-downloadable URL.

    Pass a path like '/files/...' taken from an operation's
    sourceDocumentAttachments[].sourceDocument.file.url, or a sales/vendor
    invoice filePath. Returns the signed S3 URL (publicly downloadable, expires).
    """
    url = await _get_client().resolve_file_url(path)
    return {"url": url}


@mcp.tool()
async def validate_operation(
    operation_id: int,
    validated: bool = True,
    company_id: int | None = None,
) -> dict[str, Any]:
    """Validate (or un-validate) a bank operation.

    Validation confirms the operation's accounting categorization (its
    breakdowns). The operation should already be categorized — validating an
    uncategorized one files it under "Non catégorisé". Pass validated=False to
    re-open a validated operation. Returns the updated operation.
    """
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    operation = await client.get(f"/companies/{cid}/operations/{operation_id}")
    operation["validated"] = validated
    return await client.post(f"/companies/{cid}/operations/{operation_id}", json=operation)


def _read_upload(file_path: str) -> tuple[str, bytes, str]:
    """Read a local file off the event loop; returns (name, bytes, content_type)."""
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValueError(f"file not found: {file_path}")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.name, path.read_bytes(), content_type


@mcp.tool()
async def add_attachment(
    operation_id: int,
    file_path: str,
    company_id: int | None = None,
) -> dict[str, Any]:
    """Attach a supporting document (receipt/invoice file) to an operation.

    file_path is a path to a local file (PDF or image). Returns the updated
    operation; the new attachment is in sourceDocumentAttachments (its `id` is
    what remove_attachment needs).
    """
    name, content, content_type = await asyncio.to_thread(_read_upload, file_path)
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.post(
        f"/companies/{cid}/operations/{operation_id}/source-document-attachments/actions/create-from-formdata",
        files={"file": (name, content, content_type)},
    )


@mcp.tool()
async def remove_attachment(
    operation_id: int,
    attachment_id: int,
    company_id: int | None = None,
) -> Any:
    """Remove an attachment from an operation by attachment id.

    The attachment id is sourceDocumentAttachments[].id on the operation.
    """
    client = _get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.delete(
        f"/companies/{cid}/operations/{operation_id}/source-document-attachments/{attachment_id}"
    )


@mcp.tool()
async def raw_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Low-level authenticated GET on the Dougs API (read-only).

    Use for endpoints not yet wrapped, e.g. '/companies/12345/invoices'.
    Path is relative to the API base URL.
    """
    if not path.startswith("/"):
        path = "/" + path
    return await _get_client().get(path, params=params)
