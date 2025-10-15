/**
 * Example usage of the custom cycle layout
 * This demonstrates how to use the layout algorithm independently
 */

import {
  identifyCycles,
  applyCycleLayout,
  toggleCycleCollapsed,
  updateEdgesForCycles,
} from '../utils/cycleLayout'

// Example: Simple pipeline with one review cycle
export const simpleExample = () => {
  const nodes = [
    { id: 'created', type: 'pipelineEvent', data: { label: 'Pipeline Started' } },
    { id: 'agent-business_analyst-0', type: 'pipelineEvent', data: { label: 'Business Analyst' } },
    { id: 'agent-software_architect-0', type: 'pipelineEvent', data: { label: 'Software Architect (1)' } },
    { id: 'agent-software_architect-1', type: 'pipelineEvent', data: { label: 'Software Architect (2)' } },
    { id: 'agent-software_architect-2', type: 'pipelineEvent', data: { label: 'Software Architect (3)' } },
    { id: 'agent-senior_software_engineer-0', type: 'pipelineEvent', data: { label: 'Senior Software Engineer' } },
    { id: 'completed', type: 'pipelineEvent', data: { label: 'Pipeline Completed' } },
  ]
  
  const edges = [
    { id: 'e1', source: 'created', target: 'agent-business_analyst-0' },
    { id: 'e2', source: 'agent-business_analyst-0', target: 'agent-software_architect-0' },
    { id: 'e3', source: 'agent-software_architect-0', target: 'agent-software_architect-1' },
    { id: 'e4', source: 'agent-software_architect-1', target: 'agent-software_architect-2' },
    { id: 'e5', source: 'agent-software_architect-2', target: 'agent-senior_software_engineer-0' },
    { id: 'e6', source: 'agent-senior_software_engineer-0', target: 'completed' },
  ]
  
  // Mock agent executions
  const agentExecutions = new Map([
    ['business_analyst', [{ taskId: '1', status: 'completed' }]],
    ['software_architect', [
      { taskId: '2', status: 'completed' },
      { taskId: '3', status: 'completed' },
      { taskId: '4', status: 'completed' },
    ]],
    ['senior_software_engineer', [{ taskId: '5', status: 'completed' }]],
  ])
  
  // Identify cycles
  const cycles = identifyCycles([], agentExecutions)
  console.log('Detected cycles:', cycles)
  // Output: Map { 'software_architect' => { agent: 'software_architect', iterations: 3, ... } }
  
  // Apply layout
  const { nodes: layoutedNodes, cycleNodes, edges: layoutedEdges } = applyCycleLayout(
    nodes,
    edges,
    cycles,
    {
      nodeWidth: 250,
      nodeHeight: 80,
      horizontalSpacing: 150,
      cycleGap: 100,
      cyclePadding: 40,
      viewportHeight: 600,
    }
  )
  
  console.log('Layout result:')
  console.log('- Regular nodes:', layoutedNodes.length)
  console.log('- Cycle bounding nodes:', cycleNodes.length)
  
  return { nodes: layoutedNodes, edges: layoutedEdges }
}

// Example: Multiple review cycles
export const multiCycleExample = () => {
  const agentExecutions = new Map([
    ['requirements_reviewer', [
      { taskId: '1', status: 'completed' },
      { taskId: '2', status: 'completed' },
    ]],
    ['software_architect', [
      { taskId: '3', status: 'completed' },
      { taskId: '4', status: 'completed' },
      { taskId: '5', status: 'completed' },
    ]],
    ['code_reviewer', [
      { taskId: '6', status: 'completed' },
      { taskId: '7', status: 'completed' },
    ]],
  ])
  
  const cycles = identifyCycles([], agentExecutions)
  console.log('Multiple cycles detected:', cycles.size)
  // Output: 3 cycles
  
  return cycles
}

// Example: Toggling cycle collapsed state
export const collapseExample = () => {
  const agentExecutions = new Map([
    ['software_architect', [
      { taskId: '1', status: 'completed' },
      { taskId: '2', status: 'completed' },
      { taskId: '3', status: 'completed' },
    ]],
  ])
  
  let cycles = identifyCycles([], agentExecutions)
  console.log('Initial state:', cycles.get('software_architect').isCollapsed)
  // Output: false (expanded by default)
  
  // Toggle collapsed
  cycles = toggleCycleCollapsed(cycles, 'software_architect')
  console.log('After toggle:', cycles.get('software_architect').isCollapsed)
  // Output: true (now collapsed)
  
  // Toggle again
  cycles = toggleCycleCollapsed(cycles, 'software_architect')
  console.log('After second toggle:', cycles.get('software_architect').isCollapsed)
  // Output: false (expanded again)
  
  return cycles
}

