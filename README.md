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
  The session cookie is kept in an httpx cookie jar and refreshed transparently
  on `401`.
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
# DOUGS_COMPANY_ID=18533   # optional; defaults to your preferred company
```

## Tools

### Read tools

| Tool | Description |
|------|-------------|
| `get_me` | Authenticated user profile |
| `list_companies` | Companies accessible to the user |
| `get_company` | A company's accounting configuration |
| `list_operations` | List/search bank operations — `query` (text), `amount` (`>1000`, `<500`, `1000-2000`), `date_from`/`date_to` (ISO), `inbound` (entrées/sorties), `operation_type`, `treasury_account_id`, `validated`, `needs_attention`; paginated via `limit` (max 500) / `offset` |
| `get_categories` | Resolve operation category ids (`resolvedCategoryId`) to labels — wording, accounting group, account number |
| `list_sales_channels` | Configured sales channels |
| `get_active_accounting_year` | Current fiscal year (id, opening/closing dates) |
| `list_accounting_years` | All fiscal years |
| `get_accounting_stat` | Aggregated stat for a fiscal year: revenue, income statement, operating result, charges, cash, VAT tracking, corporate tax, remunerations… (see `stat_type`) |
| `list_sales_invoices` | Customer invoices (client, amount, VAT, due date, status) |
| `list_vendor_invoices` | Supplier invoices |
| `get_invoices_overview` | Sales & vendor invoice counts and amounts (paid/waiting/late/draft) |
| `get_product_catalog` | Product/service catalog |
| `list_partners` | Customers and suppliers |
| `list_bank_accounts` | Bank/treasury accounts (Qonto, PayPal…) with balances and a ready `filterValue` for `treasury_account_id` |
| `get_file_url` | Resolve a Dougs file path (`/files/…`) to a downloadable signed S3 URL |
| `raw_get` | Low-level read-only GET on any API path (for unwrapped endpoints) |

`get_accounting_stat` `stat_type` values: `chiffre-d-affaires`, `compte-de-resultat`,
`resultat-d-exploitation`, `charges-d-exploitation`, `repartition-des-charges`,
`tresorerie-compte-treso`, `flux-de-tresorerie-compte-treso`, `suivi-tva`,
`suivi-impots-societes`, `suivi-cfe`, `remunerations`, `remunerations-for-accounting-year`,
`tns-social-charges`, `fonds-propres`, `comptes-d-associes`, `autres-reserves-reports-a-nouveau`.

### Write tools

These **modify** your accounting data:

| Tool | Description |
|------|-------------|
| `validate_operation` | Validate / un-validate an operation (confirms its categorization; `validated=False` re-opens it) |
| `attach_justificatif` | Attach a local file (PDF/image receipt or invoice) to an operation |
| `detach_justificatif` | Remove an attachment from an operation (by `attachment_id`) |

## Use with Claude Code

```bash
claude mcp add dougs -- uv --directory /Users/falistos/Workspace/dougs-mcp run dougs-mcp
```

Credentials are read from the project's `.env` (thanks to `--directory`).

## Use with Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dougs": {
      "command": "uv",
      "args": ["--directory", "/Users/falistos/Workspace/dougs-mcp", "run", "dougs-mcp"]
    }
  }
}
```

## Development

```bash
uv run ruff check src
uv run mypy src
```
