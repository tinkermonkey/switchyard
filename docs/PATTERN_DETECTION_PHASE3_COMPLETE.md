

# Pattern Detection System - Phase 3 Complete

## Summary

Phase 3 (GitHub Integration & Human-in-the-Loop) of the Self-Improvement Pattern Detection System has been implemented. The system now automatically creates GitHub Discussions for detected patterns, collects community feedback through voting, and creates GitHub Issues for approved pattern fixes.

## What Was Implemented

### 1. Pattern GitHub Integration (`services/pattern_github_integration.py`)

Comprehensive GitHub integration service that manages the full pattern approval workflow:

**Key Features:**
- **Threshold Detection:** Automatically identifies patterns that have exceeded occurrence thresholds
- **Discussion Creation:** Creates formatted GitHub Discussions with pattern details
- **Approval Voting:** Monitors discussion comments for approval/rejection votes
- **Issue Creation:** Converts approved discussions into tracked GitHub Issues
- **Database Tracking:** Records all GitHub entities in PostgreSQL for auditing

**Core Methods:**
- `check_patterns_for_thresholds()` - Find patterns needing discussion
- `create_pattern_discussion()` - Create GitHub Discussion
- `check_discussions_for_approval()` - Poll for approval votes
- `create_issue_from_discussion()` - Convert approved to Issue
- `process_patterns()` - Main orchestration method

### 2. Pattern GitHub Processor (`services/pattern_github_processor.py`)

Background service that runs GitHub integration on a schedule:

**Responsibilities:**
- Runs every 5 minutes (configurable)
- Processes patterns through the approval workflow
- Tracks statistics and metrics
- Handles errors gracefully with backoff

**Processing Flow:**
```
Every 5 minutes:
1. Check for patterns with >= 5 occurrences (no GitHub discussion yet)
2. Create GitHub Discussions for qualifying patterns
3. Check existing discussions for >= 3 approval votes
4. Create GitHub Issues for approved patterns
5. Update PostgreSQL with status changes
```

### 3. Discussion Template Format

Automatically generated discussions include:

**Header Section:**
- Pattern name and description
- Occurrence frequency and severity
- Impact score calculation
- Category classification

**Details Section:**
- First/last seen timestamps
- Affected projects and agents
- Example error messages

**Proposed Fix Section:**
- CLAUDE.md section to update
- Formatted fix content with code examples
- Rationale for the fix

**Community Interaction:**
- Clear approval instructions
- Voting keywords (APPROVE, REJECT)
- Threshold requirements (3 approvals)
- Links to documentation

**Example Discussion:**
```markdown
## Pattern Detected: git_directory_confusion

**Frequency:** 12 occurrences
**Severity:** MEDIUM
**Category:** workspace_navigation
**Impact Score:** 24

### Description

Agent attempts git operation outside repository

### Occurrence Details

- **First Seen:** 2025-10-05 10:15:23
- **Last Seen:** 2025-10-05 12:46:50
- **Affected Projects:** context-studio, orchestrator
- **Affected Agents:** senior_software_engineer, dev_environment_setup

### Proposed CLAUDE.md Fix

**Section:** `Git Operations`

\```markdown
### Git Operations Safety
Before running git commands, verify you are in the correct directory:
- Project repos are in `/workspace/<project-name>/`
- Always use `pwd` to confirm location before git operations
\```

### Community Input Needed

1. **Is this a real inefficiency or expected behavior?**
2. **Would the proposed fix help prevent this pattern?**
3. **Any additional context or alternative approaches?**

### How to Approve

- Comment with ✅ `APPROVE` to approve
- Comment with ❌ `REJECT` if not an issue
- Comment with 💬 feedback for improvements

Once this pattern receives **3 approvals**, it will be converted to an Issue.
```

### 4. Approval Workflow

**Voting Mechanism:**
- Users comment on discussions with approval keywords
- Approval keywords: `approve`, `approved`, `✅`, `👍`, `lgtm`, `yes`
- Rejection keywords: `reject`, `rejected`, `❌`, `👎`, `no`

**Approval Criteria:**
- Minimum 3 approval votes required
- Approvals must exceed rejections
- Votes counted from all discussion comments

**Automatic Issue Creation:**
Once approved, system automatically:
1. Creates GitHub Issue with `[Pattern Fix]` prefix
2. Links back to original discussion
3. Adds labels: `pattern-detection`, `automation`, `approved`
4. Includes implementation checklist
5. Defines success criteria for validation

### 5. PostgreSQL Integration

Enhanced `pattern_github_issues` table tracks workflow:

