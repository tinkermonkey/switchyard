"""
Shared utilities for agent output parsing.
"""

import json
import re
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def parse_json_block(text: str, first_delimiter: str = '{') -> Optional[Any]:
    """
    Parse a JSON value from text that may be wrapped in code fences or have
    prose before/after it.

    Modeled on pr_review_stage._parse_consolidated_findings(), which is the
    proven reference implementation for this pattern.

    Strategy:
      1. Strip code fences defensively — agent may wrap despite instructions.
      2. Try json.loads() directly.
      3. Fall back to raw_decode() starting at the first occurrence of
         first_delimiter, which ignores leading prose and trailing content
         without the greedy-regex pitfall of extracting a substring.

    Args:
        text: Raw text possibly containing a JSON value.
        first_delimiter: Starting character to seek when direct parse fails.
            Use '{' for JSON objects, '[' for JSON arrays.

    Returns:
        Parsed JSON value, or None if all parsing attempts fail.
    """
    json_text = text.strip()

    # Strip code fences defensively — agent may wrap despite instructions
    if json_text.startswith('```'):
        json_text = re.sub(r'^```[a-z]*\s*\n?', '', json_text)
        json_text = re.sub(r'\n?```\s*$', '', json_text).strip()

    data = None
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        # Fallback: agent may have added prose before/after the JSON value.
        # raw_decode starts at the first delimiter and parses exactly one JSON
        # value, ignoring any trailing content — avoids the greedy-regex
        # pitfall where a closing delimiter in trailing prose causes an
        # extracted substring to be unparseable.
        idx = json_text.find(first_delimiter)
        if idx >= 0:
            try:
                data, _ = json.JSONDecoder().raw_decode(json_text, idx)
            except json.JSONDecodeError:
                pass

    if data is None:
        logger.warning(
            f"Could not parse JSON block (delimiter={first_delimiter!r}). "
            f"Output excerpt: {json_text[:200]!r}"
        )

    return data
