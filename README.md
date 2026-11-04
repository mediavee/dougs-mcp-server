# dougs-mcp

MCP server for [Dougs](https://www.dougs.fr) online accounting: an MCP client (Claude Code,
Claude Desktop, Hermes…) reads and works on your accounting through the private API the Dougs web
app uses (`app.dougs.fr`).

> **Unofficial.** It logs in with your own credentials and may break when Dougs changes its
> backend. Dougs now offers an official MCP for the Claude and ChatGPT apps; this server covers
> what that one refuses on purpose: unattended use from scripts, CLIs and self-hosted agents.

## How it works

- **Auth**: `POST /auth/api/login` with your email and password. The session cookie is cached
  under `~/.cache/dougs-mcp/` (mode `600`) and reused across restarts, since Dougs allows 25
  logins per hour and per account, then renewed on a `401`.
- **MFA**: when the account has two-factor authentication, the server answers it with a TOTP
  code computed from `DOUGS_TOTP_SECRET`. Enable the **authenticator app** factor in Dougs (an
  account with only the email factor cannot log in unattended) and paste its secret.
- **Company**: tools act on your preferred company, or on `DOUGS_COMPANY_ID`; `select_company`
  switches for the rest of the session.
- **Answers** are compact views, not raw payloads: lists are paginated and filtered, and point to
  the tool that gives the detail.
- **Out of reach on purpose**: finalizing or sending an invoice or a quote, confirming a tax
  filing, forcing a bank sync. Tools stop at drafts and return the app URL.

## Requirements

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
cp .env.example .env   # then fill in your credentials
```

```
DOUGS_EMAIL=you@example.com
DOUGS_PASSWORD=your-password
# DOUGS_TOTP_SECRET=JBSWY3DPEHPK3PXP   # required if MFA is on; base32 secret or otpauth:// URI
# DOUGS_COMPANY_ID=12345               # optional; defaults to your preferred company
```

`DOUGS_TOTP_SECRET` is the `secret` parameter of the `otpauth://` link Dougs shows when you set up
the authenticator app (the whole link works too). Next to the password it is a second key to the
account: keep `.env` private.

## Tools

✏️ changes data, ⚠️ deletes or overwrites it; the others only read.

### Account and company

| Tool | What it does |
|---|---|
| `get_account` | Return who is logged in and the companies they can act on; every tool acts on the one flagged `isDefault` (switch with select_company). get_company gives its settings. |
| `select_company` | Switch the company every other tool acts on, for the rest of the session. |
| `get_company` | Return a company's identity and accounting settings: legal form, SIREN, activity, VAT regime and periodicity, tax regime, current fiscal year, lock date and Dougs plan. |
| `list_company_people` | List the people and companies holding a role in the company: officers (gérant, président), associates with their shares, and employees, with each role's dates. |
| `list_bank_accounts` | List the company's bank and treasury accounts (Qonto, PayPal, Stripe…) with their balance. `filterValue` goes to list_operations(treasury_account_id=...); list_bank_connections tells whether their synchronization works. |
| `list_bank_connections` | Report the health of each bank connection: status, the action the bank asks for (such as a strong authentication), when it last fetched data and imported transactions, and its accounts. Re-authenticating or forcing a sync stays a step in the Dougs app. |
| `list_customer_todos` | List what Dougs is waiting for from the customer (documents to attach, a VAT return to complete, an account to regularize), with due dates and whether each is overdue. Completing or ignoring an item stays a step in the Dougs app. |
| `list_billing_invoices` | List the invoices Dougs bills the company for its subscription, newest first, with the upcoming one and the plan. Pass an invoice's `filePath` to get_file_url to download its PDF. |

### Bank operations

