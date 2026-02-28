/**
 * Custom layout algorithm for pipeline runs that:
 * - Centers all nodes vertically on a horizontal timeline
 * - Groups review cycles into expandable/collapsible bounding boxes
 * - Shows each iteration of a cycle as its own "column" within the box
 */

// Debug logging control - set to true to enable verbose console logging
const DEBUG_CYCLE_LAYOUT = false

/**
 * Detects cycle boundaries from decision events
 * Uses the orchestrator's decision events for accurate cycle boundary detection.
 * Handles three types of cycles:
 * 1. Review cycles: review_cycle_started -> review_cycle_completed
 * 2. Repair cycles: repair_cycle_*_cycle_started -> repair_cycle_*_cycle_completed
 * 3. Conversational loops: conversational_loop_started -> conversational_loop_completed
 * 
 * @param {Array} events - All pipeline run events (sorted chronologically)
 * @returns {Array} Array of cycle objects with start/end timestamps and type
 */
export function detectCycleBoundaries(events) {
  if (DEBUG_CYCLE_LAYOUT) {
    console.group('🔍 detectCycleBoundaries - Enhanced with orchestrator decision events')
    console.log('📋 Total events:', events.length)
  }
  
  const cycles = []
  
  // Sort events chronologically
  const sortedEvents = [...events].sort((a, b) => 
    new Date(a.timestamp) - new Date(b.timestamp)
  )
  
  // Filter to decision events only
  const decisionEvents = sortedEvents.filter(e => e.event_category === 'decision')
  
  if (DEBUG_CYCLE_LAYOUT) {
    console.log('🎯 Decision events:', decisionEvents.length)
    const eventTypeCounts = {}
    decisionEvents.forEach(e => {
      eventTypeCounts[e.event_type] = (eventTypeCounts[e.event_type] || 0) + 1
    })
    console.table(eventTypeCounts)
  }
  
  // Track open cycles by type (to handle nested/parallel cycles)
  // Repair cycles can have multiple nested cycles (test_cycle and fix_cycle)
  const openCycles = {
    review_cycle: null,
    repair_test_cycle: null,
    repair_fix_cycle: null,
    conversational_loop: null
  }
  
  decisionEvents.forEach(event => {
    const eventType = event.event_type
    
    // === REVIEW CYCLES ===
    if (eventType === 'review_cycle_started') {
      // Start a new review cycle
      const cycleId = `review_cycle_${cycles.length + 1}`
      openCycles.review_cycle = {
        id: cycleId,
        type: 'review_cycle',
        startTime: event.timestamp,
        startEvent: event,
        endTime: null,
        endEvent: null,
        agentExecutions: [],
        metadata: {
          maker_agent: event.inputs?.maker_agent,
          reviewer_agent: event.inputs?.reviewer_agent,
          max_iterations: event.max_iterations
        }
      }
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✅ Review cycle started: ${cycleId} (${event.inputs?.maker_agent} -> ${event.inputs?.reviewer_agent})`)
      }
    } 
    else if (eventType === 'review_cycle_completed' && openCycles.review_cycle) {
      // Complete the review cycle
      openCycles.review_cycle.endTime = event.timestamp
      openCycles.review_cycle.endEvent = event
      openCycles.review_cycle.metadata.status = event.status
      openCycles.review_cycle.metadata.total_iterations = event.total_iterations
      
      cycles.push(openCycles.review_cycle)
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✅ Review cycle completed: ${openCycles.review_cycle.id} (status: ${event.status}, iterations: ${event.total_iterations})`)
      }
      
      openCycles.review_cycle = null
    }
    
    // === REPAIR CYCLES ===
    // Repair cycles can be: repair_cycle_test_cycle_started/completed, repair_cycle_fix_cycle_started/completed
    // These can be nested (fix_cycle inside test_cycle)
    else if (eventType === 'repair_cycle_test_cycle_started') {
      // Start a test cycle (outer cycle)
      const cycleId = `repair_test_cycle_${cycles.length + 1}`
      
      openCycles.repair_test_cycle = {
        id: cycleId,
        type: 'repair_cycle',
        subtype: 'test_cycle',
        startTime: event.timestamp,
        startEvent: event,
        endTime: null,
        endEvent: null,
        agentExecutions: [],
        metadata: {
          test_type: event.test_type,
          test_type_index: event.test_type_index,
          total_test_types: event.total_test_types,
          max_iterations: event.max_iterations
        }
      }
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✅ Repair test cycle started: ${cycleId} (test_type: ${event.test_type})`)
      }
    }
    else if (eventType === 'repair_cycle_test_cycle_completed' && openCycles.repair_test_cycle) {
      // Complete the test cycle
      openCycles.repair_test_cycle.endTime = event.timestamp
      openCycles.repair_test_cycle.endEvent = event
      
      cycles.push(openCycles.repair_test_cycle)
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✅ Repair test cycle completed: ${openCycles.repair_test_cycle.id}`)
      }
      
      openCycles.repair_test_cycle = null
    }
    else if (eventType === 'repair_cycle_fix_cycle_started') {
      // Start a fix cycle (can be nested inside test cycle)
      const cycleId = `repair_fix_cycle_${cycles.length + 1}`
      
      openCycles.repair_fix_cycle = {
        id: cycleId,
        type: 'repair_cycle',
        subtype: 'fix_cycle',
        startTime: event.timestamp,
        startEvent: event,
        endTime: null,
        endEvent: null,
        agentExecutions: [],
        metadata: {
          test_type: event.test_type,
          test_cycle_iteration: event.test_cycle_iteration,
          file_count: event.file_count,
          total_failures: event.total_failures
        }
      }
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✅ Repair fix cycle started: ${cycleId} (${event.file_count} files, ${event.total_failures} failures)`)
      }
    }
    else if (eventType === 'repair_cycle_fix_cycle_completed' && openCycles.repair_fix_cycle) {
      // Complete the fix cycle
      openCycles.repair_fix_cycle.endTime = event.timestamp
      openCycles.repair_fix_cycle.endEvent = event
      openCycles.repair_fix_cycle.metadata.files_fixed = event.files_fixed || event.total_files_fixed
      
      cycles.push(openCycles.repair_fix_cycle)
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✅ Repair fix cycle completed: ${openCycles.repair_fix_cycle.id} (${event.files_fixed} files fixed)`)
      }
      
      openCycles.repair_fix_cycle = null
    }
    
    // === CONVERSATIONAL LOOPS ===
    else if (eventType === 'conversational_loop_started') {
      // Start conversational loop
      const cycleId = `conv_loop_${cycles.length + 1}`
      openCycles.conversational_loop = {
        id: cycleId,
        type: 'conversational_loop',
        startTime: event.timestamp,
        startEvent: event,
        endTime: null,
        endEvent: null,
        agentExecutions: [],
        metadata: {
          agent: event.agent,
          workspace_type: event.workspace_type
        }
      }
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✅ Conversational loop started: ${cycleId} (agent: ${event.agent})`)
      }
    }
    else if (eventType === 'conversational_loop_completed' && openCycles.conversational_loop) {
      // Complete conversational loop
      openCycles.conversational_loop.endTime = event.timestamp
      openCycles.conversational_loop.endEvent = event
      
      cycles.push(openCycles.conversational_loop)
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✅ Conversational loop completed: ${openCycles.conversational_loop.id}`)
      }
      
      openCycles.conversational_loop = null
    }
  })
  
  // Close any remaining open cycles (still in progress)
  Object.values(openCycles).forEach(openCycle => {
    if (openCycle) {
      openCycle.endTime = sortedEvents[sortedEvents.length - 1]?.timestamp
      openCycle.isOpen = true // Mark as incomplete
      cycles.push(openCycle)
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`⚠️ Closed incomplete cycle: ${openCycle.id} (still running)`)
      }
    }
  })
  
  // Establish parent-child relationships based on time containment
  // A cycle is a child of another if it's completely contained within the parent's time range
  cycles.forEach(potentialChild => {
    const childStart = new Date(potentialChild.startTime).getTime()
    const childEnd = potentialChild.endTime ? new Date(potentialChild.endTime).getTime() : Date.now()
    
    cycles.forEach(potentialParent => {
      // Skip self and don't nest cycles of the same subtype
      if (potentialChild.id === potentialParent.id) return
      if (potentialChild.subtype === potentialParent.subtype) return
      
      const parentStart = new Date(potentialParent.startTime).getTime()
      const parentEnd = potentialParent.endTime ? new Date(potentialParent.endTime).getTime() : Date.now()
      
      // Check if child is contained within parent
      if (childStart >= parentStart && childEnd <= parentEnd) {
        // Repair fix cycles should be children of repair test cycles
        if (potentialChild.subtype === 'fix_cycle' && potentialParent.subtype === 'test_cycle') {
          potentialChild.parentCycleId = potentialParent.id
          
          if (DEBUG_CYCLE_LAYOUT) {
            console.log(`🔗 Parent-child: ${potentialChild.id} is child of ${potentialParent.id}`)
          }
        }
      }
    })
  })
  
  if (DEBUG_CYCLE_LAYOUT) {
    console.log(`✅ Detected ${cycles.length} cycles total:`)
    cycles.forEach(cycle => {
      const duration = cycle.endTime 
        ? ((new Date(cycle.endTime) - new Date(cycle.startTime)) / 1000).toFixed(1) 
        : 'ongoing'
      const parentInfo = cycle.parentCycleId ? ` (child of ${cycle.parentCycleId})` : ''
      const openInfo = cycle.isOpen ? ' [OPEN]' : ''
      console.log(`  ${cycle.id}: ${cycle.type}/${cycle.subtype || 'unknown'} (${duration}s)${parentInfo}${openInfo}`)
    })
    console.groupEnd()
  }
  
  return cycles
}

