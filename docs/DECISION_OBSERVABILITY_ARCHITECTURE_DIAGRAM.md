# Orchestrator Decision Observability - Architecture Diagram

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          ORCHESTRATOR SERVICES                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐      │
│  │ ProjectMonitor   │    │  ReviewCycle     │    │ WorkspaceRouter  │      │
│  │                  │    │  Manager         │    │                  │      │
│  │ - Status changes │    │ - Maker routing  │    │ - Issues vs      │      │
│  │ - Feedback       │    │ - Reviewer       │    │   Discussions    │      │
│  │   detection      │    │   routing        │    │                  │      │
│  └────────┬─────────┘    └────────┬─────────┘    └────────┬─────────┘      │
│           │                       │                       │                  │
│           │ DecisionEventEmitter  │                       │                  │
│           └───────────────────────┴───────────────────────┘                  │
│                                   │                                          │
│                                   ▼                                          │
│                    ┌──────────────────────────────┐                          │
│                    │   DecisionEventEmitter       │                          │
│                    │                              │                          │
│                    │  - emit_agent_routing()      │                          │
│                    │  - emit_feedback_detected()  │                          │
│                    │  - emit_status_progression() │                          │
│                    │  - emit_review_cycle()       │                          │
│                    │  - emit_error_decision()     │                          │
│                    │  - emit_workspace_routing()  │                          │
│                    └──────────────┬───────────────┘                          │
│                                   │                                          │
│                                   ▼                                          │
│                    ┌──────────────────────────────┐                          │
│                    │   ObservabilityManager       │                          │
│                    │                              │                          │
│                    │  - emit(event_type, data)    │                          │
│                    │  - Redis pub/sub             │                          │
│                    │  - Redis Stream              │                          │
│                    └──────────────┬───────────────┘                          │
│                                   │                                          │
└───────────────────────────────────┼──────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              REDIS                                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  ┌─────────────────────────────────┐    ┌─────────────────────────────────┐ │
│  │  Pub/Sub Channel                │    │  Stream (History)               │ │
│  │  "orchestrator:agent_events"    │    │  "orchestrator:event_stream"    │ │
│  │                                 │    │                                 │ │
│  │  - Real-time event broadcast    │    │  - Last 1000 events             │ │
│  │  - No persistence               │    │  - 2 hour TTL                   │ │
│  │  - Immediate delivery           │    │  - Queryable history            │ │
│  └─────────────────────────────────┘    └─────────────────────────────────┘ │
│           │                                         │                        │
└───────────┼─────────────────────────────────────────┼────────────────────────┘
            │                                         │
            ▼                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      OBSERVABILITY SERVER                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  redis_subscriber_thread()                                           │   │
