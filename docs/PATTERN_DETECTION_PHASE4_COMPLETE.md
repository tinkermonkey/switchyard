# Pattern Detection System - Phase 4 Complete

## Summary

Phase 4 (Statistical Analysis & LLM Meta-Analysis) of the Self-Improvement Pattern Detection System has been implemented with an **optimized 4-container architecture**.

**Key Achievements:**
- ✅ **Container Consolidation**: Reduced from 8 to 4 containers (50% reduction)
- ✅ **Elasticsearch-Only**: Eliminated PostgreSQL dependency
- ✅ **Production Scheduling**: APScheduler with cron triggers, jitter, and manual endpoints
- ✅ **Resource Efficiency**: Shared connection pools, unified logging
- ✅ **LLM Integration**: Claude API for automated CLAUDE.md improvement proposals

**Architecture:**
1. **pattern-ingestion** - Log collection + real-time pattern detection (consolidated)
2. **pattern-github-processor** - GitHub Discussions/Issues integration
3. **pattern-analysis** - Daily stats + weekly LLM analysis + similarity detection (consolidated)
4. **elasticsearch** - All data storage

## What Was Implemented

### 1. Daily Aggregation Service (`services/pattern_daily_aggregator_es.py`)

Runs daily statistical analysis on agent logs to discover patterns not caught by rules:

**Key Analyses:**
- **Error Sequence Analysis** - Most common error messages across agents/projects
- **Tool Retry Analysis** - Tools that frequently need retries
- **Tool Performance Analysis** - Success rates and latency percentiles by tool
- **Context Usage Analysis** - Agents approaching token limits
- **Time/Project Correlations** - Error patterns by time-of-day and project

**Core Methods:**
- `_analyze_error_sequences()` - Find recurring error messages
- `_analyze_tool_retries()` - Identify tools with high retry rates
- `_analyze_tool_performance()` - Calculate success rates and p50/p90/p99 latencies
- `_analyze_context_usage()` - Find high context token usage patterns
- `_analyze_correlations()` - Temporal and project-based error patterns
- `_store_insight()` - Save results to `pattern-insights` index

**Pattern Candidate Discovery:**
Each analysis identifies potential new patterns (10+ occurrences) that aren't yet in the rule-based detector. These become candidates for LLM analysis.

### 2. LLM Meta-Analysis Service (`services/pattern_llm_analyzer.py`)

Uses Claude to analyze high-occurrence patterns and generate CLAUDE.md improvement proposals:

**Analysis Flow:**
```
Weekly run:
1. Query pattern-occurrences for patterns with 20+ occurrences
2. Skip recently analyzed patterns (last 30 days)
3. For each pattern (max 5 per run):
   - Gather occurrence details and examples
   - Build structured prompt for Claude
   - Call Claude 3.5 Sonnet API
   - Parse response into proposed CLAUDE.md change
   - Store analysis in pattern-llm-analysis index
```

**Prompt Structure:**
- Pattern summary (frequency, severity, projects, agents)
- Example error instances
- Task: Generate git diff for CLAUDE.md
- Output format: Section name, diff, expected impact, reasoning

**Quality Assessment:**
- `proposal_quality_score` - Heuristic score (0-1) based on completeness
- `requires_human_review` - Flag for low-quality proposals (< 0.7)

**Cost Tracking:**
- Tracks tokens used and estimated cost per analysis
- Claude 3.5 Sonnet: ~$3/MTok input, $15/MTok output
- Typical cost: $0.01-0.05 per pattern analysis

### 3. Pattern Similarity Analyzer (`services/pattern_similarity_analyzer.py`)

Identifies similar patterns for consolidation to prevent CLAUDE.md bloat:

**Similarity Calculation:**
Multi-signal approach with weighted scoring:
- **Pattern name similarity** (40%) - Text similarity using SequenceMatcher
- **Category match** (20%) - Same category = 1.0, different = 0.0
- **Common projects** (15%) - Jaccard similarity of project sets
- **Common agents** (15%) - Jaccard similarity of agent sets
- **Error message similarity** (10%) - Max similarity between error samples

**Consolidation Flagging:**
- Pairs with similarity >= 75% stored in `pattern-similarity` index
- Pairs with similarity >= 85% flagged `should_consolidate=true`
- Consolidation priority score (1-100) for ranking

