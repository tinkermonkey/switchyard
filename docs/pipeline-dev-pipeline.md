# Development Pipeline Flow

This pipeline streamlines requirements analysis through code implementation with maker-checker patterns.

## Pipeline Configuration
- **Template**: `dev_pipeline`
- **Workflow Type**: `maker_checker`
- **Workspace**: `hybrid`
- **Discussion Stages**: `requirements`, `design`
- **Issue Stages**: `implementation`

## Flow Diagram

```mermaid
flowchart TD
    Start([GitHub Issue/Discussion Created]) --> TaskQueue[Task Queue Enqueue]

    TaskQueue --> ValidateTask{Validate Task<br/>Can Run?}

    ValidateTask -->|Dev Container Required| CheckDevContainer{Dev Container<br/>Verified?}
    CheckDevContainer -->|No| QueueDevSetup[Queue dev_environment_setup]
    QueueDevSetup --> WaitSetup[Wait for Setup]
    WaitSetup --> ValidateTask
    CheckDevContainer -->|Yes| ProceedTask

    ValidateTask -->|No Dev Container Needed| ProceedTask[Proceed with Task]

    ProceedTask --> Stage1Start[Stage 1: Requirements Analysis]

    subgraph DiscussionWorkspace1["GitHub Discussions Workspace"]
        subgraph DockerBA["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
            Stage1Start --> CreateBAAgent[Create business_analyst AgentStage]
            CreateBAAgent --> BuildBAPrompt[Build Prompt from Discussion]
            BuildBAPrompt --> RunBAClaude[Run Claude Code in Container]
            RunBAClaude --> BAOutput[Stream JSON Output]
            BAOutput --> PostBAComment[Post Requirements to Discussion]
        end
    end

    PostBAComment --> CheckBAQuality{Quality Gates<br/>Met?}

    CheckBAQuality -->|completeness_score < 0.6| RetryBA{Retries<br/>< 3?}
    RetryBA -->|Yes| Stage1Start
    RetryBA -->|No| FailPipeline[Circuit Breaker Opens]

    CheckBAQuality -->|Pass| Stage2Start[Stage 2: Architecture & Design]

    subgraph DiscussionWorkspace2["GitHub Discussions Workspace"]
        subgraph DockerArchitect["Docker Container: clauditoreum-orchestrator:latest"]
            Stage2Start --> CreateArchAgent[Create software_architect AgentStage]
            CreateArchAgent --> BuildArchPrompt[Build Prompt with BA Context]
            BuildArchPrompt --> RunArchClaude[Run Claude Code in Container]
            RunArchClaude --> ArchOutput[Stream JSON Output]
            ArchOutput --> PostArchComment[Post Architecture to Discussion]
        end
    end

    PostArchComment --> CheckArchQuality{Quality Gates<br/>Met?}

    CheckArchQuality -->|architectural_soundness < 0.6| RetryArch{Retries<br/>< 3?}
    RetryArch -->|Yes| Stage2Start
    RetryArch -->|No| FailPipeline

    CheckArchQuality -->|Pass| CreateIssue[Transition to GitHub Issues]
    CreateIssue --> CreateBranch[Create Feature Branch]
    CreateBranch --> Stage3Start[Stage 3: Implementation]

    subgraph IssueWorkspace["GitHub Issues Workspace"]
        subgraph MakerChecker["Maker-Checker Review Cycle"]
            subgraph DevContainer["Docker Container: {project}-dev:latest"]
                Stage3Start --> CreateEngineerAgent[Create senior_software_engineer AgentStage]
                CreateEngineerAgent --> BuildCodePrompt[Build Prompt with Design Context]
                BuildCodePrompt --> RunCodeClaude[Run Claude Code in Container]
                RunCodeClaude --> WriteCode[Write Code to /workspace]
                WriteCode --> RunTests[Run Tests in Dev Container]
                RunTests --> CodeOutput[Stream JSON Output]
                CodeOutput --> AutoCommit1[Auto-commit Changes to Branch]
            end

            AutoCommit1 --> CheckCodeQuality{Quality Gates<br/>Met?}

            CheckCodeQuality -->|code_quality < 0.7<br/>OR test_coverage < 0.7| RetryCode{Retries<br/>< 3?}
            RetryCode -->|Yes| Stage3Start
            RetryCode -->|No| FailPipeline

            CheckCodeQuality -->|Pass| ReviewRequired{Review<br/>Required?}

            ReviewRequired -->|Yes| StartReview[Start Review Cycle]

            subgraph ReviewCycle["Review Cycle Executor"]
                StartReview --> ReviewIteration[Iteration Counter: 0]

                ReviewIteration --> IncrementIter[Increment Iteration]

                subgraph DockerReviewer["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
                    IncrementIter --> CreateReviewerAgent[Create code_reviewer AgentStage]
                    CreateReviewerAgent --> BuildReviewPrompt[Build Review Prompt with Code Context]
                    BuildReviewPrompt --> RunReviewClaude[Run Claude Code in Container]
                    RunReviewClaude --> ReviewOutput[Stream JSON Output]
                    ReviewOutput --> PostReviewComment[Post Review to Issue]
                end

                PostReviewComment --> ParseReviewStatus{Parse Review<br/>Status}

                ParseReviewStatus -->|APPROVED| ReviewApproved[Review Approved]

                ParseReviewStatus -->|CHANGES_REQUESTED| CheckMaxIter{Iteration >=<br/>Max (3)?}

                ParseReviewStatus -->|BLOCKED| CheckBlockIter{Iteration > 1?}

                CheckBlockIter -->|Yes + escalate_on_blocked| EscalateBlocked[Escalate to Human]
                EscalateBlocked --> WaitHumanFeedback[Wait for Human Feedback]
                WaitHumanFeedback --> HumanResponded{Human<br/>Responded?}

                HumanResponded -->|Yes| RerunReviewer[Re-invoke Reviewer with Feedback]
                RerunReviewer --> ParseUpdatedReview{Updated Review<br/>Status}

                ParseUpdatedReview -->|APPROVED| ReviewApproved
                ParseUpdatedReview -->|CHANGES_REQUESTED| MakerRevision
                ParseUpdatedReview -->|BLOCKED| ReviewBlocked[Review Blocked - Manual]

                HumanResponded -->|No - Timeout| ReviewBlocked

                CheckBlockIter -->|No| MakerRevision[Re-invoke Maker with Feedback]

                CheckMaxIter -->|Yes| EscalateMaxIter[Escalate Max Iterations]
                EscalateMaxIter --> ReviewBlocked

                CheckMaxIter -->|No| MakerRevision

                subgraph DevContainerRevision["Docker Container: {project}-dev:latest"]
                    MakerRevision --> CreateEngineerAgentRev[Create senior_software_engineer AgentStage]
                    CreateEngineerAgentRev --> BuildRevisionPrompt[Build Prompt with Review Feedback]
                    BuildRevisionPrompt --> RunRevisionClaude[Run Claude Code in Container]
                    RunRevisionClaude --> UpdateCode[Update Code in /workspace]
                    UpdateCode --> RunTestsRev[Run Tests in Dev Container]
                    RunTestsRev --> RevisionOutput[Stream JSON Output]
                    RevisionOutput --> AutoCommit2[Auto-commit Changes to Branch]
                end

                AutoCommit2 --> ReviewIteration
            end

            ReviewApproved --> MoveToNext[Move to Next Column]
            ReviewBlocked --> StayInColumn[Stay in Current Column]

            ReviewRequired -->|No| MoveToNext
        end
    end

    MoveToNext --> CompletePipeline[Pipeline Complete]
    StayInColumn --> ManualIntervention[Requires Manual Intervention]

    FailPipeline --> LogFailure[Log Stage Failure]
    CompletePipeline --> LogSuccess[Log Stage Completion]
    ManualIntervention --> LogBlocked[Log Blocked State]

    LogSuccess --> End([End])
    LogFailure --> End
    LogBlocked --> End

    style DiscussionWorkspace1 fill:#e3f2fd
    style DiscussionWorkspace2 fill:#e3f2fd
    style IssueWorkspace fill:#fff3e0
    style DockerBA fill:#b3e5fc
    style DockerArchitect fill:#b3e5fc
    style DevContainer fill:#ffe0b2
    style DevContainerRevision fill:#ffe0b2
    style DockerReviewer fill:#f3e5f5
    style ReviewCycle fill:#fce4ec
    style FailPipeline fill:#ffebee
    style CompletePipeline fill:#e8f5e9
    style ManualIntervention fill:#fff9c4
```

