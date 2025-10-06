# Self-Improvement Pattern Detection System

## Overview

A feedback loop system that monitors agent behavior through live logs, detects inefficiency patterns, and proposes improvements to CLAUDE.md configuration files. The system creates a continuous improvement cycle where the orchestrator learns from its own mistakes and optimizations.

## Core Concept

Monitor the Redis live log data stream to identify:
- Repeated tool call failures followed by retries
- User corrections immediately after tool results
- Extended file exploration (context gaps)
- Permission request patterns (should be auto-approved)
- Common error sequences that could be prevented

Transform these observations into actionable CLAUDE.md improvements with human oversight through GitHub Discussions and Issues.

## Architecture

```
┌─────────────────────┐
│ Redis Live Logs     │
│ (TTL: 1 hour)       │
└──────┬──────────────┘
       │ Stream Consumer
       ▼
┌─────────────────────────────────────┐
│ Log Collector Service               │
│ - Parse tool calls/results          │
│ - Enrich with session context       │
│ - Immediate pattern detection       │
└──────┬──────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────┐
│ Elasticsearch                       │
│ - Full historical logs              │
│ - Indexed by session/agent/tool     │
│ - 90-day retention                  │
└──────┬──────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────┐
│ Pattern Recognition Engine          │
│ - Rule-based (real-time)            │
│ - Statistical (daily)               │
│ - LLM meta-analysis (weekly)        │
└──────┬──────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────┐
│ GitHub Integration Layer            │
│ - Create discussions (observations) │
│ - Create issues (proposals)         │
│ - Auto-create PRs (approved)        │
└──────┬──────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────┐
│ Metrics & Impact Tracker            │
│ - Before/after comparison           │
│ - Pattern recurrence monitoring     │
│ - CLAUDE.md efficiency tracking     │
└─────────────────────────────────────┘
```

## Data Storage Strategy

### Elasticsearch (Primary Analytical Store)
**Purpose:** Long-term log storage and pattern analysis

**Schema:**
```json
{
  "timestamp": "2025-10-05T14:23:01Z",
  "session_id": "abc-123",
  "agent_name": "senior-software-engineer",
  "project": "context-studio",
  "event_type": "tool_call" | "tool_result" | "user_message",
  "tool_name": "Bash",
  "tool_params": {...},
  "result": {...},
  "success": true,
  "duration_ms": 1234,
  "context_tokens": 45000,
  "error_message": null,
  "retry_count": 0,
  "user_correction": false
}
```

**Advantages:**
- Excellent full-text search across logs
- Complex aggregation queries for pattern detection
- Time-series analysis capabilities
- Similarity search for finding related events

### PostgreSQL (Metrics & Patterns Store)
**Purpose:** Aggregated patterns, metrics, and improvement tracking

**Schema:**
```sql
CREATE TABLE detected_patterns (
    id SERIAL PRIMARY KEY,
    pattern_type VARCHAR(50),
    pattern_signature TEXT,
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    occurrence_count INT,
    affected_projects TEXT[],
    severity ENUM('low', 'medium', 'high', 'critical'),
    avg_impact_seconds FLOAT,
    status ENUM('detected', 'discussed', 'proposed', 'approved', 'implemented'),
    github_discussion_url TEXT,
    github_issue_url TEXT,
    claude_md_change TEXT
);

CREATE TABLE improvement_metrics (
    id SERIAL PRIMARY KEY,
    pattern_id INT REFERENCES detected_patterns(id),
    measurement_date DATE,
    occurrence_count INT,
    avg_duration_ms FLOAT,
    success_rate FLOAT
);

CREATE TABLE claude_md_changes (
    id SERIAL PRIMARY KEY,
    pattern_id INT REFERENCES detected_patterns(id),
    change_date TIMESTAMP,
    file_path TEXT,
    section TEXT,
    change_diff TEXT,
    pr_url TEXT,
    impact_score FLOAT
);
```

## Pattern Recognition Layers

### Layer 1: Rule-Based Detection (Real-time)

Detects known anti-patterns as they occur:

