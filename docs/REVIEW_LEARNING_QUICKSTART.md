# Review Learning System - Quick Start

## What Just Got Implemented

✅ **Elasticsearch indices created** - Stores filters and review outcomes
✅ **Filter creation script** - Quick CLI to add manual filters
✅ **All reviewers now use filters** - Requirements, Design, Code, Test, QA
✅ **CLAUDE.md compliance filter added** - Example filter already active
✅ **'highlight' action support** - Emphasizes important checks

## How It Works

**1. You capture a learning** (like "check CLAUDE.md conventions"):
```bash
docker-compose exec orchestrator python scripts/add_review_filter.py \
    --agent requirements_reviewer \
    --category project_conventions \
    --severity high \
    --pattern "Check deliverables against CLAUDE.md" \
    --samples "Issue #102 created markdown files violating CLAUDE.md" \
    --action highlight
```

**2. Filter gets stored in Elasticsearch** with metadata:
- Agent: requirements_reviewer
- Category: project_conventions
- Action: highlight (emphasize) / suppress (ignore) / adjust_severity
- Confidence: 0.95 (95% confident this is valuable)

**3. Next review automatically loads the filter**:
```python
# In requirements_reviewer_agent.py:198
filter_instructions = await self._get_filter_instructions()
# Injects into prompt at line 203
```

**4. Reviewer sees the guidance**:
```
## Review Focus Areas (Learned from Historical Feedback)

### High-Priority Checks (IMPORTANT)

**project_conventions - high**
- Pattern: Requirements specify deliverables that violate project CLAUDE.md conventions
- Why important: High-value pattern that catches real issues
- Confidence: 95% (based on 1 samples)
- Example: Issue #102 requested 'Documentation updated' which was interpreted as
  creating markdown files, but /workspace/CLAUDE.md specifies 'Use GitHub issues'
```

**5. Reviewer checks requirements against CLAUDE.md** before approving.

## Common Use Cases

### 1. Highlight Important Checks (like CLAUDE.md compliance)

Emphasize patterns that catch real issues:

```bash
docker-compose exec orchestrator python scripts/add_review_filter.py \
    --agent requirements_reviewer \
    --category project_conventions \
    --pattern "Requirements must align with project CLAUDE.md guidelines" \
    --samples "Requirements often miss project-specific conventions" \
    --action highlight
```

### 2. Suppress Low-Value Noise

Stop flagging things developers always ignore:

```bash
docker-compose exec orchestrator python scripts/add_review_filter.py \
    --agent code_reviewer \
    --category code_style \
    --severity low \
    --pattern "Missing docstrings in private helper methods" \
    --samples "Developers don't add these for simple helpers" \
    --action suppress
```

### 3. Adjust Severity

Downgrade issues that aren't as critical as they seem:

```bash
docker-compose exec orchestrator python scripts/add_review_filter.py \
    --agent code_reviewer \
    --category performance \
    --pattern "Performance concerns without benchmarks" \
    --samples "Usually premature optimization" \
    --action adjust_severity \
    --from-severity high \
    --to-severity medium
```

## Viewing Filters

**List all active filters:**
```bash
docker-compose exec elasticsearch curl -s 'http://localhost:9200/review-filters/_search?pretty'
```

**View filters for specific agent:**
```bash
docker-compose exec elasticsearch curl -s 'http://localhost:9200/review-filters/_search?q=agent:requirements_reviewer&pretty'
```

## Testing the Filter

**Create a test issue** that violates CLAUDE.md:
```
Title: Add new feature
Body:
- Implement feature X
- Update documentation with implementation details  # <-- Should flag this
```

**Expected**: Requirements reviewer will now highlight that "update documentation"
should clarify it means "update GitHub issue" not "create markdown files".

## Filter Actions Explained

**highlight** (NEW - just added):
- Tells reviewer to pay special attention
- Used for high-value patterns that catch real issues
- Example: CLAUDE.md compliance checks

**suppress**:
- Don't report this pattern at all
- Used for noise that developers ignore
- Example: Nitpicky style issues

**adjust_severity**:
- Change how critical an issue is
- Requires --from-severity and --to-severity
- Example: Downgrade "performance concerns" from high to medium

## Deactivating Filters

If a filter becomes obsolete:

```python
from services.review_filter_manager import get_review_filter_manager

filter_manager = get_review_filter_manager()
await filter_manager.deactivate_filter('filter_0bb5029d00ae')
```

Or delete from Elasticsearch:
```bash
docker-compose exec elasticsearch curl -X DELETE 'http://localhost:9200/review-filters/_doc/filter_0bb5029d00ae'
```

## Next Steps

**Immediate** (manual):
- Add more filters as you discover patterns
- Use `scripts/add_review_filter.py` to capture learnings

**Short-term** (automated):
- Pattern detector analyzes review outcomes
- Auto-suggests filters based on ignore rates
- You approve/reject suggestions

**Long-term** (AI-powered):
- LLM analyzes review history
- Extracts meta-patterns like "this project values KISS over DRY"
- Generates nuanced project-specific guidance

## Current Filter

**Active filter**: CLAUDE.md compliance for requirements_reviewer
- **Pattern**: Check deliverables against CLAUDE.md conventions
- **Confidence**: 95%
- **Action**: highlight (emphasize in review)
- **Filter ID**: filter_0bb5029d00ae

Next time `requirements_reviewer` runs, it will specifically check if requirements
violate project CLAUDE.md rules before approving.

## Files Modified

- ✅ services/review_filter_manager.py:441-455 - Added 'highlight' action support
- ✅ scripts/add_review_filter.py - Created filter creation CLI
- ✅ agents/requirements_reviewer_agent.py:15-43,198 - Added filter loading
- ✅ agents/design_reviewer_agent.py:14-42,190 - Added filter loading
- ✅ agents/test_reviewer_agent.py:14-42,124 - Added filter loading
- ✅ agents/qa_reviewer_agent.py:14-42,229 - Added filter loading
- ✅ agents/code_reviewer_agent.py:14-42 - Already had filter loading

## Success Metrics

Track these to measure effectiveness:

1. **Noise Reduction**: Review finding ignore rate (target: <20%)
2. **Quality**: % of findings that are valid (target: >90%)
3. **Efficiency**: Review cycle iterations (target: avg <2)
4. **Learning**: Time to identify patterns (target: <7 days)