/**
 * Groups agent executions into detected cycles
 * @param {Array} cycleBoundaries - Cycle boundaries from detectCycleBoundaries
 * @param {Map} agentExecutions - Map of agent -> [execution instances]
 * @returns {Map} Map of cycle ID -> cycle data with executions
 */
export function groupExecutionsIntoCycles(cycleBoundaries, agentExecutions) {
  if (DEBUG_CYCLE_LAYOUT) {
    console.group('🔗 groupExecutionsIntoCycles')
    console.log('📊 Cycle boundaries:', cycleBoundaries.length)
    console.log('👥 Agent executions:', Array.from(agentExecutions.entries()).map(([agent, execs]) => 
      ({ agent, count: execs.length })
    ))
  }
  
  const cycleMap = new Map()
  
  cycleBoundaries.forEach(cycle => {
    const cycleStart = new Date(cycle.startTime)
    const cycleEnd = cycle.endTime ? new Date(cycle.endTime) : new Date()
    
    // Find all agent executions that fall within this cycle's time range
    const executionsInCycle = []
    
    agentExecutions.forEach((executions, agent) => {
      executions.forEach(execution => {
        const execStart = new Date(execution.startTime)
        
        // Check if this execution started within the cycle boundary
        if (execStart >= cycleStart && execStart <= cycleEnd) {
          executionsInCycle.push({
            agent,
            execution,
          })
        }
      })
    })
    
    // Only include cycles that have agent executions
    if (executionsInCycle.length > 0) {
      cycleMap.set(cycle.id, {
        ...cycle,
        agentExecutions: executionsInCycle,
        iterations: executionsInCycle.length,
        isCollapsed: false,
      })
    }
  })
  
  if (DEBUG_CYCLE_LAYOUT) {
    console.log('✅ Cycles with executions:', cycleMap.size)
    cycleMap.forEach((cycle, cycleId) => {
      console.log(`  ${cycleId}: ${cycle.agentExecutions.length} executions`, 
        cycle.agentExecutions.map(e => e.agent))
    })
    console.groupEnd()
  }
  
  return cycleMap
}