**Use Cases:**
- Identify duplicate patterns (e.g., `git_push_fail` vs `git_push_rejected`)
- Merge variations (e.g., `file_not_found_src` vs `file_not_found_tests`)
- Generalize specific patterns into broader guidance

### 4. Elasticsearch-Only Architecture (PostgreSQL Eliminated)

All Phase 4 components use **Elasticsearch exclusively**:

**Indices Used:**
- `agent-logs-*` - Source data for aggregations
- `pattern-occurrences` - Detected patterns
- `pattern-insights` - Daily aggregation results
- `pattern-llm-analysis` - Claude-generated proposals
- `pattern-similarity` - Similarity findings

**Benefits:**
- Simpler deployment (2 datastores instead of 3)
- Data co-located with source logs
- Leverages ES aggregation power
- No schema migrations or SQL complexity

### 5. Docker Compose Integration - 4 Container Architecture

The pattern detection system uses **4 optimized containers** (consolidated from original 8):

**1. elasticsearch**
- Data store for all pattern detection data
- Stores agent-logs-*, pattern-occurrences, pattern-llm-analysis, etc.

**2. pattern-ingestion (consolidated log-collector + pattern-detector)**
```yaml
environment:
  # Redis (log collection)
  - REDIS_HOST=redis
  # Elasticsearch (shared)
  - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
  # Log collector config
  - LOG_BATCH_SIZE=50
  - LOG_BATCH_TIMEOUT=5.0
  # Pattern detector config
  - DETECTION_INTERVAL=60
  - LOOKBACK_MINUTES=5
command: ["python", "-m", "services.pattern_ingestion_service"]
```
- Handles Redis → ES log collection
- Detects patterns in real-time
- Runs continuously (log collection + 60s pattern detection)

**3. pattern-github-processor**
```yaml
environment:
  - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
  - GITHUB_PROCESSING_INTERVAL=300
  - MIN_OCCURRENCES_FOR_DISCUSSION=5
command: ["python", "-m", "services.pattern_github_processor_es"]
```
- Creates GitHub Discussions/Issues for patterns
- Isolated failure domain (GitHub API)
- 5-minute interval

**4. pattern-analysis (consolidated daily-aggregator + llm-analyzer + similarity-analyzer)**
```yaml
environment:
  # Elasticsearch
  - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
  # Daily Aggregator
  - AGGREGATION_INTERVAL_HOURS=24
  # LLM Analyzer
  - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
  - LLM_ANALYSIS_INTERVAL_HOURS=168
  # Similarity Analyzer
  - SIMILARITY_ANALYSIS_INTERVAL_HOURS=24
command: ["python", "-m", "services.pattern_analysis_service"]
```
- Statistical analysis, LLM meta-analysis, similarity detection
- Single scheduler manages all three jobs
- Daily/weekly cron schedules

**Consolidation Benefits:**
- **8 → 4 containers** (50% reduction)
- **Removed Kibana** - can access ES directly or add back for debugging
- **Shared connection pools** - more efficient resource usage
- **Unified logging** - easier monitoring per functional area
- **Failure isolation** - GitHub API failures don't affect pattern detection

## Architecture Flow

