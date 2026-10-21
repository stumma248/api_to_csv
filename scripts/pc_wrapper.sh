#!/bin/sh
# Informatica PowerCenter Unix Command Task wrapper.
SCRIPT="/opt/api_ingestion/api_to_csv.py"
CONFIG="/opt/api_ingestion/config/orders.yaml"

python3 "$SCRIPT" --config "$CONFIG"
RC=$?

echo "API_TO_CSV_EXIT_CODE=$RC"
exit $RC