/**
 * Legacy function - identifies cycles (agents with multiple executions) from the event data
 * This is the simple version that doesn't consider cycle boundaries
 * @param {Array} events - Pipeline run events
 * @param {Map} agentExecutions - Map of agent -> [execution instances]
 * @returns {Map} Map of agent name -> cycle metadata
 */
export function identifyCyclesSimple(events, agentExecutions) {
  const cycles = new Map()
  
  agentExecutions.forEach((executions, agent) => {
    if (executions.length > 1) {
      cycles.set(agent, {
        agent,
        iterations: executions.length,
        executions: executions,
        isCollapsed: false, // Default to expanded
        type: 'unknown', // Type unknown without boundary detection
      })
    }
  })
  
  return cycles
}

/**
 * Enhanced cycle identification using boundary detection
 * @param {Array} events - Pipeline run events
 * @param {Map} agentExecutions - Map of agent -> [execution instances]
 * @returns {Map} Map of cycle ID -> cycle metadata
 */
/**
 * Identifies cycles from workflow configuration using decision events for boundaries
 * @param {Array} events - All pipeline events
 * @param {Map} agentExecutions - Map of agent name to execution data
 * @param {Object} workflowConfig - Workflow configuration with columns
 * @returns {Map} Map of cycle IDs to cycle data
 */