```
┌──────────────────────────────────────────────────────────────┐
│           Pattern Detection System (4 Containers)             │
└──────────────────────────────────────────────────────────────┘

Container 1: pattern-ingestion (log collection + detection)
├─ Runs two concurrent tasks via asyncio.gather():
│
├─> Task 1: Log Collection (continuous)
│   1. Read from Redis streams (orchestrator:event_stream, orchestrator:claude_logs_stream)
│   2. Batch events (50 events or 5s timeout)
│   3. Write to Elasticsearch agent-logs-* indices
│   4. Ack Redis messages
│
└─> Task 2: Pattern Detection (60s interval)
    1. Load pattern rules from config/patterns/*.yaml
    2. Query agent-logs-* with rules
    3. Store matches to pattern-occurrences index
    4. Alert on high-severity patterns

Container 2: pattern-github-processor (5min interval)
1. Query pattern-occurrences for patterns with 5+ occurrences
2. Check if already tracked in pattern-github-tracking
3. Create GitHub Discussion (Ideas category)
4. Store tracking record in pattern-github-tracking

Container 3: pattern-analysis (consolidated, scheduled)
├─ Single AsyncIOScheduler managing all jobs
├─ Shared Elasticsearch client
└─ Three scheduled jobs:

   Job 1: Daily Aggregation (3 AM daily)
   ├─> Error sequence analysis
   ├─> Tool retry analysis
   ├─> Tool performance analysis
   ├─> Context usage analysis
   └─> Store insights to pattern-insights

   Job 2: LLM Analysis (Sundays 4 AM)
   ├─> Query high-frequency patterns (20+ occurrences)
   ├─> Call Claude API for CLAUDE.md proposals
   └─> Store to pattern-llm-analysis

   Job 3: Similarity Analysis (5 AM daily)
   ├─> Compare all pattern pairs
   ├─> Calculate multi-signal similarity
   └─> Store consolidation candidates to pattern-similarity

Container 4: elasticsearch
├─ agent-logs-* (source data)
├─ pattern-occurrences (detected patterns)
├─ pattern-github-tracking (GitHub state)
├─ pattern-insights (daily stats)
├─ pattern-llm-analysis (Claude proposals)
└─ pattern-similarity (consolidation candidates)

Human Review Workflow (GitHub):
1. Review LLM proposals in pattern-llm-analysis index
2. Approve via GitHub Discussion workflow
3. Create PR with approved CLAUDE.md changes
4. Track impact in pattern-claude-md-changes index
```

## Scheduler Architecture

The consolidated `pattern-analysis` service uses **a single AsyncIOScheduler managing all three jobs**:

### Single Scheduler, Multiple Jobs

One scheduler instance registers all analysis jobs:

```python
scheduler = AsyncIOScheduler()

# Job 1: Daily Aggregation
scheduler.add_job(daily_aggregator._run_daily_aggregations,
                  CronTrigger(hour=3, minute=0, jitter=300))

# Job 2: LLM Analysis
scheduler.add_job(llm_analyzer._run_llm_analysis,
                  CronTrigger(day_of_week='sun', hour=4, minute=0, jitter=600))

# Job 3: Similarity Analysis
scheduler.add_job(similarity_analyzer._run_similarity_analysis,
                  CronTrigger(hour=5, minute=0, jitter=300))

scheduler.start()
```

### Cron Triggers

Instead of simple intervals, jobs use cron triggers for specific time-of-day execution:

**Daily Aggregation Job:**
- Runs daily at **3 AM** using `CronTrigger(hour=3, minute=0)`
- Consistent timing ensures predictable resource usage

**LLM Analysis Job:**
- Runs weekly on **Sundays at 4 AM** using `CronTrigger(day_of_week='sun', hour=4, minute=0)`
- Staggered 1 hour after aggregator to avoid contention

**Similarity Analysis Job:**
- Runs daily at **5 AM** using `CronTrigger(hour=5, minute=0)`
- Staggered 2 hours after aggregator for load distribution

### Jitter for Reliability

Each service implements jitter at two levels to prevent thundering herd:

**1. Scheduled Jitter (via APScheduler):**
```python
CronTrigger(hour=3, minute=0, jitter=300)  # ±5 minute random offset
```
- Daily aggregator: ±5 minutes (300 seconds)
- LLM analyzer: ±10 minutes (600 seconds)
- Similarity analyzer: ±5 minutes (300 seconds)

**2. Startup Jitter (application level):**
```python
startup_delay = random.randint(30, 120)  # 30-120 seconds
await asyncio.sleep(startup_delay)
```
- Prevents all services from querying Elasticsearch simultaneously on container restart
- Daily aggregator: 30-120 seconds
- LLM analyzer: No startup run (waits for schedule to avoid API costs)
- Similarity analyzer: 45-150 seconds

### Manual Trigger Endpoints

The consolidated service exposes `run_now()` methods for each analyzer:

```python
from services.pattern_analysis_service import PatternAnalysisService

# Get the consolidated service instance
service = PatternAnalysisService(...)

# Trigger individual analyses manually
service.run_daily_aggregation_now()  # Immediate aggregation
service.run_llm_analysis_now()       # Immediate LLM analysis
service.run_similarity_analysis_now() # Immediate similarity check
```

**Use Cases:**
- Testing after configuration changes
- Debugging pattern detection issues
- On-demand analysis before important deployments
- Integration with external triggers (webhooks, APIs)

