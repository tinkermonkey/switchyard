# Review Feedback Loop: Self-Improving Review Quality System

## Overview

This system creates a feedback loop that learns from review outcomes to continuously improve review agent quality. By analyzing which review feedback is accepted vs. ignored, the system identifies low-value patterns and filters them out, reducing noise and improving developer experience.

## Core Concept

Not all review feedback is equally valuable. Some comments lead to code improvements, others are ignored or rejected. By tracking which feedback patterns are accepted vs. rejected, we can train the system to generate higher-quality reviews over time.

## Architecture

### Data Flow

```
Redis Streams (Already Capturing Everything)
├─ orchestrator:event_stream
│  ├─ Agent execution events
│  ├─ Review outcomes (NEW)
│  └─ Pattern detection triggers
├─ orchestrator:claude_logs_stream
│  └─ Live Claude API calls
└─ Review cycle state
   ├─ Maker outputs per iteration
   └─ Review outputs per iteration

↓ (LogCollector - already exists)

Elasticsearch
├─ agent-logs-* (existing)
├─ review-outcomes-* (NEW)
├─ review-filters (NEW)
└─ Pattern aggregations

↓ (Pattern Detection - already exists)

Learning Pipeline
├─ ReviewOutcomeCorrelator (NEW)
│  └─ Correlates findings with maker responses
├─ ReviewPatternDetector (NEW)
│  └─ Identifies low-value patterns
└─ ReviewFilterManager (NEW)
   └─ Manages suppression rules

↓

Agent Prompt Enhancement
└─ Inject learned filters into review prompts
```

### Components

#### 1. Review Outcome Correlator

**Purpose**: Analyze completed review cycles to determine which findings were addressed vs. ignored.

**Location**: `services/review_outcome_correlator.py`

**Key Functions**:
- `analyze_review_cycle_outcome()`: Process completed review cycle
- `_correlate_finding_with_response()`: Match finding to maker action
- `_publish_outcome()`: Send to Redis stream for pattern detection

**Signals Used**:
1. Git diff between iterations (code changes)
2. Maker response text (mentions of finding)
3. Finding recurrence in subsequent reviews

**Outcome Types**:
- `accepted`: Finding addressed in next iteration
- `modified`: Addressed differently than suggested
- `ignored`: Finding persists or not addressed
- `unclear`: Cannot determine

#### 2. Review Pattern Detector

**Purpose**: Identify patterns in review outcomes with high ignore rates.

**Location**: `services/review_pattern_detector.py`

**Key Functions**:
- `detect_low_value_patterns()`: Elasticsearch aggregation on outcomes
- `_extract_semantic_pattern()`: Use LLM to identify common themes
- `_validate_pattern_stability()`: Ensure pattern persists over time

**Detection Criteria**:
- Minimum 10 samples
- 60%+ ignore rate
- 30-day stability window
- 80%+ confidence threshold

#### 3. Review Filter Manager

**Purpose**: Manage learned filters and inject into agent prompts.

**Location**: `services/review_filter_manager.py`

**Filter Types**:
- **Suppression**: Prevent known low-value feedback
- **Severity Adjustment**: Calibrate importance based on history
- **Context-Aware**: Apply based on development stage
- **Clustering**: Deduplicate similar feedback

**Storage**: Elasticsearch index `review-filters`

#### 4. Integration Points

**Review Cycle Integration** (`services/review_cycle.py`):
- After cycle completes, trigger outcome analysis
- Extract learning data from iteration history
- Publish outcomes to Redis stream

**Scheduled Learning Pipeline** (`services/scheduled_tasks.py`):
- Daily pattern detection
- Filter creation/update
- Metric tracking
- Stale filter pruning

**Agent Prompt Enhancement** (`agents/base_reviewer_agent.py`):
- Fetch active filters for agent
- Inject suppression rules into prompt
- Add severity adjustment guidance
- Provide context-aware filtering

## Data Schemas

### ReviewOutcome Event (Redis Stream)

```python
{
    "type": "review_outcome",
    "agent": "code_reviewer",
    "finding_category": "Error Handling",
    "finding_severity": "high",
    "finding_message": "Missing error handling for DB connection",
    "action": "accepted",  # accepted, modified, ignored, unclear
    "context": {
        "project": "context-studio",
        "issue_number": 123,
        "iteration": 1,
        "maker_agent": "senior_software_engineer",
        "code_changed": true,
        "mentioned": true,
        "recurs": false
    },
    "timestamp": "2025-10-05T10:30:00Z"
}
```

### Review Filter (Elasticsearch)

```python
{
    "agent": "code_reviewer",
    "category": "Style",
    "severity": "low",
    "pattern_description": "Variable naming suggestions in test files",
    "reason_ignored": "Team prefers concise test variable names",
    "action": "suppress",
    "confidence": 0.87,
    "sample_size": 42,
    "active": true,
    "created_at": "2025-10-05T00:00:00Z",
    "last_updated": "2025-10-05T12:00:00Z",
    "ignore_rate": 0.89,
    "acceptance_rate": 0.11
}
```