**Fields:**
- `issue_type` - 'discussion' or 'issue'
- `issue_number` - GitHub number
- `issue_url` - Direct link
- `issue_state` - 'open' or 'closed'
- `resolution` - 'accepted', 'rejected', 'duplicate', 'wont_fix'
- `occurrence_count` - Occurrences at time of creation
- `first_occurrence` / `last_occurrence` - Time range

**Status Tracking:**
- Discussion created → `issue_type='discussion', issue_state='open'`
- Discussion approved → `resolution='accepted', closed_at=NOW()`
- Issue created → New row with `issue_type='issue'`

### 6. Docker Compose Integration

Added `pattern-github-processor` service:

**Configuration:**
```yaml
pattern-github-processor:
  environment:
    - GITHUB_ORG=your-org
    - GITHUB_REPO=orchestrator
    - GITHUB_APP_ID=${GITHUB_APP_ID}
    - GITHUB_APP_INSTALLATION_ID=${GITHUB_APP_INSTALLATION_ID}
    - GITHUB_APP_PRIVATE_KEY_PATH=${GITHUB_APP_PRIVATE_KEY_PATH}
    - GITHUB_PROCESSING_INTERVAL=300  # 5 minutes
    - GITHUB_DISCUSSION_CATEGORY=Ideas
    - MIN_OCCURRENCES_FOR_DISCUSSION=5
```

**Dependencies:**
- PostgreSQL (for pattern data)
- GitHub App authentication (for API access)

## Architecture Flow

```
┌──────────────────────────────────────────────────────────────┐
│            Pattern GitHub Integration Workflow                 │
└──────────────────────────────────────────────────────────────┘

Every 5 minutes (pattern-github-processor):

1. Query PostgreSQL for Patterns
   └─> SELECT patterns with >= 5 occurrences
       └─> AND no existing GitHub discussion
           └─> ORDER BY occurrence count DESC

2. Create GitHub Discussions
   └─> For each qualifying pattern:
       ├─> Build discussion body with pattern details
       ├─> POST to GitHub Discussions API
       ├─> Record discussion in pattern_github_issues
       └─> Log creation event

3. Check Discussions for Approval
   └─> Query pattern_github_issues for open discussions
       └─> For each discussion:
           ├─> GET discussion with comments from GitHub
           ├─> Count approval/rejection keywords
           ├─> Check if >= 3 approvals && approvals > rejections
           └─> Return approved discussions

4. Create GitHub Issues
   └─> For each approved discussion:
       ├─> Create Issue via GitHub REST API
       ├─> Add labels: pattern-detection, approved
       ├─> Link to original discussion
       ├─> Record issue in pattern_github_issues
       ├─> Update discussion status to 'accepted'
       └─> Comment on discussion with issue link

5. Track Metrics
   └─> Log: patterns checked, discussions created,
       approvals found, issues created
```

## Configuration

### Environment Variables

```bash
# Required for GitHub Integration
export GITHUB_ORG="your-github-org"
export GITHUB_REPO="orchestrator"  # or your repo name
export GITHUB_APP_ID="123456"
export GITHUB_APP_INSTALLATION_ID="78901234"
export GITHUB_APP_PRIVATE_KEY_PATH="/home/orchestrator/.orchestrator/github-app-private-key.pem"

# Optional Tuning
export GITHUB_PROCESSING_INTERVAL="300"  # seconds (default: 5 minutes)
export GITHUB_DISCUSSION_CATEGORY="Ideas"  # or "Q&A", "Show and tell", etc.
export MIN_OCCURRENCES_FOR_DISCUSSION="5"  # threshold for discussion creation
```

### Thresholds

**Discussion Creation Threshold:**
- Default: 5 occurrences
- Rationale: Enough to confirm pattern, not so low as to create noise
- Configurable via `MIN_OCCURRENCES_FOR_DISCUSSION`

**Issue Creation Threshold:**
- Requirement: 3 approval votes
- Additional: Approvals must exceed rejections
- Prevents premature implementation of controversial fixes

### GitHub Discussion Category

Must be a valid category in your repository:
- "Ideas" (recommended) - For improvement proposals
- "Q&A" - If patterns need investigation
- "Show and tell" - For pattern metrics/analysis

List available categories:
```bash
# Via GitHub CLI
gh api repos/OWNER/REPO/discussions/categories

# Or check in repository settings → Discussions
```

## How to Use

### 1. Configure GitHub App Authentication

Ensure GitHub App is properly configured:

