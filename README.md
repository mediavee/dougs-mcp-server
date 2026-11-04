# dougs-mcp

MCP server for [Dougs](https://www.dougs.fr) online accounting.

It exposes tools over Dougs' internal API (`app.dougs.fr`) so an MCP client
(Claude Code, Claude Desktop, …) can query — and update — your accounting data.

> **Unofficial.** This talks to the same private API the Dougs web app uses,
> authenticating with your own credentials, and may break if Dougs changes their
> backend. Most tools are read-only; the few that modify your data are listed
> under [Write tools](#write-tools).

## How it works

- **Auth**: automatic login via `POST /auth/api/login` with `{email, password}`.
  The session cookie is cached under `~/.cache/dougs-mcp/` (mode `600`) and
  reused across restarts — Dougs allows only 25 logins per hour and per account
  — then refreshed transparently on a `401`.
- **MFA**: if the account has two-factor authentication, login answers
  `mfaRequired` and the session stays unauthenticated until a code is verified
  (`POST /auth/api/mfa/verify`). Enable the **authenticator app** factor in
  Dougs and set `DOUGS_TOTP_SECRET`: the server then computes the code itself,
  so it can log in unattended. Email codes cannot be automated.
- **Base URL**: `https://app.dougs.fr`
- **Company**: most tools act on a company id. It defaults to your preferred
  company — override per call with `company_id`, or pin one via
  `DOUGS_COMPANY_ID`.

## Requirements

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
cp .env.example .env   # then fill in your credentials
```

`.env`:

```
DOUGS_EMAIL=you@example.com
DOUGS_PASSWORD=your-password
# DOUGS_COMPANY_ID=12345   # optional; defaults to your preferred company
# DOUGS_TOTP_SECRET=JBSWY3DPEHPK3PXP   # required if MFA is on; base32 secret or otpauth:// URI
```

`DOUGS_TOTP_SECRET` is the `secret` parameter of the `otpauth://` link Dougs
shows when you set up the authenticator app (the full link works too). Stored
next to the password it is a second key to the account: keep `.env` private.

## Tools

### Read tools

| Tool | Description |
|------|-------------|
| `get_me` | Authenticated user profile |
| `list_companies` | Companies accessible to the user |
| `get_company` | A company's accounting configuration |
| `list_operations` | List/search bank operations — `query` (text), `amount` (`>1000`, `<500`, `1000-2000`), `date_from`/`date_to` (ISO), `inbound` (entrées/sorties), `operation_type`, `treasury_account_id`, `validated`, `needs_attention`; paginated via `limit` (max 500) / `offset` |
| `get_operation` | One operation as a compact, edit-oriented view: each line with its category, VAT, what is editable, and the questions Dougs asks about it |
| `get_categories` | Resolve operation category ids (`resolvedCategoryId`) to labels — wording, accounting group, account number |
| `search_categories` | Browse the whole category catalog (no operation needed): `search` over labels/keywords/descriptions, `inbound`, `group` |
| `list_available_categories` | Categories assignable to a given breakdown (contextual — 84 of 221 for a bank expense), with `search`, `is_refund`, `preferred_only` |
| `list_breakdown_questions` | The questions (association slots) a breakdown still needs answered — VAT exemption reason, supplier, loan, partner… |
| `list_question_options` | Accepted answers for one question (enum values, or records filtered by `search`) |
| `list_sales_channels` | Configured sales channels |
| `get_active_accounting_year` | Current fiscal year (id, opening/closing dates) |
| `list_accounting_years` | All fiscal years |
| `list_metrics` | The company's accounting time series (105 on a typical account): revenue, expenses, income-statement lines, cash per bank account |
| `get_metrics` | Values of one or more series — monthly/quarterly/yearly, per-period or cumulative, over the full history |
| `list_investments` | Fixed assets and their depreciation, plus portfolio totals |
| `list_loans` | Loans: amount, start date, duration, rate |
| `list_declarations` | Tax filings (VAT, corporate tax, CFE): period, amount due, confirmation |
| `get_accounting_stat` | Aggregated stat for a fiscal year: revenue, income statement, operating result, charges, cash, VAT tracking, corporate tax, remunerations… (see `stat_type`) |
| `list_sales_invoices` | Customer invoices (client, amount, VAT, due date, status) |
| `list_vendor_invoices` | Supplier invoices |
| `get_invoices_overview` | Sales & vendor invoice counts and amounts (paid/waiting/late/draft) |
| `get_product_catalog` | Product/service catalog |
| `list_partners` | Customers and suppliers |
| `list_bank_accounts` | Bank/treasury accounts (Qonto, PayPal…) with balances and a ready `filterValue` for `treasury_account_id` |
| `get_file_url` | Resolve a Dougs file path (`/files/…`) to a downloadable signed S3 URL |
| `raw_get` | Low-level read-only GET on any API path (for unwrapped endpoints) |

### Editing an operation

Dougs models an operation as a set of **breakdowns** (accounting lines): the
categorized line(s) plus a non-editable counterpart (the bank side). VAT is
derived from the category, and a category may need extra input before the line
is complete — Dougs asks these as **questions** (association slots): the VAT
exemption reason of a 0% line, the supplier, the loan, the partner and period of
a remuneration…

A typical pass:

1. `get_operation` — read the lines, their ids and their pending questions.
2. `list_available_categories` — pick a valid `category_id` for a line
   (`search_categories` browses the full catalog when no line is at hand).
3. `set_breakdown_category` — assign it; the result shows the recomputed VAT and
   any question the new category raised.
4. `list_question_options` then `set_breakdown_vat` / `set_breakdown_association`
   — answer them.
5. `validate_operation` — confirm the categorization.

Use `split_operation` when one transaction covers several categories: it
replaces the operation's lines with the ones you pass (amounts must add up to
the operation total).

### Financial analysis

`get_accounting_stat` returns **year-scoped** aggregates (income statement, VAT
tracking, corporate tax…). For anything spanning several years, use the time
series instead:

```
list_metrics(search="chiffre")      -> accounting.chiffre-d-affaires, …
get_metrics(["accounting.chiffre-d-affaires",
             "accounting.charges-d-exploitation"], group="year")
```

Series go back to the company's first year, in `month` / `quarter` / `year`
granularity, per-period or `cumulative`. Dougs pads them with zero-valued future
periods; those are dropped unless `include_future=True`, and a period still in
progress is flagged `partial`. Alongside the aggregates, `list_investments`,
`list_loans` and `list_declarations` cover assets, debt and tax burden.

`get_accounting_stat` `stat_type` values: `chiffre-d-affaires`, `compte-de-resultat`,
`resultat-d-exploitation`, `charges-d-exploitation`, `repartition-des-charges`,
`tresorerie`, `tresorerie-compte-treso`, `flux-de-tresorerie`,
`flux-de-tresorerie-compte-treso`, `suivi-tva`, `suivi-impots-societes`, `suivi-cfe`,
`remunerations`, `remunerations-for-accounting-year`, `tns-social-charges`,
`social-charges`, `indemnite-kilometrique`, `compte-de-l-exploitant`,
`compte-de-debours`, `comptes-de-filiale`, `fonds-propres`, `comptes-d-associes`,
`autres-reserves-reports-a-nouveau`.

### Sales invoice drafts

Prepare customer invoices without ever issuing them. Drafts live under
`/companies/{id}/sales-invoices-drafts`, a resource separate from finalized
invoices: a draft has no invoice number and is not an accounting entry. No tool
finalizes or sends an invoice; review the draft and finalize it in the Dougs
app (each tool returns its `url`).

| Tool | Description |
|------|-------------|
| `list_customers` | Registered customers an invoice can be addressed to (id, name, SIREN, B2B) |
| `list_sales_invoice_drafts` | Drafts with client, lines and totals |
| `get_sales_invoice_draft` | One draft: client, dates, lines, totals computed by Dougs |

The draft tools that write are listed below. `due_in_days` maps to the payment
terms Dougs offers (0 = immediate, 15, 30, 60 days); any other delay is stored
as a custom due date.

### Write tools

These **modify** your accounting data:

| Tool | Description |
|------|-------------|
| `set_breakdown_category` | Re-categorize one line of an operation |
| `set_breakdown_vat` | Adjust a line's VAT: manual amount, back to automatic, "subject to VAT?" answer, or the exemption reason for a 0% line |
| `set_breakdown_association` | Answer any other question on a line (supplier, loan, partner, period…) |
| `split_operation` | Split an operation into several lines (amount + category each), or merge them back |
| `validate_operation` | Validate / un-validate an operation (confirms its categorization; `validated=False` re-opens it) |
| `add_attachment` | Attach a local file (PDF/image receipt or invoice) to an operation |
| `remove_attachment` | Remove an attachment from an operation (by `attachment_id`) |
| `create_sales_invoice_draft` | Create a sales invoice **draft** for a customer (id, SIREN or name) from lines: title, quantity, unit price, VAT rate, optional unit/description |
| `update_sales_invoice_draft` | Change a draft's lines, date, due date or subject (only the arguments passed) |
| `delete_sales_invoice_draft` | Delete a draft (finalized invoices are out of reach) |

## Use with Claude Code

```bash
claude mcp add dougs -- uv --directory /absolute/path/to/dougs-mcp-server run dougs-mcp
```

Credentials are read from the project's `.env` (thanks to `--directory`).

## Use with Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dougs": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/dougs-mcp-server", "run", "dougs-mcp"]
    }
  }
}
```

## Development

```bash
uv run ruff check src
uv run mypy src
```