## Key Implementation Details

### Workspace Transitions
- **Stage 1 (Requirements)**: GitHub Discussions workspace, read-only filesystem
- **Stage 2 (Design)**: GitHub Discussions workspace, read-write filesystem
- **Stage 3 (Implementation)**: GitHub Issues workspace, project-specific dev container

### Container Isolation
- **Business Analyst**: `clauditoreum-orchestrator:latest` (Read-Only FS)
- **Software Architect**: `clauditoreum-orchestrator:latest` (Read-Write)
- **Senior Software Engineer**: `{project}-dev:latest` (project dependencies installed)
- **Code Reviewer**: `clauditoreum-orchestrator:latest` (Read-Only FS)

### Git Workflow
1. Issue created triggers feature branch creation: `feature/issue-{number}`
2. All agent commits are auto-committed to feature branch
3. Branch remains open until review approved and merged

### Review Cycle Details
- **Max Iterations**: 3 (configurable)
- **Escalation Triggers**:
  - Blocking issues found on 2nd+ iteration
  - Max iterations reached without approval
- **Human-in-the-Loop**:
  - Escalation posts comment requesting human feedback
  - System polls for human response (1 hour timeout)
  - Reviewer re-runs with human feedback incorporated
  - Cycle resumes based on updated review status

### Quality Gates
- **Requirements**: completeness_score >= 0.6
- **Design**: architectural_soundness >= 0.6
- **Implementation**: code_quality >= 0.7, test_coverage >= 0.7

### Auto-Commit Behavior
- Triggered for agents with `makes_code_changes: true`
- Commits include agent name and task ID
- Automatic push to feature branch
- Preserves git history for audit trail