```python
PATTERN_RULES = {
    "retry_after_failure": {
        "detector": "tool_result.success=false → same_tool.success=true within 5min",
        "severity": "medium",
        "description": "Tool call failed then succeeded, suggests missing instruction"
    },

    "exploration_thrashing": {
        "detector": "5+ consecutive Read/Glob calls in different directories",
        "severity": "medium",
        "description": "Agent searching for context, suggests missing architectural overview"
    },

    "permission_request_pattern": {
        "detector": "Same command requires user approval 3+ times",
        "severity": "low",
        "description": "Should be added to auto-approved commands list"
    },

    "immediate_user_correction": {
        "detector": "user_message within 30s of tool_result",
        "severity": "high",
        "description": "Agent misunderstood task or context"
    },

    "git_directory_confusion": {
        "detector": "git command fails with 'not a git repository'",
        "severity": "medium",
        "description": "Agent in wrong directory, suggests clearer workspace guidance"
    },

    "repeated_file_not_found": {
        "detector": "Same file path fails 2+ times across sessions",
        "severity": "low",
        "description": "Outdated path reference or missing setup step"
    }
}
```

### Layer 2: Statistical Analysis (Daily Batch)

Aggregate patterns over time:

**Elasticsearch Aggregation Queries:**
```python
# Find most common error sequences
GET /agent-logs/_search
{
  "aggs": {
    "error_sequences": {
      "terms": {
        "field": "error_message.keyword",
        "min_doc_count": 5
      },
      "aggs": {
        "by_agent": {"terms": {"field": "agent_name"}},
        "by_project": {"terms": {"field": "project"}},
        "avg_duration": {"avg": {"field": "duration_ms"}}
      }
    }
  }
}

# Find tools that frequently require retries
GET /agent-logs/_search
{
  "query": {"range": {"retry_count": {"gt": 0}}},
  "aggs": {
    "tools_needing_retry": {
      "terms": {"field": "tool_name"},
      "aggs": {
        "avg_retries": {"avg": {"field": "retry_count"}},
        "contexts": {"terms": {"field": "tool_params.command.keyword"}}
      }
    }
  }
}
```

**Pattern Extraction:**
- Command sequences that lead to success vs failure
- Tools with high retry rates
- Context token usage patterns (inefficient context loading)
- Time-of-day or project-type correlations

### Layer 3: LLM Meta-Analysis (Weekly)

Use Claude to analyze aggregated patterns and propose specific improvements:

**Prompt Template:**
```
You are analyzing agent behavior logs to improve CLAUDE.md instructions.

## Pattern Summary
Type: {pattern_type}
Frequency: {occurrence_count} times across {num_sessions} sessions
Projects affected: {project_list}
Average impact: {avg_duration_seconds}s wasted per occurrence

## Example Instances
{log_examples}

## Current CLAUDE.md Section
{relevant_claude_md_section}

## Task
Propose a specific, concise addition or modification to CLAUDE.md that would prevent this pattern. Follow these constraints:
- Be specific and actionable (not philosophical)
- Use concrete examples where helpful
- Keep under 100 words
- Format as a git diff

Output format:
### Proposed Change
```diff
...
```

### Expected Impact
{1-2 sentences on how this prevents the pattern}
```

## Human-in-the-Loop Workflow

### GitHub Discussions (Pattern Observations)

**Creation Threshold:** Pattern occurs 5+ times OR severity=high

**Template:**
```markdown
## Pattern Detected: {pattern_type}

**Frequency:** {count} occurrences across {days} days
**Projects:** {project_list}
**Severity:** {severity}
**Average Impact:** {duration}s per occurrence

### Description
{human_readable_description}

### Example Instances
{links_to_elasticsearch_queries}

### Community Input Needed
- Have you observed this pattern?
- Is this a real inefficiency or expected behavior?
- Any additional context that might help?

**React with 👍 if you've seen this, 👎 if false positive**
```

**Categories:**
- Error Patterns
- Inefficiency Observations
- Context Gaps
- Permission Issues

### GitHub Issues (Actionable Proposals)

**Creation Threshold:**
- 20+ occurrences OR
- Severity=critical OR
- Discussion has 5+ 👍 reactions