```bash
# Check GitHub App credentials
ls -la ~/.orchestrator/github-app-private-key.pem

# Verify environment variables
env | grep GITHUB_

# Test GitHub App authentication (optional)
docker-compose run orchestrator python scripts/test_github_app.py
```

### 2. Start the Pattern GitHub Processor

```bash
# Start the processor service
docker-compose up -d pattern-github-processor

# Verify service is running
docker-compose ps pattern-github-processor

# Monitor logs
docker-compose logs -f pattern-github-processor
```

### 3. Monitor Processing

Expected log output:
```
INFO - PatternGitHubIntegration initialized for your-org/orchestrator
INFO - Starting Pattern GitHub Processor service...
INFO - Starting pattern GitHub processing run...
INFO - Found 2 patterns exceeding discussion threshold
INFO - Created discussion #42 for pattern 'git_directory_confusion'
INFO - Created discussion #43 for pattern 'git_push_rejected'
INFO - Pattern GitHub processing complete in 2.34s:
       2 patterns checked, 2 discussions created, 0 approved, 0 issues created
```

### 4. Approve Patterns via GitHub

Navigate to repository Discussions:
1. Go to `https://github.com/OWNER/REPO/discussions`
2. Find pattern discussions (in configured category)
3. Review pattern details and proposed fix
4. Comment with approval:
   - `APPROVE` - simple approval
   - `✅ Looks good!` - approval with emoji
   - `LGTM` - approval acronym

### 5. Automatic Issue Creation

After 3 approvals:
1. Processor detects approval threshold met
2. Creates GitHub Issue automatically
3. Issue links back to discussion
4. Discussion marked as "accepted"
5. Implementation tracked in Issues

### 6. Query Status

```bash
# List patterns with GitHub discussions
docker-compose exec postgres psql -U orchestrator -d pattern_detection -c "
  SELECT p.pattern_name, pgi.issue_type, pgi.issue_number, pgi.issue_state,
         pgi.resolution, pgi.occurrence_count
  FROM pattern_github_issues pgi
  JOIN patterns p ON pgi.pattern_id = p.id
  ORDER BY pgi.created_at DESC;
"

# Count discussions by status
docker-compose exec postgres psql -U orchestrator -d pattern_detection -c "
  SELECT issue_state, resolution, COUNT(*) as count
  FROM pattern_github_issues
  WHERE issue_type = 'discussion'
  GROUP BY issue_state, resolution;
"

# List approved patterns waiting for implementation
docker-compose exec postgres psql -U orchestrator -d pattern_detection -c "
  SELECT p.pattern_name, pgi.issue_number, pgi.issue_url
  FROM pattern_github_issues pgi
  JOIN patterns p ON pgi.pattern_id = p.id
  WHERE pgi.issue_type = 'issue'
    AND pgi.issue_state = 'open';
"
```

## Success Criteria (Phase 3)

✅ **Discussion Creation:** Patterns auto-create discussions at threshold
✅ **Voting System:** Comments parsed for approval/rejection keywords
✅ **Issue Automation:** Approved patterns convert to Issues
✅ **Database Tracking:** Full lifecycle tracked in PostgreSQL
✅ **Error Handling:** Graceful handling of GitHub API errors
✅ **Human-in-Loop:** Community involvement before implementation

## Integration with Existing Systems

### Reused Components

**GitHub Integration (`services/github_discussions.py`):**
- Reused `create_discussion()` for pattern discussions
- Reused `get_discussion_by_number()` for approval checking
- Reused `add_discussion_comment()` for issue links
- Reused `find_category_by_name()` for category lookup

**GitHub App Authentication (`services/github_app.py`):**
- Reused App authentication for API calls
- Reused `graphql_request()` for Discussions API
- Reused `rest_request()` for Issues API

**Human Feedback Loop Pattern:**
- Adopted comment-based approval pattern
- Similar polling mechanism for feedback
- Consistent status tracking approach

## Metrics and Monitoring

### Processor Metrics

Available via `get_stats()`:
- `total_runs` - Total processing runs executed
- `total_discussions_created` - Cumulative discussions
- `total_issues_created` - Cumulative issues
- `processing_interval_seconds` - Current interval

### Processing Statistics

Logged per run:
- `patterns_checked` - Patterns evaluated
- `discussions_created` - New discussions this run
- `discussions_approved` - Approvals detected
- `issues_created` - New issues this run

### Health Indicators

**Healthy System:**
- Processing runs complete in < 5s
- No repeated GitHub API errors
- Discussions created match pattern frequency
- Approval rate 20-50% (not too high/low)

