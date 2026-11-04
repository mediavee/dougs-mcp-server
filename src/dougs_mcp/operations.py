"""Bank operations: search, categorization, VAT, splits, validation, receipts and expense reports."""

import asyncio
import mimetypes
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from .client import DougsClient
from .server import DESTRUCTIVE, READ, WRITE, compact, get_client, mcp

OperationId = Annotated[int, Field(description="Operation id, from list_operations.")]
BreakdownId = Annotated[
    int, Field(description="Breakdown (accounting line) id, from get_operation's breakdowns[].id.")
]
Query = Annotated[
    str | None, Field(description="Full-text search over wording, memo and amount, e.g. 'OVH'.")
]
AmountFilter = Annotated[
    str | None,
    Field(description="Amount in euros: '>1000', '<500' or a range '1000-2000'."),
]
DateFrom = Annotated[str | None, Field(description="Earliest operation date, 'YYYY-MM-DD'.")]
DateTo = Annotated[str | None, Field(description="Latest operation date, 'YYYY-MM-DD'.")]
Inbound = Annotated[
    bool | None, Field(description="True: money in only; False: money out only; omit for both.")
]
OperationType = Annotated[
    str | None,
    Field(
        description="Exact operation nature: 'bank' (bank transactions), 'expense' (expense "
        "reports), 'cashPayment', 'dispatch:ecommerce', 'dispatch:ecommerce:salesChannel', "
        "'miscellaneous:manual' (accountant entries)."
    ),
]
TreasuryAccount = Annotated[
    str | None,
    Field(
        description="Bank account, as list_bank_accounts' filterValue, e.g. "
        "'synchronizedAccount:131937'."
    ),
]
Validated = Annotated[
    bool | None,
    Field(description="True: validated only; False: still to validate only; omit for both."),
]
NeedsAttention = Annotated[
    bool | None, Field(description="True: only operations the app flags as needing attention.")
]

# Operation kinds a customer creates by hand in the app, hence the only ones delete_operation
# accepts; bank transactions come from the bank synchronization.
_HAND_MADE_TYPES = {"expense", "cashPayment", "cashExpense", "kilometricIndemnity"}

# Dougs caps operation counts at this value and returns it for anything above.
_COUNT_CAP = 100


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
    return params


async def _account_labels(client: DougsClient, company_id: int) -> dict[int, str]:
    accounts = await client.get(f"/companies/{company_id}/treasury-accounts")
    # subName carries a masked account number suffix ("Compte principal ****"): drop it.
    return {a["id"]: (a.get("subName") or a.get("name") or "").rstrip("* ") for a in accounts}


def _signed(op: dict[str, Any]) -> float | None:
    amount = op.get("amount")
    if amount is None:
        return None
    return amount if op.get("isInbound") else -amount


def _main_lines(op: dict[str, Any]) -> list[dict[str, Any]]:
    """The categorizable lines: everything but the bank-side counterpart and fees."""
    return [
        b for b in op.get("breakdowns") or [] if not b.get("isCounterpart") and not b.get("isFee")
    ]


def _operation_row(op: dict[str, Any], accounts: dict[int, str]) -> dict[str, Any]:
    account_id = (op.get("transaction") or {}).get("accountId")
    categories = list(dict.fromkeys(b.get("categoryWording") for b in _main_lines(op)))
    has_receipt = bool(op.get("sourceDocumentAttachments"))
    return compact(
        {
            "id": op.get("id"),
            "date": op.get("date"),
            "wording": op.get("wording"),
            "memo": op.get("memo"),
            "amount": _signed(op),
            "type": op.get("type") if op.get("type") != "bank" else None,
            "account": accounts.get(account_id) if account_id else None,
            "categories": categories,
            "validated": op.get("validated"),
            "needsAttention": op.get("needsAttention") or None,
            "receipt": has_receipt,
            "receiptSuggested": True
            if op.get("sourceDocumentCandidate") and not has_receipt
            else None,
        }
    )