// Example: Edge redirection for collapsed cycles
export const edgeRedirectionExample = () => {
  const edges = [
    { id: 'e1', source: 'previous-node', target: 'agent-software_architect-0' },
    { id: 'e2', source: 'agent-software_architect-0', target: 'agent-software_architect-1' },
    { id: 'e3', source: 'agent-software_architect-1', target: 'agent-software_architect-2' },
    { id: 'e4', source: 'agent-software_architect-2', target: 'next-node' },
  ]
  
  const cycles = new Map([
    ['software_architect', {
      agent: 'software_architect',
      iterations: 3,
      isCollapsed: true, // COLLAPSED
      executions: [],
    }],
  ])
  
  const nodesByCycle = new Map([
    ['software_architect', [
      { id: 'agent-software_architect-0' },
      { id: 'agent-software_architect-1' },
      { id: 'agent-software_architect-2' },
    ]],
  ])
  
  const updatedEdges = updateEdgesForCycles(edges, cycles, nodesByCycle)
  
  console.log('Original edges:', edges.length)
  console.log('Updated edges:', updatedEdges.length)
  // Edges to/from cycle nodes are redirected to 'cycle-software_architect'
  
  console.log('Updated edge sources/targets:')
  updatedEdges.forEach(edge => {
    console.log(`${edge.source} -> ${edge.target}`)
  })
  // Output:
  // previous-node -> cycle-software_architect
  // cycle-software_architect -> next-node
  
  return updatedEdges
}

// Visual representation of the layout
export const visualExample = `
EXPANDED STATE:
===============

[Created] ──→ [Business Analyst] ──→ ╔══════════════════════════════════════════════╗ ──→ [Senior Engineer] ──→ [Completed]
                                      ║  Software Architect (3 iterations) [▼]      ║
                                      ║  ╭─────────────────────────────────────────╮ ║
                                      ║  │  [Iteration #1] → [Iteration #2] →    │ ║
                                      ║  │  [Iteration #3]                        │ ║
                                      ║  ╰─────────────────────────────────────────╯ ║
                                      ║  #1            #2            #3              ║
                                      ╚══════════════════════════════════════════════╝


COLLAPSED STATE:
================

[Created] ──→ [Business Analyst] ──→ ╔═══════════════════════╗ ──→ [Senior Engineer] ──→ [Completed]
                                      ║ Software Architect [▶]║
                                      ║                       ║
                                      ║   Review Cycle        ║
                                      ║        3×             ║
                                      ║  Click to expand      ║
                                      ╚═══════════════════════╝


VERTICAL CENTERING:
==================

                                    ─────────────────────────────
                                          (viewport center)
    [Node] ──→ [Node] ──→ ╔═══════╗ ──→ [Node] ──→ [Node]
                          ║ Cycle ║
                          ╚═══════╝
                                    ─────────────────────────────
                                          (viewport center)

All nodes centered on horizontal line, flowing left to right
`

// Example: Integration with React Flow
export const reactFlowIntegration = `
import { useState, useCallback } from 'react'
import { ReactFlow, useNodesState, useEdgesState } from '@xyflow/react'
import CycleBoundingNode from '../components/CycleBoundingNode'
import { applyCycleLayout, toggleCycleCollapsed, updateEdgesForCycles } from '../utils/cycleLayout'

const nodeTypes = {
  pipelineEvent: PipelineEventNode,
  cycleBounding: CycleBoundingNode,
}

function MyPipelineView() {
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [cycles, setCycles] = useState(new Map())
  
  const handleToggleCycle = useCallback((agent) => {
    setCycles(prevCycles => toggleCycleCollapsed(prevCycles, agent))
    // Trigger re-layout
    rebuildFlowchart()
  }, [])
  
  const rebuildFlowchart = useCallback(() => {
    // ... build initial nodes and edges from data ...
    
    // Apply custom layout
    const { nodes: layoutedNodes } = applyCycleLayout(
      rawNodes,
      rawEdges,
      cycles,
      { viewportHeight: 600 }
    )
    
    // Add toggle callback to cycle nodes
    const finalNodes = layoutedNodes.map(node => {
      if (node.type === 'cycleBounding') {
        return {
          ...node,
          data: {
            ...node.data,
            onToggleCollapse: handleToggleCycle,
          },
        }
      }
      return node
    })
    
    // Update edges for collapsed cycles
    const updatedEdges = updateEdgesForCycles(rawEdges, cycles, agentExecutions)
    
    setNodes(finalNodes)
    setEdges(updatedEdges)
  }, [cycles, handleToggleCycle])
  
  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      fitView
    >
      <Controls />
      <Background />
    </ReactFlow>
  )
}
`

export default {
  simpleExample,
  multiCycleExample,
  collapseExample,
  edgeRedirectionExample,
  visualExample,
  reactFlowIntegration,
}
