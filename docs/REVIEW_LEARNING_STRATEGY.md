# Review Learning Strategy - Capturing and Injecting Feedback

## Overview

This document outlines how to capture detailed feedback (like "agents should defer to CLAUDE.md") and inject it into reviewer agents to reduce noise and improve review quality.

## Current State

**Already Built**:
- ✅ Pattern detection infrastructure (services/pattern_detector.py, pattern_llm_analyzer.py)
- ✅ Review learning schema (services/review_learning_schema.py)
- ✅ Review filter manager (services/review_filter_manager.py)
- ✅ Review outcome correlator (services/review_outcome_correlator.py)
- ✅ Code reviewer uses filters (agents/code_reviewer_agent.py:14-42)

**Missing**:
- ❌ Requirements reviewer doesn't use filters yet
- ❌ Design reviewer doesn't use filters yet
- ❌ Test reviewer doesn't use filters yet
- ❌ QA reviewer doesn't use filters yet
- ❌ Manual feedback capture workflow not documented

## Strategy: Three-Tier Learning System

### Tier 1: Immediate Manual Injection (Hours)

When you discover a high-value pattern (like CLAUDE.md deference), immediately inject it manually:

**How**:
1. Create a filter in Elasticsearch via review_filter_manager
2. Next reviewer execution automatically loads and applies it

**Example - CLAUDE.md Deference Filter**:
```python
from services.review_filter_manager import get_review_filter_manager

filter_manager = get_review_filter_manager()

# Create filter for all reviewers
await filter_manager.create_filter({
    'agent': 'requirements_reviewer',  # Apply to each reviewer separately
    'category': 'project_conventions',
    'severity': 'high',
    'pattern_description': 'Requirements violate project CLAUDE.md conventions',
    'reason_ignored': 'Project CLAUDE.md defines specific conventions that override general patterns',
    'sample_findings': [
        'Issue requests "Documentation updated" but project CLAUDE.md specifies "Use GitHub issues for change documentation"',
        'Acceptance criteria includes creating markdown files in root directory but CLAUDE.md forbids this'
    ],
    'action': 'highlight',  # Don't suppress, but emphasize
    'confidence': 0.95,
    'sample_size': 1,
    'active': True
})
```

**Where to add manual filters**: Create a script at `scripts/add_review_filter.py` for quick manual additions.

### Tier 2: Pattern Detection from Observability (Days)

Let the system automatically detect patterns from review outcomes:

**How**:
1. Pattern detector analyzes review outcomes in Elasticsearch
2. Identifies patterns with high ignore rates (low-value findings)
3. Identifies patterns with high acceptance rates (high-value findings)
4. Auto-generates filter suggestions for human approval

**Example Patterns Detected**:
- "Missing docstrings" findings ignored 80% of the time → suppress for this project
- "CLAUDE.md violations" accepted 95% of the time → emphasize in prompts
- "Performance concerns without benchmarks" modified 70% → adjust severity

**Enable this**: Pattern detection already runs (services/pattern_detector.py), just need to wire it to review_filter_manager.

### Tier 3: LLM-Based Learning (Weeks)

Use pattern_llm_analyzer.py to extract higher-order insights:

**How**:
1. LLM analyzes clusters of review outcomes
2. Identifies meta-patterns (e.g., "This project values simplicity over completeness")
3. Generates prompt guidance that's too nuanced for rules
4. Injects into reviewer prompts as "Project-Specific Learning"

**Example LLM-Generated Guidance**:
```
## Project-Specific Learning (context-studio)

Based on 47 review cycles, this project demonstrates:
- **Documentation Philosophy**: Use GitHub issues for ALL change documentation, not markdown files
- **Testing Threshold**: 80%+ coverage is genuinely enforced, not aspirational
- **Architecture Bias**: Strongly prefers simple solutions over sophisticated ones (KISS > DRY)
- **Common Rejection Patterns**:
  - Creating summary markdown files instead of updating GitHub
  - Adding features not explicitly requested (YAGNI violations)
```

## Implementation Roadmap

### Phase 1: Extend Filter Loading to All Reviewers (1-2 hours)

**Goal**: All reviewer agents load and apply learned filters like code_reviewer already does.

**Steps**:
1. Copy `_get_filter_instructions()` method from code_reviewer_agent.py to:
   - agents/requirements_reviewer_agent.py
   - agents/design_reviewer_agent.py
   - agents/test_reviewer_agent.py
   - agents/qa_reviewer_agent.py

2. Inject filter instructions into each reviewer's prompt (see code_reviewer_agent.py:216,238 for pattern)

**Code Pattern**:
```python
# In each reviewer agent's __init__ or as a method:
async def _get_filter_instructions(self) -> str:
    """Get learned filter instructions for this reviewer"""
    try:
        from services.review_filter_manager import get_review_filter_manager

        filter_manager = get_review_filter_manager()
        filters = await filter_manager.get_agent_filters(
            agent_name='requirements_reviewer',  # Change per agent
            min_confidence=0.75,
            active_only=True
        )

        if not filters:
            return ""

        return filter_manager.build_filter_instructions(filters)
    except Exception as e:
        logger.warning(f"Failed to load review filters: {e}")
        return ""

# In execute() method:
filter_instructions = await self._get_filter_instructions()

# In prompt:
prompt = f"""
You are a {self.agent_role} conducting review.
{iteration_context}
{filter_instructions}  # <-- Inject here

## Original Requirements
...
"""
```

