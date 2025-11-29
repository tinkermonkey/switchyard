# Fix Plan

**Failure Signature:** `sha256:666740522d3aee5757ac87c1ddb67a98bfae2d9486f7e76a6b3344a0a9e4bd1c`

## Proposed Solution

Pass the required `elasticsearch_hosts` and `anthropic_api_key` arguments when instantiating `PatternLLMAnalyzer` in the `ReviewPatternDetector.__init__()` method. Source the Anthropic API key from environment variables using the same pattern as other services.

## Implementation Steps

1. **Update ReviewPatternDetector.__init__()** to accept and pass through required parameters
2. **Add import statement** for os module to access environment variables
3. **Update get_review_pattern_detector()** to pass configuration from environment
4. **Add ANTHROPIC_API_KEY** to .env file (or use existing CLAUDE_CODE_OAUTH_TOKEN)
5. **Test the fix** by manually triggering the scheduled task

## Code Changes Required

### File: services/review_pattern_detector.py

#### Change 1: Update imports (after line 6)
```python
# Before
import logging
import json
from typing import Dict, Any, List, Optional
from datetime import datetime
from elasticsearch import Elasticsearch

# After
import logging
import json
import os
from typing import Dict, Any, List, Optional
from datetime import datetime
from elasticsearch import Elasticsearch
```

#### Change 2: Update ReviewPatternDetector.__init__() (lines 26-40)
```python
# Before
def __init__(
    self,
    elasticsearch_hosts: List[str] = None,
    min_sample_size: int = 10,
    ignore_rate_threshold: float = 0.6,
    confidence_threshold: float = 0.8
):
    if elasticsearch_hosts is None:
        elasticsearch_hosts = ["http://elasticsearch:9200"]

    self.es = Elasticsearch(elasticsearch_hosts)
    self.llm_analyzer = PatternLLMAnalyzer()
    self.min_sample_size = min_sample_size
    self.ignore_rate_threshold = ignore_rate_threshold
    self.confidence_threshold = confidence_threshold

# After
def __init__(
    self,
    elasticsearch_hosts: List[str] = None,
    anthropic_api_key: str = None,
    min_sample_size: int = 10,
    ignore_rate_threshold: float = 0.6,
    confidence_threshold: float = 0.8
):
    if elasticsearch_hosts is None:
        elasticsearch_hosts = ["http://elasticsearch:9200"]

    if anthropic_api_key is None:
        # Try ANTHROPIC_API_KEY first, fall back to CLAUDE_CODE_OAUTH_TOKEN
        anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")

    if not anthropic_api_key:
        raise ValueError("anthropic_api_key must be provided or ANTHROPIC_API_KEY/CLAUDE_CODE_OAUTH_TOKEN environment variable must be set")

    self.es = Elasticsearch(elasticsearch_hosts)
    self.llm_analyzer = PatternLLMAnalyzer(
        elasticsearch_hosts=elasticsearch_hosts,
        anthropic_api_key=anthropic_api_key
    )
    self.min_sample_size = min_sample_size
    self.ignore_rate_threshold = ignore_rate_threshold
    self.confidence_threshold = confidence_threshold
```

#### Change 3: Update get_review_pattern_detector() (lines 332-337)
```python
# Before
def get_review_pattern_detector() -> ReviewPatternDetector:
    """Get global ReviewPatternDetector instance"""
    global _pattern_detector
    if _pattern_detector is None:
        _pattern_detector = ReviewPatternDetector()
    return _pattern_detector

# After
def get_review_pattern_detector() -> ReviewPatternDetector:
    """Get global ReviewPatternDetector instance"""
    global _pattern_detector
    if _pattern_detector is None:
        elasticsearch_hosts = [os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")]
        anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        _pattern_detector = ReviewPatternDetector(
            elasticsearch_hosts=elasticsearch_hosts,
            anthropic_api_key=anthropic_api_key
        )
    return _pattern_detector
```

### File: .env (optional - if you want a dedicated key)

```bash
# Add this line (or ensure CLAUDE_CODE_OAUTH_TOKEN is present)
ANTHROPIC_API_KEY=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
```

**Note:** The existing `CLAUDE_CODE_OAUTH_TOKEN` can be used as a fallback, so adding `ANTHROPIC_API_KEY` is optional.

## Testing Strategy

### 1. Unit Test (optional but recommended)
Create a test in `tests/unit/test_review_pattern_detector.py`:
```python
import os
import pytest
from services.review_pattern_detector import ReviewPatternDetector

def test_pattern_detector_initialization():
    """Test that ReviewPatternDetector initializes with environment variables"""
    # Set environment variable
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "test-key"

    # Should not raise
    detector = ReviewPatternDetector()
    assert detector.llm_analyzer is not None
```

### 2. Integration Test
```bash
# Restart orchestrator to pick up code changes
docker-compose restart orchestrator

# Monitor logs for the next scheduled run (3:00 AM)
docker logs clauditoreum-orchestrator-1 -f | grep "review learning cycle"

# Or manually trigger (if there's a manual trigger endpoint)
# Check services/scheduled_tasks.py for manual trigger capability
```

### 3. Verification
After the fix, the 3:00 AM scheduled task should execute without errors. Look for:
```
INFO - Starting scheduled review learning cycle
INFO - Detecting low-value review patterns (30d lookback)
INFO - Detected N low-value patterns
```

Instead of:
```
ERROR - Fatal error in review learning cycle: PatternLLMAnalyzer.__init__() missing 2 required positional arguments
```

## Risks and Considerations

### Low Risk
- The change is purely additive (passing required parameters)
- No breaking changes to existing API contracts
- Existing `.env` already has `CLAUDE_CODE_OAUTH_TOKEN` which can be used

### Potential Issues
1. **API Key Validity**: Ensure the API key in `.env` is valid and has sufficient quota
2. **Elasticsearch Connection**: Ensure Elasticsearch is accessible at the configured host
3. **Lazy Loading**: The singleton pattern means the instance is created on first use, so errors won't appear until 3:00 AM unless manually triggered

### Mitigation
- Add validation at service startup to verify credentials
- Add health check endpoint for the review learning system
- Consider adding metrics/alerts for scheduled task failures

## Deployment Plan

### 1. Apply Code Changes
```bash
# Edit services/review_pattern_detector.py with the changes above
# Commit changes
git add services/review_pattern_detector.py
git commit -m "Fix PatternLLMAnalyzer initialization in ReviewPatternDetector"
```

### 2. Restart Service
```bash
# Restart orchestrator container to load new code
docker-compose restart orchestrator

# Verify service is running
docker-compose ps orchestrator
docker logs clauditoreum-orchestrator-1 --tail 50
```

### 3. Monitor
```bash
# Watch logs for the next scheduled run or errors
docker logs clauditoreum-orchestrator-1 -f | grep -i "pattern\|review learning"
```

### 4. Rollback Plan (if needed)
```bash
# Revert commit
git revert HEAD

# Restart service
docker-compose restart orchestrator
```

## Additional Recommendations

1. **Add Startup Validation**: Add a health check that validates API credentials on startup
2. **Improve Error Messages**: Add more descriptive error messages about missing environment variables
3. **Document Dependencies**: Update documentation to list required environment variables
4. **Consider Making API Key Optional**: If LLM analysis is optional, make the analyzer conditionally initialized only when credentials are available