**Template:**
```markdown
## Pattern: {pattern_type}

### Impact
- **Frequency:** {count} occurrences
- **Time Wasted:** ~{total_hours} hours cumulative
- **Affected Projects:** {project_list}
- **Severity:** {severity}

### Evidence
- Discussion: {discussion_url}
- Query: {elasticsearch_query_link}
- Example Logs: {log_links}

### Root Cause Analysis
{LLM_generated_analysis}

### Proposed CLAUDE.md Change

**File:** `.claude/CLAUDE.md` (or project-specific)
**Section:** {section_name}

```diff
{proposed_diff}
```

### Expected Impact
{expected_improvement_description}

### Before/After Metrics
We will track:
- Occurrence rate (target: -80%)
- Average duration (target: {target_duration}s)
- Retry count (target: <0.5 per session)

### Review Checklist
- [ ] Change is specific and actionable
- [ ] Change doesn't conflict with existing instructions
- [ ] Change scope is appropriate (not too broad/narrow)
- [ ] Expected impact is measurable

**Labels:** `pattern-improvement`, `severity-{level}`, `auto-generated`
```

### Approval Workflow

```
Pattern Detected (ES aggregation)
    ↓
Severity >= Medium? → Create Discussion
    ↓
Community validation (3-day window)
    ↓
Thumbs up >= 5 OR Occurrences >= 20?
    ↓
Create Issue with LLM-generated proposal
    ↓
Human review (7-day window)
    ↓
Label: approved-pattern
    ↓
Auto-create PR with CLAUDE.md changes
    ↓
Human merge review
    ↓
Deploy change
    ↓
Track metrics for 14 days
    ↓
Report impact in Issue
```

## CLAUDE.md Bloat Prevention

### Strategies

**1. Severity Thresholds**
Only add patterns meeting minimum impact score:
```
impact_score = occurrence_count × avg_time_wasted × severity_multiplier
minimum_threshold = 100 (e.g., 20 occurrences × 5s × 1.0 severity)
```

**2. Hierarchical File Structure**
```
.claude/
├── CLAUDE.md                          # Core essentials only (<500 lines)
├── patterns/
│   ├── git-operations.md             # Detailed git guidance
│   ├── file-system-safety.md         # Workspace boundary rules
│   ├── docker-workflows.md           # Container interaction patterns
│   └── common-mistakes.md            # Quick reference for known issues
└── examples/
    ├── successful-handoffs.md
    └── error-recovery-examples.md
```

**3. Pattern Consolidation (Weekly Job)**
```python
def consolidate_patterns():
    """Merge similar patterns to avoid redundancy"""
    # Find patterns with high semantic similarity
    similar_patterns = find_similar(threshold=0.85)

    for pattern_group in similar_patterns:
        # Merge into single, generalized instruction
        merged = create_generalized_instruction(pattern_group)

        # Update CLAUDE.md with merged version
        # Archive individual patterns for reference
```

**4. Automatic Pruning**
```python
def prune_obsolete_patterns():
    """Remove patterns that no longer occur"""
    patterns = get_patterns_in_claude_md()

    for pattern in patterns:
        # Check if pattern still occurs
        recent_occurrences = count_occurrences(
            pattern_signature=pattern.signature,
            days=30
        )

        if recent_occurrences == 0:
            # Pattern solved or no longer relevant
            archive_pattern(pattern)
            remove_from_claude_md(pattern)
            log_removal(pattern, reason="obsolete")
```

**5. A/B Testing for Impact Validation**
```python
def measure_change_impact(pattern_id, days_before=14, days_after=14):
    """Measure if CLAUDE.md change actually helped"""

    baseline = get_metrics(
        pattern_id=pattern_id,
        days=days_before,
        end_date=change_deployed_date
    )

    post_change = get_metrics(
        pattern_id=pattern_id,
        days=days_after,
        start_date=change_deployed_date
    )

    improvement = {
        "occurrence_reduction": (baseline.count - post_change.count) / baseline.count,
        "duration_improvement": (baseline.avg_duration - post_change.avg_duration) / baseline.avg_duration,
        "success_rate_delta": post_change.success_rate - baseline.success_rate
    }

    if improvement["occurrence_reduction"] < 0.20:
        # Less than 20% improvement = ineffective change
        flag_for_revision(pattern_id)
```

## Key Metrics

### Pattern Detection Metrics
- **Detection Rate:** Patterns found per 100 agent sessions
- **False Positive Rate:** Rejected issues / total issues created
- **Time to Detection:** Days from first occurrence to pattern identified
- **Coverage:** Percentage of agent errors that match known patterns

