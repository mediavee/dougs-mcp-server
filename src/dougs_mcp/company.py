"""Account and company: user, company settings, people, bank accounts and connections, to-dos, billing."""

import asyncio
from typing import Annotated, Any

from pydantic import Field

from .server import READ, compact, get_client, mcp

APP_URL = "https://app.dougs.fr"

# Labels from the Dougs app's VAT settings screen; "standardQuaterly" is Dougs' spelling.
VAT_TYPES = {
    "exemption": "VAT exemption (franchise en base)",
    "simplified": "simplified regime, annual return (CA12)",
    "standardMonthly": "standard regime, monthly return (CA3)",
    "standardQuaterly": "standard regime, quarterly return (CA3)",
}

TAX_REGIMES = {
    "is": "corporate income tax (IS)",
    "ir_2031": "income tax, BIC real regime (2031)",
    "ir_2035": "income tax, BNC controlled declaration (2035)",
    "ir_micro_bic": "income tax, micro-BIC",
    "ir_micro_bnc": "income tax, micro-BNC",
}


def _day(value: str | None) -> str | None:
    """'2026-11-21T00:00:00.000Z' -> '2026-11-21'."""
    return value[:10] if value else None


@mcp.tool(annotations=READ)
async def get_account() -> dict[str, Any]:
    """Return who is logged in and the companies they can act on; every tool acts on the one
    flagged `isDefault` (switch with select_company). get_company gives its settings."""
    client = get_client()
    me, companies, default_id = await asyncio.gather(
        client.get("/users/me"),
        client.get("/users/me/companies"),
        client.resolve_company_id(),
    )
    profile = me.get("profile") or {}
    return compact(
        {
            "user": {
                "id": me.get("id"),
                "name": profile.get("fullName"),
                "email": me.get("email"),
                "role": me.get("role"),
            },
            "defaultCompanyId": default_id,
            "companies": [
                {
                    "id": c.get("id"),
                    "name": c.get("fullName"),
                    "legalForm": c.get("legalForm"),
                    "siren": c.get("siren"),
                    "isDefault": c.get("id") == default_id,
                }
                for c in companies
            ],
        }
    )


# Changes which company the session acts on, not any data.
@mcp.tool(annotations=READ)
async def select_company(
    company_id: Annotated[int, Field(description="A company id from get_account.")],
) -> dict[str, Any]:
    """Switch the company every other tool acts on, for the rest of the session."""
    client = get_client()
    companies = await client.get("/users/me/companies")
    known = {c.get("id"): c.get("fullName") for c in companies or []}
    if company_id not in known:
        raise ValueError(f"company {company_id} is not accessible; get_account lists {list(known)}")
    client.select_company(company_id)
    return {"companyId": company_id, "name": known[company_id]}


@mcp.tool(annotations=READ)
async def get_company() -> dict[str, Any]:
    """Return a company's identity and accounting settings: legal form, SIREN, activity, VAT
    regime and periodicity, tax regime, current fiscal year, lock date and Dougs plan."""
    client = get_client()
    cid = await client.resolve_company_id()
    c = await client.get(f"/companies/{cid}")
    vat = c.get("vatConfiguration") or {}
    vat_since = next(
        (
            v.get("startDate")
            for v in c.get("vatConfigurations") or []
            if (v.get("value") or {}).get("type") == vat.get("type")
        ),
        None,
    )
    year = c.get("accountingYear") or {}
    hq = c.get("headquarter") or {}
    legal_form = c.get("legalFormConfig") or {}
    config = c.get("accountingConfiguration") or {}
    return compact(
        {
            "id": c.get("id"),
            "name": c.get("fullName"),
            "legalName": c.get("legalName"),
            "brandName": c.get("brandName"),
            "legalForm": legal_form.get("label") or c.get("legalForm"),
            "siren": c.get("siren"),
            "vatNumber": c.get("vatNumber"),
            "activity": c.get("activity"),
            "ape": f"{hq['ape']} {hq.get('apeDescription') or ''}".strip()
            if hq.get("ape")
            else None,
            "headquarters": {
                "siret": hq.get("siret"),
                "address": ", ".join(
                    p
                    for p in (
                        hq.get("street"),
                        hq.get("additionalAddress"),
                        hq.get("zipcode"),
                        hq.get("city"),
                    )
                    if p
                ),
            },
            "registrationDate": c.get("registrationDate"),
            "shareCapital": c.get("shareCapital"),
            "totalShares": c.get("totalShares"),
            "hasEmployee": c.get("hasEmployee"),
            "taxRegime": {
                "code": c.get("taxRegime"),
                "label": TAX_REGIMES.get(c.get("taxRegime") or ""),
            },
            "vat": {
                "type": vat.get("type"),
                "label": VAT_TYPES.get(vat.get("type") or ""),
                "since": _day(vat_since),
                "declarationDay": c.get("vatDeclarationDay"),
                "oss": config.get("hasOss") or None,
                "ioss": config.get("hasIoss") or None,
            },
            "fiscalYear": {
                "id": year.get("id"),
                "openingDate": year.get("openingDate"),
                "closingDate": year.get("effectiveClosingDate") or year.get("closingDate"),
                "closed": year.get("closed"),
                "accountingType": year.get("accountingType"),
            },
            "lockDate": c.get("lockDate"),
            "plan": (c.get("subscription") or {}).get("plan"),
            "appUrl": f"{APP_URL}/companies/{cid}",
        }
    )


