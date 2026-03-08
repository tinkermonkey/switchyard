This is a comprehensive visual system designed for a complex flowchart of agentic workflows.

### Design Philosophy

To make a complex flowchart readable, we use a tiered visual strategy:

1. **Color Coding (Lane/Category):** The outermost border or background color of the flowchart node immediately identifies which *sub-system* or *lifecycle phase* the event belongs to.
2. **Status Indication (Semantic Color):** For success/failure/warning states, we override the category color or use specific icons to provide immediate status context.
3. **Iconography (Action):** The central icon describes the *specific action* taken within that category.

---

### Global Color Palette & Status Semantics

Use these semantic colors for backgrounds, borders, or icon highlights to denote status across all categories.

| Status | Color | Hex | Meaning |
| --- | --- | --- | --- |
| **Success / Complete** | Emerald Green | `#10B981` | Positive outcome, step finished. |
| **Failure / Error** | Red | `#EF4444` | Negative outcome, process stopped. |
| **Warning / Pause / Escalation** | Amber Yellow | `#F59E0B` | Attention required, process delayed or altered. |
| **Information / Neutral** | Sky Blue | `#0EA5E9` | Start of a process, generic update. |
| **Hidden / Background** | Cool Grey | `#94A3B8` | Low-priority logistical event. |

---

### Iconography & Color System by Category

#### 1. Agent Execution & Routing

**Theme Color:** Deep Purple (`#8B5CF6`) - Represents intelligence and decision-making.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `agent_initialized` | 🟣 + ⚡ (`#8B5CF6`) | A high-voltage bolt or power symbol inside a circle. The agent is powered on. |
| `agent_routing_decision` | 🟣 + 🧠 (`#8B5CF6`) | A stylized brain with arrows splitting from it. The orchestrator is thinking. |
| `agent_selected` | 🟢 + 👤 (`#10B981`) | A user avatar with a checkmark. A specific actor has been chosen. |
| `workspace_routing_decision` | 🟣 + 🗺️ (`#8B5CF6`) | A map with destination pins. Deciding where the work happens. |

#### 2. Pipeline Lifecycle & Status Progression

**Theme Color:** Status-Dependent (Semantic) - These are high-level state changes.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `pipeline_run_started` | 🔵 + ▶️ (`#0EA5E9`) | The standard "Play" triangle. |
| `pipeline_run_completed` | 🟢 + 🎉 (`#10B981`) | A party popper or trophy. Major success. |
| `pipeline_run_failed` | 🔴 + 🛑 (`#EF4444`) | An octagonal stop sign. |
| `pipeline_stage_transition` | 🔵 + ⏭️ (`#0EA5E9`) | "Next track" symbol. Moving forward a major step. |
| `status_progression_started` | 🔵 + ⚙️ (`#0EA5E9`) | A spinning gear. Work is in motion (moving columns). |
| `status_progression_completed` | 🟢 + ✅ (`#10B981`) | A standard checkmark. The move is done. |
| `status_progression_failed` | 🔴 + ⚠️ (`#EF4444`) | A warning triangle. The move failed. |

#### 3. Review Cycle (Maker-Checker)

**Theme Color:** Magenta/Pink (`#EC4899`) - Highlights distinct human-in-the-loop or cross-checking processes.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `review_cycle_started` | 💖 + 📋 (`#EC4899`) | A clipboard. The checklist is open. |
| `review_cycle_iteration` | 💖 + 🔄 (`#EC4899`) | A cyclic arrow. Another round. |
| `review_cycle_maker_selected` | 💖 + ✍️ (`#EC4899`) | A hand holding a pen. The creator is assigned. |
| `review_cycle_reviewer_selected` | 💖 + 👁️ (`#EC4899`) | An open eye. The inspector is assigned. |
| `review_cycle_escalated` | 🟠 + 🔔 (`#F59E0B`) | A ringing alert bell. Attention required. |
| `review_cycle_completed` | 🟢 + 🤝 (`#10B981`) | Shaking hands. Agreement reached, review passed. |

#### 4. Repair Cycle (Test-Fix) - Complex Sub-System

**Theme Color:** Teal (`#14B8A6`) - Represents technical, structural, and foundational work.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `repair_cycle_started` | 🌐 + 🛠️ (`#14B8A6`) | Crossed wrench and hammer. Heavy work begins. |
| `repair_cycle_iteration` | 🌐 + 🔁 (`#14B8A6`) | Repeat arrows. Another attempt to fix. |
| `repair_cycle_completed` | 🟢 + 🩹 (`#10B981`) | A band-aid over a crack. Issue is patched/fixed. |
| `repair_cycle_failed` | 🔴 + 💥 (`#EF4444`) | A collision/explosion symbol. The repair didn't work. |
| `repair_cycle_env_rebuild_started` | 🌐 + 🏗️ (`#14B8A6`) | A construction crane. Rebuilding the foundation. |
| `repair_cycle_env_rebuild_completed` | 🟢 + 🏢 (`#10B981`) | A completed building. Environment is ready. |

#### 5. Repair Sub-cycles (Test, Fix, Warning, Systemic)

*Use the Teal (`#14B8A6`) border/theme, but distinct icons for the focus.*