### Agent Performance Metrics (Aggregated)

```python
{
    "agent": "code_reviewer",
    "time_period": "30d",
    "overall_acceptance_rate": 0.73,
    "category_breakdown": {
        "security": {"acceptance_rate": 0.94, "sample_size": 45},
        "logic": {"acceptance_rate": 0.81, "sample_size": 123},
        "style": {"acceptance_rate": 0.42, "sample_size": 89}
    },
    "active_filters": 5,
    "total_reviews": 1247,
    "trend_direction": "improving"
}
```

## Implementation Phases

### Phase 1: Data Collection (Week 1)
- [x] Redis streams already capture review cycles
- [ ] Implement `ReviewOutcomeCorrelator`
- [ ] Create Elasticsearch indices for outcomes
- [ ] Integrate correlator into review cycle completion

### Phase 2: Pattern Detection (Week 2)
- [ ] Implement `ReviewPatternDetector`
- [ ] Build Elasticsearch aggregation queries
- [ ] Integrate LLM-based pattern extraction
- [ ] Create initial pattern classification

### Phase 3: Filter Management (Week 3)
- [ ] Implement `ReviewFilterManager`
- [ ] Build filter CRUD operations
- [ ] Create filter effectiveness tracking
- [ ] Add filter pruning logic

### Phase 4: Agent Integration (Week 4)
- [ ] Update reviewer agents to fetch filters
- [ ] Build prompt injection logic
- [ ] Add severity adjustment rules
- [ ] Implement override mechanisms

### Phase 5: Observability (Week 5)
- [ ] Build review quality dashboard
- [ ] Add filter visualization
- [ ] Create acceptance rate charts
- [ ] Implement trend analysis

### Phase 6: Refinement (Week 6)
- [ ] Tune confidence thresholds
- [ ] Add cross-agent learning
- [ ] Implement temporal drift detection
- [ ] Build explainability features

## Key Advantages

1. **Leverages Existing Infrastructure**: Uses Redis streams and Elasticsearch already in place
2. **Zero Duplicate Collection**: All data already flows through observability pipeline
3. **Automatic Learning**: Runs continuously as review cycles complete
4. **Observable**: All learning decisions published to Redis streams
5. **Reversible**: Filters can be disabled or adjusted
6. **Explainable**: Each filter includes rationale and samples
7. **Agent-Specific**: Learning tailored to each reviewer agent
8. **Context-Aware**: Filters apply based on project stage and context

## Configuration

```yaml
# config/review_learning.yaml
review_learning:
  enabled: true

  correlation:
    minimum_iterations: 2  # Need at least 2 iterations to correlate
    git_diff_threshold: 10  # Minimum lines changed to count as "addressed"

  pattern_detection:
    minimum_sample_size: 10
    ignore_rate_threshold: 0.6  # 60%+ ignored = low value
    confidence_threshold: 0.8
    stability_period_days: 30
    analysis_frequency: "daily"

  filtering:
    enable_suppression: true
    enable_severity_adjustment: true
    enable_clustering: true
    max_feedback_per_file: 15

    suppression_rules:
      require_confidence: 0.85
      require_sample_size: 10
      allow_manual_override: true

  monitoring:
    track_filter_effectiveness: true
    alert_on_pattern_drift: true
    dashboard_refresh: "realtime"
```

## Metrics to Track

### Primary Metrics
- **Acceptance Rate**: % of feedback accepted by developers
- **Ignore Rate**: % of feedback ignored
- **Time to Action**: How quickly feedback is addressed
- **Review Cycle Count**: Iterations needed to pass review

### Filter Effectiveness
- **Filter Precision**: % of suppressed feedback correctly filtered
- **Filter Recall**: % of low-value feedback successfully caught
- **Noise Reduction**: % decrease in total feedback volume
- **Quality Improvement**: % increase in acceptance rate over time

### Agent Performance
- **Category Performance**: Acceptance rates by finding category
- **Severity Accuracy**: How well severity predictions match outcomes
- **Trend Analysis**: Improving, stable, or declining over time

## Future Enhancements

1. **Developer Preference Learning**: Learn team-specific style preferences
2. **Cross-Agent Learning**: Transfer patterns between related agents
3. **Temporal Analysis**: Detect when patterns change over time
4. **A/B Testing**: Test new filter rules before deploying
5. **Human Feedback Integration**: Allow developers to rate review quality
6. **Predictive Scoring**: Score findings by predicted acceptance probability
7. **Auto-Escalation**: Escalate when review quality metrics drop

## Success Criteria

- 20%+ reduction in ignored feedback within 30 days
- 15%+ increase in review acceptance rates
- 50%+ reduction in review cycle iterations
- 30%+ decrease in time-to-approval
- 90%+ developer satisfaction with review quality