**Stats Endpoint:**
```python
# Get aggregated stats from all analyzers
stats = service.get_stats()
# Returns:
# {
#   "service": "pattern_analysis_consolidated",
#   "daily_aggregator": {...},
#   "llm_analyzer": {...},
#   "similarity_analyzer": {...}
# }
```

### Scheduler Benefits

This consolidated architecture provides:

1. **Reduced Resource Usage** - Single Python process instead of 3 containers
2. **Shared Connection Pool** - One Elasticsearch client shared by all analyzers
3. **Efficient Scheduling** - Single AsyncIOScheduler manages all jobs
4. **Predictable Execution** - Jobs run at known times instead of random intervals
5. **Load Distribution** - Staggered schedules prevent Elasticsearch contention
6. **Resilience** - Jitter prevents thundering herd on restarts
7. **Testability** - Manual triggers enable validation without waiting for schedules
8. **Cost Control** - LLM analyzer avoids expensive startup runs
9. **Unified Monitoring** - Single log stream for all pattern analysis activities

## Configuration

### Environment Variables

All configuration for the consolidated `pattern-analysis` service:

```bash
# Elasticsearch
export ELASTICSEARCH_HOSTS="http://elasticsearch:9200"

# Daily Aggregator
export AGGREGATION_INTERVAL_HOURS="24"  # How often to run
export LOOKBACK_DAYS="7"                # Days of history to analyze

# LLM Analyzer
export ANTHROPIC_API_KEY="sk-ant-..."  # Required for LLM analysis
export LLM_ANALYSIS_INTERVAL_HOURS="168"  # Weekly
export MIN_OCCURRENCES_FOR_LLM="20"    # Minimum to analyze
export MAX_PATTERNS_PER_LLM_RUN="5"    # Rate limiting

# Similarity Analyzer
export SIMILARITY_ANALYSIS_INTERVAL_HOURS="24"  # Daily
export SIMILARITY_THRESHOLD="0.75"     # 75% similarity minimum
export MIN_OCCURRENCES_FOR_SIMILARITY="5"  # Threshold
```

**Note:** If `ANTHROPIC_API_KEY` is not provided, LLM analysis is disabled but the other two analyzers continue running.

### Tuning Recommendations

**Daily Aggregator:**
- `LOOKBACK_DAYS=7` captures weekly patterns
- `LOOKBACK_DAYS=30` for monthly trend analysis
- Adjust based on log volume (more data = longer runtime)

**LLM Analyzer:**
- `MIN_OCCURRENCES_FOR_LLM=20` ensures significant patterns
- `MAX_PATTERNS_PER_LLM_RUN=5` controls API costs
- Run weekly to allow pattern accumulation
- Budget: ~$0.25/week (5 patterns × $0.05 each)

**Similarity Analyzer:**
- `SIMILARITY_THRESHOLD=0.75` balances precision/recall
- Lower (0.60) finds more candidates, higher (0.85) reduces noise
- Run daily to keep consolidation suggestions current

## How to Use

### 1. Start Phase 4 Services

```bash
# Start all Phase 4 services
docker-compose up -d pattern-daily-aggregator pattern-llm-analyzer pattern-similarity-analyzer

# Verify running
docker-compose ps | grep pattern

# Monitor logs
docker-compose logs -f pattern-llm-analyzer
```

### 2. Daily Aggregation Insights

```bash
# View latest insights
curl -s "http://localhost:9200/pattern-insights/_search?size=5&sort=created_at:desc" | jq '.hits.hits[]._source | {analysis_type, analysis_date, pattern_candidates}'

# Count insights by type
curl -s "http://localhost:9200/pattern-insights/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_type": {
      "terms": {"field": "analysis_type"}
    }
  }
}' | jq '.aggregations.by_type.buckets'
```

### 3. LLM Analysis Results

```bash
# View LLM proposals
curl -s "http://localhost:9200/pattern-llm-analysis/_search?size=5&sort=created_at:desc" | jq '.hits.hits[]._source | {pattern_name, proposed_change_diff, impact_score, requires_human_review}'

# Find high-quality proposals
curl -s "http://localhost:9200/pattern-llm-analysis/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"status": "pending"}},
        {"range": {"proposal_quality_score": {"gte": 0.7}}}
      ]
    }
  },
  "sort": [{"impact_score": "desc"}]
}' | jq '.hits.hits[]._source | {pattern_name, impact_score, proposed_change_diff}'
```

### 4. Similarity Findings