| Event Type | Visual Representation (Icon only) | Icon Metaphor |
| --- | --- | --- |
| **Test Sub-cycle** | `_test_` events | **🧪 (Beaker)**: Testing, lab work. |
| **Test Execution** | `_test_execution_` events | **⏱️ (Stopwatch)**: A specific test run. |
| **Fix Sub-cycle** | `_fix_` events | **👨‍🏭 (Welder/Wrench)**: Applying the code fix. |
| **File Fix** | `_file_fix_` events | **📄 (File + Wrench)**: Targeted fix on one file. |
| **Warning Review** | `_warning_review_` events | **🧐 (Monocle Face)**: Closely inspecting subtle warnings. |
| **Systemic Analysis** | `_systemic_analysis_` | **🔍 (Magnifying Glass)**: Searching for root causes. |
| **Systemic Fix** | `_systemic_fix_` | **🕸️ (Network/Web)**: Fixing the architecture/broad scope. |

#### 6. Repair — Container Sub-cycle

*Containers are logistical. We use a darker variation of Teal or neutral grey borders.*

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `repair_cycle_container_started` | 🌐 + 📦 (`#14B8A6`) | A shipping container. The environment is launched. |
| `repair_cycle_container_checkpoint` | 🔘 + 💾 (`#94A3B8`) | *[Hidden/Muted]* A floppy disk. State saved. |
| `repair_cycle_container_recovered` | 🟢 + 🦾 (`#10B981`) | A robotic arm. Resilient recovery. |
| `repair_cycle_container_killed` | 🔴 + ☠️ (`#EF4444`) | Skull and crossbones. Forcibly terminated. |
| `repair_cycle_container_completed` | 🟢 + 📤 (`#10B981`) | Outbox arrow. Container shut down cleanly, results out. |

#### 7. PR Review

*Treat this as a specific, formal type of Review.*
**Theme Color:** Rose (`#F43F5E`) - A variation of Review pink, signifying the final gate.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `pr_review_stage_...` | 🌹 + 🔱 (`#F43F5E`) | A Git Fork icon inside a shield. The final merge request. |

#### 8. Feedback & Conversational Loop

**Theme Color:** Orange (`#F97316`) - Represents communication and active interaction.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `feedback_...` | 🟠 + 📣 (`#F97316`) | A megaphone. External input detected. |
| `feedback_ignored` | 🔘 + 🔇 (`#94A3B8`) | *[Muted]* A muted speaker. Feedback logged but skipped. |
| `conversational_loop_started` | 🟠 + 👥 (`#F97316`) | Two speech bubbles/people icons. The chat is open. |
| `conversational_loop_paused` | 🟠 + ⏸️ (`#F97316`) | Standard pause symbol. Waiting on user. |

#### 9. Error Handling & Circuit Breakers

**Theme Color:** Status-Dependent (Red/Green/Yellow) - Crucial safety events.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `error_encountered` | 🔴 + ☣️ (`#EF4444`) | Biohazard symbol or complex bug icon. Something went wrong *internally*. |
| `error_recovered` | 🟢 + 🩹 (`#10B981`) | Band-aid over the bug. System patched itself. |
| `circuit_breaker_opened` | 🔴 + 💥 (`#EF4444`) | An electrical spark/explosion. The fuse blew to save the system. |
| `circuit_breaker_closed` | 🟢 + 💡 (`#10B981`) | A lit lightbulb. Power is restored, service back online. |
| `retry_attempted` | 🔘 + 🔁 (`#94A3B8`) | *[Hidden/Muted]* Circular loop arrows. Automatic retry. |

#### 10. Task Queue

**Theme Color:** Slate Grey (`#475569`) - The background plumbing and logistics.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `task_queued` | 🔘 + 📥 (`#475569`) | Inbox tray. Task arrived. |
| `task_dequeued` | 🔘 + 📤 (`#475569`) | Outbox tray. Task is being processed. |
| `task_priority_changed` | 🔘 + ⚖️ (`#475569`) | *[Hidden]* A balance scale. Re-prioritization. |
| `task_cancelled` | 🔴 + 🗑️ (`#EF4444`) | A trash can. Task discarded. |

#### 11. Branch & Issue Management

**Theme Color:** Indigo (`#4F46E5`) - Represents Git/Source control and external platform integration.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `branch_...` | 🔵 + 🌿 (`#4F46E5`) | A seedling or literal branch. A code branch. |
| `branch_conflict_detected` | 🔴 + ⚔️ (`#EF4444`) | Crossed swords. Merge conflict. |
| `branch_stale_detected` | 🟠 + 🧟 (`#F59E0B`) | A zombie icon. The branch is "dead" or too old. |
| `sub_issue_...` | 🔵 + 🏷️ (`#4F46E5`) | A price tag/label. A GitHub Issue/Ticket. |

#### 12. System Operations

**Theme Color:** Neutral Grey (`#71717A`) border with high-contrast icons.

| Event Type | Visual Representation | Icon Metaphor |
| --- | --- | --- |
| `execution_state_reconciled` | 🟢 + 🔄 (`#10B981`) | Synchronized arrows. State is consistent again. |
| `status_validation_failure` | 🔴 + ❌ (`#EF4444`) | A hard X. Logic check failed. |
| `result_persistence_failed` | 🔴 + 📉 (`#EF4444`) | A downward trend graph. Data lost/couldn't save. |
| `fallback_storage_used` | 🟠 + 🪣 (`#F59E0B`) | A bucket. Using the "bucket" storage backup. |
| `output_validation_failed` | 🔴 + 🕵️ (`#EF4444`) | A detective. The inspector rejected the output. |
| `empty_output_detected` | 🟠 + 👻 (`#F59E0B`) | A ghost. The agent returned nothing. |
| `container_result_recovered` | 🟢 + 🎣 (`#10B981`) | A fishing rod. Rescuing data from backup. |