@mcp.tool(annotations=READ)
async def list_company_people() -> list[dict[str, Any]]:
    """List the people and companies holding a role in the company: officers (gérant,
    président), associates with their shares, and employees, with each role's dates."""
    client = get_client()
    cid = await client.resolve_company_id()
    partners = await client.get(f"/companies/{cid}/partners")
    rows = []
    for p in partners:
        roles = [
            {
                "role": pos.get("label"),
                "since": _day(pos.get("startDate")),
                "until": _day(pos.get("endDate")),
            }
            for pos in p.get("positions") or []
            if pos.get("isActive")
        ]
        person = p.get("naturalPerson") or p.get("legalPerson") or {}
        associate = (p.get("metadata") or {}).get("associate") or {}
        rows.append(
            compact(
                {
                    "id": p.get("id"),
                    "name": p.get("fullName"),
                    "type": p.get("type"),
                    "email": person.get("email"),
                    "siren": person.get("siren"),
                    "roles": roles,
                    "shares": associate.get("shareCount") if p.get("isAssociate") else None,
                    "hasAssociateAccount": p.get("hasAssociateAccount") or None,
                }
            )
        )
    return rows


@mcp.tool(annotations=READ)
async def list_bank_accounts(
    include_hidden: Annotated[
        bool, Field(description="Also return accounts hidden in the app.")
    ] = False,
) -> list[dict[str, Any]]:
    """List the company's bank and treasury accounts (Qonto, PayPal, Stripe…) with their balance.
    `filterValue` goes to list_operations(treasury_account_id=...); list_bank_connections tells
    whether their synchronization works."""
    client = get_client()
    cid = await client.resolve_company_id()
    accounts = await client.get(f"/companies/{cid}/treasury-accounts")
    return [
        compact(
            {
                "filterValue": f"{a.get('type')}:{a.get('id')}",
                "name": a.get("subName") or a.get("name"),
                "memo": a.get("memo"),
                "ledgerAccount": a.get("accountNumber"),
                "balance": a.get("balance"),
                "currency": a.get("originalCurrency"),
                "originalCurrencyBalance": a.get("originalCurrencyBalance")
                if a.get("originalCurrency")
                else None,
                "hidden": a.get("hidden") or None,
                "closedAt": _day(a.get("closedAt")),
            }
        )
        for a in accounts
        if include_hidden or not a.get("hidden")
    ]


@mcp.tool(annotations=READ)
async def list_bank_connections() -> list[dict[str, Any]]:
    """Report the health of each bank connection: status, the action the bank asks for (such as
    a strong authentication), when it last fetched data and imported transactions, and its
    accounts. Re-authenticating or forcing a sync stays a step in the Dougs app."""
    client = get_client()
    cid = await client.resolve_company_id()
    connections, accounts = await asyncio.gather(
        client.get(f"/companies/{cid}/connections"),
        client.get(f"/companies/{cid}/accounts"),
    )
    by_connection: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for account in accounts:
        for provider in account.get("providers") or []:
            if provider.get("connectionId") is not None:
                by_connection.setdefault(provider["connectionId"], []).append((account, provider))
    rows = []
    for conn in connections:
        links = by_connection.get(conn.get("id"), [])
        status = conn.get("status") or {}
        ok = status.get("code") == "ok"
        imports: list[str] = [
            p["transactionsDigestedAt"] for _, p in links if p.get("transactionsDigestedAt")
        ]
        rows.append(
            compact(
                {
                    "id": conn.get("id"),
                    "name": conn.get("name"),
                    "connector": conn.get("connectorId"),
                    "status": status.get("code"),
                    "problem": None if ok else status.get("userMessage") or status.get("message"),
                    "action": status.get("action"),
                    # Dougs keeps a replaced connection with every account link disabled.
                    "disabled": (bool(links) and all(p.get("disabledAt") for _, p in links))
                    or None,
                    "dataRefreshedAt": conn.get("dataRefreshedAt"),
                    "lastImportAt": max(imports) if imports else None,
                    "accounts": [
                        compact(
                            {
                                "filterValue": f"synchronizedAccount:{a.get('id')}",
                                "name": a.get("name"),
                                "disabledAt": _day(p.get("disabledAt")),
                            }
                        )
                        for a, p in links
                    ],
                }
            )
        )
    return rows