│  │                                                                       │   │
│  │  - Subscribes to pub/sub channel                                     │   │
│  │  - Listens for events                                                │   │
│  │  - Broadcasts to WebSocket clients                                   │   │
│  └────────────────────────────────┬─────────────────────────────────────┘   │
│                                   │                                          │
│                                   │ socketio.emit('agent_event', event)      │
│                                   ▼                                          │
│                        ┌───────────────────────┐                             │
│                        │   WebSocket Server    │                             │
│                        │   (Socket.IO)         │                             │
│                        └───────────┬───────────┘                             │
│                                    │                                         │
└────────────────────────────────────┼─────────────────────────────────────────┘
                                     │
                                     │ ws://localhost:5001/
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         WEB UI (Browser)                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │  observability.html                                                    │ │
│  │                                                                        │ │
│  │  socket.on('agent_event', (event) => {                                │ │
│  │    if (event.event_type === 'agent_routing_decision') {              │ │
│  │      renderRoutingDecision(event);                                    │ │
│  │    } else if (event.event_type === 'feedback_detected') {            │ │
│  │      renderFeedbackEvent(event);                                      │ │
│  │    } else if (event.event_type === 'review_cycle_started') {         │ │
│  │      renderReviewCycleEvent(event);                                   │ │
│  │    }                                                                   │ │
│  │    // ... etc for all event types                                     │ │
│  │  });                                                                   │ │
│  │                                                                        │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                               │
│  ┌───────────────────────────────────────────────────────────────┐          │
│  │  VISUAL DISPLAY                                               │          │
│  │                                                                │          │
│  │  ┌─────────────────────────────────────────────────────────┐ │          │
│  │  │ AGENT_ROUTING_DECISION                                  │ │          │
│  │  │ ⚡ Orchestrator selected: software_architect            │ │          │
│  │  │ Issue #123 | Board: dev | Status: In Progress          │ │          │
│  │  │ Reason: Status maps to architecture stage              │ │          │
│  │  │ Alternatives: [business_analyst, product_manager]      │ │          │
│  │  └─────────────────────────────────────────────────────────┘ │          │
│  │                                                                │          │
│  │  ┌─────────────────────────────────────────────────────────┐ │          │
│  │  │ FEEDBACK_DETECTED                                       │ │          │
│  │  │ 💬 Feedback detected on issue #123                      │ │          │
│  │  │ Source: comment | Action: queue_agent_task             │ │          │
│  │  │ Target Agent: software_architect                        │ │          │
│  │  └─────────────────────────────────────────────────────────┘ │          │
│  │                                                                │          │
│  │  ┌─────────────────────────────────────────────────────────┐ │          │
│  │  │ REVIEW_CYCLE_STARTED                                    │ │          │
│  │  │ 🔄 Review cycle started for issue #123                  │ │          │
│  │  │ Maker: senior_software_engineer                         │ │          │
│  │  │ Reviewer: code_reviewer                                 │ │          │
│  │  │ Max iterations: 3                                       │ │          │
│  │  └─────────────────────────────────────────────────────────┘ │          │
│  └───────────────────────────────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Event Flow Diagram

```
┌───────────────────────────────────────────────────────────────────────────┐
│  DECISION POINT IN CODE                                                    │
└───────────────────────────────────────────────────────────────────────────┘
                                   │
                                   │ 1. Decision logic executes
                                   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  decision_events.emit_agent_routing_decision(                             │
│      issue_number=123,                                                     │
│      project="my-project",                                                 │
│      selected_agent="architect",                                           │
│      reason="Status mapping"                                               │
│  )                                                                         │
└───────────────────────────────────────────────────────────────────────────┘
                                   │
                                   │ 2. DecisionEventEmitter formats data
                                   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  obs.emit(                                                                 │
│      EventType.AGENT_ROUTING_DECISION,                                     │
│      agent="orchestrator",                                                 │
│      task_id="routing_my-project_123",                                     │
│      project="my-project",                                                 │
│      data={...}                                                            │
│  )                                                                         │
└───────────────────────────────────────────────────────────────────────────┘
                                   │
                                   │ 3. ObservabilityManager creates event
                                   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  event = ObservabilityEvent(                                               │
│      timestamp="2025-10-09T12:34:56Z",                                     │
│      event_type="agent_routing_decision",                                  │
│      agent="orchestrator",                                                 │
│      task_id="routing_my-project_123",                                     │
│      project="my-project",                                                 │
│      data={...}                                                            │
│  )                                                                         │
└───────────────────────────────────────────────────────────────────────────┘
                                   │
                                   │ 4. Publish to Redis
                                   ▼
                  ┌────────────────────────────────────┐
                  │                                    │
         ┌────────▼────────┐              ┌───────────▼──────────┐
         │  Pub/Sub         │              │  Stream              │
         │  (Real-time)     │              │  (History)           │
         │                  │              │                      │
         │  PUBLISH         │              │  XADD                │
         │  orchestrator:   │              │  orchestrator:       │
         │  agent_events    │              │  event_stream        │
         └────────┬─────────┘              └───────────┬──────────┘
                  │                                    │
                  │ 5. Redis distributes               │
                  ▼                                    ▼
         ┌─────────────────────────────────────────────────────┐
         │  ObservabilityServer subscribes and forwards        │
         └─────────────────┬───────────────────────────────────┘
                           │
                           │ 6. WebSocket broadcast
                           ▼
         ┌─────────────────────────────────────────────────────┐
         │  socketio.emit('agent_event', event)                │
         └─────────────────┬───────────────────────────────────┘
                           │
                           │ 7. Browser receives
                           ▼
         ┌─────────────────────────────────────────────────────┐
         │  socket.on('agent_event', (event) => {              │
         │      addEvent(event);                               │
         │      renderDecisionEvent(event);                    │
         │  });                                                │
         └─────────────────┬───────────────────────────────────┘
                           │
                           │ 8. User sees event
                           ▼
         ┌─────────────────────────────────────────────────────┐
         │  ⚡ AGENT ROUTING DECISION                           │
         │  Orchestrator → software_architect                  │
         │  Issue #123 | Reason: Status mapping               │
         └─────────────────────────────────────────────────────┘
```