| Tool | What it does |
|---|---|
| `list_operations` | Search bank operations, newest first, one compact row each: signed amount (negative = money out), category labels, validation state and whether a receipt is attached. Call get_operation for an operation's lines, VAT, questions and receipts. |
| `count_operations` | Count the operations matching the filters that are still to validate, need attention, or are validated. Dougs stops counting at 100, reported as "100+". |
| `validate_operation` ✏️ | Validate an operation, confirming its categorization; an uncategorized one is filed under "Non catégorisé". Returns the operation as get_operation does. |
| `get_operation` | Return one operation with its accounting lines (category, VAT, what is editable, the questions Dougs asks about each), its receipts and any suggested receipt. |
| `update_operation` ✏️ | Set an operation's memo, or change the date or amount of an expense report or cash receipt created with create_operation. |
| `create_operation` ✏️ | Create an expense report or a cash receipt, to validate like a bank operation. Attach its receipt with add_attachment. |
| `delete_operation` ⚠️ | Delete an expense report, cash receipt or mileage allowance for good. Bank transactions cannot be deleted. |
| `list_available_categories` | List the categories Dougs accepts for one line, suggested ones first: pick set_breakdown_category's category_id here. `asksQuestions` marks categories whose VAT is settled by a question once assigned. |
| `search_categories` | Search the company's category catalog, or resolve category ids to labels. To categorize an existing line, list_available_categories is narrower: it returns only what Dougs accepts for that line. |
| `list_question_options` | List the accepted answers to one question of a line (VAT exemption reasons, suppliers, loans…); pass an item's `value` to set_breakdown_association. |
| `set_breakdown_category` ✏️ | Change the category of one line. Re-read `questions` in the result: Dougs drops the answers the new category does not use, and does not restore them on switching back. |
| `set_breakdown_association` ✏️ | Answer one of the questions Dougs asks about a line (supplier, loan, partner, period…). |
| `set_breakdown_vat` ✏️ | Override the VAT of one line. The rate comes from the category: change it with set_breakdown_category. |
| `split_operation` ✏️ | Split an operation into several categorized lines. A result with isTotalAmountValid false means the amounts do not add up. |
| `add_attachment` ✏️ | Attach a receipt or invoice file to an operation. Dougs files it as a vendor invoice in Purchases and reads its date and amount within a few seconds. |
| `remove_attachment` ✏️ | Detach a receipt from an operation. The document itself stays in Purchases as a vendor invoice; delete it there if it was attached by mistake. |

### Purchases

| Tool | What it does |
|---|---|
| `list_vendor_invoices` | List vendor (supplier) invoices, most recent first (undated ones on top), as compact rows. Also returns the total still to pay in EUR across all unpaid invoices. Use get_vendor_invoice for one invoice's detail and file path. |
| `get_vendor_invoice` | One vendor invoice in detail: supplier, amounts, VAT breakdown, OCR status, refusal, attached and suggested operations, the fields update_vendor_invoice may change, and the file_path to pass to get_file_url. |
| `upload_vendor_invoice` ✏️ | Upload a supplier invoice file; Dougs reads it by OCR within seconds. Returns the new invoice; call get_vendor_invoice again once `ocr` is no longer 'initialised' to see what it read, and fix it with update_vendor_invoice. |
| `update_vendor_invoice` ✏️ | Correct the fields of a vendor invoice, typically what the OCR misread; only the fields given change. E-invoices received through the e-invoicing network are locked except memo. |
| `delete_vendor_invoice` ⚠️ | Delete a vendor invoice and its file. Dougs refuses while it is attached to a validated operation. |
| `attach_vendor_invoice` ✏️ | Attach a vendor invoice to the bank operation that paid it, as its receipt, or detach it. Dougs refuses to detach from a validated operation: un-validate it first with validate_operation, then validate it again. The invoice's payment status follows a few seconds later, so the status in this answer is the previous one. |

### Sales

| Tool | What it does |
|---|---|
| `list_sales_invoices` | List finalized sales invoices, newest first: number, client, amounts, payment status and the bank operations they are matched to. Drafts are in list_sales_invoice_drafts. |
| `get_sales_overview` | Sales dashboard: invoice and quote counts, issued/waiting/late amounts, receivables aging, and the year's net revenue by month and by client. |
| `list_customers` | List registered customers (the address book used for invoices and quotes). |
| `save_customer` ✏️ | Create a customer, or change one (only the arguments passed change). Documents already issued keep the client details they were made with. |
| `delete_customer` ⚠️ | Delete a registered customer. To keep it out of lists without deleting it, archive it with save_customer. |
| `list_sales_invoice_drafts` | List sales invoice drafts (not finalized) with client, dates and totals; read the lines with get_sales_invoice_draft. |
| `get_sales_invoice_draft` | Read a sales invoice draft: client, dates, lines, totals and its app URL. |
| `save_sales_invoice_draft` ✏️ | Create a sales invoice draft, or change one (only the arguments passed change; a new date or delay recomputes the due date). Dougs computes the totals. A draft is never finalized nor sent: that stays in the app (returned url). |
| `delete_sales_invoice_draft` ⚠️ | Delete a sales invoice draft. Finalized invoices cannot be deleted here. |
| `list_quotes` | List quotes (devis), drafts included, newest first: number, status, client, dates and totals. Read a draft's lines with get_quote_draft. |
| `get_quote_draft` | Read a quote draft: client, dates, lines, totals and its app URL. |
| `save_quote_draft` ✏️ | Create a quote (devis) draft, or change one (only the arguments passed change; a new date or validity recomputes the expiration). Dougs computes the totals. A draft is never finalized nor sent: that stays in the app (returned url). |
| `delete_quote_draft` ⚠️ | Delete a quote draft. Issued quotes cannot be deleted here. |
| `list_invoice_products` | List the company's saved products and services, with their unit price and VAT rate, to reuse as invoice or quote lines. |
| `save_invoice_product` ✏️ | Save a product or service to reuse on invoices and quotes, or change one (only the arguments passed change). |
| `delete_invoice_product` ⚠️ | Delete a saved product. Invoices and quotes that used it keep their lines. |
| `list_sales_channels` | List the sales channels declared to Dougs (website, marketplace, shop...) and the payment processors attached to each. |