function identifyCyclesFromWorkflow(events, agentExecutions, workflowConfig) {
  if (DEBUG_CYCLE_LAYOUT) {
    console.log("🔍 identifyCyclesFromWorkflow called");
    console.log("Available agents in execution map:", Array.from(agentExecutions.keys()));
    console.log("Workflow columns:", workflowConfig.columns.map(c => ({ name: c.name, type: c.type, maker: c.maker_agent, agent: c.agent })));
  }
  
  const cycles = new Map();
  
  // Find review cycle decision events to determine boundaries
  const reviewCycleEvents = events.filter(e => 
    e.event_category === 'decision' && 
    e.decision_category === 'review_cycle'
  );
  
  if (DEBUG_CYCLE_LAYOUT) {
    console.log(`Found ${reviewCycleEvents.length} review cycle decision events`);
  }
  
  if (reviewCycleEvents.length === 0) {
    if (DEBUG_CYCLE_LAYOUT) {
      console.log("⚠️ No review cycle decision events found, cannot identify review cycles");
    }
    return cycles;
  }
  
  // Group events by review cycle start/end
  const reviewCycleStarts = reviewCycleEvents.filter(e => e.event_type === 'review_cycle_started');
  const reviewCycleEnds = reviewCycleEvents.filter(e => e.event_type === 'review_cycle_completed');
  
  if (DEBUG_CYCLE_LAYOUT) {
    console.log(`Found ${reviewCycleStarts.length} review cycle starts, ${reviewCycleEnds.length} review cycle ends`);
  }
  
  // Process each review cycle
  reviewCycleStarts.forEach((startEvent, index) => {
    const endEvent = reviewCycleEnds[index]; // Match by index (assumes chronological order)
    
    if (!endEvent) {
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`⚠️ No matching end event for review cycle start at ${startEvent.timestamp}`);
      }
      return;
    }
    
    const makerAgent = startEvent.inputs?.maker_agent;
    const reviewerAgent = startEvent.inputs?.reviewer_agent;
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`\n📋 Processing review cycle ${index + 1}:`);
      console.log(`  Maker: "${makerAgent}", Reviewer: "${reviewerAgent}"`);
    }
    
    // The review cycle actually starts when the maker begins work, not when review_cycle_started fires
    // review_cycle_started fires when the reviewer starts, but we need to include the maker's work
    // Look for maker executions that completed BEFORE the review started
    const reviewStartTime = new Date(startEvent.timestamp).getTime();
    const reviewEndTime = new Date(endEvent.timestamp).getTime();
    
    // Find all maker executions that are NOT part of a repair cycle and happened before this review started
    const makerExecutions = events.filter(e => {
      if (e.event_type !== 'agent_initialized') return false;
      if (e.agent !== makerAgent) return false;
      // Exclude repair cycle work - check both execution_type and task_id for backward compat
      const isRepairExec = (e.execution_type && e.execution_type.startsWith('repair_'))
        || (e.task_id && e.task_id.startsWith('repair_'));
      if (isRepairExec) return false;
      
      const eventTime = new Date(e.timestamp).getTime();
      // Look backwards from review start to find the maker work that led to this review
      // Typically within the last hour before review started
      const oneHourBefore = reviewStartTime - (60 * 60 * 1000);
      return eventTime >= oneHourBefore && eventTime < reviewStartTime;
    });
    
    // Find reviewer executions within the review cycle timeframe
    const reviewerExecutions = events.filter(e => {
      if (e.event_type !== 'agent_initialized') return false;
      if (e.agent !== reviewerAgent) return false;
      
      const eventTime = new Date(e.timestamp).getTime();
      return eventTime >= reviewStartTime && eventTime <= reviewEndTime;
    });
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`  Found ${makerExecutions.length} maker executions (before review)`);
      console.log(`  Found ${reviewerExecutions.length} reviewer executions (during review)`);
    }
    
    // Helper to find execution index in agentExecutions map
    const findExecutionIndex = (agent, taskId) => {
      const executions = agentExecutions.get(agent);
      if (!executions) return -1;
      return executions.findIndex(e => e.taskId === taskId);
    };
    
    // Find the earliest maker execution time (this is when the cycle really starts)
    const earliestMakerTime = makerExecutions.length > 0 
      ? Math.min(...makerExecutions.map(e => new Date(e.timestamp).getTime()))
      : reviewStartTime;
    
    // Combine maker and reviewer executions with their indices
    const cycleExecutions = [
      ...makerExecutions.map(e => ({
        agent: e.agent,
        taskId: e.task_id,
        executionIndex: findExecutionIndex(e.agent, e.task_id),
        timestamp: e.timestamp,
        containerId: e.container_name,
        branch: e.branch_name,
      })),
      ...reviewerExecutions.map(e => ({
        agent: e.agent,
        taskId: e.task_id,
        executionIndex: findExecutionIndex(e.agent, e.task_id),
        timestamp: e.timestamp,
        containerId: e.container_name,
        branch: e.branch_name,
      }))
    ].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    
    // Find all decision events within the cycle time boundaries
    const decisionEvents = events.filter(e => {
      if (e.event_category !== 'decision') return false;
      const eventTime = new Date(e.timestamp).getTime();
      return eventTime >= earliestMakerTime && eventTime <= reviewEndTime;
    });
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`  Found ${decisionEvents.length} decision events in cycle timeframe`);
      console.log(`  Total executions in this review cycle: ${cycleExecutions.length}`);
    }
    
    if (cycleExecutions.length === 0) {
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`  ⚠️ No executions found in this time range`);
      }
      return;
    }
    
    // Create cycle with both agent executions and decision events
    const cycleId = `review_cycle_${index + 1}`;
    cycles.set(cycleId, {
      id: cycleId,
      type: 'review_cycle',
      startTime: new Date(earliestMakerTime).toISOString(), // Use earliest maker time as start
      endTime: endEvent.timestamp,
      agentExecutions: cycleExecutions,
      decisionEvents: decisionEvents, // Store decision events for the cycle
      isCollapsed: false,
      metadata: {
        iteration: index + 1,
        makerAgent,
        reviewerAgent,
        status: endEvent.status,
        totalIterations: endEvent.total_iterations,
      },
    });
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`  ✅ Created cycle "${cycleId}" with ${cycleExecutions.length} executions and ${decisionEvents.length} decisions`);
    }
  });
  
  // Also detect repair cycles - check both execution_type and task_id for backward compat
  const repairEvents = events.filter(e => {
    if (e.event_type !== 'agent_initialized') return false;
    return (e.execution_type && e.execution_type.startsWith('repair_'))
      || (e.task_id && e.task_id.startsWith('repair_'));
  });
  
  if (repairEvents.length > 0) {
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`\n🛠️ Found ${repairEvents.length} repair cycle executions`);
    }
    
    // Helper to find execution index in agentExecutions map
    const findExecutionIndex = (agent, taskId) => {
      const executions = agentExecutions.get(agent);
      if (!executions) return -1;
      return executions.findIndex(e => e.taskId === taskId);
    };
    
    const repairExecutions = repairEvents.map(e => ({
      agent: e.agent,
      taskId: e.task_id,
      executionIndex: findExecutionIndex(e.agent, e.task_id),
      timestamp: e.timestamp,
      containerId: e.container_name,
      branch: e.branch_name,
    })).sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    
    // Find time boundaries for repair cycle
    const repairStartTime = new Date(repairExecutions[0].timestamp).getTime();
    const repairEndTime = new Date(repairExecutions[repairExecutions.length - 1].timestamp).getTime();
    
    // Find all decision events within the repair cycle time boundaries
    const decisionEvents = events.filter(e => {
      if (e.event_category !== 'decision') return false;
      const eventTime = new Date(e.timestamp).getTime();
      return eventTime >= repairStartTime && eventTime <= repairEndTime;
    });
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`  Found ${decisionEvents.length} decision events in repair cycle timeframe`);
    }
    
    cycles.set('repair_cycle_1', {
      id: 'repair_cycle_1',
      type: 'error_handling',
      startTime: repairExecutions[0].timestamp,
      endTime: repairExecutions[repairExecutions.length - 1].timestamp,
      agentExecutions: repairExecutions,
      decisionEvents: decisionEvents, // Store decision events for the cycle
      isCollapsed: false,
      metadata: {
        cycleType: 'test_repair',
      },
    });
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`  ✅ Created repair cycle with ${repairExecutions.length} executions and ${decisionEvents.length} decisions`);
    }
  }
  
  return cycles;
}

/**
 * Main cycle identification function with three-tier detection strategy
 * @param {Array} events - All pipeline run events
 * @param {Map} agentExecutions - Map of agent name -> [execution instances]
 * @param {Object} workflowConfig - Optional workflow configuration
 * @returns {Map} Map of cycle ID -> cycle metadata
 */
