# dougs-mcp-server

MCP server over the private API of the Dougs web app (`app.dougs.fr`). Tools live in one module per
domain (`company`, `operations`, `purchases`, `sales`, `accounting`); `server.py` holds the `mcp`
instance and the shared helpers, `client.py` the authenticated HTTP client.

## Gate

`./scripts/check` (lockfile, ruff, ruff format, mypy, server boot). CI runs the same script. Run it
before every commit.

## Tool design

An agent pays for every tool twice: its definition sits in the context of every session, and its
answers fill the context of every call. Design for both.

- **Names**: `verb_noun` in snake_case (`list_vendor_invoices`, `get_operation`,
  `set_breakdown_vat`). One tool per task a person would name. Creating and changing a record is
  one `save_*` tool: without an id it creates, with one it changes only the arguments passed. Merge tools an agent would always
  call together; split a tool whose arguments switch between unrelated jobs.
- **Description**: one to three sentences: what it returns or changes, and when to pick it over a
  sibling. No restatement of the parameters, no history.
- **Parameters**: document each one with `Annotated[T, Field(description=...)]`, with an example
  value when the format is not obvious. Never rely on docstring sections: fastmcp parses Google,
  NumPy and Sphinx docstrings and drops the description text after the first section it recognises
  (`Filters:`, `Returns:`, `Note:`…). Tools take no company argument: they act on the session's
  company (`select_company`), which keeps it out of every definition.
- **Annotations**: every tool passes `annotations=READ`, `WRITE` (additive or reversible) or
  `DESTRUCTIVE` (deletes or overwrites data).
- **Answers**: return what the agent needs to decide and to make the next call, not the raw
  payload. Keep ids, labels, amounts, dates and statuses; drop internal flags, audit fields, nested
  duplicates and Dougs' UI metadata. Pass results through `compact()`. A list row should stay near
  100 tokens and a default page under 5 000 tokens; Claude Code truncates a tool answer at 25 000.
  Lists take filters and `limit`/`offset`, and say how to get the rest when they are cut.
- **Errors**: raise `ValueError` with the fix (valid values, the tool that lists them).
- **Out of reach on purpose**: finalizing or sending an invoice or a quote, confirming a tax
  filing, triggering a bank synchronization. These stay a human step in the Dougs app; tools stop
  at drafts and return the app URL.

`LeanTools` in `server.py` serves every definition with optional arguments flattened and no
output schema. The catalog is paid by every session: measure its size when you add a tool.

## Verifying against the live API

Payload shapes come from the API, not from guesses: check every new endpoint against a real
account before wrapping it. Logins are limited to 25 per hour per account; the client caches the
session under `~/.cache/dougs-mcp/`, so reuse it rather than logging in again. A write test creates
its own record and deletes it in the same run.