```bash
# View consolidation candidates
curl -s "http://localhost:9200/pattern-similarity/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "term": {"should_consolidate": true}
  },
  "sort": [{"consolidation_priority": "desc"}]
}' | jq '.hits.hits[]._source | {pattern_a_name, pattern_b_name, similarity_score}'

# Count similar pairs by similarity range
curl -s "http://localhost:9200/pattern-similarity/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_similarity": {
      "histogram": {
        "field": "similarity_score",
        "interval": 0.1
      }
    }
  }
}' | jq '.aggregations.by_similarity.buckets'
```

## Success Criteria (Phase 4)

✅ **Pattern Discovery:** Daily aggregations identify patterns not caught by rules
✅ **LLM Proposals:** Claude generates actionable CLAUDE.md improvements
✅ **Similarity Detection:** Consolidation candidates identified automatically
✅ **Cost Efficiency:** LLM analysis costs < $1/week with rate limiting
✅ **Quality Filtering:** Low-quality proposals flagged for human review
✅ **ES-Only Architecture:** PostgreSQL eliminated, simpler deployment

## Integration with Existing Phases

### Reused from Phase 1-3:
- **Elasticsearch indices** - `agent-logs-*`, `pattern-occurrences`
- **Pattern detection flow** - Rule-based detector feeds aggregations
- **GitHub workflow** - LLM proposals feed into approval workflow
- **ES aggregations** - Daily aggregator builds on Phase 1 infrastructure

### Workflow Integration:

```
Phase 2: Rule-Based Detection
└─> pattern-occurrences index

Phase 3: GitHub Integration
└─> GitHub Discussions/Issues

Phase 4: Statistical Analysis + LLM
├─> Daily Aggregator discovers new patterns
├─> LLM Analyzer generates CLAUDE.md proposals
├─> Similarity Analyzer prevents bloat
└─> Proposals flow into Phase 3 GitHub workflow
```

## Metrics and Monitoring

### Daily Aggregator Metrics

Available via `get_stats()`:
- `total_runs` - Aggregation runs executed
- `total_insights_created` - Cumulative insights
- `total_pattern_candidates` - Cumulative candidates discovered
- `aggregation_interval_hours` - Current interval

### LLM Analyzer Metrics

Available via `get_stats()`:
- `total_runs` - Analysis runs executed
- `total_analyses` - Patterns analyzed
- `total_proposals` - Proposals generated
- `total_tokens_used` - Cumulative Claude tokens
- `total_cost_usd` - Cumulative API costs

### Similarity Analyzer Metrics

Available via `get_stats()`:
- `total_runs` - Analysis runs executed
- `total_comparisons` - Pattern pairs compared
- `total_similarities_found` - Similar pairs found
- `similarity_threshold` - Current threshold

### Health Indicators

**Healthy System:**
- Daily aggregator finds 5-15 pattern candidates/day
- LLM analyzer quality scores > 0.7 for 70%+ of proposals
- Similarity analyzer finds 1-5 consolidation candidates/day
- LLM costs < $1/week

**Warning Signs:**
- Zero pattern candidates (logs too sparse or lookback too short)
- LLM quality scores consistently < 0.5 (prompt tuning needed)
- Similarity analyzer finds 50+ candidates (threshold too low)
- LLM costs > $5/week (run less frequently or reduce max patterns)

## Known Limitations

1. **No Multi-Event Sequences:** Aggregations find single-event patterns only
2. **Simple Similarity:** Text-based similarity, not semantic embeddings
3. **LLM Parsing:** Regex-based response parsing (fragile)
4. **No Auto-PR:** Approved proposals still need manual PR creation
5. **Cost Monitoring:** No hard budget limits on LLM API calls
6. **Pattern Versioning:** No tracking of how patterns evolve over time

## Future Enhancements (Phase 5+)

1. **Impact Tracking:**
   - Measure pattern reduction post-CLAUDE.md change
   - A/B testing infrastructure
   - ROI calculation (time saved vs. analysis cost)

2. **Automated PR Creation:**
   - Convert approved LLM proposals to pull requests
   - Auto-update CLAUDE.md files
   - Run tests before merging

3. **Semantic Similarity:**
   - Use embeddings for better pattern clustering
   - Claude API or local model for similarity
   - Multi-language pattern matching