### Impact Metrics
- **Impact Score:** (Frequency × Time_Saved × Severity_Multiplier)
- **Cumulative Time Saved:** Sum of (occurrence_reduction × avg_duration)
- **Error Reduction Rate:** Percentage decrease in errors after fix
- **Success Rate Improvement:** Increase in first-attempt success rate

### Efficiency Metrics
- **CLAUDE.md Size Growth:** Lines added per month
- **Instruction Density:** Impact score per line of CLAUDE.md
- **Pattern Consolidation Rate:** Patterns merged / patterns added
- **Obsolescence Rate:** Patterns removed / total patterns

### Feedback Loop Metrics
- **Detection → Merge Time:** Days from pattern detected to fix deployed
- **Community Engagement:** Discussion participation rate
- **Approval Rate:** Approved proposals / total proposals
- **Reoccurrence Rate:** Patterns that resurface after being "fixed"

## Implementation Phases

### Phase 1: Data Collection Foundation (Week 1-2)
**Goal:** Establish data pipeline from Redis to Elasticsearch

**Components:**
- Log collector service consuming Redis streams
- Elasticsearch cluster setup and schema design
- Basic log enrichment (session context, agent metadata)
- Initial indexing of historical Redis data (if available)

**Deliverables:**
- Running log collector service
- Elasticsearch with 7+ days of historical data
- Basic Kibana dashboards for manual exploration
- Documentation on data schema and access

**Success Criteria:**
- 99%+ log capture rate from Redis
- Query latency <500ms for common patterns
- Zero data loss during Redis TTL expiry

### Phase 2: Rule-Based Pattern Detection (Week 3-4)
**Goal:** Implement real-time detection of known anti-patterns

**Components:**
- Pattern rule engine with configurable rules
- PostgreSQL schema for detected patterns
- Real-time alerting for high-severity patterns
- Initial set of 10+ pattern rules

**Deliverables:**
- Pattern detection service
- PostgreSQL database with pattern tracking
- Configuration file for pattern rules
- Slack/Discord webhooks for critical patterns

**Success Criteria:**
- Detect at least 3 patterns within first week
- Zero false positives for critical severity
- <5 minute latency from occurrence to detection

### Phase 3: GitHub Integration & Human Loop (Week 5-6)
**Goal:** Create feedback mechanism through GitHub Discussions and Issues

**Components:**
- GitHub API integration for Discussions/Issues
- Template system for pattern reports
- Approval workflow automation
- Community voting mechanism

**Deliverables:**
- Auto-creation of Discussions for patterns
- Issue creation for high-impact patterns
- Webhook listeners for approval labels
- Documentation on review process

**Success Criteria:**
- Successfully create first Discussion from detected pattern
- Complete first full workflow: Pattern → Discussion → Issue → Approval
- Zero manual GitHub API calls needed

### Phase 4: Statistical Analysis & LLM Meta-Analysis (Week 7-8)
**Goal:** Add intelligent pattern discovery and improvement proposals

**Components:**
- Daily Elasticsearch aggregation jobs
- LLM meta-analysis prompts and workflows
- Similarity detection for pattern clustering
- Automated CLAUDE.md diff generation

**Deliverables:**
- Daily pattern analysis reports
- LLM-generated improvement proposals
- Pattern similarity clustering
- Draft PR generation for approved changes

**Success Criteria:**
- Discover at least 1 pattern not caught by rules
- LLM proposals require <30% human editing
- Similarity clustering reduces duplicates by 50%+

### Phase 5: Metrics & Impact Tracking (Week 9-10)
**Goal:** Close the loop with before/after measurement

**Components:**
- Metrics collection framework
- A/B testing infrastructure
- Impact dashboards
- Automated reporting on merged changes

**Deliverables:**
- Metrics dashboard showing pattern trends
- Automated impact reports posted to Issues
- Weekly summary of improvements
- CLAUDE.md efficiency tracking

**Success Criteria:**
- Track impact of at least 3 merged changes
- Demonstrate measurable improvement (>20% reduction)
- Dashboard accessible to all stakeholders

### Phase 6: Advanced Features (Week 11-12)
**Goal:** Optimization and advanced capabilities

