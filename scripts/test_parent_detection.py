#!/usr/bin/env python3
"""
Test script to verify parent issue detection
"""
import re

def test_parent_patterns():
    """Test all parent issue patterns"""
    
    test_cases = [
        {
            "body": """## Parent Issue
Closes #1

## Overview
This is a sub-issue""",
            "expected": 1,
            "description": "## Parent Issue followed by Closes #N"
        },
        {
            "body": """## Parent Issue
Part of #123

## Overview""",
            "expected": 123,
            "description": "## Parent Issue followed by Part of #N"
        },
        {
            "body": "Parent Issue: #456",
            "expected": 456,
            "description": "Parent Issue: #N"
        },
        {
            "body": "Parent Issue #789",
            "expected": 789,
            "description": "Parent Issue #N (no colon)"
        },
        {
            "body": "This is Part of #999",
            "expected": 999,
            "description": "Part of #N"
        },
        {
            "body": "Sub-issue of #111",
            "expected": 111,
            "description": "Sub-issue of #N"
        },
        {
            "body": "Child of #222",
            "expected": 222,
            "description": "Child of #N"
        },
        {
            "body": "No parent reference here",
            "expected": None,
            "description": "No parent reference"
        },
    ]
    
    patterns = [
        r'Parent Issue[:\s]+#(\d+)',  # "Parent Issue: #123" or "Parent Issue #123"
        r'Part of #(\d+)',            # "Part of #123"
        r'##\s*Parent Issue[^\d]*#(\d+)',  # "## Parent Issue\nPart of #123"
        r'##\s*Parent Issue[^\d]*Closes\s+#(\d+)',  # "## Parent Issue\nCloses #123"
        r'Sub-issue of #(\d+)',       # "Sub-issue of #123"
        r'Child of #(\d+)',           # "Child of #123"
    ]
    
    print("Testing parent issue detection patterns...\n")
    
    all_passed = True
    for test_case in test_cases:
        body = test_case["body"]
        expected = test_case["expected"]
        description = test_case["description"]
        
        found = None
        matched_pattern = None
        
        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                found = int(match.group(1))
                matched_pattern = pattern
                break
        
        passed = found == expected
        all_passed = all_passed and passed
        
        status = "✓" if passed else "✗"
        print(f"{status} {description}")
        print(f"  Expected: {expected}, Found: {found}")
        if matched_pattern:
            print(f"  Matched pattern: {matched_pattern}")
        if not passed:
            print(f"  Body: {repr(body[:100])}")
        print()
    
    if all_passed:
        print("✓ All tests passed!")
        return 0
    else:
        print("✗ Some tests failed")
        return 1

if __name__ == "__main__":
    exit(test_parent_patterns())