## Event Type Hierarchy

```
EventType (Enum)
├── EXISTING EVENTS (26)
│   ├── Lifecycle (5)
│   │   ├── TASK_RECEIVED
│   │   ├── AGENT_INITIALIZED
│   │   ├── AGENT_STARTED
│   │   ├── AGENT_COMPLETED
│   │   └── AGENT_FAILED
│   ├── Prompts (3)
│   │   ├── PROMPT_CONSTRUCTED
│   │   ├── CLAUDE_API_CALL_STARTED
│   │   └── CLAUDE_API_CALL_COMPLETED
│   ├── Responses (3)
│   │   ├── RESPONSE_CHUNK_RECEIVED
│   │   ├── RESPONSE_PROCESSING_STARTED
│   │   └── RESPONSE_PROCESSING_COMPLETED
│   ├── Tools (2)
│   │   ├── TOOL_EXECUTION_STARTED
│   │   └── TOOL_EXECUTION_COMPLETED
│   └── Performance (2)
│       ├── PERFORMANCE_METRIC
│       └── TOKEN_USAGE
│
└── NEW DECISION EVENTS (32)
    ├── Feedback (4)
    │   ├── FEEDBACK_DETECTED ◄─── Feedback found on issue
    │   ├── FEEDBACK_LISTENING_STARTED ◄─── Start monitoring
    │   ├── FEEDBACK_LISTENING_STOPPED ◄─── Stop monitoring
    │   └── FEEDBACK_IGNORED ◄─── Feedback not actionable
    │
    ├── Routing (3)
    │   ├── AGENT_ROUTING_DECISION ◄─── Which agent to run
    │   ├── AGENT_SELECTED ◄─── Agent selected (simplified)
    │   └── WORKSPACE_ROUTING_DECISION ◄─── Issues vs discussions
    │
    ├── Progression (4)
    │   ├── STATUS_PROGRESSION_STARTED ◄─── Before status change
    │   ├── STATUS_PROGRESSION_COMPLETED ◄─── After success
    │   ├── STATUS_PROGRESSION_FAILED ◄─── After failure
    │   └── PIPELINE_STAGE_TRANSITION ◄─── Stage change
    │
    ├── Review Cycles (6)
    │   ├── REVIEW_CYCLE_STARTED ◄─── Cycle begins
    │   ├── REVIEW_CYCLE_ITERATION ◄─── New iteration
    │   ├── REVIEW_CYCLE_MAKER_SELECTED ◄─── Maker chosen
    │   ├── REVIEW_CYCLE_REVIEWER_SELECTED ◄─── Reviewer chosen
    │   ├── REVIEW_CYCLE_ESCALATED ◄─── Escalate to human
    │   └── REVIEW_CYCLE_COMPLETED ◄─── Cycle done
    │
    ├── Conversational (4)
    │   ├── CONVERSATIONAL_LOOP_STARTED ◄─── Loop begins
    │   ├── CONVERSATIONAL_QUESTION_ROUTED ◄─── Question → agent
    │   ├── CONVERSATIONAL_LOOP_PAUSED ◄─── Loop paused
    │   └── CONVERSATIONAL_LOOP_RESUMED ◄─── Loop resumed
    │
    ├── Error Handling (5)
    │   ├── ERROR_ENCOUNTERED ◄─── Error happened
    │   ├── ERROR_RECOVERED ◄─── Recovery successful
    │   ├── CIRCUIT_BREAKER_OPENED ◄─── Circuit opened
    │   ├── CIRCUIT_BREAKER_CLOSED ◄─── Circuit closed
    │   └── RETRY_ATTEMPTED ◄─── Retry attempted
    │
    └── Task Management (4)
        ├── TASK_QUEUED ◄─── Task added to queue
        ├── TASK_DEQUEUED ◄─── Task taken from queue
        ├── TASK_PRIORITY_CHANGED ◄─── Priority changed
        └── TASK_CANCELLED ◄─── Task cancelled
```

