# dougs-mcp

[![CI](https://github.com/mediavee/dougs-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/mediavee/dougs-mcp-server/actions/workflows/ci.yml)
[![Licence : MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

Confiez votre comptabilité [Dougs](https://www.dougs.fr) à un agent IA : catégoriser les
opérations bancaires, rattacher les justificatifs, préparer et envoyer les factures, suivre la TVA
et les déclarations.

> [!WARNING]
> **Non officiel.** Ce serveur passe par l'API privée de l'application web Dougs, avec vos
> propres identifiants, et peut casser quand Dougs la modifie. Le MCP officiel de Dougs sert les
> applications Claude et ChatGPT ; celui-ci vise l'usage sans surveillance : Claude Code, scripts,
> agents auto-hébergés.

## Ce que vous pouvez demander

- *« Quelles opérations d'octobre n'ont pas encore de justificatif ? »*
- *« Catégorise les paiements OVH en hébergement et valide-les. »*
- *« Prépare une facture à ACME pour 5 jours de développement à 600 € HT. »*
- *« Comment ont évolué le chiffre d'affaires et les charges sur trois ans ? »*
- *« Qu'est-ce que Dougs attend de moi, et ma synchro bancaire fonctionne-t-elle ? »*

Tout ce qu'un client peut faire dans l'application Dougs est un outil (79), actions
irréversibles comprises. Les réponses sont des vues compactes, calibrées pour le contexte d'un
agent, pas les payloads bruts de l'API.

## Démarrage rapide

Il faut Python ≥ 3.13 et [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/mediavee/dougs-mcp-server && cd dougs-mcp-server
uv sync
cp .env.example .env
```

Remplissez `.env` :

```bash
DOUGS_EMAIL=vous@exemple.fr
DOUGS_PASSWORD=votre-mot-de-passe
DOUGS_TOTP_SECRET=JBSWY3DPEHPK3PXP   # si le compte a la double authentification (voir plus bas)
# DOUGS_COMPANY_ID=12345             # facultatif ; par défaut, votre société préférée
```

Puis déclarez le serveur dans votre client MCP.

**Claude Code**

```bash
claude mcp add dougs -- uv --directory /chemin/absolu/vers/dougs-mcp-server run dougs-mcp
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "dougs": {
      "command": "uv",
      "args": ["--directory", "/chemin/absolu/vers/dougs-mcp-server", "run", "dougs-mcp"]
    }
  }
}
```

**Hermes** (`~/.hermes/config.yaml`)

```yaml
mcp_servers:
  dougs:
    command: /chemin/absolu/vers/dougs-mcp-server/.venv/bin/dougs-mcp
    cwd: /chemin/absolu/vers/dougs-mcp-server
    trust: untrusted
```

Deux choix y comptent :
- **`.venv/bin/dougs-mcp` plutôt que `uv run`** : `uv run` garde le verrou du cache de uv tant que
  le serveur tourne, et `uv cache prune` ne passe plus.
- **`trust: untrusted`** : Hermes demande alors une validation avant chaque appel qui n'est pas en
  lecture seule. Gardez-le.

Ensuite, liez le serveur aux plateformes voulues dans `platform_toolsets`. Hermes ne relance pas
`uv sync` : refaites-le après chaque mise à jour.

**Autre client** : lancez `.venv/bin/dougs-mcp` en stdio, avec le dossier du repo comme
répertoire de travail (le chemin de `.env` est relatif).

### Double authentification

Dougs peut demander un second facteur à la connexion. Sans surveillance, il faut le facteur
**application d'authentification** : activez-le dans Dougs, puis mettez dans `DOUGS_TOTP_SECRET`
le paramètre `secret` du lien `otpauth://` affiché (ou le lien entier). Un compte qui n'a que le
facteur e-mail ne peut pas se connecter sans vous. Avec le mot de passe, ce secret est une
seconde clé du compte : gardez `.env` privé.

## Sécurité

Chaque outil porte des annotations MCP, et c'est votre client qui décide de ce qui demande votre
accord :

| Marque | Signification | Exemples |
|---|---|---|
| | Lecture seule | `list_operations`, `get_declaration` |
| ✏️ | Modifie, réversible | `save_sales_invoice_draft`, `validate_operation` |
| ⚠️ | Irréversible ou supprime | `finalize_sales_document`, `delete_vendor_invoice` |
| 📤 | Irréversible et touche un tiers | `send_sales_document`, `confirm_declaration` |

Laissez votre client demander avant les appels ⚠️ et 📤 : Claude Code demande avant tout appel
MCP que vous n'avez pas autorisé, et Hermes avant chaque écriture avec `trust: untrusted`. Les
outils irréversibles refont aussi les contrôles de l'application avant d'agir :
`finalize_sales_document` lance d'abord la vérification de Dougs, `confirm_declaration` refuse
tant que la déclaration a des erreurs ou que ses engagements n'ont pas été acceptés.

## Outils

Les outils agissent sur une société par session : `get_account` liste les vôtres,
`select_company` en change. Les descriptions sont en anglais : c'est ce que lit l'agent.

<details>
<summary><b>Compte et société</b> (10 outils)</summary>

| Outil | Ce qu'il fait |
|---|---|
| `get_account` | Return who is logged in and the companies they can act on; every tool acts on the one flagged `isDefault` (switch with select_company). get_company gives its settings. |
| `select_company` ✏️ | Switch the company every other tool acts on, for the rest of the session. |
| `get_company` | Return a company's identity and accounting settings: legal form, SIREN, activity, VAT regime and periodicity, tax regime, current fiscal year, lock date and Dougs plan. |
| `list_company_people` | List the people and companies holding a role in the company: officers (gérant, président), associates with their shares, and employees, with each role's dates. |
| `list_bank_accounts` | List the company's bank and treasury accounts (Qonto, PayPal, Stripe…) with their balance. `filterValue` goes to list_operations(treasury_account_id=...); list_bank_connections tells whether their synchronization works. |
| `list_bank_connections` | Report the health of each bank connection: status, the action the bank asks for (such as a strong authentication), when it last fetched data and imported transactions, and its accounts. sync_bank_connection forces a sync. |
| `sync_bank_connection` ✏️ | Synchronize one bank connection and return its state afterwards. A connection whose bank asks for a strong authentication or new credentials cannot be fixed here: the user renews it in the Dougs app, in a browser. |
| `list_customer_todos` | List what Dougs is waiting for from the customer (documents to attach, a VAT return to complete, an account to regularize), with due dates and whether each is overdue, then the tips Dougs suggests (`tip: true`). resolve_customer_todo acts on them. |
| `resolve_customer_todo` ⚠️ | Complete, dismiss or reopen a to-do Dougs is waiting on, or dismiss a tip. Dougs reads a completed to-do as the customer's answer; to-dos whose app step asks questions (a consent, a pro space check) are better completed in the app. |
| `list_billing_invoices` | List the invoices Dougs bills the company for its subscription, newest first, with the upcoming one and the plan. Pass an invoice's `filePath` to get_file_url to download its PDF. |

</details>

<details>
<summary><b>Opérations bancaires</b> (19 outils)</summary>

| Outil | Ce qu'il fait |
|---|---|
| `list_operations` | Search bank operations, newest first, one compact row each: signed amount (negative = money out), category labels, validation state and whether a receipt is attached. Call get_operation for an operation's lines, VAT, questions and receipts. |
| `count_operations` | Count the operations matching the filters that are still to validate, need attention, or are validated. Dougs stops counting at 100, reported as "100+". |
| `validate_operation` ✏️ | Validate an operation, confirming its categorization; an uncategorized one is filed under "Non catégorisé". Returns the operation as get_operation does. |
| `request_categorization_help` 📤 | Ask the Dougs accountant to categorize an operation; they answer in the app. Returns the operation as get_operation does. |
| `get_operation` | Return one operation with its accounting lines (category, VAT, what is editable, the questions Dougs asks about each), its receipts and any suggested receipt. |
| `update_operation` ✏️ | Set an operation's memo, or change the date or amount of an expense report or cash receipt created with create_operation. |
| `create_operation` ✏️ | Create an expense report or a cash receipt, to validate like a bank operation. Attach its receipt with add_attachment. |
| `create_mileage_allowance` ✏️ | Record a mileage allowance (indemnité kilométrique) for a business trip in a personal vehicle; Dougs computes the amount from the official scale. Delete it with delete_operation. |
| `delete_operation` ⚠️ | Delete an expense report, cash receipt or mileage allowance for good. Bank transactions cannot be deleted. |
| `list_available_categories` | List the categories Dougs accepts for one line, suggested ones first: pick set_breakdown_category's category_id here. `asksQuestions` marks categories whose VAT is settled by a question once assigned. |
| `search_categories` | Search the company's category catalog, or resolve category ids to labels. To categorize an existing line, list_available_categories is narrower: it returns only what Dougs accepts for that line. |
| `list_question_options` | List the accepted answers to one question of a line (VAT exemption reasons, suppliers, loans…); pass an item's `value` to set_breakdown_association. |
| `set_breakdown_category` ⚠️ | Change the category of one line. Re-read `questions` in the result: Dougs drops the answers the new category does not use, and does not restore them on switching back. |
| `set_breakdown_association` ✏️ | Answer one of the questions Dougs asks about a line (supplier, loan, partner, period…). |
| `apply_to_similar_operations` ⚠️ | Copy an operation's categorization onto the operations Dougs finds similar, as the app does after a change; returns the operations it changed. |
| `set_breakdown_vat` ✏️ | Override the VAT of one line. The rate comes from the category: change it with set_breakdown_category. |
| `split_operation` ⚠️ | Split an operation into several categorized lines. A result with isTotalAmountValid false means the amounts do not add up. |
| `add_attachment` ✏️ | Attach a receipt or invoice file to an operation. Dougs files it as a vendor invoice in Purchases and reads its date and amount within a few seconds. |
| `remove_attachment` ✏️ | Detach a receipt from an operation. The document itself stays in Purchases as a vendor invoice; delete it there if it was attached by mistake. |

</details>

<details>
<summary><b>Achats</b> (9 outils)</summary>

| Outil | Ce qu'il fait |
|---|---|
| `list_vendor_invoices` | List vendor (supplier) invoices, most recent first (undated ones on top), as compact rows. Also returns the total still to pay in EUR across all unpaid invoices. Use get_vendor_invoice for one invoice's detail and file path. |
| `get_vendor_invoice` | One vendor invoice in detail: supplier, amounts, VAT breakdown, OCR status, refusal, attached and suggested operations, the fields update_vendor_invoice may change, and the file_path to pass to get_file_url. |
| `upload_vendor_invoice` ✏️ | Upload a supplier invoice file; Dougs reads it by OCR within seconds. Returns the new invoice; call get_vendor_invoice again once `ocr` is no longer 'initialised' to see what it read, and fix it with update_vendor_invoice. |
| `update_vendor_invoice` ✏️ | Correct the fields of a vendor invoice, typically what the OCR misread; only the fields given change. E-invoices received through the e-invoicing network are locked except memo. |
| `delete_vendor_invoice` ⚠️ | Delete a vendor invoice and its file. Dougs refuses while it is attached to a validated operation. |
| `attach_vendor_invoice` ✏️ | Attach a vendor invoice to the bank operations that paid it, as their receipt, or detach it. Dougs refuses to detach from a validated operation: un-validate it first with validate_operation, then validate it again. The invoice's payment status follows a few seconds later, so the status in this answer is the previous one. |
| `review_vendor_invoice_match` ⚠️ | Accept or reject the bank operation Dougs suggests as the payment of a vendor invoice (suggested_operation in get_vendor_invoice). To attach another operation, use attach_vendor_invoice. |
| `set_vendor_invoice_status` 📤 | Mark a vendor invoice paid or unpaid, or refuse an e-invoice. On an e-invoice the status reaches the supplier through the e-invoicing network ('Paiement transmis', or the refusal with its reason). |
| `move_vendor_invoice_to_sales` ⚠️ | Move an uploaded document filed by mistake under purchases to the sales invoices; it gets a new id there. Dougs refuses to move it back. |

</details>

<details>
<summary><b>Ventes</b> (26 outils)</summary>

| Outil | Ce qu'il fait |
|---|---|
| `list_sales_invoices` | List finalized sales invoices, newest first: number, client, amounts, payment status and the bank operations they are matched or suggested to. Drafts are in list_sales_invoice_drafts. |
| `get_sales_overview` | Sales dashboard: invoice and quote counts, issued/waiting/late amounts, receivables aging, and the year's net revenue by month and by client. |
| `list_customers` | List registered customers (the address book used for invoices and quotes). |
| `save_customer` ✏️ | Create a customer, or change one (only the arguments passed change). Documents already issued keep the client details they were made with. |
| `delete_customer` ⚠️ | Delete a registered customer. To keep it out of lists without deleting it, archive it with save_customer. |
| `list_sales_invoice_drafts` | List sales invoice drafts (not finalized) with client, dates and totals; read the lines with get_sales_invoice_draft. |
| `get_sales_invoice_draft` | Read a sales invoice draft: client, dates, lines, totals, its app URL and what still blocks finalizing it. |
| `save_sales_invoice_draft` ✏️ | Create a sales invoice draft, or change one (only the arguments passed change; a new date or delay recomputes the due date). Dougs computes the totals; finalize it with finalize_sales_document. |
| `delete_sales_invoice_draft` ⚠️ | Delete a sales invoice draft. Finalized invoices cannot be deleted here. |
| `list_quotes` | List quotes (devis), drafts included, newest first: number, status, client, dates and totals. Read a draft's lines with get_quote_draft. |
| `get_quote_draft` | Read a quote draft: client, dates, lines, totals, its app URL and what still blocks finalizing it. |
| `save_quote_draft` ✏️ | Create a quote (devis) draft, or change one (only the arguments passed change; a new date or validity recomputes the expiration). Dougs computes the totals; finalize it with finalize_sales_document. |
| `delete_quote_draft` ⚠️ | Delete a quote draft. Issued quotes cannot be deleted here. |
| `finalize_sales_document` ⚠️ | Finalize an invoice or quote draft: Dougs numbers it, issues its PDF and freezes it. Fails with what is missing when the draft is incomplete; send it with send_sales_document. |
| `send_sales_document` 📤 | Email a finalized invoice or quote to the client, with its PDF attached. |
| `duplicate_sales_document` ✏️ | Copy an invoice or quote, finalized or draft, into a new draft. |
| `refund_sales_invoice` ⚠️ | Create the credit note (avoir) that cancels a finalized invoice, as a draft with the amounts negated: adjust it with save_sales_invoice_draft, then finalize it. |
| `set_sales_invoice_paid` ✏️ | Mark a finalized invoice paid or unpaid by hand, when no bank operation settles it. |
| `review_sales_invoice_match` ⚠️ | Accept or reject the bank operation Dougs suggests for a sales invoice (suggestedOperationId in list_sales_invoices). |
| `refuse_sales_invoice` 📤 | Refuse an electronic invoice wrongly addressed to you: Dougs cancels it and reports the refusal to its issuer. |
| `set_quote_status` ⚠️ | Record a finalized quote as accepted or refused (final), or reopen it as a draft. |
| `create_invoice_from_quote` ⚠️ | Turn a finalized quote into an invoice draft with its lines; the quote is marked accepted. |
| `list_invoice_products` | List the company's saved products and services, with their unit price and VAT rate, to reuse as invoice or quote lines. |
| `save_invoice_product` ✏️ | Save a product or service to reuse on invoices and quotes, or change one (only the arguments passed change). |
| `delete_invoice_product` ⚠️ | Delete a saved product. Invoices and quotes that used it keep their lines. |
| `list_sales_channels` | List the sales channels declared to Dougs (website, marketplace, shop...) and the payment processors attached to each. |

</details>

<details>
<summary><b>Comptabilité et fiscalité</b> (13 outils)</summary>

| Outil | Ce qu'il fait |
|---|---|
| `list_accounting_years` | List the company's accounting (fiscal) years, the active one flagged `active`. Closed years carry their closing documents (balance sheet, FEC, final print), whose `url` goes to get_file_url. |
| `get_accounting_stat` | Return an aggregated figure of one accounting year (totals; monthly values on request). Income-statement lines carry a `seriesName` to pass to get_metrics for a multi-year trend. |
| `get_category_breakdown` | Return one accounting year's income and expenses per accounting category and account. Finer than get_accounting_stat's income statement: each family (sales, operating costs, personnel…) is split into the categories booked, with their account numbers; also gives the year's results (gross margin, EBITDA, operating, fiscal) and the growth against last year. |
| `list_metrics` | List the company's monthly accounting time series, from its first year to now. Revenue, expenses, income-statement lines, cash per account, shareholder accounts. Pass a `name` to get_metrics. `balance: true` marks a running balance rather than a flow. |
| `get_metrics` | Return the values of accounting time series, in euros, for trends over several years. Each point is dated on the last day of its period; the last one may be a period in progress (`partial: true`). |
| `list_investments` | List the company's fixed assets (immobilisations) and their depreciation, with totals. Each asset gives its purchase, depreciation plan, amounts already written off and the sale date if it was disposed of. |
| `list_loans` | List the company's loans: amount, start date, duration in months, rate, contract files. |
| `list_declarations` | List the company's tax filings (VAT, corporate tax, CFE, accounts approval), latest due first. Gives the period, due date, amount and filing state of each; get_declaration returns the figures of one filing. |
| `get_declaration` | Return one tax filing with its non-zero form boxes (box code, label, amount) and files, what blocks its confirmation, the statements confirming it certifies, and whether confirm_declaration or revise_declaration applies. |
| `confirm_declaration` 📤 | File a tax return: Dougs teletransmits it to the tax administration with the payment it authorizes, or records a manual deposit as done. |
| `revise_declaration` ⚠️ | Reopen a confirmed tax return for changes, or cancel a corrective one. A return that was teletransmitted (`depositMode: auto`) sends a corrective filing when confirmed again. |
| `run_remote_filing_setup` 📤 | Run Dougs' e-filing (télédéclaration) setup assistant and return each step's status: tax regime, IBAN, then the check of the impots.gouv.fr professional space (up to 12 hours). |
| `get_company_summary` | Return the accountant's summary of the company for one year. Legal form, tax and VAT regimes with their history, capital, partners and employees, activity, and the Kbis and bylaws files. |

</details>

<details>
<summary><b>Fichiers et accès brut</b> (2 outils)</summary>

| Outil | Ce qu'il fait |
|---|---|
| `get_file_url` | Turn a Dougs file path into a signed download URL (public, short-lived). |
| `raw_get` | Authenticated GET on any Dougs API path, raw JSON back: for what no tool covers yet. |

</details>

## Parcours courants

### Catégoriser une opération

Dougs découpe une opération en lignes (breakdowns) : la ou les lignes catégorisées et la
contrepartie bancaire. La TVA suit la catégorie, et certaines catégories posent des questions
avant que la ligne soit complète (motif d'exonération d'une ligne à 0 %, fournisseur, emprunt…).

1. `get_operation` : les lignes, leurs ids et leurs `questions` ouvertes.
2. `list_available_categories` : un `category_id` valide pour une ligne.
3. `set_breakdown_category` : la réponse montre la TVA recalculée et les nouvelles questions.
4. `list_question_options`, puis `set_breakdown_association` ou `set_breakdown_vat`.
5. `validate_operation`.

`split_operation` remplace les lignes quand une transaction couvre plusieurs catégories.

### Facturer

1. `save_sales_invoice_draft` avec un client (enregistré, ou toute entreprise française par son
   SIREN) et des lignes ; Dougs calcule les totaux.
2. `get_sales_invoice_draft` montre ce qui bloque encore la finalisation (`finalizeProblems`).
3. `finalize_sales_document`, puis `send_sales_document`.

### Tendances sur plusieurs années

`get_accounting_stat` et `get_category_breakdown` portent sur un exercice. Pour les tendances,
utilisez les séries mensuelles :

```
list_metrics(search="chiffre")
get_metrics(["chiffre-d-affaires", "charges-d-exploitation"], group="year")
```

## Fonctionnement

- **Connexion** : e-mail et mot de passe sur `POST /auth/api/login`, plus le code TOTP quand Dougs
  le demande.
- **Session** : le cookie est mis en cache dans `~/.cache/dougs-mcp/` (mode `600`) et réutilisé
  d'un redémarrage à l'autre, car Dougs limite à 25 connexions par heure et par compte ; il est
  renouvelé sur un `401`.
- **Périmètre** : les endpoints réservés aux experts-comptables (grand livre, exports) répondent
  `403` à un compte client et n'ont pas d'outil. `raw_get` atteint tous les autres chemins.

## Développement

```bash
./scripts/check   # lockfile, ruff, ruff format, mypy et démarrage du serveur ; la CI lance le même
```

[`AGENTS.md`](AGENTS.md) contient les règles de conception des outils : lisez-le avant d'en
ajouter un. Les payloads sont vérifiés sur un vrai compte, jamais devinés.