export function identifyCycles(events, agentExecutions, workflowConfig = null) {
  if (DEBUG_CYCLE_LAYOUT) {
    console.group('🎯 identifyCycles - Three-tier detection strategy')
  }
  
  // Strategy 1: Use workflow configuration if available (most accurate)
  if (workflowConfig) {
    if (DEBUG_CYCLE_LAYOUT) {
      console.log('✅ Strategy 1: Using workflow configuration (preferred)')
    }
    const cycles = identifyCyclesFromWorkflow(events, agentExecutions, workflowConfig)
    
    if (cycles.size > 0) {
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`✨ Success! Found ${cycles.size} cycles using workflow config`)
        console.groupEnd()
      }
      return cycles
    }
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log('⚠️ No cycles found with workflow config, falling back to decision events')
    }
  } else {
    if (DEBUG_CYCLE_LAYOUT) {
      console.log('⚠️ No workflow config provided, skipping strategy 1')
    }
  }
  
  // Strategy 2: Use decision event boundaries (good accuracy)
  if (DEBUG_CYCLE_LAYOUT) {
    console.log('📋 Strategy 2: Using decision event boundaries')
  }
  const boundaries = detectCycleBoundaries(events)
  
  if (boundaries.length > 0) {
    const cycles = new Map()
    
    boundaries.forEach((boundary, index) => {
      const cycleId = boundary.id // Use the ID from detectCycleBoundaries
      
      // Find all agent executions within this time range
      const startTime = new Date(boundary.startTime).getTime()
      const endTime = boundary.endTime ? new Date(boundary.endTime).getTime() : Date.now()
      
      const cycleExecutions = []
      agentExecutions.forEach((executions, agent) => {
        executions.forEach(execution => {
          const executionTime = new Date(execution.startTime).getTime()
          if (executionTime >= startTime && executionTime <= endTime) {
            cycleExecutions.push({
              agent,
              ...execution
            })
          }
        })
      })
      
      cycles.set(cycleId, {
        ...boundary, // Include all metadata from detectCycleBoundaries
        id: cycleId,
        agentExecutions: cycleExecutions,
        isCollapsed: false,
      })
    })
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`✨ Success! Found ${cycles.size} cycles using decision events`)
      console.groupEnd()
    }
    return cycles
  }
  
  if (DEBUG_CYCLE_LAYOUT) {
    console.log('⚠️ No cycles found with decision events, falling back to simple detection')
  }
  
  // Strategy 3: Simple agent repeat counting (basic)
  if (DEBUG_CYCLE_LAYOUT) {
    console.log('🔢 Strategy 3: Simple agent repeat counting (fallback)')
  }
  const simpleCycles = identifyCyclesSimple(events, agentExecutions)
  if (DEBUG_CYCLE_LAYOUT) {
    console.log(`✨ Found ${simpleCycles.size} cycles using simple detection`)
    console.groupEnd()
  }
  
  return simpleCycles
}

/**
 * Custom layout algorithm that positions nodes with cycles as bounding boxes.
 *
 * Supports two layouts:
 *
 * 1. NEW (3-level): nodes produced by the new buildFlowchart.js that use
 *    reviewCycleContainer / repairCycleContainer / iterationContainer node types
 *    with parentId hierarchy already set.
 *    Layout proceeds bottom-up (size containers) then top-down (place everything).
 *
 * 2. LEGACY (2-level): nodes produced by the old buildFlowchart using cycleBounding
 *    and a flat cycles Map. Falls back to the previous algorithm.
 *
 * @param {Array} nodes   - React Flow nodes to layout (may have parentId set)
 * @param {Array} edges   - React Flow edges
 * @param {Map}   cycles  - Map of cycle ID → cycle data (used for legacy path + collapse state)
 * @param {Object} options
 * @returns {{ nodes, cycleNodes, edges }}
 */
