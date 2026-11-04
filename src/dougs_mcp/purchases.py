"""Purchase side: vendor invoices and partners."""

from typing import Any

from .server import get_client, mcp


@mcp.tool()
async def list_vendor_invoices(company_id: int | None = None) -> list[dict[str, Any]]:
    """List vendor (supplier) invoices: supplier, amount, VAT, payable amount, status."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/vendor-invoices")


@mcp.tool()
async def list_partners(company_id: int | None = None) -> Any:
    """List the company's partners (customers and suppliers)."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/partners")
