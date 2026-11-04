"""Account, companies and bank accounts."""

from typing import Any

from .server import get_client, mcp


@mcp.tool()
async def get_me() -> dict[str, Any]:
    """Return the authenticated Dougs user profile (id, email, role, company ids)."""
    return await get_client().get("/users/me")


@mcp.tool()
async def list_companies() -> list[dict[str, Any]]:
    """List the companies accessible to the authenticated user."""
    return await get_client().get("/users/me/companies")


@mcp.tool()
async def get_company(company_id: int | None = None) -> dict[str, Any]:
    """Return a company's detailed accounting configuration (VAT, legal form, regimes).

    Defaults to the user's preferred company when company_id is omitted.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}")


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
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    accounts = await client.get(f"/companies/{cid}/treasury-accounts")
    result = []
    for a in accounts:
        if a.get("hidden") and not include_hidden:
            continue
        result.append({**a, "filterValue": f"{a.get('type')}:{a.get('id')}"})
    return result
