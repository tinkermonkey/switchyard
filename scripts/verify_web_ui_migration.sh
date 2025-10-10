#!/bin/bash
# Verify Web UI Migration

echo "========================================"
echo "Web UI Migration Verification"
echo "========================================"
echo ""

echo "✓ Step 1: Check old web_ui_v2 directory doesn't exist"
if [ -d "web_ui_v2" ]; then
    echo "  ✗ FAIL: web_ui_v2 still exists"
    exit 1
else
    echo "  ✓ PASS: web_ui_v2 directory removed"
fi

echo ""
echo "✓ Step 2: Check new web_ui directory exists"
if [ -d "web_ui" ]; then
    echo "  ✓ PASS: web_ui directory exists"
else
    echo "  ✗ FAIL: web_ui directory not found"
    exit 1
fi

echo ""
echo "✓ Step 3: Check for any remaining web_ui_v2 references"
# Exclude the migration documentation and this script itself
if grep -r "web_ui_v2" . \
    --exclude-dir=.git \
    --exclude-dir=node_modules \
    --exclude-dir=.venv \
    --exclude="*.md~" \
    --exclude-dir=orchestrator_data \
    --exclude="WEB_UI_MIGRATION_COMPLETE.md" \
    --exclude="verify_web_ui_migration.sh" \
    2>/dev/null | grep -v "Binary file"; then
    echo "  ✗ FAIL: Found web_ui_v2 references"
    exit 1
else
    echo "  ✓ PASS: No web_ui_v2 references found (excluding migration docs)"
fi

echo ""
echo "✓ Step 4: Check docker-compose.yml"
if grep -q "build: ./web_ui" docker-compose.yml; then
    echo "  ✓ PASS: docker-compose.yml updated"
else
    echo "  ✗ FAIL: docker-compose.yml not updated"
    exit 1
fi

echo ""
echo "✓ Step 5: Check package.json"
if grep -q '"name": "web_ui"' web_ui/package.json; then
    echo "  ✓ PASS: package.json updated"
else
    echo "  ✗ FAIL: package.json not updated"
    exit 1
fi

echo ""
echo "✓ Step 6: Test Docker build"
echo "  Building web-ui service..."
if docker-compose build web-ui > /dev/null 2>&1; then
    echo "  ✓ PASS: Docker build succeeded"
else
    echo "  ✗ FAIL: Docker build failed"
    exit 1
fi

echo ""
echo "========================================"
echo "✅ All Verification Checks Passed!"
echo "========================================"
echo ""
echo "Migration Complete:"
echo "  - Old web_ui removed"
echo "  - web_ui_v2 renamed to web_ui"
echo "  - All references updated"
echo "  - Docker build working"
echo ""
echo "Next Steps:"
echo "  1. Run: docker-compose up web-ui"
echo "  2. Access: http://localhost:3000"
echo "  3. Or dev mode: cd web_ui && npm run dev"
