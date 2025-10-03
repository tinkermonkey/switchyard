# Full SDLC Pipeline Flow

This pipeline implements the complete software development lifecycle with comprehensive maker-checker patterns.

## Pipeline Configuration
- **Template**: `full_sdlc`
- **Workflow Type**: `maker_checker`
- **Workspace**: `hybrid`
- **Discussion Stages**: `research`, `requirements`, `design`, `test_planning`
- **Issue Stages**: `implementation`, `qa_testing`, `documentation`

## Flow Diagram

```mermaid
flowchart TD
    Start([GitHub Discussion Created]) --> TaskQueue[Task Queue Enqueue]

    TaskQueue --> ValidateTask{Validate Task<br/>Can Run?}

    ValidateTask -->|Dev Container Required| CheckDevContainer{Dev Container<br/>Verified?}
    CheckDevContainer -->|No| QueueDevSetup[Queue dev_environment_setup]
    QueueDevSetup --> WaitSetup[Wait for Setup]
    WaitSetup --> ValidateTask
    CheckDevContainer -->|Yes| ProceedTask

    ValidateTask -->|No Dev Container Needed| ProceedTask[Proceed with Task]

    ProceedTask --> Stage1[Stage 1: Research]

    subgraph Discussion1["GitHub Discussions - Research"]
        subgraph DockerResearch["Docker Container: clauditoreum-orchestrator:latest"]
            Stage1 --> Research[idea_researcher Agent]
            Research --> ResearchOutput[Post to Discussion]
        end
    end

    ResearchOutput --> CheckResearchGates{Quality Gates<br/>research_depth >= 0.7<br/>feasibility >= 0.6}

    CheckResearchGates -->|Fail| RetryResearch{Retries<br/>< 2?}
    RetryResearch -->|Yes| Stage1
    RetryResearch -->|No| FailPipeline[Circuit Breaker Opens]

    CheckResearchGates -->|Pass| Stage2[Stage 2: Requirements]

    subgraph Discussion2["GitHub Discussions - Requirements"]
        subgraph MakerChecker1["Maker-Checker Review Cycle 1"]
            subgraph DockerBA["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
                Stage2 --> BA[business_analyst Agent - Maker]
                BA --> BAOutput[Post Requirements]
            end

            BAOutput --> CheckBAGates{Quality Gates<br/>completeness >= 0.7<br/>clarity >= 0.7}

            CheckBAGates -->|Fail| RetryBA{Retries<br/>< 3?}
            RetryBA -->|Yes| Stage2
            RetryBA -->|No| FailPipeline

            CheckBAGates -->|Pass| ReviewReq{Review<br/>Required?}

            ReviewReq -->|Yes| StartReqReview[Start Review Cycle]

            subgraph ReqReviewCycle["Requirements Review Cycle"]
                StartReqReview --> ReqIter[Iteration: 1]

                subgraph DockerPM["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
                    ReqIter --> PM[product_manager Agent - Reviewer]
                    PM --> PMReview[Post Review]
                end

                PMReview --> ParsePMReview{Review<br/>Status}

                ParsePMReview -->|APPROVED| ReqApproved[Requirements Approved]

                ParsePMReview -->|BLOCKED| CheckReqBlockIter{Iteration > 1<br/>+ escalate?}
                CheckReqBlockIter -->|Yes| EscalateReqBlock[Escalate Blocking Issues]
                EscalateReqBlock --> WaitReqHuman[Wait for Human Feedback]
                WaitReqHuman --> ReqHumanResp{Human<br/>Responded?}
                ReqHumanResp -->|Yes| RerunPM[Re-invoke PM with Feedback]
                RerunPM --> ParsePMUpdated{Updated<br/>Status}
                ParsePMUpdated -->|APPROVED| ReqApproved
                ParsePMUpdated -->|CHANGES| ReqRevision
                ParsePMUpdated -->|BLOCKED| ReqBlocked[Review Blocked]
                ReqHumanResp -->|No| ReqBlocked

                CheckReqBlockIter -->|No| ReqRevision

                ParsePMReview -->|CHANGES_REQUESTED| CheckReqMaxIter{Iteration >= 3?}
                CheckReqMaxIter -->|Yes| EscalateReqMax[Escalate Max Iterations]
                EscalateReqMax --> ReqBlocked
                CheckReqMaxIter -->|No| ReqRevision

                subgraph DockerBARev["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
                    ReqRevision[Re-invoke BA with Feedback] --> BARev[business_analyst Revision]
                    BARev --> BARevOutput[Post Revised Requirements]
                end

                BARevOutput --> IncrReqIter[Increment Iteration]
                IncrReqIter --> ReqIter
            end

            ReqApproved --> MoveToDesign[Move to Design]
            ReqBlocked --> StayReq[Stay in Requirements]

            ReviewReq -->|No| MoveToDesign
        end
    end

    StayReq --> ManualReq[Manual Intervention Required]
    MoveToDesign --> Stage3[Stage 3: Design]

    subgraph Discussion3["GitHub Discussions - Design"]
        subgraph MakerChecker2["Maker-Checker Review Cycle 2"]
            subgraph DockerArch["Docker Container: clauditoreum-orchestrator:latest"]
                Stage3 --> Arch[software_architect Agent - Maker]
                Arch --> ArchOutput[Post Architecture]
            end

            ArchOutput --> CheckArchGates{Quality Gates<br/>soundness >= 0.7<br/>scalability >= 0.6<br/>security >= 0.7}

            CheckArchGates -->|Fail| RetryArch{Retries<br/>< 3?}
            RetryArch -->|Yes| Stage3
            RetryArch -->|No| FailPipeline

            CheckArchGates -->|Pass| ReviewDesign{Review<br/>Required?}

            ReviewDesign -->|Yes| StartDesignReview[Start Review Cycle]

            subgraph DesignReviewCycle["Design Review Cycle"]
                StartDesignReview --> DesignIter[Iteration: 1]

                subgraph DockerDesignRev["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
                    DesignIter --> DesignReviewer[design_reviewer Agent]
                    DesignReviewer --> DesignRevOutput[Post Review]
                end

                DesignRevOutput --> ParseDesignReview{Review<br/>Status}

                ParseDesignReview -->|APPROVED| DesignApproved[Design Approved]

                ParseDesignReview -->|BLOCKED| CheckDesignBlockIter{Iteration > 1<br/>+ escalate?}
                CheckDesignBlockIter -->|Yes| EscalateDesignBlock[Escalate Blocking Issues]
                EscalateDesignBlock --> WaitDesignHuman[Wait for Human Feedback]
                WaitDesignHuman --> DesignHumanResp{Human<br/>Responded?}
                DesignHumanResp -->|Yes| RerunDesignRev[Re-invoke Reviewer with Feedback]
                RerunDesignRev --> ParseDesignUpdated{Updated<br/>Status}
                ParseDesignUpdated -->|APPROVED| DesignApproved
                ParseDesignUpdated -->|CHANGES| DesignRevision
                ParseDesignUpdated -->|BLOCKED| DesignBlocked[Review Blocked]
                DesignHumanResp -->|No| DesignBlocked

                CheckDesignBlockIter -->|No| DesignRevision

                ParseDesignReview -->|CHANGES_REQUESTED| CheckDesignMaxIter{Iteration >= 3?}
                CheckDesignMaxIter -->|Yes| EscalateDesignMax[Escalate Max Iterations]
                EscalateDesignMax --> DesignBlocked
                CheckDesignMaxIter -->|No| DesignRevision

                subgraph DockerArchRev["Docker Container: clauditoreum-orchestrator:latest"]
                    DesignRevision[Re-invoke Architect with Feedback] --> ArchRev[software_architect Revision]
                    ArchRev --> ArchRevOutput[Post Revised Design]
                end

                ArchRevOutput --> IncrDesignIter[Increment Iteration]
                IncrDesignIter --> DesignIter
            end

            DesignApproved --> MoveToTestPlan[Move to Test Planning]
            DesignBlocked --> StayDesign[Stay in Design]

            ReviewDesign -->|No| MoveToTestPlan
        end
    end

    StayDesign --> ManualDesign[Manual Intervention Required]
    MoveToTestPlan --> Stage4[Stage 4: Test Planning]

    subgraph Discussion4["GitHub Discussions - Test Planning"]
        subgraph MakerChecker3["Maker-Checker Review Cycle 3"]
            subgraph DockerTestPlan["Docker Container: clauditoreum-orchestrator:latest"]
                Stage4 --> TestPlanner[test_planner Agent - Maker]
                TestPlanner --> TestPlanOutput[Post Test Strategy]
            end

            TestPlanOutput --> CheckTestGates{Quality Gates<br/>coverage_target >= 0.7<br/>automation >= 0.6<br/>completeness >= 0.7}

            CheckTestGates -->|Fail| RetryTest{Retries<br/>< 2?}
            RetryTest -->|Yes| Stage4
            RetryTest -->|No| FailPipeline

            CheckTestGates -->|Pass| ReviewTest{Review<br/>Required?}

            ReviewTest -->|Yes| StartTestReview[Start Review Cycle]

            subgraph TestReviewCycle["Test Plan Review Cycle"]
                StartTestReview --> TestIter[Iteration: 1]

                subgraph DockerTestRev["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
                    TestIter --> TestReviewer[test_reviewer Agent]
                    TestReviewer --> TestRevOutput[Post Review]
                end

                TestRevOutput --> ParseTestReview{Review<br/>Status}

                ParseTestReview -->|APPROVED| TestApproved[Test Plan Approved]
                ParseTestReview -->|CHANGES/BLOCKED| TestRevisionFlow[Test Revision Flow]

                TestRevisionFlow --> TestPlanRevision[Similar Review Cycle as Above]
                TestPlanRevision --> TestApproved
            end

            TestApproved --> CreateIssue[Transition to GitHub Issues]
            ReviewTest -->|No| CreateIssue
        end
    end

    CreateIssue --> CreateBranch[Create Feature Branch]
    CreateBranch --> Stage5[Stage 5: Implementation]

    subgraph Issue1["GitHub Issues - Implementation"]
        subgraph MakerChecker4["Maker-Checker Review Cycle 4"]
            subgraph ProjectDevContainer["Docker Container: {project}-dev:latest"]
                Stage5 --> Engineer[senior_software_engineer Agent - Maker]
                Engineer --> WriteCode[Write Code to /workspace]
                WriteCode --> RunUnitTests[Run Unit Tests]
                RunUnitTests --> CodeOutput[Stream Output]
                CodeOutput --> AutoCommit1[Auto-commit to Feature Branch]
            end

            AutoCommit1 --> CheckCodeGates{Quality Gates<br/>code_quality >= 0.8<br/>test_coverage >= 0.8<br/>security >= 0.8}

            CheckCodeGates -->|Fail| RetryCode{Retries<br/>< 3?}
            RetryCode -->|Yes| Stage5
            RetryCode -->|No| FailPipeline

            CheckCodeGates -->|Pass| ReviewCode{Review<br/>Required?}

            ReviewCode -->|Yes| StartCodeReview[Start Review Cycle]

            subgraph CodeReviewCycle["Code Review Cycle"]
                StartCodeReview --> CodeIter[Iteration: 1]

                subgraph DockerCodeRev["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
                    CodeIter --> CodeReviewer[code_reviewer Agent]
                    CodeReviewer --> CodeRevOutput[Post Review to Issue]
                end

                CodeRevOutput --> ParseCodeReview{Review<br/>Status}

                ParseCodeReview -->|APPROVED| CodeApproved[Code Approved]

                ParseCodeReview -->|BLOCKED| CheckCodeBlockIter{Iteration > 1<br/>+ escalate?}
                CheckCodeBlockIter -->|Yes| EscalateCodeBlock[Escalate with PR Creation]
                EscalateCodeBlock --> WaitCodeHuman[Wait for Human Feedback]
                WaitCodeHuman --> CodeHumanResp{Human<br/>Responded?}
                CodeHumanResp -->|Yes| RerunCodeRev[Re-invoke Reviewer with Feedback]
                RerunCodeRev --> ParseCodeUpdated{Updated<br/>Status}
                ParseCodeUpdated -->|APPROVED| CodeApproved
                ParseCodeUpdated -->|CHANGES| CodeRevision
                ParseCodeUpdated -->|BLOCKED| CodeBlocked[Review Blocked]
                CodeHumanResp -->|No| CodeBlocked

                CheckCodeBlockIter -->|No| CodeRevision

                ParseCodeReview -->|CHANGES_REQUESTED| CheckCodeMaxIter{Iteration >= 3?}
                CheckCodeMaxIter -->|Yes| EscalateCodeMax[Escalate Max Iterations]
                EscalateCodeMax --> CodeBlocked
                CheckCodeMaxIter -->|No| CodeRevision

                subgraph ProjectDevContainerRev["Docker Container: {project}-dev:latest"]
                    CodeRevision[Re-invoke Engineer with Feedback] --> EngRev[senior_software_engineer Revision]
                    EngRev --> UpdateCode[Update Code]
                    UpdateCode --> RunTestsRev[Run Tests]
                    RunTestsRev --> AutoCommit2[Auto-commit Updates]
                end

                AutoCommit2 --> IncrCodeIter[Increment Iteration]
                IncrCodeIter --> CodeIter
            end

            CodeApproved --> MoveToQA[Move to QA Testing]
            CodeBlocked --> StayImpl[Stay in Implementation]

            ReviewCode -->|No| MoveToQA
        end
    end

    StayImpl --> ManualCode[Manual Intervention Required]
    MoveToQA --> Stage6[Stage 6: QA Testing]

    subgraph Issue2["GitHub Issues - QA Testing"]
        subgraph ProjectDevContainer2["Docker Container: {project}-dev:latest"]
            Stage6 --> QAEngineer[senior_qa_engineer Agent]
            QAEngineer --> WriteTests[Write E2E & Integration Tests]
            WriteTests --> RunE2ETests[Run Full Test Suite]
            RunE2ETests --> QAOutput[Stream Output]
            QAOutput --> AutoCommitQA[Auto-commit Test Code]
        end

        AutoCommitQA --> CheckQAGates{Quality Gates<br/>quality_score >= 0.8<br/>prod_readiness >= 0.8}

        CheckQAGates -->|Fail| RetryQA{Retries<br/>< 3?}
        RetryQA -->|Yes| Stage6
        RetryQA -->|No| FailPipeline

        CheckQAGates -->|Pass| MoveToDocs[Move to Documentation]
    end

    MoveToDocs --> Stage7[Stage 7: Documentation]

    subgraph Issue3["GitHub Issues - Documentation"]
        subgraph MakerChecker5["Maker-Checker Review Cycle 5"]
            subgraph DockerWriter["Docker Container: clauditoreum-orchestrator:latest"]
                Stage7 --> TechWriter[technical_writer Agent - Maker]
                TechWriter --> CreateDocs[Create Documentation Files]
                CreateDocs --> DocsOutput[Stream Output]
                DocsOutput --> AutoCommitDocs[Auto-commit Documentation]
            end

            AutoCommitDocs --> CheckDocsGates{Quality Gates<br/>completeness >= 0.8<br/>clarity >= 0.8<br/>accuracy >= 0.85}

            CheckDocsGates -->|Fail| RetryDocs{Retries<br/>< 2?}
            RetryDocs -->|Yes| Stage7
            RetryDocs -->|No| FailPipeline

            CheckDocsGates -->|Pass| ReviewDocs{Review<br/>Required?}

            ReviewDocs -->|Yes| StartDocsReview[Start Review Cycle]

            subgraph DocsReviewCycle["Documentation Review Cycle"]
                StartDocsReview --> DocsIter[Iteration: 1]

                subgraph DockerDocsRev["Docker Container: clauditoreum-orchestrator:latest (Read-Only FS)"]
                    DocsIter --> DocsReviewer[documentation_editor Agent]
                    DocsReviewer --> DocsRevOutput[Post Review]
                end

                DocsRevOutput --> ParseDocsReview{Review<br/>Status}

                ParseDocsReview -->|APPROVED| DocsApproved[Documentation Approved]
                ParseDocsReview -->|CHANGES/BLOCKED| DocsRevisionFlow[Docs Revision Flow]

                DocsRevisionFlow --> DocsPlanRevision[Similar Review Cycle as Above]
                DocsPlanRevision --> DocsApproved
            end

            DocsApproved --> CompletePipeline[Pipeline Complete]
            ReviewDocs -->|No| CompletePipeline
        end
    end

    CompletePipeline --> LogSuccess[Log Stage Completion]
    FailPipeline --> LogFailure[Log Stage Failure]
    ManualReq --> LogBlocked1[Log Blocked State]
    ManualDesign --> LogBlocked2[Log Blocked State]
    ManualCode --> LogBlocked3[Log Blocked State]

    LogSuccess --> End([End - Ready for Deployment])
    LogFailure --> End
    LogBlocked1 --> End
    LogBlocked2 --> End
    LogBlocked3 --> End

    style Discussion1 fill:#e3f2fd
    style Discussion2 fill:#e3f2fd
    style Discussion3 fill:#e3f2fd
    style Discussion4 fill:#e3f2fd
    style Issue1 fill:#fff3e0
    style Issue2 fill:#fff3e0
    style Issue3 fill:#fff3e0
    style DockerResearch fill:#b3e5fc
    style DockerBA fill:#b3e5fc
    style DockerBARev fill:#b3e5fc
    style DockerPM fill:#e1bee7
    style DockerArch fill:#b3e5fc
    style DockerArchRev fill:#b3e5fc
    style DockerDesignRev fill:#e1bee7
    style DockerTestPlan fill:#b3e5fc
    style DockerTestRev fill:#e1bee7
    style ProjectDevContainer fill:#ffe0b2
    style ProjectDevContainerRev fill:#ffe0b2
    style DockerCodeRev fill:#e1bee7
    style ProjectDevContainer2 fill:#ffe0b2
    style DockerWriter fill:#b3e5fc
    style DockerDocsRev fill:#e1bee7
    style FailPipeline fill:#ffebee
    style CompletePipeline fill:#e8f5e9
    style ManualReq fill:#fff9c4
    style ManualDesign fill:#fff9c4
    style ManualCode fill:#fff9c4
```

