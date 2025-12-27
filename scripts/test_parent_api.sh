#!/bin/bash
# Test script to verify parent_issue_url field access

echo "Testing parent_issue_url extraction for issue #149..."
PARENT_URL=$(docker exec clauditoreum-orchestrator-1 bash -c 'gh api repos/tinkermonkey/context-studio/issues/149 --jq -r ".parent_issue_url"' 2>&1)
echo "Parent URL: $PARENT_URL"

echo ""
echo "Testing parent issue number extraction from URL..."

if [ "$PARENT_URL" != "null" ] && [ -n "$PARENT_URL" ] && [ "$PARENT_URL" != "" ]; then
    PARENT_NUM=$(echo "$PARENT_URL" | sed 's|.*/issues/||')
    echo "✓ Found parent issue: #$PARENT_NUM"
else
    echo "✗ No parent issue found"
fi

echo ""
echo "Testing with issue #2 (closed, has parent #1 in body)..."
PARENT_URL2=$(docker exec clauditoreum-orchestrator-1 bash -c 'gh api repos/tinkermonkey/context-studio/issues/2 --jq -r ".parent_issue_url"' 2>&1)
echo "Parent URL: $PARENT_URL2"

if [ "$PARENT_URL2" != "null" ] && [ -n "$PARENT_URL2" ] && [ "$PARENT_URL2" != "" ]; then
    PARENT_NUM2=$(echo "$PARENT_URL2" | sed 's|.*/issues/||')
    echo "✓ Found parent issue: #$PARENT_NUM2"
else
    echo "✗ No parent issue found (may not be set in GitHub's parent field)"
fi
