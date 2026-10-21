# Mock REST API for API-to-CSV testing

This adds a local REST API to the enterprise API-to-CSV package.

## 1. Start the API

From the package root:

```bash
python mock_api/mock_api.py
```

The server runs at:

```text
http://localhost:8000
```

Endpoints:

```text
GET  /health
GET  /orders?page=1&page_size=5
POST /orders/search
```

The `/orders` response contains:

```text
orders[]
  ├── customer{}
  │    └── address{}
  ├── items[]
  │    └── suppliers[]
  └── shipping{}
       └── address{}
```

This deliberately exercises recursive flattening and nested array expansion.

## 2. Test GET + pagination

In another terminal:

```bash
python api_to_csv.py --config config/orders_get.yaml
```

This tests:

- GET
- query parameters
- next-URL pagination
- nested objects
- nested arrays
- deep nested arrays
- raw response archive
- schema discovery
- audit logging

Output:

```text
output/orders_get.csv
```

## 3. Test POST

```bash
python api_to_csv.py --config config/orders_post.yaml
```

This sends:

```json
{
  "status": "ACTIVE",
  "min_qty": 2
}
```

and extracts `result.orders`.

Output:

```text
output/orders_post.csv
```

## 4. Test deep nesting without pagination

```bash
python api_to_csv.py --config config/orders_deep.yaml
```

## 5. Run everything automatically

```bash
python scripts/run_end_to_end_test.py
```

This starts the mock API, runs all three ingestion tests, validates the CSVs, and stops the API.

No internet connection is required.

## Example output

Because the API contains nested suppliers, the CSV contains repeated parent fields, for example:

```text
order_id,customer_name,customer_address_city,items_product,items_qty,items_suppliers_name,items_suppliers_country
10002,Raj,Bangalore,Mouse,3,XYZ Global,Singapore
10002,Raj,Bangalore,Mouse,3,Backup Supplier,India
...
```

The exact row count is larger than the number of orders because nested arrays are exploded into rows.
