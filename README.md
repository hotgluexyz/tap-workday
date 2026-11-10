# tap-workday

Singer tap for Workday Finance reference data, using Workday Web Services SOAP
with WS-Security UsernameToken (PasswordText), not REST. Built with the Hotglue
Singer SDK. Python 3.10 is required.

## Streams

| Stream | Service / operation | Primary key |
| --- | --- | --- |
| `companies` | Financial_Management / Get_Workday_Companies | `id` (WID) |
| `cost_centers` | Financial_Management / Get_Cost_Centers | `id` (WID) |
| `spend_categories` | Financial_Management / Get_Resource_Categories | `id` (WID) |
| `ledger_accounts` | Financial_Management / Get_Account_Sets | `account_set_id`, `account_number` |
| `suppliers` | Resource_Management / Get_Suppliers | `id` (WID) |

All streams use **FULL_TABLE** replication: each sync refreshes the reference
snapshot, including inactive/retired entries. No incremental bookmark is claimed.
Numbered pages share a fixed effective date and entry timestamp per stream so
changes during extraction do not shift page boundaries.

Records expose coding IDs, names, and relevant status fields. Non-ledger streams
also include `reference_id` and a typed `references` array. `raw_xml` preserves
the complete source object (without the SOAP authentication header), including
relationships not yet flattened into columns. Treat these extracts as sensitive.

Ledger accounts are flattened from every returned account set, retaining the set
ID because account numbers are not globally unique. Included account sets are
returned by the unfiltered Get_Account_Sets request as their own objects; the tap
does not recursively duplicate accounts into the parent set. Use the set ID and
account number together for downstream mapping. This is a read-only tap; posting
card transactions is outside its scope.

## Configuration

Store credentials in a git-ignored `.secrets/config.json` (never commit it):

```json
{
  "host": "https://your-services-host.myworkday.com",
  "tenant": "your_tenant",
  "username": "integration_user@your_tenant",
  "password": "YOUR_PASSWORD",
  "api_version": "v48.0",
  "page_size": 100,
  "request_timeout": 120
}
```

`host`, `tenant`, `username`, and `password` are required. `host` must be an HTTPS
origin, not the complete service URL. The remaining values shown are defaults;
`page_size` accepts 1–999. URLs are built as
`{host}/ccx/service/{tenant}/{service}/{api_version}`.

The integration user needs Get access to the security domains securing each
selected operation. Activate pending security policy changes in Workday after
granting access. A SOAP “task submitted is not authorized” fault fails the sync
with the operation name rather than silently treating the stream as empty.
HTTP 429/502/503/504 and connection errors have bounded retries; rate-limit retries
honor Retry-After and otherwise use exponential backoff. SOAP faults
are not retried. Request bodies and credentials are never logged by the tap.

## Install and run

```bash
uv venv --python 3.10 --seed .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/tap-workday --config .secrets/config.json --discover > .secrets/catalog.json
```

Select streams and fields using Singer catalog `selected` metadata, saving the
result to `.secrets/catalog-selected.json`, then run:

```bash
.venv/bin/tap-workday --config .secrets/config.json \
  --catalog .secrets/catalog-selected.json > .secrets/data.singer
```

Discovery is offline and does **not** verify credentials or permissions. Only
selected streams make API requests. Do not select an inaccessible stream until
its Workday permissions have been granted.

## Development

```bash
uv pip install --python .venv/bin/python -e . --group dev
.venv/bin/pytest -q
.venv/bin/ruff check tap_workday tests
.venv/bin/ruff format --check tap_workday tests
```

Tests use synthetic SOAP responses, including multi-page extraction, typed IDs,
account-set-scoped keys, boolean/null conversion, permission faults, and retries.
Live acceptance must additionally verify the selected operations in your tenant.