### Phase 2: Manual Filter Workflow (30 minutes)

**Goal**: Quick way to capture valuable learnings and inject them.

**Create**: `scripts/add_review_filter.py`

```python
#!/usr/bin/env python3
"""
Quick script to add a manual review filter based on learnings.

Usage:
    python scripts/add_review_filter.py \\
        --agent requirements_reviewer \\
        --category project_conventions \\
        --severity high \\
        --pattern "Requirements violate CLAUDE.md conventions" \\
        --samples "Issue #102 created markdown docs despite CLAUDE.md forbidding it"
"""
import asyncio
import argparse
from services.review_filter_manager import get_review_filter_manager

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--agent', required=True)
    parser.add_argument('--category', required=True)
    parser.add_argument('--severity', default='medium')
    parser.add_argument('--pattern', required=True)
    parser.add_argument('--samples', required=True)
    parser.add_argument('--action', default='highlight', choices=['suppress', 'highlight', 'adjust_severity'])
    parser.add_argument('--confidence', type=float, default=0.90)

    args = parser.parse_args()

    filter_manager = get_review_filter_manager()

    filter_id = await filter_manager.create_filter({
        'agent': args.agent,
        'category': args.category,
        'severity': args.severity,
        'pattern_description': args.pattern,
        'sample_findings': [args.samples],
        'action': args.action,
        'confidence': args.confidence,
        'sample_size': 1,  # Manual, so sample_size=1
        'active': True,
        'manual_override': True  # Mark as manually created
    })

    print(f"Created filter: {filter_id}")

if __name__ == '__main__':
    asyncio.run(main())
```

**Usage Example** (for the CLAUDE.md issue):
```bash
python scripts/add_review_filter.py \
    --agent requirements_reviewer \
    --category project_conventions \
    --severity high \
    --pattern "Requirements request documentation deliverables that violate project CLAUDE.md conventions" \
    --samples "Issue #102 requested 'Documentation updated' which agent interpreted as creating markdown files, violating CLAUDE.md's 'Use GitHub issues for documentation' rule" \
    --action highlight
```

### Phase 3: Automated Pattern Detection Integration (2-3 hours)

**Goal**: Wire existing pattern detection to auto-suggest filters.

**Create**: `services/review_filter_suggestions.py`

```python
class ReviewFilterSuggestions:
    """
    Analyzes review outcomes and suggests filters for human approval.
    """

    async def generate_suggestions(self, lookback_days: int = 30) -> List[Dict]:
        """
        Analyze recent review outcomes and suggest filters.

        Returns list of suggested filters with supporting evidence.
        """
        # Use pattern_detector to find recurring patterns
        # Use review_outcome_correlator to identify outcomes
        # Generate filter suggestions with confidence scores
        pass

    async def present_for_approval(self, suggestions: List[Dict]):
        """
        Post suggestions to GitHub discussion for human review.

        Humans can approve/reject/modify suggested filters.
        """
        pass
```

**Scheduled Task**: Run weekly, post suggestions to a GitHub discussion for review.

### Phase 4: LLM-Based Meta-Learning (Optional, 4-6 hours)

**Goal**: Use LLM to extract higher-order patterns from review history.

**Enhance**: `services/pattern_llm_analyzer.py` to generate project-specific guidance.

**Output**: Project-specific prompt additions that get injected alongside filters.

## Measuring Success

**Metrics to Track**:

1. **Noise Reduction**:
   - Review finding ignore rate (target: <20%)
   - Human escalation rate (target: <10% of reviews)
   - Review cycle iterations (target: avg <2)

2. **Learning Effectiveness**:
   - Filter acceptance rate after injection (target: >80%)
   - Time to identify new patterns (target: <7 days)
   - Pattern recurrence after filter creation (target: <5%)

3. **Review Quality**:
   - Precision: % of findings that are valid (target: >90%)
   - Recall: % of real issues caught (harder to measure, use bug escapes as proxy)
   - Review cycle time (target: <2 hours per cycle)

## Quick Start - Address CLAUDE.md Issue

**Immediate Action** (15 minutes):

1. Add filter for all reviewers to check CLAUDE.md compliance:
```bash
# For each reviewer agent
for agent in requirements_reviewer design_reviewer code_reviewer test_reviewer qa_reviewer; do
    python scripts/add_review_filter.py \
        --agent $agent \
        --category project_conventions \
        --severity high \
        --pattern "Deliverables violate project CLAUDE.md conventions (check /workspace/CLAUDE.md for project-specific rules)" \
        --samples "Issue #102 requested 'Documentation updated', agent created PHASE4_*.md files violating CLAUDE.md 'use GitHub issues' rule" \
        --action highlight
done
```

2. Implement `_get_filter_instructions()` in all reviewer agents (copy from code_reviewer_agent.py)

3. Test on next review cycle

**Expected Outcome**: Next time an issue requests "documentation", reviewer will flag if it violates CLAUDE.md conventions, catching the problem before implementation.

## References

- agents/code_reviewer_agent.py:14-42 - Filter loading pattern
- services/review_filter_manager.py - Filter CRUD operations
- services/review_learning_schema.py - Elasticsearch schema
- services/pattern_detector.py - Automated pattern detection
- services/pattern_llm_analyzer.py - LLM-based analysis