**Components:**
- Pattern consolidation automation
- Automatic pruning of obsolete patterns
- Cross-project pattern sharing
- Predictive pattern suggestions

**Deliverables:**
- Automated CLAUDE.md maintenance
- Pattern library for community sharing
- Real-time suggestions during agent sessions
- Documentation on advanced features

**Success Criteria:**
- CLAUDE.md stays under 500 lines despite additions
- Share 5+ patterns with community
- Successfully prune 3+ obsolete patterns

## Configuration

### Pattern Detection Configuration
```yaml
# config/pattern_detection.yaml

elasticsearch:
  host: "localhost:9200"
  index_prefix: "agent-logs"
  retention_days: 90

postgresql:
  host: "localhost:5432"
  database: "pattern_detection"

pattern_rules:
  enabled: true
  rule_files:
    - "config/patterns/git_patterns.yaml"
    - "config/patterns/file_system_patterns.yaml"
    - "config/patterns/permission_patterns.yaml"

detection_thresholds:
  discussion_creation:
    min_occurrences: 5
    min_severity: "medium"

  issue_creation:
    min_occurrences: 20
    min_severity: "medium"
    min_impact_score: 100

  auto_pr_creation:
    required_approvals: 1
    review_period_days: 7

metrics:
  measurement_window_days: 14
  min_improvement_threshold: 0.20

claude_md_management:
  max_lines: 500
  consolidation_similarity_threshold: 0.85
  pruning_days_without_occurrence: 30

github:
  discussion_category: "Pattern Observations"
  issue_labels: ["pattern-improvement", "auto-generated"]
  pr_labels: ["claude-md-update", "auto-generated"]
```

### Pattern Rule Example
```yaml
# config/patterns/git_patterns.yaml

patterns:
  - name: "git_directory_confusion"
    description: "Agent attempts git operation outside repository"
    severity: "medium"

    detection:
      event_sequence:
        - tool: "Bash"
          command_pattern: "git .*"
          result_pattern: "fatal: not a git repository"

    proposed_fix:
      section: "Git Operations"
      content: |
        ### Git Operations Safety
        Before running git commands, verify you are in the correct directory:
        - Project repos are in `/workspace/<project-name>/`
        - The orchestrator itself is in `/workspace/clauditoreum/`
        - Always use `pwd` to confirm location before git operations

  - name: "git_merge_without_pull"
    description: "Merge fails due to outdated local branch"
    severity: "low"

    detection:
      event_sequence:
        - tool: "Bash"
          command_pattern: "git merge .*"
          result_pattern: "CONFLICT|divergent branches"

    proposed_fix:
      section: "Git Workflow"
      content: |
        Before merging, always pull latest changes:
        ```bash
        git fetch origin
        git pull origin $(git branch --show-current)
        git merge <branch>
        ```
```

## Open Questions

1. **Privacy & Sensitive Data:** How do we ensure API keys, tokens, and other sensitive data in logs don't get stored in Elasticsearch?

2. **Pattern Ownership:** Should patterns be global (all projects) or project-specific? How do we handle conflicts?

3. **LLM Costs:** Weekly meta-analysis could get expensive. What's the budget and how do we optimize?

4. **Community Management:** Who moderates Discussions? How do we prevent spam or off-topic conversations?

5. **Versioning:** Should CLAUDE.md changes be tagged/versioned so we can rollback if a change makes things worse?

6. **Multi-Tenancy:** If multiple teams use the orchestrator, should pattern detection be isolated or shared?

## Success Criteria

### Phase 1-3 Success (MVP)
- Detect and report at least 5 distinct patterns
- Create 3+ GitHub Discussions with community engagement
- Complete 1 full workflow from detection to merged PR
- Demonstrate measurable improvement (>20% error reduction) for 1 pattern

### Full Implementation Success
- 50+ patterns detected and categorized
- 10+ CLAUDE.md improvements merged with positive impact
- CLAUDE.md stays under 500 lines despite additions
- Average time-to-fix for new patterns under 14 days
- Community engagement (5+ participants in Discussions)

### Long-Term Success (3-6 months)
- 30%+ reduction in overall agent errors
- 20%+ improvement in first-attempt success rate
- Pattern library shared across community (10+ external users)
- Automatic pattern detection finds issues before humans notice
- CLAUDE.md becomes a living document that continuously improves
