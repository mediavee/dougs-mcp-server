"""Accounting years, aggregated statistics, time series, assets, loans and tax filings."""

import asyncio
from datetime import date
from typing import Any

from .server import get_client, mcp

# Accounting stats exposed under /companies/{id}/stats/{type} (all take accountingYearId).
STAT_TYPES = (
    "chiffre-d-affaires",
    "compte-de-resultat",
    "resultat-d-exploitation",
    "charges-d-exploitation",
    "repartition-des-charges",
    "tresorerie",
    "tresorerie-compte-treso",
    "flux-de-tresorerie",
    "flux-de-tresorerie-compte-treso",
    "suivi-tva",
    "suivi-impots-societes",
    "suivi-cfe",
    "remunerations",
    "remunerations-for-accounting-year",
    "tns-social-charges",
    "social-charges",
    "indemnite-kilometrique",
    "compte-de-l-exploitant",
    "compte-de-debours",
    "comptes-de-filiale",
    "fonds-propres",
    "comptes-d-associes",
    "autres-reserves-reports-a-nouveau",
)


@mcp.tool()
async def get_active_accounting_year(company_id: int | None = None) -> dict[str, Any]:
    """Return the company's currently active accounting year (id, opening/closing dates)."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    return await client.get(f"/companies/{cid}/accounting-years/active")


@mcp.tool()
async def list_accounting_years(company_id: int | None = None) -> list[dict[str, Any]]:
    """List all accounting years (fiscal years) for a company."""
    client = get_client()
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

    These are year-scoped aggregates. For trends over several years, use
    list_metrics / get_metrics, which expose monthly time series.
    """
    if stat_type not in STAT_TYPES:
        raise ValueError(f"unknown stat_type '{stat_type}'. Valid: {', '.join(STAT_TYPES)}")
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    year_id = accounting_year_id or await client.active_accounting_year_id(cid)
    params: dict[str, Any] = {"accountingYearId": year_id}
    if show_previous:
        params["showPreviousAccountingYear"] = "true"
    return await client.get(f"/companies/{cid}/stats/{stat_type}", params=params)


def _period_start(end_date: str, group: str) -> date:
    """First day of the period a series point covers (points are dated on its last day)."""
    end = date.fromisoformat(end_date)
    if group == "year":
        return end.replace(month=1, day=1)
    if group == "quarter":
        return end.replace(month=end.month - (end.month - 1) % 3, day=1)
    return end.replace(day=1)


