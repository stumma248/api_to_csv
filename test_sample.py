import json
from api_to_csv import expand_node, discover_columns, normalize_columns

with open("sample_response.json", encoding="utf-8") as f:
    payload = json.load(f)

rows = expand_node(payload, array_mode="cartesian")
columns = discover_columns(rows)

print("ROWS:", len(rows))
print("COLUMNS:", columns)
for row in rows:
    print(row)

assert len(rows) == 3
assert "orders_order_id" in columns or "orders" in columns or "order_id" in columns
print("SAMPLE TEST PASSED")