export function applyCycleLayout(nodes, edges, cycles, options = {}) {
  const {
    nodeWidth = 250,
    nodeHeight = 80,
    horizontalSpacing = 150,
    verticalSpacing = 120,
    cycleGap = 40,
    cyclePadding = 40,
    // Iteration / grandchild layout constants
    iterHeaderHeight = 24, // height of the iteration pill label
    iterPadding = 20,      // padding inside iteration container (top & bottom)
    innerVertSpacing = 20, // vertical gap between grandchildren
    containerHeaderHeight = 36, // height of cycle container header bar
    viewportWidth = 1200,
    centerX = null,
  } = options

  const centerXPosition = centerX !== null ? centerX : viewportWidth / 2

  // ── Detect which layout path to use ──────────────────────────────────────
  const hasNewContainers = nodes.some(
    n => n.type === 'reviewCycleContainer' || n.type === 'repairCycleContainer'
  )

  if (!hasNewContainers) {
    // ── LEGACY PATH ────────────────────────────────────────────────────────
    return _legacyApplyCycleLayout(nodes, edges, cycles, {
      nodeWidth, nodeHeight, horizontalSpacing, verticalSpacing,
      cycleGap, cyclePadding, centerXPosition,
    })
  }

  // ── NEW 3-LEVEL LAYOUT PATH ───────────────────────────────────────────────

  // Categorise nodes by type and parent relationship
  const cycleContainers = nodes.filter(
    n => (n.type === 'reviewCycleContainer' || n.type === 'repairCycleContainer') && !n.parentId
  )
  const iterContainers = nodes.filter(n => n.type === 'iterationContainer')
  const grandchildren = nodes.filter(n => n.parentId && n.type === 'pipelineEvent' &&
    iterContainers.some(ic => ic.id === n.parentId))
  // Direct pipelineEvent children of cycle containers (review_cycle_started / completed)
  const directCycleChildren = nodes.filter(n => n.parentId && n.type === 'pipelineEvent' &&
    cycleContainers.some(cc => cc.id === n.parentId))

  // Build lookup maps
  const itersByParent = new Map()   // cycleId → [iterationContainer]
  iterContainers.forEach(iter => {
    const pid = iter.parentId
    if (!itersByParent.has(pid)) itersByParent.set(pid, [])
    itersByParent.get(pid).push(iter)
  })

  const childrenByIter = new Map()  // iterId → [grandchild pipelineEvent]
  grandchildren.forEach(child => {
    const pid = child.parentId
    if (!childrenByIter.has(pid)) childrenByIter.set(pid, [])
    childrenByIter.get(pid).push(child)
  })

  const directChildrenByCycle = new Map()  // cycleId → [pipelineEvent]
  directCycleChildren.forEach(child => {
    const pid = child.parentId
    if (!directChildrenByCycle.has(pid)) directChildrenByCycle.set(pid, [])
    directChildrenByCycle.get(pid).push(child)
  })

  // ── Pass 1: Size iteration / test-cycle containers (bottom-up) ───────────
  // Uses node.measured dimensions when available (two-phase layout), falls back to params.
  const iterSizes = new Map()
  iterContainers.forEach(iter => {
    const children = childrenByIter.get(iter.id) || []
    const n = children.length
    const totalChildHeight = children.reduce(
      (sum, c) => sum + (c.measured?.height ?? nodeHeight), 0
    )
    const maxChildWidth = n > 0
      ? Math.max(...children.map(c => c.measured?.width ?? nodeWidth))
      : nodeWidth
    const height = iterHeaderHeight + iterPadding * 2 +
      totalChildHeight +
      Math.max(0, n - 1) * innerVertSpacing
    const width = maxChildWidth + iterPadding * 2
    iterSizes.set(iter.id, { width, height })
  })

  // ── Pass 2: Size cycle containers ────────────────────────────────────────
  const cycleSizes = new Map()
  cycleContainers.forEach(cc => {
    const iters = itersByParent.get(cc.id) || []
    const direct = directChildrenByCycle.get(cc.id) || []
    const maxIterHeight = iters.reduce(
      (max, it) => Math.max(max, iterSizes.get(it.id)?.height ?? 0), nodeHeight
    )

    if (cc.type === 'reviewCycleContainer') {
      // Layout: [start] [iter1] [iter2] … [end]  — all horizontal
      const numDirect = direct.length   // typically 2 (start + end events)
      const numIters = iters.length
      const iterTotalWidth = iters.reduce(
        (sum, it) => sum + (iterSizes.get(it.id)?.width ?? 0), 0
      )
      // Use measured widths for direct children (start/end events) when available
      const startWidth = direct[0]?.measured?.width ?? nodeWidth
      const endWidth = direct[1]?.measured?.width ?? nodeWidth
      const leftWidth = numDirect > 0 ? startWidth + horizontalSpacing : 0
      const rightWidth = numDirect > 1 ? horizontalSpacing + endWidth : 0
      const iterSpacingTotal = numIters > 1 ? horizontalSpacing * (numIters - 1) : 0
      const width = cyclePadding * 2 + leftWidth + iterTotalWidth + iterSpacingTotal + rightWidth
      const height = containerHeaderHeight + cyclePadding * 2 + maxIterHeight
      cycleSizes.set(cc.id, { width: Math.max(width, 500), height: Math.max(height, 180) })
    } else if (cc.type === 'repairCycleContainer') {
      // Layout: [tc1] [tc2] [tc3]  — horizontal, no direct event children
      const numIters = iters.length
      const iterTotalWidth = iters.reduce(
        (sum, it) => sum + (iterSizes.get(it.id)?.width ?? 0), 0
      )
      const iterSpacingTotal = numIters > 1 ? horizontalSpacing * (numIters - 1) : 0
      const width = cyclePadding * 2 + iterTotalWidth + iterSpacingTotal
      const height = containerHeaderHeight + cyclePadding * 2 + maxIterHeight
      cycleSizes.set(cc.id, { width: Math.max(width, 400), height: Math.max(height, 180) })
    }
  })

  // ── Pass 3: Position root-level items vertically ─────────────────────────
  // Root items = all nodes without parentId, in the order they appear in the array
  // (buildFlowchart.js inserts them in chronological order)
  // Uses node.measured dimensions when available (two-phase layout).
  const positionedNodes = new Map()  // id → fully positioned node
  const nodeGap = Math.max(16, verticalSpacing - nodeHeight)  // gap between consecutive nodes

  // Process all root-level nodes (no parentId) in array order, which is chronological
  const rootLayoutNodes = nodes.filter(n => !n.parentId)
  let currentY = 100
  rootLayoutNodes.forEach(node => {
    if (node.type === 'reviewCycleContainer' || node.type === 'repairCycleContainer') {
      const size = cycleSizes.get(node.id) || { width: 500, height: 200 }
      positionedNodes.set(node.id, {
        ...node,
        position: { x: centerXPosition - size.width / 2, y: currentY },
        style: { ...node.style, width: size.width, height: size.height },
      })
      currentY += size.height + cycleGap + verticalSpacing
    } else {
      const w = node.measured?.width ?? nodeWidth
      const h = node.measured?.height ?? nodeHeight
      positionedNodes.set(node.id, {
        ...node,
        position: { x: centerXPosition - w / 2, y: currentY },
      })
      currentY += h + nodeGap
    }
  })

  // ── Pass 4: Position cycle container children ─────────────────────────────
  cycleContainers.forEach(cc => {
    const iters = (itersByParent.get(cc.id) || []).sort(
      (a, b) => (a.data?.iterationNumber ?? 0) - (b.data?.iterationNumber ?? 0)
    )
    const direct = (directChildrenByCycle.get(cc.id) || []).sort(
      (a, b) => new Date(a.data?.timestamp ?? 0) - new Date(b.data?.timestamp ?? 0)
    )

    const contentY = containerHeaderHeight + cyclePadding  // y offset inside container

    if (cc.type === 'reviewCycleContainer') {
      let relX = cyclePadding

      // Start event (leftmost direct child)
      if (direct[0]) {
        positionedNodes.set(direct[0].id, {
          ...direct[0],
          position: { x: relX, y: contentY },
        })
        relX += (direct[0].measured?.width ?? nodeWidth) + horizontalSpacing
      }

      // Iteration containers
      iters.forEach(iter => {
        const iterSize = iterSizes.get(iter.id) || { width: nodeWidth + iterPadding * 2, height: 200 }
        positionedNodes.set(iter.id, {
          ...iter,
          position: { x: relX, y: contentY },
          style: { ...iter.style, width: iterSize.width, height: iterSize.height },
        })
        relX += iterSize.width + horizontalSpacing
      })

      // End event (rightmost direct child)
      if (direct[1]) {
        positionedNodes.set(direct[1].id, {
          ...direct[1],
          position: { x: relX, y: contentY },
        })
      }
    } else if (cc.type === 'repairCycleContainer') {
      let relX = cyclePadding

      iters.forEach(iter => {
        const iterSize = iterSizes.get(iter.id) || { width: nodeWidth + iterPadding * 2, height: 200 }
        positionedNodes.set(iter.id, {
          ...iter,
          position: { x: relX, y: contentY },
          style: { ...iter.style, width: iterSize.width, height: iterSize.height },
        })
        relX += iterSize.width + horizontalSpacing
      })
    }
  })

  // ── Pass 5: Position grandchildren within iteration containers ────────────
  // Uses cumulative measured heights for accurate vertical stacking.
  // Children are horizontally centered within the iteration container.
  iterContainers.forEach(iter => {
    const children = childrenByIter.get(iter.id) || []
    const iterSize = iterSizes.get(iter.id) || { width: nodeWidth + iterPadding * 2 }
    let childY = iterHeaderHeight + iterPadding
    // Preserve insertion order (chronological — buildFlowchart.js inserts them that way)
    children.forEach(child => {
      const childWidth = child.measured?.width ?? nodeWidth
      const centeredX = (iterSize.width - childWidth) / 2
      positionedNodes.set(child.id, {
        ...child,
        position: { x: centeredX, y: childY },
      })
      childY += (child.measured?.height ?? nodeHeight) + innerVertSpacing
    })
  })

  // ── Collect and order final nodes (parents before children) ──────────────
  const parentNodes = nodes.filter(n => !n.parentId).map(n => positionedNodes.get(n.id) ?? n)
  const childNodes = nodes.filter(n => n.parentId).map(n => positionedNodes.get(n.id) ?? n)

  const cycleNodes = nodes.filter(
    n => n.type === 'reviewCycleContainer' || n.type === 'repairCycleContainer'
  ).map(n => positionedNodes.get(n.id) ?? n)

  return {
    nodes: [...parentNodes, ...childNodes],
    cycleNodes,
    edges,
  }
}