## Key Implementation Details

### 7-Stage Pipeline Overview
1. **Research** (Discussion) - Market analysis and feasibility
2. **Requirements** (Discussion) - Business analysis with PM review
3. **Design** (Discussion) - Architecture with design review
4. **Test Planning** (Discussion) - Test strategy with test review
5. **Implementation** (Issue) - Code development with code review
6. **QA Testing** (Issue) - End-to-end testing validation
7. **Documentation** (Issue) - Technical documentation with editorial review

### Container Strategy by Stage
- **Stages 1-4**: `clauditoreum-orchestrator:latest` (no project dependencies)
- **Stages 5-6**: `{project}-dev:latest` (project dependencies + test frameworks)
- **Stage 7**: `clauditoreum-orchestrator:latest` (documentation only)

### Maker-Checker Patterns
Five review cycles with different configurations:
1. **Requirements**: business_analyst (maker) → product_manager (reviewer)
2. **Design**: software_architect (maker) → design_reviewer (reviewer)
3. **Test Planning**: test_planner (maker) → test_reviewer (reviewer)
4. **Implementation**: senior_software_engineer (maker) → code_reviewer (reviewer)
5. **Documentation**: technical_writer (maker) → documentation_editor (reviewer)

### Escalation Strategy
- **Blocking Issues**: Escalate after 2nd iteration, wait for human feedback (1 hour timeout)
- **Max Iterations**: Escalate when 3 iterations reached without approval
- **PR Creation**: Implementation stage creates GitHub PR on escalation for easier human review

### Quality Gate Thresholds
- **Research**: research_depth >= 0.7, feasibility >= 0.6
- **Requirements**: completeness >= 0.7, clarity >= 0.7
- **Design**: soundness >= 0.7, scalability >= 0.6, security >= 0.7
- **Test Planning**: coverage_target >= 0.7, automation >= 0.6, completeness >= 0.7
- **Implementation**: code_quality >= 0.8, test_coverage >= 0.8, security >= 0.8
- **QA Testing**: quality_score >= 0.8, prod_readiness >= 0.8
- **Documentation**: completeness >= 0.8, clarity >= 0.8, accuracy >= 0.85

### State Persistence
- Checkpoints created before each stage
- Review cycle state tracked in ReviewCycleState
- Pipeline can resume from any checkpoint on failure

### Human-in-the-Loop Integration
- Automated escalation with clear next steps
- Polling mechanism detects human comments
- Reviewer re-runs with human feedback context
- Cycle continues based on updated review status