4. **Pattern Lifecycle Management:**
   - Auto-prune obsolete patterns (30+ days no occurrence)
   - Track pattern evolution (mutations, splits, merges)
   - Pattern effectiveness scoring

5. **Real-Time Suggestions:**
   - Detect patterns during agent session
   - Suggest fixes in real-time
   - Agent self-correction

## Troubleshooting

### Daily Aggregator Not Finding Patterns

```bash
# Check if enough logs exist
curl -s "http://localhost:9200/agent-logs-*/_count?q=timestamp:[now-7d TO now]" | jq .

# Verify aggregator is running
docker-compose logs --tail=50 pattern-daily-aggregator

# Check lookback window
docker-compose exec pattern-daily-aggregator env | grep LOOKBACK_DAYS
```

### LLM Analyzer Not Running

```bash
# Check API key is set
docker-compose logs pattern-llm-analyzer | grep "ANTHROPIC_API_KEY"

# Check if patterns meet threshold
curl -s "http://localhost:9200/pattern-occurrences/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_pattern": {
      "terms": {"field": "pattern_name", "min_doc_count": 20}
    }
  }
}' | jq '.aggregations.by_pattern.buckets | length'

# Check recent analyses
curl -s "http://localhost:9200/pattern-llm-analysis/_search?size=1&sort=created_at:desc" | jq '.hits.hits[]._source | {pattern_name, created_at}'
```

### Similarity Analyzer Not Finding Similar Patterns

```bash
# Check total patterns available
curl -s "http://localhost:9200/pattern-occurrences/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "unique_patterns": {
      "cardinality": {"field": "pattern_name"}
    }
  }
}' | jq '.aggregations.unique_patterns.value'

# If < 5 patterns, similarity analysis won't find much

# Check threshold
docker-compose exec pattern-similarity-analyzer env | grep SIMILARITY_THRESHOLD

# Lower threshold to find more candidates
docker-compose up -d -e SIMILARITY_THRESHOLD=0.60 pattern-similarity-analyzer
```

### LLM Costs Too High

```bash
# Check current costs
curl -s "http://localhost:9200/pattern-llm-analysis/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "total_cost": {
      "sum": {"field": "llm_cost_usd"}
    },
    "by_week": {
      "date_histogram": {
        "field": "created_at",
        "calendar_interval": "week"
      },
      "aggs": {
        "cost": {"sum": {"field": "llm_cost_usd"}}
      }
    }
  }
}' | jq '.aggregations'

# Reduce frequency or max patterns
docker-compose up -d \
  -e LLM_ANALYSIS_INTERVAL_HOURS=336 \
  -e MAX_PATTERNS_PER_LLM_RUN=3 \
  pattern-llm-analyzer
```

## Files Created

### New Services (ES-Only)
- `services/pattern_daily_aggregator_es.py` - Daily aggregation service
- `services/pattern_llm_analyzer.py` - LLM meta-analysis service
- `services/pattern_similarity_analyzer.py` - Similarity detection service

### Modified Files
- `docker-compose.yml` - Added 3 Phase 4 services
- `requirements.txt` - anthropic library already present

### Documentation
- `docs/PATTERN_DETECTION_PHASE4_COMPLETE.md` - This document

### Elasticsearch Indices Used
- `pattern-insights` - Daily aggregation results
- `pattern-llm-analysis` - LLM proposals
- `pattern-similarity` - Similarity findings

## Next Steps: Phase 5 (Optional)

**Metrics & Impact Tracking:**
1. Before/after measurement for merged changes
2. A/B testing infrastructure
3. Automated impact reporting to GitHub Issues
4. ROI calculation and dashboards

**See `docs/SELF_IMPROVEMENT_PATTERN_DETECTION.md` for full roadmap.**

## Resources

- **Claude API Docs:** https://docs.anthropic.com/en/api/
- **Elasticsearch Aggregations:** https://www.elastic.co/guide/en/elasticsearch/reference/current/search-aggregations.html
- **Design Document:** `docs/SELF_IMPROVEMENT_PATTERN_DETECTION.md`
- **Phase 1 Details:** `docs/PATTERN_DETECTION_PHASE1_COMPLETE.md`
- **Phase 2 Details:** `docs/PATTERN_DETECTION_PHASE2_COMPLETE.md`
- **Phase 3 Details:** `docs/PATTERN_DETECTION_PHASE3_COMPLETE.md`