**Warning Signs:**
- Processing time > 30s (API slowness)
- Zero discussions ever created (threshold too high)
- 100% approval rate (threshold too low)
- GitHub API rate limit errors

## Known Limitations

1. **Comment-Based Voting:** No GitHub reactions API, relies on comment parsing
2. **No Vote De-duplication:** Same user can vote multiple times
3. **English Keywords Only:** Non-English approvals not detected
4. **No Vote Weighting:** All votes equal, no maintainer override
5. **Static Thresholds:** Same threshold for all pattern severities
6. **No Discussion Expiry:** Old discussions stay open indefinitely

## Future Enhancements (Phase 4)

1. **LLM Meta-Analysis:**
   - Weekly aggregate pattern analysis via Claude
   - Identify pattern correlations
   - Suggest consolidated fixes

2. **Impact Tracking:**
   - Measure pattern reduction post-fix
   - Calculate time saved
   - ROI metrics for improvements

3. **Smart Thresholds:**
   - Severity-based thresholds (critical=3, low=10)
   - Project-specific thresholds
   - Time-windowed thresholds

4. **Automated CLAUDE.md Updates:**
   - PR creation for approved fixes
   - Automated testing of fixes
   - Gradual rollout with monitoring

## Troubleshooting

### Service Won't Start

```bash
# Check PostgreSQL connectivity
docker-compose exec pattern-github-processor \
  python -c "import psycopg2; psycopg2.connect(host='postgres', user='orchestrator', password='orchestrator_dev', database='pattern_detection')"

# Check GitHub App authentication
docker-compose logs pattern-github-processor | grep "GitHub"
```

### No Discussions Created

```bash
# Verify patterns exist above threshold
docker-compose exec postgres psql -U orchestrator -d pattern_detection -c "
  SELECT p.pattern_name, COUNT(po.id) as count
  FROM patterns p
  INNER JOIN pattern_occurrences po ON p.id = po.pattern_id
  GROUP BY p.pattern_name
  HAVING COUNT(po.id) >= 5;
"

# Check if discussions already exist
docker-compose exec postgres psql -U orchestrator -d pattern_detection -c "
  SELECT * FROM pattern_github_issues WHERE issue_type = 'discussion';
"

# Verify GitHub category exists
docker-compose run orchestrator python -c "
from services.github_discussions import GitHubDiscussions
d = GitHubDiscussions()
cats = d.get_discussion_categories('OWNER', 'REPO')
print([c['name'] for c in cats])
"
```

### Discussions Not Converting to Issues

```bash
# Manually check discussion for approval
# Visit: https://github.com/OWNER/REPO/discussions/NUMBER

# Verify approval logic
docker-compose run orchestrator python -c "
from services.pattern_github_integration import PatternGitHubIntegration
p = PatternGitHubIntegration({...}, 'OWNER', 'REPO')
status = p._check_discussion_approval(NUMBER)
print(f'Approvals: {status[\"approve_count\"]}, Rejections: {status[\"reject_count\"]}')
"
```

### GitHub API Rate Limits

```bash
# Check rate limit status
curl -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/rate_limit

# Increase processing interval to reduce API calls
docker-compose up -d -e GITHUB_PROCESSING_INTERVAL=600 pattern-github-processor
```

## Files Changed/Created

### New Files
- `services/pattern_github_integration.py` - GitHub integration service
- `services/pattern_github_processor.py` - Background processor
- `docs/PATTERN_DETECTION_PHASE3_COMPLETE.md` - This document

### Modified Files
- `docker-compose.yml` - Added pattern-github-processor service

### Existing Files Reused
- `services/github_discussions.py` - Discussion API client
- `services/github_app.py` - GitHub App authentication
- `services/human_feedback_loop.py` - Human-in-loop patterns

## Next Steps: Phase 4

**LLM Meta-Analysis & Impact Tracking:**

1. Weekly pattern aggregation and analysis
2. Correlation detection (patterns that co-occur)
3. Automated CLAUDE.md PR creation
4. Fix impact measurement
5. ROI calculation and reporting

See `docs/SELF_IMPROVEMENT_PATTERN_DETECTION.md` for full roadmap.

## Resources

- **GitHub Discussions API:** https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions
- **GitHub App Auth:** https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app
- **Design Document:** `docs/SELF_IMPROVEMENT_PATTERN_DETECTION.md`
- **Phase 1 Details:** `docs/PATTERN_DETECTION_PHASE1_COMPLETE.md`
- **Phase 2 Details:** `docs/PATTERN_DETECTION_PHASE2_COMPLETE.md`