@mcp.tool()
async def list_metrics(
    search: str | None = None,
    company_id: int | None = None,
) -> list[dict[str, Any]]:
    """List the company's accounting time series, for trend analysis.

    Dougs keeps monthly series going back to the company's first year (revenue,
    expenses, income-statement lines, cash, shareholder accounts). Each entry
    gives the `name` to pass to get_metrics, a human label, and the accounting
    accounts it aggregates.

    - search: filters on the name and the label (case-insensitive).
    - format: "delta" = the amount for each period, "accumulation" = running
      total. get_metrics can return either regardless of this default.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    catalog = await client.metric_catalog(cid)
    needle = (search or "").casefold()
    result = [
        {
            "name": m.get("name"),
            "label": m.get("label"),
            "format": m.get("format"),
            "accountNumberRanges": (m.get("metadata") or {}).get("accountNumberRanges"),
        }
        for m in catalog.values()
        if not needle
        or needle in (m.get("name") or "").casefold()
        or needle in (m.get("label") or "").casefold()
    ]
    result.sort(key=lambda m: m["name"] or "")
    return result


@mcp.tool()
async def get_metrics(
    names: list[str],
    group: str = "month",
    cumulative: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    include_future: bool = False,
    company_id: int | None = None,
) -> dict[str, Any]:
    """Return the values of one or more accounting time series.

    Pass names from list_metrics (e.g. ["accounting.chiffre-d-affaires",
    "accounting.charges-d-exploitation"]) — requesting several at once is the
    way to compare them over the same period.

    - group: "month" (default), "quarter" or "year".
    - cumulative: True for a running total instead of the per-period amount.
    - date_from / date_to: ISO dates (YYYY-MM-DD) bounding the range; the full
      history is returned when omitted.

    Each series comes back as points of {date, value}, dated on the last day of
    the period. Amounts are in euros.

    Dougs pads its series with zero-valued periods beyond today; those are
    dropped unless include_future=True, so a trend never reads as a collapse to
    zero. The last point may still be a period in progress (`partial: true`).
    """
    if group not in ("month", "quarter", "year"):
        raise ValueError(f"group must be month, quarter or year; got '{group}'")
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    catalog = await client.metric_catalog(cid)
    unknown = [n for n in names if n not in catalog]
    if unknown:
        raise ValueError(f"unknown metric(s) {unknown}; see list_metrics for valid names")
    params: dict[str, Any] = {
        "group": group,
        "format": "accumulation" if cumulative else "delta",
        "fill": "forward",
    }
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    values = await asyncio.gather(
        *(
            client.get(f"/companies/{cid}/stats/series/{catalog[n]['id']}/values", params=params)
            for n in names
        )
    )
    today = date.today()
    result: dict[str, Any] = {}
    for name, points in zip(names, values, strict=True):
        kept = []
        for p in points or []:
            day = p["date"][:10]
            if not include_future and _period_start(day, group) > today:
                continue
            kept.append({"date": day, "value": p["value"]})
        if kept and date.fromisoformat(kept[-1]["date"]) > today:
            kept[-1]["partial"] = True
        result[name] = {
            "label": catalog[name].get("label"),
            "group": group,
            "cumulative": cumulative,
            "points": kept,
        }
    return result


@mcp.tool()
async def list_investments(company_id: int | None = None) -> dict[str, Any]:
    """List the company's fixed assets (immobilisations) and their depreciation.

    Returns each asset with its purchase amount and date, depreciation method
    and duration, how much is already written off, and the sale date if it was
    disposed of — plus portfolio totals.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    investments, stats = await asyncio.gather(
        client.get(f"/companies/{cid}/investments"),
        client.get(f"/companies/{cid}/investments/stats"),
    )
    return {
        "stats": stats,
        "investments": [
            {
                "id": i.get("id"),
                "name": i.get("name"),
                "categoryId": i.get("categoryId"),
                "purchaseDate": i.get("purchaseDate"),
                "purchaseAmount": i.get("purchaseAmount"),
                "amortizableAmount": i.get("amortizableAmount"),
                "amortizationType": i.get("amortizationType"),
                "amortizationYearCount": i.get("amortizationYearCount"),
                "previousYearsAmortizedAmount": i.get("previousYearsAmortizedAmount"),
                "currentYearAmortizedAmount": i.get("currentYearAmortizedAmount"),
                "saleDate": i.get("saleDate"),
                "isDraft": i.get("isDraft"),
            }
            for i in investments or []
        ],
    }


@mcp.tool()
async def list_loans(company_id: int | None = None) -> list[dict[str, Any]]:
    """List the company's loans: amount, start date, duration, rate."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    loans = await client.get(f"/companies/{cid}/loans")
    return [
        {
            "id": loan.get("id"),
            "name": loan.get("name"),
            "description": loan.get("description"),
            "amount": loan.get("amount"),
            "startDate": loan.get("startDate"),
            "monthlyDuration": loan.get("monthlyDuration"),
            "fixedRate": loan.get("fixedRate"),
            "isVariableRate": loan.get("isVariableRate"),
            "accountingNumber": loan.get("accountingNumber"),
        }
        for loan in loans or []
    ]


@mcp.tool()
async def list_declarations(
    declaration_type: str | None = None,
    limit: int = 40,
    company_id: int | None = None,
) -> list[dict[str, Any]]:
    """List the company's tax filings (VAT, corporate tax, CFE), most recent first.

    Covers what was declared, for which period, the amount due and whether it
    is confirmed — useful to reconcile the tax burden over time.
    declaration_type filters on the technical type (e.g. "CA3-2025", "CFE").

    The full filing payload (every form line) is deliberately left out; fetch it
    with raw_get on /companies/{id}/declarations/{declarationId} if needed.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    declarations = await client.get(f"/companies/{cid}/declarations")
    rows = [
        {
            "id": d.get("id"),
            "type": d.get("type"),
            "label": d.get("label"),
            "group": d.get("group"),
            "periodStartDate": d.get("periodStartDate"),
            "periodEndDate": d.get("periodEndDate"),
            "paymentAmount": d.get("paymentAmount"),
            "paidAmount": d.get("paidAmount"),
            "confirmedAt": d.get("confirmedAt"),
            "skipped": d.get("skipped"),
        }
        for d in declarations or []
        if not declaration_type or d.get("type") == declaration_type
    ]
    rows.sort(key=lambda d: d["periodEndDate"] or "", reverse=True)
    return rows[:limit]
