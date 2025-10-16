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
      if (e.task_id && e.task_id.startsWith('repair_')) return false; // Exclude repair cycle work
      
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
  
  // Also detect repair cycles (they have distinct task IDs starting with "repair_")
  const repairEvents = events.filter(e => 
    e.event_type === 'agent_initialized' && 
    e.task_id && 
    e.task_id.startsWith('repair_')
  );
  
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
 * Custom layout algorithm that positions nodes with cycles as bounding boxes
 * Root-level nodes are stacked vertically and center-aligned
 * Cycle nodes are arranged horizontally (each iteration in a column)
 * @param {Array} nodes - React Flow nodes to layout
 * @param {Array} edges - React Flow edges
 * @param {Map} cycles - Map of cycle ID -> cycle data from identifyCycles
 * @param {Object} options - Layout options
 * @returns {Object} { nodes, cycleNodes, edges }
 */
export function applyCycleLayout(nodes, edges, cycles, options = {}) {
  const {
    nodeWidth = 250,
    nodeHeight = 80,
    horizontalSpacing = 150, // Spacing between iterations within a cycle
    verticalSpacing = 120, // Spacing between root-level nodes
    cycleGap = 40, // Gap between cycle boxes and other elements
    cyclePadding = 40, // Padding inside cycle boxes
    viewportWidth = 1200,
    centerX = null, // Auto-calculate if not provided
  } = options
  
  // Debug logging
  if (DEBUG_CYCLE_LAYOUT) {
    console.group('🎨 applyCycleLayout - Debug Info')
    console.log('📊 Input nodes:', nodes.length)
    console.log('🔄 Detected cycles:', cycles.size)
    
    // Log cycle details
    if (cycles.size > 0) {
      console.group('🔄 Cycle Details:')
      cycles.forEach((cycleData, cycleId) => {
        console.log(`  ${cycleId}:`, {
          type: cycleData.type,
          agentExecutions: cycleData.agentExecutions?.length || 0,
          isCollapsed: cycleData.isCollapsed,
          startTime: cycleData.startTime,
          endTime: cycleData.endTime,
          agents: cycleData.agentExecutions?.map(e => e.agent) || []
        })
      })
      console.groupEnd()
    }
  }
  
  // Calculate or use provided center X for vertical layout
  const centerXPosition = centerX !== null ? centerX : viewportWidth / 2
  
  // Group nodes by cycle membership (using agent executions and decision events in each cycle)
  const nodesByCycle = new Map()
  const standaloneNodes = []
  
  nodes.forEach(node => {
    // Skip special nodes (created, completed)
    if (node.id === 'created' || node.id === 'completed') {
      standaloneNodes.push(node)
      return
    }
    
    // Check if this node is part of a cycle
    let belongsToCycle = false
    
    for (const [cycleId, cycleData] of cycles.entries()) {
      let inCycle = false
      
      // Check if this is an agent execution node
      if (node.id.startsWith('agent-')) {
        // Check if this node belongs to any agent execution in this cycle
        inCycle = cycleData.agentExecutions?.some((execution) => {
          // Execution has fields: agent, taskId, executionIndex, timestamp, containerId, branch
          // Node IDs use execution index not taskId: agent-{agent}-{executionIndex}
          // Match exactly by executionIndex
          return execution.executionIndex >= 0 && 
                 node.id === `agent-${execution.agent}-${execution.executionIndex}`
        })
      }
      
      // Check if this is a decision node
      else if (node.id.startsWith('decision-') && node.data?.timestamp) {
        // Check if this decision event falls within the cycle's time boundaries
        const nodeTime = new Date(node.data.timestamp).getTime()
        const cycleStart = new Date(cycleData.startTime).getTime()
        const cycleEnd = new Date(cycleData.endTime).getTime()
        inCycle = nodeTime >= cycleStart && nodeTime <= cycleEnd
      }
      
      if (inCycle) {
        if (!nodesByCycle.has(cycleId)) {
          nodesByCycle.set(cycleId, [])
        }
        nodesByCycle.get(cycleId).push(node)
        belongsToCycle = true
        break
      }
    }
    
    if (!belongsToCycle) {
      standaloneNodes.push(node)
    }
  })
  
  // Debug: Log node grouping
  if (DEBUG_CYCLE_LAYOUT) {
    console.group('📦 Node Grouping:')
    console.log('  Standalone nodes:', standaloneNodes.map(n => n.id))
    console.log('  Nodes by cycle:')
    nodesByCycle.forEach((nodes, cycleId) => {
      console.log(`    ${cycleId}:`, nodes.map(n => n.id))
    })
    console.groupEnd()
  }
  
  // Build vertical layout for root-level elements
  const layoutItems = [] // Array of { type, data, y }
  let currentY = 100 // Starting Y position
  
  // Sort standalone nodes by their original position/order
  const sortedStandalone = [...standaloneNodes].sort((a, b) => {
    // Extract any sequence number from ID
    const getSequence = (id) => {
      const match = id.match(/-(\d+)$/)
      return match ? parseInt(match[1]) : 0
    }
    return getSequence(a.id) - getSequence(b.id)
  })
  
  // Add 'created' node
  const createdNode = nodes.find(n => n.id === 'created')
  if (createdNode) {
    layoutItems.push({
      type: 'standalone',
      node: createdNode,
      y: currentY,
    })
    currentY += verticalSpacing
  }
  
  // Add other standalone nodes
  sortedStandalone
    .filter(n => n.id !== 'created' && n.id !== 'completed')
    .forEach(node => {
      layoutItems.push({
        type: 'standalone',
        node,
        y: currentY,
      })
      currentY += verticalSpacing
    })
  
  // Add cycles with their internal horizontal layout
  // Only process parent cycles (top-level) - child cycles will be nested inside
  const cycleNodes = []
  for (const [cycleId, cycleData] of cycles.entries()) {
    // Skip child cycles - they'll be rendered inside their parent
    if (cycleData.parentCycleId) {
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`⏭️  Skipping child cycle ${cycleId} (parent: ${cycleData.parentCycleId})`)
      }
      continue
    }
    
    const cycleNodeList = nodesByCycle.get(cycleId) || []
    
    // Also include nodes from child cycles
    const childCycles = Array.from(cycles.values()).filter(c => c.parentCycleId === cycleId)
    childCycles.forEach(childCycle => {
      const childNodes = nodesByCycle.get(childCycle.id) || []
      cycleNodeList.push(...childNodes)
    })
    
    if (cycleNodeList.length === 0) continue
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`\n🔄 Processing cycle ${cycleId}`)
      console.log(`  Nodes in cycle:`, cycleNodeList.map(n => n.id))
    }
    
    // Sort cycle nodes by timestamp (chronologically)
    cycleNodeList.sort((a, b) => {
      // Get timestamp from node data
      const getTimestamp = (node) => {
        // Decision nodes have timestamp in data
        if (node.data?.timestamp) {
          return new Date(node.data.timestamp).getTime()
        }
        // Agent nodes: try to find matching execution in cycleData
        if (node.id.startsWith('agent-')) {
          const match = node.id.match(/^agent-(.+)-(\d+)$/)
          if (match) {
            const [, agent, executionIndex] = match
            const execution = cycleData.agentExecutions?.find(
              e => e.agent === agent && e.executionIndex === parseInt(executionIndex)
            )
            if (execution) {
              return new Date(execution.timestamp).getTime()
            }
          }
        }
        return 0
      }
      return getTimestamp(a) - getTimestamp(b)
    })
    
    // Calculate cycle dimensions
    const cycleWidth = (cycleNodeList.length * (nodeWidth + horizontalSpacing)) - horizontalSpacing + (cyclePadding * 2)
    const cycleHeight = nodeHeight + (cyclePadding * 2)
    
    // Position cycle centered horizontally
    const cycleX = centerXPosition - cycleWidth / 2
    const cycleY = currentY
    
    // Layout nodes within the cycle horizontally (left to right)
    // IMPORTANT: These positions are relative to the parent cycle's (0,0) corner
    const cycleChildNodes = cycleNodeList.map((node, index) => {
      const relativeX = cyclePadding + (index * (nodeWidth + horizontalSpacing))
      const relativeY = cyclePadding
      
      if (DEBUG_CYCLE_LAYOUT) {
        console.log(`  📍 Positioning child ${node.id} at relative (${relativeX}, ${relativeY})`)
      }
      
      return {
        node,
        cycleId,
        relativeX,
        relativeY,
      }
    })
    
    if (DEBUG_CYCLE_LAYOUT) {
      console.log(`📦 Cycle ${cycleId}: parent at (${cycleX}, ${cycleY}), size ${cycleWidth}x${cycleHeight}`)
      console.log(`   Contains ${cycleChildNodes.length} children`)
    }
    
    layoutItems.push({
      type: 'cycle',
      cycleId,
      cycleData,
      cycleX,
      cycleY,
      cycleWidth,
      cycleHeight,
      childNodes: cycleChildNodes,
    })
    
    // Get cycle type label
    const typeLabels = {
      'review_cycle': 'Review Cycle',
      'repair_cycle': 'Repair Cycle',
      'error_handling': 'Repair Cycle', // Legacy support
      'conversational_loop': 'Conversational Loop',
      'unknown': 'Cycle',
    }
    let cycleLabel = typeLabels[cycleData.type] || 'Cycle'
    
    // Add subtype for repair cycles
    if (cycleData.type === 'repair_cycle' && cycleData.subtype) {
      cycleLabel = cycleData.subtype === 'test_cycle' ? 'Test Repair Cycle' : 'Fix Repair Cycle'
    }
    
    // Show if cycle is still open
    if (cycleData.isOpen) {
      cycleLabel += ' (In Progress)'
    }
    
    // Count child cycles
    const childCount = childCycles.length
    if (childCount > 0) {
      cycleLabel += ` [${childCount} nested cycle${childCount > 1 ? 's' : ''}]`
    }
    
    // Create cycle bounding node (parent node)
    const cycleNode = {
      id: cycleId,
      type: 'cycleBounding',
      position: {
        x: cycleX,
        y: cycleY,
      },
      data: {
        cycleId,
        cycleType: cycleData.type,
        label: cycleLabel,
        iterationCount: cycleNodeList.length,
        isCollapsed: cycleData.isCollapsed || false,
        width: cycleWidth,
        height: cycleHeight,
        cyclePadding,
        startTime: cycleData.startTime,
        endTime: cycleData.endTime,
      },
      style: {
        width: cycleWidth,
        height: cycleHeight,
        zIndex: -1, // Behind other nodes
      },
      // Enable parent-child functionality
      expandParent: !cycleData.isCollapsed, // Allow children outside bounds when expanded
      draggable: false,
    }
    
    cycleNodes.push(cycleNode)
    
    // Move Y position down for next element
    currentY += cycleHeight + cycleGap + verticalSpacing
  }
  
  // Add 'completed' node
  const completedNode = nodes.find(n => n.id === 'completed')
  if (completedNode) {
    layoutItems.push({
      type: 'standalone',
      node: completedNode,
      y: currentY,
    })
  }
  
  // Apply positions to all nodes
  const layoutedNodes = []
  
  layoutItems.forEach(item => {
    if (item.type === 'standalone') {
      // Standalone nodes: center horizontally
      layoutedNodes.push({
        ...item.node,
        position: {
          x: centerXPosition - nodeWidth / 2,
          y: item.y,
        },
      })
    } else if (item.type === 'cycle') {
      // Cycle child nodes: position relative to parent (0,0)
      // React Flow automatically offsets these by the parent's position
      item.childNodes.forEach(({ node, cycleId, relativeX, relativeY }) => {
        layoutedNodes.push({
          ...node,
          position: {
            x: relativeX, // Relative to parent's (0,0)
            y: relativeY, // Relative to parent's (0,0)
          },
          // Set parent relationship - use "parentId" (React Flow v11+) or "parentNode" (React Flow v10)
          parentId: cycleId,  // FIXED: was "parent", should be "parentId"
          // Don't constrain to parent bounds - let nodes be positioned freely within parent
          // extent: 'parent', // REMOVED: This was constraining incorrectly
          // Child nodes have higher zIndex so they appear above parent
          style: {
            ...node.style,
            zIndex: 10,
          },
        })
      })
    }
  })
  
  // When using parent-child relationships, React Flow automatically handles
  // hiding children when parent is collapsed, so we don't need to filter
  
  // CRITICAL: Define these BEFORE the debug block so they're always available
  const parentNodes = [...cycleNodes, ...layoutedNodes.filter(n => !n.parentId)]
  const childNodes = layoutedNodes.filter(n => n.parentId)
  
  // Debug: Log final node structure
  if (DEBUG_CYCLE_LAYOUT) {
    console.group('🏗️ Final Node Structure:')
    console.log('  Parent nodes:', parentNodes.map(n => ({ id: n.id, type: n.type, pos: n.position })))
    console.log('  Child nodes:', childNodes.map(n => ({ 
      id: n.id, 
      parentId: n.parentId, 
      relativePos: n.position,
      parentPos: parentNodes.find(p => p.id === n.parentId)?.position 
    })))
    console.groupEnd()
    console.groupEnd() // End applyCycleLayout group
  }
  
  // CRITICAL: Parent nodes must come BEFORE child nodes in React Flow
  // Standalone nodes and cycle parent nodes first, then cycle children
  return {
    nodes: [...parentNodes, ...childNodes],
    cycleNodes,
    edges,
  }
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