/**
 * Legacy layout path for the old flat cycles Map approach (cycleBounding node type).
 * Kept intact for backward compatibility with any existing data flows.
 */
function _legacyApplyCycleLayout(nodes, edges, cycles, opts) {
  const {
    nodeWidth, nodeHeight, horizontalSpacing, verticalSpacing,
    cycleGap, cyclePadding, centerXPosition,
  } = opts

  if (DEBUG_CYCLE_LAYOUT) {
    console.group('🎨 applyCycleLayout (legacy) - Debug Info')
    console.log('📊 Input nodes:', nodes.length)
    console.log('🔄 Detected cycles:', cycles.size)
  }

  const nodesByCycle = new Map()
  const standaloneNodes = []

  nodes.forEach(node => {
    if (node.id === 'created' || node.id === 'completed') {
      standaloneNodes.push(node)
      return
    }
    let belongsToCycle = false
    for (const [cycleId, cycleData] of cycles.entries()) {
      let inCycle = false
      if (node.id.startsWith('agent-')) {
        inCycle = cycleData.agentExecutions?.some(execution =>
          execution.executionIndex >= 0 &&
          node.id === `agent-${execution.agent}-${execution.executionIndex}`
        )
      } else if (node.id.startsWith('decision-') && node.data?.timestamp) {
        const nodeTime = new Date(node.data.timestamp).getTime()
        const cycleStart = new Date(cycleData.startTime).getTime()
        const cycleEnd = new Date(cycleData.endTime).getTime()
        inCycle = nodeTime >= cycleStart && nodeTime <= cycleEnd
      }
      if (inCycle) {
        if (!nodesByCycle.has(cycleId)) nodesByCycle.set(cycleId, [])
        nodesByCycle.get(cycleId).push(node)
        belongsToCycle = true
        break
      }
    }
    if (!belongsToCycle) standaloneNodes.push(node)
  })

  const layoutItems = []
  let currentY = 100

  const createdNode = nodes.find(n => n.id === 'created')
  if (createdNode) {
    layoutItems.push({ type: 'standalone', node: createdNode, y: currentY })
    currentY += verticalSpacing
  }

  const sortedStandalone = [...standaloneNodes]
    .filter(n => n.id !== 'created' && n.id !== 'completed')
    .sort((a, b) => {
      const getSeq = id => { const m = id.match(/-(\d+)$/); return m ? parseInt(m[1]) : 0 }
      return getSeq(a.id) - getSeq(b.id)
    })

  sortedStandalone.forEach(node => {
    layoutItems.push({ type: 'standalone', node, y: currentY })
    currentY += verticalSpacing
  })

  const cycleNodes = []
  for (const [cycleId, cycleData] of cycles.entries()) {
    if (cycleData.parentCycleId) continue
    const cycleNodeList = nodesByCycle.get(cycleId) || []
    const childCycles = Array.from(cycles.values()).filter(c => c.parentCycleId === cycleId)
    childCycles.forEach(cc => cycleNodeList.push(...(nodesByCycle.get(cc.id) || [])))
    if (cycleNodeList.length === 0) continue

    cycleNodeList.sort((a, b) => {
      const getTs = node => {
        if (node.data?.timestamp) return new Date(node.data.timestamp).getTime()
        if (node.id.startsWith('agent-')) {
          const match = node.id.match(/^agent-(.+)-(\d+)$/)
          if (match) {
            const exec = cycleData.agentExecutions?.find(
              e => e.agent === match[1] && e.executionIndex === parseInt(match[2])
            )
            if (exec) return new Date(exec.timestamp).getTime()
          }
        }
        return 0
      }
      return getTs(a) - getTs(b)
    })

    const cycleWidth = (cycleNodeList.length * (nodeWidth + horizontalSpacing)) - horizontalSpacing + cyclePadding * 2
    const cycleHeight = nodeHeight + cyclePadding * 2
    const cycleX = centerXPosition - cycleWidth / 2

    const cycleChildNodes = cycleNodeList.map((node, index) => ({
      node, cycleId,
      relativeX: cyclePadding + index * (nodeWidth + horizontalSpacing),
      relativeY: cyclePadding,
    }))

    layoutItems.push({
      type: 'cycle', cycleId, cycleData,
      cycleX, cycleY: currentY, cycleWidth, cycleHeight,
      childNodes: cycleChildNodes,
    })

    const typeLabels = {
      review_cycle: 'Review Cycle', repair_cycle: 'Repair Cycle',
      error_handling: 'Repair Cycle', conversational_loop: 'Conversational Loop', unknown: 'Cycle',
    }
    let cycleLabel = typeLabels[cycleData.type] || 'Cycle'
    if (cycleData.type === 'repair_cycle' && cycleData.subtype) {
      cycleLabel = cycleData.subtype === 'test_cycle' ? 'Test Repair Cycle' : 'Fix Repair Cycle'
    }
    if (cycleData.isOpen) cycleLabel += ' (In Progress)'
    if (childCycles.length > 0) cycleLabel += ` [${childCycles.length} nested cycle${childCycles.length > 1 ? 's' : ''}]`

    cycleNodes.push({
      id: cycleId,
      type: 'cycleBounding',
      position: { x: cycleX, y: currentY },
      data: {
        cycleId, cycleType: cycleData.type, label: cycleLabel,
        iterationCount: cycleNodeList.length,
        isCollapsed: cycleData.isCollapsed || false,
        width: cycleWidth, height: cycleHeight,
        cyclePadding,
        startTime: cycleData.startTime, endTime: cycleData.endTime,
      },
      style: { width: cycleWidth, height: cycleHeight, zIndex: -1 },
      expandParent: !cycleData.isCollapsed,
      draggable: false,
    })

    currentY += cycleHeight + cycleGap + verticalSpacing
  }

  const completedNode = nodes.find(n => n.id === 'completed')
  if (completedNode) {
    layoutItems.push({ type: 'standalone', node: completedNode, y: currentY })
  }

  const layoutedNodes = []
  layoutItems.forEach(item => {
    if (item.type === 'standalone') {
      layoutedNodes.push({
        ...item.node,
        position: { x: centerXPosition - nodeWidth / 2, y: item.y },
      })
    } else if (item.type === 'cycle') {
      item.childNodes.forEach(({ node, cycleId, relativeX, relativeY }) => {
        layoutedNodes.push({
          ...node,
          position: { x: relativeX, y: relativeY },
          parentId: cycleId,
          style: { ...node.style, zIndex: 10 },
        })
      })
    }
  })

  if (DEBUG_CYCLE_LAYOUT) console.groupEnd()

  const parentNodes = [...cycleNodes, ...layoutedNodes.filter(n => !n.parentId)]
  const childNodes = layoutedNodes.filter(n => n.parentId)
  return { nodes: [...parentNodes, ...childNodes], cycleNodes, edges }
}