## Integration Points Map

```
┌────────────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR CODEBASE                                                  │
├────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  services/project_monitor.py                                            │
│  ├─ detect_changes()                                                    │
│  │  └─> emit_status_progression() ◄── Status changes detected          │
│  │                                                                       │
│  ├─ _get_agent_for_status()                                             │
│  │  └─> emit_agent_routing_decision() ◄── Agent selection              │
│  │                                                                       │
│  └─ check_for_feedback()                                                │
│     └─> emit_feedback_detected() ◄── Feedback found                    │
│                                                                          │
│  services/review_cycle.py                                               │
│  ├─ start_review_cycle()                                                │
│  │  └─> emit_review_cycle_decision(type='start')                       │
│  │                                                                       │
│  ├─ _execute_review_loop()                                              │
│  │  ├─> emit_review_cycle_decision(type='maker_selected')              │
│  │  ├─> emit_review_cycle_decision(type='reviewer_selected')           │
│  │  └─> emit_review_cycle_decision(type='escalate')                    │
│  │                                                                       │
│  └─ complete_review_cycle()                                             │
│     └─> emit_review_cycle_decision(type='complete')                    │
│                                                                          │
│  services/workspace_router.py                                           │
│  └─ determine_workspace()                                               │
│     └─> emit_workspace_routing() ◄── Issues vs discussions choice      │
│                                                                          │
│  services/pipeline_progression.py                                       │
│  └─ progress_to_next_stage()                                            │
│     ├─> emit_status_progression(success=None) ◄── Before               │
│     ├─> [execute move]                                                  │
│     └─> emit_status_progression(success=True/False) ◄── After          │
│                                                                          │
│  agents/orchestrator_integration.py                                     │
│  └─ process_task_integrated()                                           │
│     └─> emit_error_decision() ◄── Error handling                       │
│                                                                          │
│  services/circuit_breaker.py (future)                                   │
│  ├─ open_circuit()                                                      │
│  │  └─> emit_circuit_breaker_opened()                                  │
│  └─ close_circuit()                                                     │
│     └─> emit_circuit_breaker_closed()                                  │
│                                                                          │
└────────────────────────────────────────────────────────────────────────┘
```

## Data Flow - Complete Example

```
[User Action] → [GitHub] → [ProjectMonitor] → [Decision Events] → [Redis] → [UI]

1. USER: Moves issue #123 to "In Progress" in GitHub Projects
   │
2. GITHUB: Issue status updated
   │
3. PROJECT_MONITOR: Detects change in next poll
   │
   ├─> emit_status_progression()
   │   └─> EventType: STATUS_PROGRESSION_COMPLETED
   │       Data: from="Backlog", to="In Progress", trigger="manual"
   │
   ├─> _get_agent_for_status()
   │   │
   │   ├─> emit_agent_routing_decision()
   │   │   └─> EventType: AGENT_ROUTING_DECISION
   │   │       Data: selected="software_architect", reason="Status mapping"
   │   │
   │   └─> determine_workspace()
   │       │
   │       └─> emit_workspace_routing()
   │           └─> EventType: WORKSPACE_ROUTING_DECISION
   │               Data: workspace="issues"
   │
   └─> task_queue.enqueue()
       │
       └─> emit_task_queued()
           └─> EventType: TASK_QUEUED
               Data: agent="software_architect", priority="NORMAL"
               
4. REDIS:
   ├─> Pub/Sub: Broadcasts events immediately
   └─> Stream: Stores events for history
   
5. OBSERVABILITY_SERVER:
   └─> WebSocket: Forwards events to connected clients
   
6. WEB_UI:
   └─> Renders events in real-time dashboard
   
7. USER: Sees complete decision flow in observability UI
```

## Visual Legend

```
Symbol Key:
─────────────
→   Data flow
▼   Downward flow
├── Branch
└── End branch
◄── Event emission point
⚡  Decision event
💬  Feedback event
🔄  Review cycle event
```