### Accounting and tax

| Tool | What it does |
|---|---|
| `list_accounting_years` | List the company's accounting (fiscal) years, the active one flagged `active`. Closed years carry their closing documents (balance sheet, FEC, final print), whose `url` goes to get_file_url. |
| `get_accounting_stat` | Return an aggregated figure of one accounting year (totals; monthly values on request). Income-statement lines carry a `seriesName` to pass to get_metrics for a multi-year trend. |
| `get_category_breakdown` | Return one accounting year's income and expenses per accounting category and account. Finer than get_accounting_stat's income statement: each family (sales, operating costs, personnel…) is split into the categories booked, with their account numbers; also gives the year's results (gross margin, EBITDA, operating, fiscal) and the growth against last year. |
| `list_metrics` | List the company's monthly accounting time series, from its first year to now. Revenue, expenses, income-statement lines, cash per account, shareholder accounts. Pass a `name` to get_metrics. `balance: true` marks a running balance rather than a flow. |
| `get_metrics` | Return the values of accounting time series, in euros, for trends over several years. Each point is dated on the last day of its period; the last one may be a period in progress (`partial: true`). |
| `list_investments` | List the company's fixed assets (immobilisations) and their depreciation, with totals. Each asset gives its purchase, depreciation plan, amounts already written off and the sale date if it was disposed of. |
| `list_loans` | List the company's loans: amount, start date, duration in months, rate, contract files. |
| `list_declarations` | List the company's tax filings (VAT, corporate tax, CFE, accounts approval), latest due first. Gives the period, due date, amount and filing state of each; get_declaration returns the figures of one filing. |
| `get_declaration` | Return one tax filing with its non-zero form boxes (box code, label, amount) and files. Confirming or correcting a filing stays a step in the Dougs app. |
| `get_company_summary` | Return the accountant's summary of the company for one year. Legal form, tax and VAT regimes with their history, capital, partners and employees, activity, and the Kbis and bylaws files. |

### Files and raw access

| Tool | What it does |
|---|---|
| `get_file_url` | Turn a Dougs file path into a signed download URL (public, short-lived). |
| `raw_get` | Authenticated GET on any Dougs API path, raw JSON back: for what no tool covers yet. |

### Editing an operation

Dougs models an operation as accounting lines (breakdowns): the categorized line(s) plus the bank
counterpart. VAT follows the category, and a category may need answers before the line is
complete (the VAT exemption reason of a 0 % line, the supplier, the loan…). A typical pass:

1. `get_operation`: the lines, their ids and their open `questions`.
2. `list_available_categories`: a valid `category_id` for one line.
3. `set_breakdown_category`: the result shows the recomputed VAT and any new question.
4. `list_question_options`, then `set_breakdown_association` or `set_breakdown_vat`.
5. `validate_operation`.

`split_operation` replaces an operation's lines when one transaction covers several categories.

### Financial analysis

`get_accounting_stat` and `get_category_breakdown` work on one fiscal year. For trends across
years, use the monthly time series:

```
list_metrics(search="chiffre")
get_metrics(["chiffre-d-affaires", "charges-d-exploitation"], group="year")
```

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
./scripts/check   # lockfile, ruff, mypy, and a boot of the server; CI runs the same
```

`AGENTS.md` holds the tool design rules.
