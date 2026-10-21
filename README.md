# Enterprise REST API -> Flattened CSV Utility

## Purpose

Generic API ingestion utility for Informatica PowerCenter.

The program is deliberately schema-agnostic at runtime:

- JSON objects become columns
- JSON arrays become rows
- Parent fields are repeated
- Nested arrays are recursively expanded
- Column names are generated dynamically
- GET and POST are supported
- API pagination is configurable
- Authentication is configurable
- Output can be staged to CSV and loaded by PowerCenter

## Example

Input:

```json
{
  "orders": [
    {
      "order_id": 1001,
      "customer": {
        "name": "John"
      },
      "items": [
        {"product": "Laptop", "qty": 2},
        {"product": "Mouse", "qty": 3}
      ]
    }
  ]
}
```

Output:

```csv
order_id,customer_name,items_product,items_qty
1001,John,Laptop,2
1001,John,Mouse,3
```

For a deeper hierarchy, parent fields are repeated for every leaf row.

## Architecture

```text
             REST API
                 |
                 v
       +-------------------+
       | api_to_csv.py     |
       |                   |
       | Auth              |
       | GET/POST          |
       | Pagination        |
       | Retry             |
       | JSON flattening   |
       | Schema validation |
       | Masking           |
       | Audit             |
       +---------+---------+
                 |
                 v
              CSV
                 |
                 v
       PowerCenter Session
                 |
                 v
              Database
```

## Installation

```bash
python -m pip install -r requirements.txt
```

Use a dedicated virtual environment if possible.

## Secrets

Do NOT place production secrets directly in YAML.

Use environment variables:

```bash
export API_TOKEN="..."
export API_CLIENT_ID="..."
export API_CLIENT_SECRET="..."
```

Windows:

```bat
set API_TOKEN=...
set API_CLIENT_ID=...
set API_CLIENT_SECRET=...
```

The configuration supports:

```text
${VARIABLE}
${VARIABLE:-default}
```

## Authentication

### Bearer

```yaml
authentication:
  type: bearer
  token: "${API_TOKEN}"
```

### OAuth2 client credentials

```yaml
authentication:
  type: oauth2_client_credentials
  token_url: "https://auth.company.com/oauth/token"
  client_id: "${API_CLIENT_ID}"
  client_secret: "${API_CLIENT_SECRET}"
  scope: "orders.read"
```

The token is refreshed when it is close to expiry.

### Basic

The implementation can be extended with Basic authentication if your environment requires it.

## Pagination

Supported modes:

### None

```yaml
pagination:
  mode: none
```

### Page number

```yaml
pagination:
  mode: page_number
  page_param: page
  start_page: 1
  page_size: 100
  max_pages: 10000
  data_path: orders
```

### Offset

```yaml
pagination:
  mode: offset
  offset_param: offset
  limit_param: limit
  start_offset: 0
  page_size: 100
  max_pages: 10000
  data_path: orders
```

### Next URL

For:

```json
{
  "data": [...],
  "next": "https://api.company.com/orders?page=2"
}
```

use:

```yaml
pagination:
  mode: next_url
  data_path: data
  next_url_path: next
```

### Cursor

For:

```json
{
  "data": [...],
  "meta": {
    "next_cursor": "abc123"
  }
}
```

use:

```yaml
pagination:
  mode: cursor
  cursor_param: cursor
  next_cursor_path: meta.next_cursor
  data_path: data
```

## Nested arrays

The default is:

```yaml
array_mode: cartesian
```

Example:

```json
{
  "order_id": 1001,
  "items": [
    {"product": "A"},
    {"product": "B"}
  ],
  "payments": [
    {"type": "CARD"},
    {"type": "UPI"}
  ]
}
```

Cartesian produces:

```text
1001,A,CARD
1001,A,UPI
1001,B,CARD
1001,B,UPI
```

Use:

```yaml
array_mode: zip
```

to produce:

```text
1001,A,CARD
1001,B,UPI
```

This must be a business/data-model decision. A generic program cannot infer the intended relationship between unrelated sibling arrays.

## Schema drift

### Discover

Records the observed schema:

```yaml
schema:
  mode: discover
  file: "./audit/orders_schema.json"
```

### Strict

Fails if columns differ:

```yaml
schema:
  mode: strict
  columns:
    - order_id
    - customer_name
    - product
    - qty
```

The process returns exit code 3.

### Allow additive

Allows new columns but fails if an existing column disappears:

```yaml
schema:
  mode: allow_additive
  file: "./audit/orders_schema.json"
```

This is usually a good default for APIs whose schemas evolve.

## PII masking

Example:

```yaml
masking:
  mode: hash
  columns:
    - customer_email
    - customer_phone
```

Modes:

- hash
- full
- partial

Do this before CSV output.

## Raw response archive

Useful for reconciliation and debugging:

```yaml
raw_archive:
  enabled: true
  directory: "./raw/orders"
```

Be careful: raw API responses can contain PII or sensitive information. Apply retention, encryption and access controls in production.

## Audit

Every successful or failed run can create a JSONL audit record:

```json
{
  "run_id": "...",
  "api_name": "order_api",
  "status": "SUCCESS",
  "pages": 5,
  "rows": 1243,
  "columns": 12,
  "output_file": "..."
}
```

This can later be loaded into an operational database.

## PowerCenter

### Windows Command Task

Use:

```bat
C:\api_ingestion\pc_wrapper.bat
```

### Unix/Linux Command Task

Use:

```sh
/opt/api_ingestion/pc_wrapper.sh
```

The wrapper returns the Python process exit code.

Recommended workflow:

```text
Start
  |
  v
Command Task
  |
  +---- failure ----> Email/Alert/Stop
  |
 success
  |
  v
File Watch / validation
  |
  v
PowerCenter Session
  |
  v
Database
  |
  v
Audit
```

Do not allow the database session to run if the API utility fails.

## Exit codes

```text
0 = success
1 = runtime/unexpected error
2 = configuration/dependency error
3 = schema contract/drift failure
4 = API/authentication failure
5 = CSV/output/audit failure
```

PowerCenter can branch based on the Command Task return code.

## Production hardening checklist

Recommended before enterprise rollout:

1. Store secrets in enterprise secret manager.
2. Use TLS certificate validation.
3. Configure proxy if required.
4. Restrict raw response retention.
5. Add API-specific rate limits where needed.
6. Add API-specific pagination configuration.
7. Maintain schema contracts for critical feeds.
8. Add source-system request/correlation IDs.
9. Add idempotency for POST APIs where supported.
10. Add checksum/file-size validation before PowerCenter loads.
11. Add quarantine for malformed responses.
12. Monitor API latency and HTTP status.
13. Monitor row counts against expected ranges.
14. Add retry only for transient errors.
15. Do not blindly retry non-idempotent POST operations.
16. Avoid logging access tokens or request bodies containing secrets.
17. Use a dedicated service account.
18. Restrict filesystem permissions.
19. Apply PII masking before staging when required.
20. Add retention policies for CSV/raw/audit files.

## Important limitation

"No schema required" does not mean "no data-model decisions required."

The program can automatically flatten arbitrary JSON, but some structures are inherently ambiguous.

For example:

```json
{
  "items": [{"id": 1}, {"id": 2}],
  "payments": [{"type": "CARD"}, {"type": "UPI"}]
}
```

There is no way to know whether the intended relationship is:

- item 1 -> CARD and item 2 -> UPI
- every item -> every payment
- items and payments are separate child collections

Therefore `cartesian` vs `zip` is explicitly configurable.

For complex APIs with independent child collections, separate extraction configurations are preferable to inventing a relationship.

## Recommended enterprise pattern

```text
API
 |
 | HTTPS
 v
API-to-CSV Utility
 |
 +-- Authentication
 +-- Pagination
 +-- Retry/rate handling
 +-- Response validation
 +-- Recursive flattening
 +-- Schema drift
 +-- PII masking
 +-- Raw archive (optional)
 +-- Audit
 |
 v
CSV staging
 |
 v
PowerCenter
 |
 +-- Validation
 +-- Transform
 +-- Reject
 +-- Target
 |
 v
Database / DWH
```
