"""Accounting years, aggregated statistics, time series, assets, loans and tax filings."""

import asyncio
import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from .client import DougsClient
from .server import READ, CompanyId, compact, get_client, mcp

YearId = Annotated[
    int | None,
    Field(
        description="Accounting year id from list_accounting_years; defaults to the active year."
    ),
]

StatType = Literal[
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
]

_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T")


async def _year_id(client: DougsClient, cid: int, year_id: int | None) -> int:
    return year_id or await client.active_accounting_year_id(cid)


def _files(attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Attachments as name + path for get_file_url."""
    return [
        {"name": (a.get("file") or {}).get("name"), "url": (a.get("file") or {}).get("url")}
        for a in attachments or []
    ]


@mcp.tool(annotations=READ)
async def list_accounting_years(company_id: CompanyId = None) -> list[dict[str, Any]]:
    """List the company's accounting (fiscal) years, the active one flagged `active`.

    Closed years carry their closing documents (balance sheet, FEC, final print), whose `url`
    goes to get_file_url.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    years, active = await asyncio.gather(
        client.get(f"/companies/{cid}/accounting-years"),
        client.get(f"/companies/{cid}/accounting-years/active"),
    )
    rows = []
    for y in years or []:
        metadata = y.get("metadata") or {}
        documents = [
            {"type": doc.get("type"), **_files([doc])[0]}
            for key in ("accountingYearBalanceSheets", "fecExports", "finalPrints")
            for doc in y.get(key) or []
        ]
        rows.append(
            {
                "id": y.get("id"),
                "openingDate": y.get("openingDate"),
                "closingDate": y.get("closingDate"),
                "active": y.get("id") == active.get("id") or None,
                "closed": y.get("closed"),
                "isLate": y.get("isLate") or None,
                "accountingType": y.get("accountingType"),
                "taxReportRegime": metadata.get("taxReportRegime"),
                "health": (metadata.get("reviewSummary") or {}).get("healthIndicator"),
                "documents": documents,
            }
        )
    return compact(rows)


def _compact_stat(value: Any, monthly: bool, last_day: str) -> Any:
    """Strip a /stats payload to its figures: dates as days, cents-level rounding, no UI flags."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _STAT_NOISE or key.startswith(("valuesCumulated", "monthlyProgression")):
                continue
            if key == "isTotal" and not item:
                continue
            if _is_series(item):
                if not monthly:
                    continue
                # Points beyond the year are zero padding.
                item = {p["date"][:10]: _round(p["value"]) for p in item if p["date"] <= last_day}
            else:
                item = _compact_stat(item, monthly, last_day)
            out[key] = item
        return out
    if isinstance(value, list):
        items = [_compact_stat(v, monthly, last_day) for v in value]
        # Income-statement and breakdown lines with nothing booked.
        return [i for i in items if not (isinstance(i, dict) and i.get("sum") == 0)]
    if isinstance(value, str) and _ISO_TIMESTAMP.match(value):
        return value[:10]
    return _round(value)


_STAT_NOISE = {
    "isAccountantOrAdmin",
    "isAnchored",
    "isSticky",
    "shouldDisplayZeroValue",
    "shouldBeCompared",
    "hint",
    "accountNumberRanges",
    "description",
}


def _is_series(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and isinstance(value[0], dict)
        and value[0].keys() == {"date", "value"}
    )


def _round(value: Any) -> Any:
    return round(value, 2) if isinstance(value, float) else value


@mcp.tool(annotations=READ)
async def get_accounting_stat(
    stat_type: Annotated[
        StatType,
        Field(
            description=(
                "chiffre-d-affaires = revenue, compte-de-resultat = income statement, "
                "charges-d-exploitation = operating expenses, repartition-des-charges = "
                "expenses by family, tresorerie-compte-treso = cash per bank account, "
                "flux-de-tresorerie = cash in/out, suivi-tva / suivi-impots-societes / "
                "suivi-cfe = VAT, corporate tax and CFE due, remunerations = pay vs social "
                "thresholds, fonds-propres = equity, comptes-d-associes = shareholder accounts."
            )
        ),
    ],
    accounting_year_id: YearId = None,
    show_previous: Annotated[
        bool, Field(description="Add the previous year's figures for comparison.")
    ] = False,
    monthly: Annotated[
        bool, Field(description="Include month-by-month values, keyed by month-end date.")
    ] = False,
    company_id: CompanyId = None,
) -> dict[str, Any]:
    """Return an aggregated figure of one accounting year (totals; monthly values on request).

    Income-statement lines carry a `seriesName` to pass to get_metrics for a multi-year trend.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    year_id = await _year_id(client, cid, accounting_year_id)
    params: dict[str, Any] = {"accountingYearId": year_id}
    if show_previous:
        params["showPreviousAccountingYear"] = "true"
    stat, year = await asyncio.gather(
        client.get(f"/companies/{cid}/stats/{stat_type}", params=params),
        client.get(f"/companies/{cid}/accounting-years/{year_id}"),
    )
    last_day = f"{year['closingDate']}T23:59:59.999Z"
    return compact(_compact_stat(stat, monthly, last_day))


@mcp.tool(annotations=READ)
async def get_category_breakdown(
    accounting_year_id: YearId = None,
    company_id: CompanyId = None,
) -> dict[str, Any]:
    """Return one accounting year's income and expenses per accounting category and account.

    Finer than get_accounting_stat's income statement: each family (sales, operating costs,
    personnel…) is split into the categories booked, with their account numbers; also gives the
    year's results (gross margin, EBITDA, operating, fiscal) and the growth against last year.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    year_id = await _year_id(client, cid, accounting_year_id)
    stats = await client.get(
        f"/companies/{cid}/accounting-stats", params={"accountingYearId": year_id}
    )
    year_stats = stats.get("accountingYearStats") or {}
    groups: dict[str, Any] = {}
    for name, group in (year_stats.get("cumulativeData") or {}).items():
        families = {
            key: {
                "label": family.get("label"),
                "amount": _round(family.get("amount")),
                "categories": [
                    {
                        "label": d.get("label"),
                        "accounts": d.get("accountingNumbers"),
                        "amount": _round(d.get("amount")),
                    }
                    for d in family.get("details") or []
                    if d.get("amount")
                ],
            }
            for key, family in (group.get("balances") or {}).items()
            if family.get("amount")
        }
        groups[name] = {
            "label": group.get("label"),
            "amount": _round(group.get("amount")),
            "families": families,
        }
    comparison = {
        key: {"label": v.get("label"), "amount": _round(v.get("amount"))}
        for key, v in year_stats.items()
        if isinstance(v, dict) and "amount" in v and v.get("amount") is not None
    }
    return compact({"accountingYearId": year_id, "totals": groups, "comparison": comparison})


def _period_start(end_date: str, group: str) -> date:
    """First day of the period a series point covers (points are dated on its last day)."""
    end = date.fromisoformat(end_date)
    if group == "year":
        return end.replace(month=1, day=1)
    if group == "quarter":
        return end.replace(month=end.month - (end.month - 1) % 3, day=1)
    return end.replace(day=1)


_METRIC_PREFIX = "accounting."


@mcp.tool(annotations=READ)
async def list_metrics(
    search: Annotated[
        str | None,
        Field(description="Case-insensitive filter on name and label, e.g. 'tresorerie'."),
    ] = None,
    include_accounts: Annotated[
        bool, Field(description="Add the account number ranges each series aggregates.")
    ] = False,
    company_id: CompanyId = None,
) -> list[dict[str, Any]]:
    """List the company's monthly accounting time series, from its first year to now.

    Revenue, expenses, income-statement lines, cash per account, shareholder accounts. Pass a
    `name` to get_metrics. `balance: true` marks a running balance rather than a flow.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    catalog = await client.metric_catalog(cid)
    needle = (search or "").casefold()
    rows = []
    for name, m in sorted(catalog.items()):
        label = m.get("label") or ""
        if needle and needle not in name.casefold() and needle not in label.casefold():
            continue
        row: dict[str, Any] = {"name": name.removeprefix(_METRIC_PREFIX), "label": label}
        if m.get("format") == "accumulation":
            row["balance"] = True
        if include_accounts:
            row["accounts"] = (m.get("metadata") or {}).get("accountNumberRanges")
        rows.append(row)
    return compact(rows)


@mcp.tool(annotations=READ)
async def get_metrics(
    names: Annotated[
        list[str],
        Field(
            description=(
                "Series names from list_metrics, e.g. ['chiffre-d-affaires', "
                "'charges-d-exploitation']; several at once compare them on the same periods."
            )
        ),
    ],
    group: Annotated[
        Literal["month", "quarter", "year"], Field(description="Period of each point.")
    ] = "month",
    cumulative: Annotated[
        bool, Field(description="Running total instead of the amount of each period.")
    ] = False,
    date_from: Annotated[
        str | None, Field(description="First day, YYYY-MM-DD; full history when omitted.")
    ] = None,
    date_to: Annotated[str | None, Field(description="Last day, YYYY-MM-DD.")] = None,
    include_future: Annotated[
        bool, Field(description="Keep the zero-valued periods Dougs pads after today.")
    ] = False,
    company_id: CompanyId = None,
) -> dict[str, Any]:
    """Return the values of accounting time series, in euros, for trends over several years.

    Each point is dated on the last day of its period; the last one may be a period in progress
    (`partial: true`).
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    catalog = await client.metric_catalog(cid)
    full = [n if n in catalog else _METRIC_PREFIX + n for n in names]
    unknown = [n for n, f in zip(names, full, strict=True) if f not in catalog]
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
            for n in full
        )
    )
    today = date.today()
    result: dict[str, Any] = {}
    for name, points in zip(full, values, strict=True):
        kept = []
        for p in points or []:
            day = p["date"][:10]
            if not include_future and _period_start(day, group) > today:
                continue
            kept.append({"date": day, "value": _round(p["value"])})
        if kept and date.fromisoformat(kept[-1]["date"]) > today:
            kept[-1]["partial"] = True
        result[name.removeprefix(_METRIC_PREFIX)] = {
            "label": catalog[name].get("label"),
            "points": kept,
        }
    return result


@mcp.tool(annotations=READ)
async def list_investments(company_id: CompanyId = None) -> dict[str, Any]:
    """List the company's fixed assets (immobilisations) and their depreciation, with totals.

    Each asset gives its purchase, depreciation plan, amounts already written off and the sale
    date if it was disposed of.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    investments, stats = await asyncio.gather(
        client.get(f"/companies/{cid}/investments"),
        client.get(f"/companies/{cid}/investments/stats"),
    )
    rows = [
        {
            "id": i.get("id"),
            "name": i.get("name"),
            "category": (i.get("categoryData") or {}).get("wording"),
            "account": (i.get("categoryData") or {}).get("accountingNumber"),
            "purchaseDate": i.get("purchaseDate"),
            "purchaseAmount": i.get("purchaseAmount"),
            "amortizableAmount": i.get("amortizableAmount"),
            "amortizationType": i.get("amortizationType"),
            "amortizationYears": i.get("amortizationYearCount"),
            "previousYearsAmortized": _round(i.get("previousYearsAmortizedAmount")),
            "currentYearAmortized": _round(i.get("currentYearAmortizedAmount")),
            "saleDate": i.get("saleDate"),
            "draft": i.get("isDraft") or None,
            "files": _files(i.get("attachments")),
        }
        for i in investments or []
    ]
    return compact({"totals": {k: _round(v) for k, v in stats.items()}, "investments": rows})


@mcp.tool(annotations=READ)
async def list_loans(company_id: CompanyId = None) -> list[dict[str, Any]]:
    """List the company's loans: amount, start date, duration in months, rate, contract files."""
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    loans = await client.get(f"/companies/{cid}/loans")
    return compact(
        [
            {
                "id": loan.get("id"),
                "name": loan.get("name"),
                "amount": loan.get("amount"),
                "startDate": loan.get("startDate"),
                "months": loan.get("monthlyDuration"),
                "fixedRate": loan.get("fixedRate"),
                "variableRate": loan.get("isVariableRate") or None,
                "account": loan.get("accountingNumber"),
                "files": _files(loan.get("attachments")),
            }
            for loan in loans or []
        ]
    )


# Dougs' filing statuses, by the name the tools expose.
_DECLARATION_STATUS = {
    "to_complete": "ready_to_complete",
    "upcoming": "upcoming",
    "completed": "completed",
}


def _declaration_row(d: dict[str, Any], status: str | None) -> dict[str, Any]:
    steps = ((d.get("metadata") or {}).get("depositSummary") or {}).get("steps") or []
    return {
        "id": d.get("id"),
        "label": (d.get("label") or "").strip(),
        "type": d.get("type"),
        "status": status,
        "periodStart": d.get("periodStartDate"),
        "periodEnd": d.get("periodEndDate"),
        "dueDate": d.get("dueDate"),
        # Negative = to pay, positive = credit.
        "amount": d.get("summaryAmount"),
        "amountNote": (d.get("amountHelpText") or "").strip("()")
        if d.get("summaryAmount") is not None
        else None,
        "paymentAmount": d.get("paymentAmount"),
        "paidAmount": d.get("paidAmount"),
        "confirmedAt": (d.get("confirmedAt") or "")[:10],
        "transmission": steps[-1].get("name") if steps else None,
        "late": d.get("isLate") or None,
        "skipped": d.get("skipped") or None,
    }


@mcp.tool(annotations=READ)
async def list_declarations(
    status: Annotated[
        Literal["to_complete", "upcoming", "completed"] | None,
        Field(
            description=(
                "to_complete = open now and waiting for the company, upcoming = not open yet, "
                "completed = filed. All when omitted."
            )
        ),
    ] = None,
    declaration_type: Annotated[
        str | None,
        Field(description="Form type, e.g. 'CA3-2026' (VAT), '2571' (IS instalment), 'CFE'."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Rows to return.")] = 30,
    offset: Annotated[int, Field(ge=0, description="Rows to skip, for the next page.")] = 0,
    company_id: CompanyId = None,
) -> dict[str, Any]:
    """List the company's tax filings (VAT, corporate tax, CFE, accounts approval), latest due first.

    Gives the period, due date, amount and filing state of each; get_declaration returns the
    figures of one filing.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    statuses = [status] if status else list(_DECLARATION_STATUS)
    pages = await asyncio.gather(
        *(
            client.get(
                f"/companies/{cid}/declarations/list-declarations",
                # Dougs ignores the type filter for customers: filter here.
                params={"status": _DECLARATION_STATUS[s], "limit": 500},
            )
            for s in statuses
        )
    )
    rows = [
        _declaration_row(d, s)
        for s, page in zip(statuses, pages, strict=True)
        for d in page or []
        if not declaration_type or d.get("type") == declaration_type
    ]
    rows.sort(key=lambda r: r["dueDate"] or "", reverse=True)
    page_rows = rows[offset : offset + limit]
    result: dict[str, Any] = {"total": len(rows), "declarations": compact(page_rows)}
    if offset + limit < len(rows):
        result["next"] = f"offset={offset + limit}"
    return result


# Item labels per form type, from /declaration-templates (1+ MB each, immutable).
_template_labels: dict[str, dict[str, str]] = {}


async def _form_labels(client: DougsClient, form_type: str) -> dict[str, str]:
    labels = _template_labels.get(form_type)
    if labels is None:
        template = await client.get(f"/declaration-templates/{form_type}")
        labels = {
            i["name"]: i.get("label") or ""
            for i in template.get("items") or []
            if not i.get("isGroup") and not i.get("isInternal")
        }
        _template_labels[form_type] = labels
    return labels


@mcp.tool(annotations=READ)
async def get_declaration(
    declaration_id: Annotated[int, Field(description="Filing id from list_declarations.")],
    company_id: CompanyId = None,
) -> dict[str, Any]:
    """Return one tax filing with its non-zero form boxes (box code, label, amount) and files.

    Confirming or correcting a filing stays a step in the Dougs app.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    d = await client.get(f"/companies/{cid}/declarations/{declaration_id}")
    labels = await _form_labels(client, d["type"])
    item_data = d.get("itemData") or {}
    boxes = [
        {"box": name, "label": labels[name], "value": _round(value)}
        for name, value in (d.get("form") or {}).items()
        if name in labels
        and isinstance(value, int | float)
        and not isinstance(value, bool)
        and value
        and (item_data.get(name) or {}).get("isVisible", True)
        and not (item_data.get(name) or {}).get("isInternal")
    ]
    files = [
        {"type": key, **f}
        for key, value in d.items()
        if ":" in key and isinstance(value, list)
        for f in _files(value)
    ]
    row = _declaration_row(d, None)
    return compact(
        {
            **row,
            "boxes": boxes,
            "files": files,
        }
    )


@mcp.tool(annotations=READ)
async def get_company_summary(
    accounting_year_id: YearId = None,
    company_id: CompanyId = None,
) -> dict[str, Any]:
    """Return the accountant's summary of the company for one year.

    Legal form, tax and VAT regimes with their history, capital, partners and employees,
    activity, and the Kbis and bylaws files.
    """
    client = get_client()
    cid = await client.resolve_company_id(company_id)
    year_id = await _year_id(client, cid, accounting_year_id)
    s = await client.get(f"/companies/{cid}/accounting-years/{year_id}/company-summary")
    return compact(
        {
            "title": s.get("title"),
            "description": (s.get("description") or {}).get("data"),
            "people": [p.get("name") + ": " + p.get("label", "") for p in _data(s, "partners")],
            "vehicles": _data(s, "vehicles"),
            "activity": _data(s, "activity"),
            "files": [
                {
                    "type": group.get("type"),
                    "name": f.get("name"),
                    "url": (f.get("url") or "").removeprefix(client.base_url),
                }
                for group in _data(s, "files")
                for f in group.get("files") or []
            ],
        }
    )


def _data(summary: dict[str, Any], section: str) -> list[Any]:
    return (summary.get(section) or {}).get("data") or []