@mcp.tool(annotations=READ)
async def list_operations(
    query: Query = None,
    amount: AmountFilter = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    inbound: Inbound = None,
    operation_type: OperationType = None,
    treasury_account_id: TreasuryAccount = None,
    validated: Validated = None,
    needs_attention: NeedsAttention = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Rows per page.")] = 25,
    offset: Annotated[int, Field(ge=0, description="Rows to skip, for the next page.")] = 0,
) -> dict[str, Any]:
    """Search bank operations, newest first, one compact row each: signed amount (negative =
    money out), category labels, validation state and whether a receipt is attached. Call
    get_operation for an operation's lines, VAT, questions and receipts."""
    client = get_client()
    cid = await client.resolve_company_id()
    params = _operation_filters(
        query, amount, date_from, date_to, inbound, operation_type, treasury_account_id
    )
    if validated is not None:
        params["validated"] = str(validated).lower()
    if needs_attention is not None:
        params["needsAttention"] = str(needs_attention).lower()
    params["limit"] = limit
    params["offset"] = offset
    operations, accounts = await asyncio.gather(
        client.get(f"/companies/{cid}/operations", params=params),
        _account_labels(client, cid),
    )
    result: dict[str, Any] = {"operations": [_operation_row(op, accounts) for op in operations]}
    if len(operations) == limit:
        result["nextOffset"] = offset + limit
    return result


@mcp.tool(annotations=READ)
async def count_operations(
    query: Query = None,
    amount: AmountFilter = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    inbound: Inbound = None,
    operation_type: OperationType = None,
    treasury_account_id: TreasuryAccount = None,
) -> dict[str, int | str]:
    """Count the operations matching the filters that are still to validate, need attention, or
    are validated. Dougs stops counting at 100, reported as "100+"."""
    client = get_client()
    cid = await client.resolve_company_id()
    filters = _operation_filters(
        query, amount, date_from, date_to, inbound, operation_type, treasury_account_id
    )
    states = {
        "toValidate": {"validated": "false", "needsAttention": "false"},
        "needsAttention": {"validated": "false", "needsAttention": "true"},
        "validated": {"validated": "true"},
    }
    counts = await client.post(
        f"/companies/{cid}/operations/counts",
        json={
            "counts": [
                {"key": key, "queryString": urlencode({**state, **filters})}
                for key, state in states.items()
            ]
        },
    )
    return {k: f"{_COUNT_CAP}+" if counts[k] >= _COUNT_CAP else counts[k] for k in states}


@mcp.tool(annotations=WRITE)
async def validate_operation(
    operation_id: OperationId,
    validated: Annotated[bool, Field(description="False re-opens a validated operation.")] = True,
) -> dict[str, Any]:
    """Validate an operation, confirming its categorization; an uncategorized one is filed
    under "Non catégorisé". Returns the operation as get_operation does."""
    client = get_client()
    cid = await client.resolve_company_id()
    operation = await client.operation(cid, operation_id)
    operation["validated"] = validated
    return _operation_view(await client.update_operation(cid, operation))


def _find_breakdown(operation: dict[str, Any], breakdown_id: int) -> dict[str, Any]:
    for b in operation.get("breakdowns") or []:
        if b.get("id") == breakdown_id:
            return b
    known = [b.get("id") for b in operation.get("breakdowns") or []]
    raise ValueError(
        f"breakdown {breakdown_id} not found on operation {operation.get('id')}; "
        f"breakdown ids are {known}"
    )


def _slot_summary(
    association: dict[str, Any], slot_name: str, slot: dict[str, Any]
) -> dict[str, Any]:
    """One answerable question on a breakdown, in the shape the tools take back."""
    selected = slot.get("selectedItem") or {}
    return compact(
        {
            "association": association.get("name"),
            "slot": slot_name,
            "question": association.get("message"),
            "type": slot.get("type"),
            "value": selected.get("value"),
            "label": selected.get("label"),
            "answered": selected.get("value") is not None,
            "required": not slot.get("isOptional"),
            "editable": False if not slot.get("isEditable") else None,
            # Dougs hides some answerable questions from its own UI (the supplier
            # behind a bank counterpart, for instance); they stay settable.
            "prompted": False if not association.get("show", True) else None,
        }
    )


def _slot_summaries(breakdown: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _slot_summary(a, name, slot)
        for a in breakdown.get("associations") or []
        for name, slot in (a.get("slots") or {}).items()
    ]


