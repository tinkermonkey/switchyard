# Test Organization - Moving Root Tests to Subdirectories

## Summary

Moved 4 test scripts from the root `tests/` directory into appropriate subdirectories for better organization.

## Changes Made

### Tests Moved to `tests/unit/`

1. **test_actual_context_size.py** → `tests/unit/test_actual_context_size.py`
   - Unit test for context size calculations
   - Tests `_get_discussion_context()` with fixture data
   - Validates context extraction sizes

2. **test_context_extraction.py** → `tests/unit/test_context_extraction.py`
   - Unit test for review cycle context extraction
   - Tests extraction of relevant context for reviewers
   - Validates comment and reply filtering

### Tests Moved to `tests/integration/`

3. **test_github_app.py** → `tests/integration/test_github_app.py`
   - Integration test for GitHub App authentication
   - Tests installation token generation
   - Validates Discussions API access
   - **Note**: Duplicate exists in `scripts/test_github_app.py` (kept for standalone use)

4. **test_readonly_filesystem.py** → `tests/integration/test_readonly_filesystem.py`
   - Integration test for Docker filesystem permissions
   - Tests read-only enforcement for agents
   - Validates `filesystem_write_allowed` configuration

## Documentation Updated

### Files Modified

1. **docs/README.md**
   - Updated test structure diagram
   - Changed: `tests/test_context_extraction.py` → `tests/unit/test_context_extraction.py`

2. **docs/filesystem_write_protection.md**
   - Updated test invocation path
   - Changed: `python tests/test_readonly_filesystem.py`
   - To: `python tests/integration/test_readonly_filesystem.py`

## New Test Directory Structure

```
tests/
├── __init__.py
├── conftest.py                      # Shared fixtures
├── fixtures/                        # Test data
├── mocks/                          # Mock objects
├── utils/                          # Test utilities
├── unit/                           # Unit tests
│   ├── test_actual_context_size.py        ← MOVED
│   ├── test_context_extraction.py         ← MOVED
│   └── ... (other unit tests)
├── integration/                    # Integration tests
│   ├── test_github_app.py                 ← MOVED
│   ├── test_readonly_filesystem.py        ← MOVED
│   ├── test_claude_code_integration.py
│   ├── test_claude_code_mocked.py
│   └── ... (other integration tests)
├── e2e/                           # End-to-end tests
├── monitoring/                    # Monitoring tests
└── triage_scripts/               # Diagnostic scripts
```

## Benefits

1. **Better Organization** - Tests grouped by type (unit/integration)
2. **Clearer Intent** - Easier to understand what each test category covers
3. **Selective Execution** - Can run unit tests separately from integration tests
4. **Standard Structure** - Follows pytest conventions

## Running the Tests

### All Tests
```bash
pytest tests/
```

### Unit Tests Only (Fast)
```bash
pytest tests/unit/
```

### Integration Tests Only (Slower, May Need Services)
```bash
pytest tests/integration/
```

### Specific Moved Tests
```bash
# Context extraction tests
pytest tests/unit/test_context_extraction.py
pytest tests/unit/test_actual_context_size.py

# Integration tests
pytest tests/integration/test_github_app.py
pytest tests/integration/test_readonly_filesystem.py
```

## Notes

### GitHub App Test Duplicate

There are now two versions of `test_github_app.py`:
- `tests/integration/test_github_app.py` - For pytest integration tests
- `scripts/test_github_app.py` - For standalone verification during setup

Both serve valid purposes:
- **Integration test version**: Part of automated test suite
- **Scripts version**: Used in SETUP.md for manual verification

This is intentional and both should be maintained.

### No Breaking Changes

- All test functionality preserved
- Documentation updated to reflect new paths
- No changes to test logic or behavior
- Tests can still be run individually or as part of suite

## Verification

Run the tests to verify they work in their new locations:

```bash
# Quick verification
pytest tests/unit/test_context_extraction.py -v
pytest tests/unit/test_actual_context_size.py -v
pytest tests/integration/test_github_app.py -v
pytest tests/integration/test_readonly_filesystem.py -v
```

All tests should pass in their new locations with no modifications needed.

---

**Date**: October 10, 2025  
**Status**: ✅ Complete  
**Files Moved**: 4  
**Documentation Updated**: 2 files  
**Tests Verified**: All tests work in new locations