@mcp.tool(annotations=READ)
async def list_customer_todos() -> list[dict[str, Any]]:
    """List what Dougs is waiting for from the customer (documents to attach, a VAT return to
    complete, an account to regularize), with due dates and whether each is overdue. Completing
    or ignoring an item stays a step in the Dougs app."""
    client = get_client()
    cid = await client.resolve_company_id()
    todos, tasks = await asyncio.gather(
        client.get(f"/companies/{cid}/customer-todo"),
        client.get(f"/companies/{cid}/customer-tasks"),
    )
    # customer-tasks carries the real deadline and overdue flag; customer-todo the wording.
    task_by_id = {t.get("id"): t for t in tasks}
    rows = []
    for todo in todos:
        task = task_by_id.pop(todo.get("id"), None) or todo.get("task") or {}
        rows.append(
            compact(
                {
                    "id": todo.get("id"),
                    "code": todo.get("code"),
                    "title": todo.get("title"),
                    "detail": todo.get("description"),
                    "action": todo.get("ctaLabel"),
                    "steps": [
                        s.get("description") or s.get("title") for s in todo.get("subtasks") or []
                    ],
                    "startDate": task.get("startDate"),
                    "dueDate": task.get("dueDate") or _day(todo.get("dueDate")),
                    "overdue": task.get("overdue") or todo.get("overdue") or None,
                    "optional": todo.get("isOptional") or None,
                }
            )
        )
    rows.extend(
        compact(
            {
                "id": t.get("id"),
                "code": t.get("code"),
                "title": t.get("title"),
                "steps": [t.get("activeSubTaskTitle")],
                "startDate": t.get("startDate"),
                "dueDate": t.get("dueDate"),
                "overdue": t.get("overdue") or None,
            }
        )
        for t in task_by_id.values()
    )
    return rows


def _billing_row(b: dict[str, Any]) -> dict[str, Any]:
    file = (b.get("invoice") or {}).get("file") or {}
    return compact(
        {
            "id": b.get("id"),
            "number": b.get("number"),
            "date": b.get("date"),
            "label": "; ".join(i.get("description") or "" for i in b.get("items") or []),
            "amountExcludingTaxes": b.get("amountExcludingTaxes"),
            "amount": b.get("amount"),
            "paidAt": b.get("paidAt"),
            "unpaid": None if b.get("isPaid") or b.get("isDraft") else True,
            "filePath": file.get("url"),
        }
    )


@mcp.tool(annotations=READ)
async def list_billing_invoices(
    unpaid_only: Annotated[bool, Field(description="Only invoices not paid yet.")] = False,
    limit: Annotated[int, Field(ge=1, le=100, description="Invoices per page, newest first.")] = 12,
    offset: Annotated[int, Field(ge=0, description="Invoices to skip, for the next page.")] = 0,
) -> dict[str, Any]:
    """List the invoices Dougs bills the company for its subscription, newest first, with the
    upcoming one and the plan. Pass an invoice's `filePath` to get_file_url to download its PDF."""
    client = get_client()
    cid = await client.resolve_company_id()
    invoices, pending, company = await asyncio.gather(
        client.get(f"/companies/{cid}/billing-invoices"),
        client.get(f"/companies/{cid}/billing-invoices/pending"),
        client.get(f"/companies/{cid}"),
    )
    invoices = sorted(invoices, key=lambda b: (b.get("date") or "", b.get("id") or 0), reverse=True)
    if unpaid_only:
        invoices = [b for b in invoices if not b.get("isPaid") and not b.get("isDraft")]
    page = invoices[offset : offset + limit]
    sub = company.get("subscription") or {}
    result = compact(
        {
            "subscription": {
                "plan": sub.get("plan"),
                "nextBillingAt": _day(sub.get("nextBillingAt")),
                "paymentMethod": sub.get("paymentMethod"),
                "card": f"{sub['cardBrand']} ****{sub.get('cardLast4')} exp {sub.get('cardExpirationDate')}"
                if sub.get("cardBrand")
                else None,
                "cardExpired": sub.get("isCardExpired") or None,
                "unpaidSince": sub.get("isUnpaidSince"),
            },
            "upcoming": [_billing_row(b) for b in pending],
            "total": len(invoices),
            "invoices": [_billing_row(b) for b in page],
        }
    )
    if offset + limit < len(invoices):
        result["nextOffset"] = offset + limit
    return result