def _find_slot(
    breakdown: dict[str, Any], association: str, slot: str | None
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Locate an association slot on a breakdown; the slot name is optional when unambiguous."""
    match = next(
        (a for a in breakdown.get("associations") or [] if a.get("name") == association), None
    )
    if match is None:
        known = [a.get("name") for a in breakdown.get("associations") or []]
        raise ValueError(
            f"breakdown {breakdown.get('id')} has no '{association}' association; it has {known}"
        )
    slots = match.get("slots") or {}
    if slot is None:
        if len(slots) != 1:
            raise ValueError(
                f"association '{association}' has several slots {list(slots)}; pass slot="
            )
        slot = next(iter(slots))
    if slot not in slots:
        raise ValueError(f"association '{association}' has no slot '{slot}'; it has {list(slots)}")
    return match, slot, slots[slot]


def _category_vat(category: dict[str, Any]) -> dict[str, Any]:
    """VAT facts about a category.

    A category can be abstract: it stands for several variants (with/without
    VAT, tourism/utility vehicle, per-country rates…) and Dougs then asks a
    question to pick one, which is what settles the rate.
    """
    if not category.get("isAbstract"):
        rate = (category.get("vat") or {}).get("rate")
        if isinstance(rate, str):  # e.g. "fromEuCountries": the rate follows the country
            return {"vatRateRule": rate, "asksQuestions": True}
        return {"vatRate": rate}
    rates = {(child.get("vat") or {}).get("rate") for child in category.get("children") or []}
    return {
        "vatRates": sorted(r for r in rates if isinstance(r, int | float)),
        "asksQuestions": True,
    }


def _category_row(category: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": category.get("id"),
        "wording": category.get("wording"),
        "group": (category.get("group") or {}).get("name") or category.get("groupName"),
        "accountingNumber": category.get("accountingNumber"),
        "description": category.get("description"),
        **_category_vat(category),
    }


def _category_page(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    page: dict[str, Any] = {"categories": rows[:limit]}
    if len(rows) > limit:
        page["more"] = f"{len(rows) - limit} more: narrow with search or raise limit"
    return page


def _breakdown_view(b: dict[str, Any]) -> dict[str, Any]:
    editable = [
        name
        for name, flag in (
            ("category", "isCategoryEditable"),
            ("vat", "isVatAmountEditable"),
            ("amount", "isAmountEditable"),
        )
        if b.get(flag)
    ]
    return {
        "id": b.get("id"),
        "amount": b.get("amount"),
        "isInbound": b.get("isInbound"),
        "isCounterpart": b.get("isCounterpart") or None,
        "isFee": b.get("isFee") or None,
        "wording": b.get("wording"),
        "categoryId": b.get("categoryId"),
        "category": b.get("categoryWording"),
        "vatRate": b.get("vatRate"),
        "vatAmount": b.get("vatAmount"),
        "manualVatAmount": b.get("manualVatAmount"),
        "editable": editable,
        "questions": _slot_summaries(b),
    }


def _attachment_view(a: dict[str, Any]) -> dict[str, Any]:
    doc = a.get("sourceDocument") or {}
    file = doc.get("file") or {}
    return {
        "id": a.get("id"),
        "file": file.get("name"),
        "path": file.get("url"),
        "kind": doc.get("type"),
        "vendorInvoiceId": doc.get("externalId") if doc.get("type") == "vendorInvoice" else None,
        "date": (doc.get("date") or "")[:10],
        "amount": doc.get("amount"),
    }


def _operation_view(op: dict[str, Any], account: str | None = None) -> dict[str, Any]:
    """Edit-oriented view of one operation; the raw payload is ~2 000 tokens."""
    candidate = op.get("sourceDocumentCandidate")
    suggested = _attachment_view(candidate) if candidate else {}
    suggested.pop("id", None)  # a match id, not an attachment: nothing takes it back
    return compact(
        {
            "id": op.get("id"),
            "date": op.get("date"),
            "wording": op.get("wording"),
            "memo": op.get("memo"),
            "type": op.get("type"),
            "account": account,
            "amount": _signed(op),
            "validated": op.get("validated"),
            "needsAttention": op.get("needsAttention") or None,
            "accountantMessage": op.get("message"),
            "vatAmount": op.get("vatAmount"),
            "isTotalAmountValid": op.get("isTotalAmountValid"),
            "errors": op.get("errors"),
            "breakdowns": [_breakdown_view(b) for b in op.get("breakdowns") or []],
            "receipts": [_attachment_view(a) for a in op.get("sourceDocumentAttachments") or []],
            "receiptRequired": (op.get("attachmentRequirement") or {}).get("reason"),
            "suggestedReceipt": suggested,
        }
    )


def _replace_breakdown(operation: dict[str, Any], updated: dict[str, Any]) -> list[dict[str, Any]]:
    return [updated if b.get("id") == updated["id"] else b for b in operation["breakdowns"]]


@mcp.tool(annotations=READ)
async def get_operation(operation_id: OperationId) -> dict[str, Any]:
    """Return one operation with its accounting lines (category, VAT, what is editable, the
    questions Dougs asks about each), its receipts and any suggested receipt."""
    client = get_client()
    cid = await client.resolve_company_id()
    op, accounts = await asyncio.gather(
        client.operation(cid, operation_id), _account_labels(client, cid)
    )
    account_id = (op.get("transaction") or {}).get("accountId")
    return _operation_view(op, accounts.get(account_id) if account_id else None)


@mcp.tool(annotations=WRITE)
async def update_operation(
    operation_id: OperationId,
    memo: Annotated[
        str | None, Field(description="Free-text note on the operation; '' clears it.")
    ] = None,
    date: Annotated[
        str | None,
        Field(description="New date 'YYYY-MM-DD'; expense reports and cash receipts only."),
    ] = None,
    amount: Annotated[
        float | None,
        Field(gt=0, description="New amount in euros; expense reports and cash receipts only."),
    ] = None,
) -> dict[str, Any]:
    """Set an operation's memo, or change the date or amount of an expense report or cash
    receipt created with create_operation."""
    if memo is None and date is None and amount is None:
        raise ValueError("pass at least one of memo, date or amount")
    client = get_client()
    cid = await client.resolve_company_id()
    operation = await client.operation(cid, operation_id)
    if (date is not None or amount is not None) and operation.get("type") not in (
        "expense",
        "cashPayment",
    ):
        raise ValueError(
            f"only expense reports and cash receipts take a new date or amount; operation "
            f"{operation_id} is of type '{operation.get('type')}'"
        )
    if memo is not None:
        operation["memo"] = memo
    if date is not None:
        _iso_to_fr(date)  # validates the format
        operation["date"] = date
    if amount is not None:
        operation["amount"] = round(amount, 2)
    return _operation_view(await client.update_operation(cid, operation))


def _expense_partners(partners: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    """Officers and associates active on that day: who the app lets file an expense report."""
    return [
        p
        for p in partners
        if any(
            pos.get("key") in ("isAssociate", "isCeo", "isDirector")
            and pos.get("isActive")
            and (not pos.get("endDate") or pos["endDate"][:10] >= day)
            for pos in p.get("positions") or []
        )
    ]


@mcp.tool(annotations=WRITE)
async def create_operation(
    kind: Annotated[
        Literal["expense", "cashPayment"],
        Field(
            description="'expense': an expense report (note de frais), a business expense an "
            "officer or associate paid personally, owed back through their associate account; "
            "'cashPayment': money received in cash (recette en espèces)."
        ),
    ],
    date: Annotated[str, Field(description="Operation date 'YYYY-MM-DD', not in the future.")],
    amount: Annotated[float, Field(gt=0, description="Amount in euros, VAT included.")],
    category_id: Annotated[
        int,
        Field(description="Category id, from search_categories(new_operation_kind=<kind>)."),
    ],
    memo: Annotated[str | None, Field(description="Free-text note, e.g. 'Déjeuner client'.")] = (
        None
    ),
    partner_id: Annotated[
        int | None,
        Field(
            description="Expense reports: the officer or associate who paid; defaults to the "
            "only one when there is a single one."
        ),
    ] = None,
) -> dict[str, Any]:
    """Create an expense report or a cash receipt, to validate like a bank operation. Attach
    its receipt with add_attachment."""
    _iso_to_fr(date)  # validates the format
    client = get_client()
    cid = await client.resolve_company_id()
    amount = round(amount, 2)
    breakdowns: list[dict[str, Any]] = [{"amount": amount, "categoryId": category_id}]
    if kind == "expense":
        partners = _expense_partners(await client.get(f"/companies/{cid}/partners"), date)
        if partner_id is None:
            if len(partners) != 1:
                options = [{"id": p.get("id"), "name": p.get("fullName")} for p in partners]
                raise ValueError(f"pass partner_id, one of {options}")
            partner_id = partners[0]["id"]
        elif partner_id not in {p.get("id") for p in partners}:
            options = [{"id": p.get("id"), "name": p.get("fullName")} for p in partners]
            raise ValueError(
                f"partner {partner_id} is not an officer or associate on {date}; use one of "
                f"{options}"
            )
        breakdowns.append(
            {
                "amount": 0,
                "categoryId": -1,
                "isCounterpart": True,
                "associationData": {"partnerId": partner_id},
            }
        )
    created = await client.post(
        f"/companies/{cid}/operations",
        json={
            "type": kind,
            "date": f"{date}T00:00:00.000Z",
            "memo": memo,
            "amount": amount,
            "breakdowns": breakdowns,
        },
    )
    return _operation_view(created)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_operation(operation_id: OperationId) -> dict[str, Any]:
    """Delete an expense report, cash receipt or mileage allowance for good. Bank transactions
    cannot be deleted."""
    client = get_client()
    cid = await client.resolve_company_id()
    operation = await client.operation(cid, operation_id)
    if operation.get("type") not in _HAND_MADE_TYPES:
        raise ValueError(
            f"operation {operation_id} is of type '{operation.get('type')}'; only "
            f"{sorted(_HAND_MADE_TYPES)} can be deleted"
        )
    await client.delete(f"/companies/{cid}/operations/{operation_id}")
    return {"deleted": operation_id}


@mcp.tool(annotations=READ)
async def list_available_categories(
    operation_id: OperationId,
    breakdown_id: BreakdownId,
    search: Annotated[
        str | None, Field(description="Server-side label and keyword filter, e.g. 'restaurant'.")
    ] = None,
    is_refund: Annotated[
        bool | None, Field(description="True lists the refund counterparts instead.")
    ] = None,
    preferred_only: Annotated[
        bool, Field(description="Only the few categories Dougs suggests for this line.")
    ] = False,
    limit: Annotated[int, Field(ge=1, le=200, description="Maximum rows.")] = 40,
) -> dict[str, Any]:
    """List the categories Dougs accepts for one line, suggested ones first: pick
    set_breakdown_category's category_id here. `asksQuestions` marks categories whose VAT is
    settled by a question once assigned."""
    client = get_client()
    cid = await client.resolve_company_id()
    base = f"/companies/{cid}/operations/{operation_id}/breakdowns/{breakdown_id}"
    params: dict[str, Any] = {}
    if search:
        params["search"] = search
    if is_refund is not None:
        params["isRefund"] = str(is_refund).lower()
    preferred_ids, available_ids = await asyncio.gather(
        client.get(f"{base}/preferred-categories"),
        client.get(f"{base}/available-categories", params=params),
    )
    preferred = set(preferred_ids or [])
    ids = [i for i in (available_ids or []) if not preferred_only or i in preferred]
    catalog = await client.category_catalog(cid)
    result = [
        compact({**_category_row(catalog.get(i) or {"id": i}), "preferred": i in preferred or None})
        for i in ids
    ]
    result.sort(key=lambda c: (not c.get("preferred"), c.get("wording") or ""))
    return _category_page(result, limit)


@mcp.tool(annotations=READ)
async def search_categories(
    search: Annotated[
        str | None, Field(description="Matches label, keywords and description, e.g. 'loyer'.")
    ] = None,
    inbound: Annotated[
        bool | None,
        Field(description="True: income categories; False: expense categories; omit for both."),
    ] = None,
    group: Annotated[
        str | None, Field(description="Accounting group name, e.g. 'Frais de fonctionnement'.")
    ] = None,
    ids: Annotated[
        list[int] | None,
        Field(description="Resolve these category ids, e.g. a breakdown's categoryId."),
    ] = None,
    new_operation_kind: Annotated[
        Literal["expense", "cashPayment"] | None,
        Field(description="Only the categories create_operation accepts for this kind."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Maximum rows.")] = 40,
) -> dict[str, Any]:
    """Search the company's category catalog, or resolve category ids to labels. To categorize
    an existing line, list_available_categories is narrower: it returns only what Dougs
    accepts for that line."""
    client = get_client()
    cid = await client.resolve_company_id()
    catalog = await client.category_catalog(cid)
    allowed: set[int] | None = set(ids) if ids else None
    if new_operation_kind:
        kind_ids = set(
            await client.get(f"/companies/{cid}/categories", params={"type": new_operation_kind})
        )
        allowed = kind_ids if allowed is None else allowed & kind_ids
    needle = (search or "").casefold()
    group_needle = (group or "").casefold()
    result = []
    for cat in catalog.values():
        if allowed is not None:
            if cat.get("id") not in allowed:
                continue
        elif not cat.get("isAssignable") or cat.get("hidden"):
            continue
        row = _category_row(cat)
        if group_needle and group_needle not in (row["group"] or "").casefold():
            continue
        if inbound is not None and cat.get("isInbound") is not inbound:
            continue
        if needle:
            haystack = " ".join(
                [
                    cat.get("wording") or "",
                    cat.get("description") or "",
                    *(cat.get("keywords") or []),
                ]
            )
            if needle not in haystack.casefold():
                continue
        result.append(compact({**row, "isInbound": cat.get("isInbound")}))
    result.sort(key=lambda c: (c.get("group") or "", c.get("wording") or ""))
    return _category_page(result, limit)


@mcp.tool(annotations=READ)
async def list_question_options(
    operation_id: OperationId,
    breakdown_id: BreakdownId,
    association: Annotated[
        str, Field(description="Question's association name, from get_operation, e.g. 'loan'.")
    ],
    slot: Annotated[
        str | None, Field(description="Slot name; omit when the association has a single one.")
    ] = None,
    search: Annotated[
        str | None, Field(description="Filters record answers (suppliers, partners, loans).")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Maximum answers.")] = 50,
    offset: Annotated[int, Field(ge=0, description="Answers to skip.")] = 0,
) -> dict[str, Any]:
    """List the accepted answers to one question of a line (VAT exemption reasons, suppliers,
    loans…); pass an item's `value` to set_breakdown_association."""
    client = get_client()
    cid = await client.resolve_company_id()
    operation = await client.operation(cid, operation_id)
    breakdown = _find_breakdown(operation, breakdown_id)
    _, slot_name, _ = _find_slot(breakdown, association, slot)
    params: dict[str, Any] = {"offset": offset, "limit": limit}
    if search:
        params["q"] = search
    data = await client.get(
        f"/companies/{cid}/operations/{operation_id}/breakdowns/{breakdown_id}"
        f"/associations/{association}/slots/{slot_name}/candidates",
        params=params,
    )

    def item(i: dict[str, Any]) -> dict[str, Any]:
        return compact(
            {"value": i.get("value"), "label": i.get("label"), "description": i.get("description")}
        )

    return compact(
        {
            "association": association,
            "slot": slot_name,
            "title": (data.get("descriptor") or {}).get("title"),
            "preferredItems": [item(i) for i in data.get("preferredItems") or []],
            "items": [item(i) for i in data.get("items") or []],
        }
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def set_breakdown_category(
    operation_id: OperationId,
    breakdown_id: BreakdownId,
    category_id: Annotated[int, Field(description="Category id, from list_available_categories.")],
) -> dict[str, Any]:
    """Change the category of one line. Re-read `questions` in the result: Dougs drops the
    answers the new category does not use, and does not restore them on switching back."""
    client = get_client()
    cid = await client.resolve_company_id()
    operation = await client.operation(cid, operation_id)
    breakdown = _find_breakdown(operation, breakdown_id)
    if not breakdown.get("isCategoryEditable"):
        raise ValueError(
            f"breakdown {breakdown_id} is not categorizable; the counterpart (bank side) line "
            "never is"
        )
    category = await client.category(cid, category_id)
    updated = {**breakdown, "categoryId": category_id, "categoryWording": category.get("wording")}
    operation["breakdowns"] = _replace_breakdown(operation, updated)
    return _operation_view(await client.update_operation(cid, operation, updated))


@mcp.tool(annotations=WRITE)
async def set_breakdown_association(
    operation_id: OperationId,
    breakdown_id: BreakdownId,
    association: Annotated[
        str, Field(description="Question's association name, from get_operation.")
    ],
    value: Annotated[
        str | int | float | bool | None,
        Field(description="Answer, from list_question_options; null clears an optional one."),
    ],
    slot: Annotated[
        str | None, Field(description="Slot name; omit when the association has a single one.")
    ] = None,
) -> dict[str, Any]:
    """Answer one of the questions Dougs asks about a line (supplier, loan, partner, period…)."""
    client = get_client()
    cid = await client.resolve_company_id()
    operation = await client.operation(cid, operation_id)
    breakdown = _find_breakdown(operation, breakdown_id)
    _, slot_name, slot_def = _find_slot(breakdown, association, slot)
    if not slot_def.get("isEditable"):
        raise ValueError(f"question '{association}.{slot_name}' is not editable on this breakdown")
    association_data = dict(breakdown.get("associationData") or {})
    association_data[slot_def["key"]] = value
    updated = {**breakdown, "associationData": association_data}
    operation["breakdowns"] = _replace_breakdown(operation, updated)
    return _operation_view(await client.update_operation(cid, operation, updated))


@mcp.tool(annotations=WRITE)
async def set_breakdown_vat(
    operation_id: OperationId,
    breakdown_id: BreakdownId,
    vat_amount: Annotated[
        float | None, Field(description="VAT amount in euros to force on the line.")
    ] = None,
    automatic_vat: Annotated[
        bool, Field(description="True drops a forced amount and lets Dougs recompute it.")
    ] = False,
    subject_to_vat: Annotated[
        bool | None,
        Field(description="Answers 'is this expense subject to VAT?' where VAT is optional."),
    ] = None,
    exemption_reason: Annotated[
        str | None,
        Field(
            description="Why the line carries no VAT, e.g. 'exemption:outbound:outsideEuropean"
            "Union'; list_question_options(association='vatExemptionReason') lists them."
        ),
    ] = None,
) -> dict[str, Any]:
    """Override the VAT of one line. The rate comes from the category: change it with
    set_breakdown_category."""
    if vat_amount is None and not automatic_vat and subject_to_vat is None and not exemption_reason:
        raise ValueError(
            "pass at least one of vat_amount, automatic_vat, subject_to_vat or exemption_reason"
        )
    if vat_amount is not None and automatic_vat:
        raise ValueError("vat_amount and automatic_vat are mutually exclusive")
    client = get_client()
    cid = await client.resolve_company_id()
    operation = await client.operation(cid, operation_id)
    breakdown = _find_breakdown(operation, breakdown_id)
    updated = {**breakdown}
    if vat_amount is not None or automatic_vat:
        if not breakdown.get("isVatAmountEditable"):
            raise ValueError(f"breakdown {breakdown_id} has no editable VAT amount")
        updated["manualVatAmount"] = None if vat_amount is None else round(vat_amount, 2)
    answers: dict[str, Any] = {}
    if subject_to_vat is not None:
        answers["hasOptionalVat"] = subject_to_vat
    if exemption_reason is not None:
        answers["vatExemptionReason"] = exemption_reason
    if answers:
        association_data = dict(breakdown.get("associationData") or {})
        for association, answer in answers.items():
            _find_slot(breakdown, association, None)  # raises if the line does not ask for it
            association_data[association] = answer
        updated["associationData"] = association_data
    operation["breakdowns"] = _replace_breakdown(operation, updated)
    return _operation_view(await client.update_operation(cid, operation, updated))


class SplitLine(BaseModel):
    """One accounting line of a split operation."""

    amount: float = Field(gt=0, description="Amount in euros, VAT included.")
    category_id: int | None = Field(
        None, description="Category id, from list_available_categories; omit to leave it blank."
    )
    breakdown_id: int | None = Field(
        None,
        description="Existing line to keep, with its answers and VAT overrides; omit for a new one.",
    )
    wording: str | None = Field(None, description="Optional label for the line.")
    is_inbound: bool | None = Field(
        None, description="Direction; defaults to the operation's, flip it for a partial refund."
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def split_operation(
    operation_id: OperationId,
    lines: Annotated[
        list[SplitLine],
        Field(
            min_length=1,
            description="Replaces every line but the bank counterpart; lines left out are "
            "deleted. Amounts should add up to the operation's.",
        ),
    ],
) -> dict[str, Any]:
    """Split an operation into several categorized lines. A result with isTotalAmountValid
    false means the amounts do not add up."""
    client = get_client()
    cid = await client.resolve_company_id()
    operation = await client.operation(cid, operation_id)
    if len(lines) > 1 and not operation.get("allowAddingBreakdown"):
        raise ValueError(f"operation {operation_id} does not accept several breakdowns")

    kept = [b for b in operation["breakdowns"] if b.get("isCounterpart") or b.get("isFee")]
    template = next(iter(_main_lines(operation)), None)
    section = (template or {}).get("section") or "main"

    main_ids = {b["id"] for b in _main_lines(operation)}
    given = [line.breakdown_id for line in lines if line.breakdown_id is not None]
    if len(given) != len(set(given)) or not set(given) <= main_ids:
        raise ValueError(
            f"breakdown_id must name distinct categorizable lines; they are {sorted(main_ids)}"
        )
    new_breakdowns: list[dict[str, Any]] = []
    for line in lines:
        existing = (
            _find_breakdown(operation, line.breakdown_id) if line.breakdown_id is not None else None
        )
        breakdown: dict[str, Any] = dict(existing) if existing else {"section": section}
        breakdown["amount"] = line.amount
        breakdown["isInbound"] = (
            line.is_inbound if line.is_inbound is not None else bool(operation.get("isInbound"))
        )
        if line.wording is not None:
            breakdown["wording"] = line.wording
        if line.category_id is not None:
            category = await client.category(cid, line.category_id)
            breakdown["categoryId"] = line.category_id
            breakdown["categoryWording"] = category.get("wording")
        elif not existing:
            breakdown["categoryWording"] = "Non catégorisé"
        new_breakdowns.append(breakdown)

    operation["breakdowns"] = new_breakdowns + kept
    return _operation_view(await client.update_operation(cid, operation, new_breakdowns[-1]))


def _read_upload(file_path: str) -> tuple[str, bytes, str]:
    """Read a local file off the event loop; returns (name, bytes, content_type)."""
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValueError(f"file not found: {file_path}")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.name, path.read_bytes(), content_type


def _receipts(op: dict[str, Any]) -> dict[str, Any]:
    return {
        "operationId": op.get("id"),
        "receipts": [
            compact(_attachment_view(a)) for a in op.get("sourceDocumentAttachments") or []
        ],
    }


@mcp.tool(annotations=WRITE)
async def add_attachment(
    operation_id: OperationId,
    file_path: Annotated[str, Field(description="Local PDF or image, e.g. '~/receipt.pdf'.")],
) -> dict[str, Any]:
    """Attach a receipt or invoice file to an operation. Dougs files it as a vendor invoice
    in Purchases and reads its date and amount within a few seconds."""
    name, content, content_type = await asyncio.to_thread(_read_upload, file_path)
    client = get_client()
    cid = await client.resolve_company_id()
    op = await client.post(
        f"/companies/{cid}/operations/{operation_id}/source-document-attachments/actions/create-from-formdata",
        files={"file": (name, content, content_type)},
    )
    return _receipts(op)


@mcp.tool(annotations=WRITE)
async def remove_attachment(
    operation_id: OperationId,
    attachment_id: Annotated[int, Field(description="Receipt id, from get_operation's receipts.")],
) -> dict[str, Any]:
    """Detach a receipt from an operation. The document itself stays in Purchases as a vendor
    invoice; delete it there if it was attached by mistake."""
    client = get_client()
    cid = await client.resolve_company_id()
    op = await client.delete(
        f"/companies/{cid}/operations/{operation_id}/source-document-attachments/{attachment_id}"
    )
    return _receipts(op)
