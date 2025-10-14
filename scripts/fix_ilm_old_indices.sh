#!/bin/bash
# Fix old indices that have ILM policies but no rollover alias configured
# This script removes ILM policies from indices older than today

set -e

ELASTICSEARCH_HOST="${ELASTICSEARCH_HOST:-localhost:9200}"
CURRENT_DATE=$(date +%Y-%m-%d)

echo "Checking for old indices with ILM policies but no rollover alias..."

# Get all indices with ILM policies
MANAGED_INDICES=$(curl -s "http://${ELASTICSEARCH_HOST}/*/_ilm/explain" | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
for idx, details in data['indices'].items():
    if details.get('managed', False):
        print(idx)
")

# Check each managed index
for INDEX in $MANAGED_INDICES; do
    # Skip current date indices
    if [[ "$INDEX" == *"$CURRENT_DATE"* ]]; then
        echo "Skipping current index: $INDEX"
        continue
    fi
    
    # Check if index has rollover alias
    SETTINGS=$(curl -s "http://${ELASTICSEARCH_HOST}/${INDEX}/_settings")
    HAS_ALIAS=$(echo "$SETTINGS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for idx, details in data.items():
    alias = details.get('settings', {}).get('index', {}).get('lifecycle', {}).get('rollover_alias')
    if alias:
        print('yes')
    else:
        print('no')
" | head -1)
    
    if [[ "$HAS_ALIAS" == "no" ]]; then
        echo "Removing ILM policy from old index: $INDEX"
        curl -s -X PUT "http://${ELASTICSEARCH_HOST}/${INDEX}/_settings" \
            -H "Content-Type: application/json" \
            -d '{"index.lifecycle.name": null}' | \
            python3 -c "import sys, json; print('✓ Success' if json.load(sys.stdin).get('acknowledged') else '✗ Failed')"
    else
        echo "Index $INDEX has rollover alias, keeping ILM policy"
    fi
done

echo "Done!"
