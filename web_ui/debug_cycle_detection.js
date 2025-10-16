#!/usr/bin/env node
/**
 * Cycle Detection Debug Harness
 * 
 * Fetches real pipeline run events from the observability API and tests
 * the cycle detection algorithm to debug why cycles aren't being detected.
 * 
 * Usage:
 *   node debug_cycle_detection.js <pipeline_run_id>
 *   node debug_cycle_detection.js 774748bf-697d-4bcc-b804-0bcc5afa9a76
 */

import fetch from 'node-fetch';
import { detectCycleBoundaries } from './src/utils/cycleLayout.js';

const API_BASE = process.env.API_BASE || 'http://localhost:5001';

// ANSI color codes for pretty output
const colors = {
  reset: '\x1b[0m',
  bright: '\x1b[1m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  magenta: '\x1b[35m',
  cyan: '\x1b[36m',
};

function log(msg, color = 'reset') {
  console.log(`${colors[color]}${msg}${colors.reset}`);
}

function logHeader(msg) {
  console.log('\n' + '='.repeat(80));
  log(msg, 'bright');
  console.log('='.repeat(80));
}

async function fetchPipelineRunEvents(pipelineRunId) {
  const url = `${API_BASE}/pipeline-run-events?pipeline_run_id=${pipelineRunId}&limit=1000`;
  log(`\n📡 Fetching events from: ${url}`, 'cyan');
  
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  
  const data = await response.json();
  log(`✅ Fetched ${data.event_count} events`, 'green');
  
  return data.events;
}

async function fetchPipelineRunInfo(pipelineRunId) {
  const url = `${API_BASE}/active-pipeline-runs`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  
  const data = await response.json();
  const run = data.runs.find(r => r.id === pipelineRunId);
  return run;
}

function analyzeEvents(events) {
  logHeader('📊 EVENT ANALYSIS');
  
  // Count by category
  const byCategory = {};
  events.forEach(e => {
    const cat = e.event_category || 'no_category';
    byCategory[cat] = (byCategory[cat] || 0) + 1;
  });
  
  log('\n📋 Events by category:', 'cyan');
  Object.entries(byCategory).forEach(([cat, count]) => {
    console.log(`  ${cat}: ${count}`);
  });
  
  // Decision events
  const decisionEvents = events.filter(e => e.event_category === 'decision');
  log(`\n🎯 Decision events: ${decisionEvents.length}`, 'yellow');
  
  // Group decision events by type
  const byType = {};
  decisionEvents.forEach(e => {
    const type = e.event_type || 'no_type';
    byType[type] = (byType[type] || 0) + 1;
  });
  
  log('\n📝 Decision events by type:', 'cyan');
  Object.entries(byType)
    .sort((a, b) => b[1] - a[1])
    .forEach(([type, count]) => {
      console.log(`  ${type}: ${count}`);
    });
  
  // Find cycle boundary events
  log('\n🔍 Cycle boundary events:', 'magenta');
  const boundaryEvents = decisionEvents.filter(e => {
    const type = e.event_type;
    return type && (
      type.includes('_started') || 
      type.includes('_completed') ||
      type === 'review_cycle_iteration' ||
      type === 'repair_cycle_iteration'
    );
  });
  
  if (boundaryEvents.length === 0) {
    log('  ⚠️  NO BOUNDARY EVENTS FOUND!', 'red');
  } else {
    boundaryEvents
      .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
      .forEach(e => {
        const time = new Date(e.timestamp).toISOString().substr(11, 8);
        const type = e.event_type.padEnd(40);
        const agent = (e.agent || 'n/a').padEnd(25);
        console.log(`  ${time} | ${type} | ${agent}`);
      });
  }
  
  return { decisionEvents, boundaryEvents };
}

function testCycleDetection(events) {
  logHeader('🧪 TESTING CYCLE DETECTION');
  
  log('\n🔬 Running detectCycleBoundaries()...', 'cyan');
  
  try {
    const cycles = detectCycleBoundaries(events);
    
    log(`\n✅ Detection completed: ${cycles.length} cycle(s) found`, 'green');
    
    if (cycles.length === 0) {
      log('⚠️  NO CYCLES DETECTED!', 'red');
      log('\n💡 This indicates the algorithm is not recognizing the cycle boundary events.', 'yellow');
      return cycles;
    }
    
    // Display detected cycles
    cycles.forEach((cycle, idx) => {
      log(`\n📦 Cycle ${idx + 1}: ${cycle.id}`, 'bright');
      console.log(`   Type: ${cycle.type}${cycle.subtype ? ` (${cycle.subtype})` : ''}`);
      console.log(`   Start: ${cycle.startTime}`);
      console.log(`   End: ${cycle.endTime || 'ONGOING'}`);
      
      if (cycle.endTime) {
        const duration = (new Date(cycle.endTime) - new Date(cycle.startTime)) / 1000;
        console.log(`   Duration: ${duration.toFixed(1)}s`);
      }
      
      if (cycle.metadata) {
        console.log(`   Metadata:`, JSON.stringify(cycle.metadata, null, 4));
      }
    });
    
    return cycles;
  } catch (error) {
    log(`\n❌ ERROR during detection: ${error.message}`, 'red');
    console.error(error.stack);
    return [];
  }
}

function diagnose(events, cycles) {
  logHeader('🩺 DIAGNOSIS');
  
  const decisionEvents = events.filter(e => e.event_category === 'decision');
  
  // Check for review cycles
  const reviewStarts = decisionEvents.filter(e => e.event_type === 'review_cycle_started');
  const reviewEnds = decisionEvents.filter(e => e.event_type === 'review_cycle_completed');
  
  log('\n📋 Review Cycles:', 'cyan');
  console.log(`   Starts: ${reviewStarts.length}`);
  console.log(`   Completions: ${reviewEnds.length}`);
  
  // Check for repair test cycles
  const testStarts = decisionEvents.filter(e => e.event_type === 'repair_cycle_test_cycle_started');
  const testEnds = decisionEvents.filter(e => e.event_type === 'repair_cycle_test_cycle_completed');
  
  log('\n🔧 Repair Test Cycles:', 'cyan');
  console.log(`   Starts: ${testStarts.length}`);
  console.log(`   Completions: ${testEnds.length}`);
  if (testStarts.length > testEnds.length) {
    log(`   ⚠️  ${testStarts.length - testEnds.length} test cycle(s) still OPEN`, 'yellow');
  }
  
  // Check for repair fix cycles
  const fixStarts = decisionEvents.filter(e => e.event_type === 'repair_cycle_fix_cycle_started');
  const fixEnds = decisionEvents.filter(e => e.event_type === 'repair_cycle_fix_cycle_completed');
  
  log('\n🛠️  Repair Fix Cycles:', 'cyan');
  console.log(`   Starts: ${fixStarts.length}`);
  console.log(`   Completions: ${fixEnds.length}`);
  if (fixStarts.length > fixEnds.length) {
    log(`   ⚠️  ${fixStarts.length - fixEnds.length} fix cycle(s) still OPEN`, 'yellow');
  }
  
  // Check for conversational loops
  const convStarts = decisionEvents.filter(e => e.event_type === 'conversational_loop_started');
  const convEnds = decisionEvents.filter(e => e.event_type === 'conversational_loop_completed');
  
  log('\n💬 Conversational Loops:', 'cyan');
  console.log(`   Starts: ${convStarts.length}`);
  console.log(`   Completions: ${convEnds.length}`);
  
  // Compare expected vs detected
  const expectedCycles = reviewStarts.length + reviewEnds.length + 
                         Math.max(fixStarts.length, fixEnds.length) +
                         Math.max(testStarts.length, testEnds.length) +
                         convStarts.length + convEnds.length;
  
  log('\n🎯 Expected vs Detected:', 'magenta');
  console.log(`   Minimum expected cycles: ${Math.max(1, Math.ceil(expectedCycles / 2))}`);
  console.log(`   Actually detected: ${cycles.length}`);
  
  if (cycles.length === 0 && expectedCycles > 0) {
    log('\n❌ PROBLEM: We have cycle boundary events but detection returned 0 cycles!', 'red');
    log('   This suggests the algorithm logic has a bug.', 'yellow');
  }
}

async function main() {
  const args = process.argv.slice(2);
  
  if (args.length === 0) {
    console.error('Usage: node debug_cycle_detection.js <pipeline_run_id>');
    console.error('\nExample:');
    console.error('  node debug_cycle_detection.js 774748bf-697d-4bcc-b804-0bcc5afa9a76');
    process.exit(1);
  }
  
  const pipelineRunId = args[0];
  
  logHeader(`🔍 CYCLE DETECTION DEBUG HARNESS`);
  log(`Pipeline Run ID: ${pipelineRunId}`, 'cyan');
  
  try {
    // Fetch pipeline run info
    log('\n📋 Fetching pipeline run info...', 'cyan');
    const runInfo = await fetchPipelineRunInfo(pipelineRunId);
    if (runInfo) {
      log(`   Project: ${runInfo.project}`, 'green');
      log(`   Issue: #${runInfo.issue_number} - ${runInfo.issue_title}`, 'green');
      log(`   Board: ${runInfo.board}`, 'green');
      log(`   Status: ${runInfo.status}`, 'green');
    } else {
      log('   ⚠️  Pipeline run not found in active runs', 'yellow');
    }
    
    // Fetch events
    const events = await fetchPipelineRunEvents(pipelineRunId);
    
    // Analyze events
    analyzeEvents(events);
    
    // Test cycle detection
    const cycles = testCycleDetection(events);
    
    // Diagnose
    diagnose(events, cycles);
    
    logHeader('✅ DEBUG COMPLETE');
    
  } catch (error) {
    log(`\n❌ ERROR: ${error.message}`, 'red');
    console.error(error.stack);
    process.exit(1);
  }
}

main();