/**
 * Toggles the collapsed state of a cycle
 * @param {Map} cycles - Map of cycles
 * @param {String} cycleId - Cycle ID to toggle
 * @returns {Map} Updated cycles map
 */
export function toggleCycleCollapsed(cycles, cycleId) {
  const updatedCycles = new Map(cycles)
  const cycleData = updatedCycles.get(cycleId)
  
  if (cycleData) {
    updatedCycles.set(cycleId, {
      ...cycleData,
      isCollapsed: !cycleData.isCollapsed,
    })
  }
  
  return updatedCycles
}

/**
 * Filters edges to hide edges connected to collapsed cycle nodes
 * @param {Array} edges - All edges
 * @param {Map} cycles - Map of cycles
 * @returns {Array} Filtered edges
 */
export function filterEdgesForCollapsedCycles(edges, cycles) {
  return edges.filter(edge => {
    // Check if source or target is inside a collapsed cycle
    for (const [agent, cycleData] of cycles.entries()) {
      if (cycleData.isCollapsed) {
        const cyclePrefix = `agent-${agent}-`
        if (edge.source.startsWith(cyclePrefix) || edge.target.startsWith(cyclePrefix)) {
          return false // Hide edge
        }
      }
    }
    return true // Show edge
  })
}

/**
 * Updates edges to connect to cycle bounding nodes when collapsed
 * @param {Array} edges - All edges
 * @param {Map} cycles - Map of cycles
 * @param {Map} nodesByCycle - Map of agent -> [nodes]
 * @returns {Array} Updated edges
 */
export function updateEdgesForCycles(edges, cycles, nodesByCycle) {
  const updatedEdges = []
  
  edges.forEach(edge => {
    let newEdge = { ...edge }
    
    // Check if source is in a collapsed cycle
    for (const [agent, cycleData] of cycles.entries()) {
      if (cycleData.isCollapsed) {
        const cyclePrefix = `agent-${agent}-`
        
        if (edge.source.startsWith(cyclePrefix)) {
          // Redirect to cycle bounding node
          newEdge = {
            ...newEdge,
            source: `cycle-${agent}`,
          }
        }
        
        if (edge.target.startsWith(cyclePrefix)) {
          // Redirect to cycle bounding node
          newEdge = {
            ...newEdge,
            target: `cycle-${agent}`,
          }
        }
      }
    }
    
    updatedEdges.push(newEdge)
  })
  
  // Remove duplicate edges (multiple iterations connecting to same external nodes)
  const edgeKeys = new Set()
  return updatedEdges.filter(edge => {
    const key = `${edge.source}->${edge.target}`
    if (edgeKeys.has(key)) {
      return false
    }
    edgeKeys.add(key)
    return true
  })
}
