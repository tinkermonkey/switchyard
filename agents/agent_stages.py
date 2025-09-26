from typing import Dict, Any
from pipeline.resilient_pipeline import ResilientPipelineStage, ResilientPipeline
from claude.claude_integration import run_claude_code
import random


# Define your agent functions
async def business_analyst_agent(context: Dict[str, Any]) -> Dict[str, Any]:
    """Run business analyst agent"""
    # Extract issue information from context
    issue = context.get('context', {}).get('issue', {})

    prompt = f"""
Analyze the following issue/requirement for business analysis:

Title: {issue.get('title', 'No title')}
Description: {issue.get('body', 'No description')}
Labels: {issue.get('labels', [])}

Provide a comprehensive business analysis with:
1. Functional requirements
2. Non-functional requirements
3. User stories with acceptance criteria
4. Risk assessment
5. Quality metrics

Return structured JSON format.
"""

    result = await run_claude_code(prompt=prompt, context=context)

    # Parse the result to extract requirements analysis and quality metrics
    import json
    analysis_data = {}
    if isinstance(result, str):
        try:
            analysis_data = json.loads(result)
        except json.JSONDecodeError:
            analysis_data = {"requirements_analysis": {"summary": result}}
    else:
        analysis_data = result

    # Add parsed data to context
    updated_context = {**context, 'requirements_analysis': analysis_data.get('requirements_analysis', {}), 'quality_metrics': analysis_data.get('quality_metrics', {})}

    # Create handoff package for next stage (even if no next stage)
    from handoff.protocol import HandoffManager
    from handoff.quality_gate import QualityGate
    from state_management.manager import StateManager

    # Get state manager from context or create one
    state_manager = context.get('state_manager')
    if not state_manager:
        state_manager = StateManager()

    handoff_manager = HandoffManager(state_manager)
    handoff = await handoff_manager.create_handoff(
        source_agent="business_analyst",
        target_agent="end_of_pipeline",
        context=updated_context,
        artifacts={
            "requirements_document": updated_context.get('requirements_analysis', {}),
            "user_stories": updated_context.get('requirements_analysis', {}).get('user_stories', [])
        }
    )

    # Validate handoff package
    quality_gate = QualityGate({
        "completeness_score": 0.7,
        "clarity_score": 0.7
    })

    passed, issues = quality_gate.evaluate(handoff)
    if not passed:
        updated_context['warnings'] = issues
        print(f"Warning: Quality gate issues: {issues}")

    updated_context['handoff_id'] = handoff.handoff_id
    print(f"Handoff package created: {handoff.handoff_id}")

    return updated_context

async def code_reviewer_agent(context: Dict[str, Any]) -> Dict[str, Any]:
    """Run code reviewer agent"""
    # Simulate potential failure
    if random.random() < 0.1:  # 10% failure rate
        raise Exception("Code review service temporarily unavailable")
    
    result = await run_claude_code(
        prompt="Review code changes...",
        context=context
    )
    return {**context, 'review': result}

# Create pipeline with resilience
def create_sdlc_pipeline(state_manager) -> ResilientPipeline:
    stages = [
        ResilientPipelineStage(
            name="business_analyst",
            agent_func=business_analyst_agent,
            max_retries=3,
            circuit_breaker_config={
                'failure_threshold': 3,
                'recovery_timeout': 300  # 5 minutes
            }
        ),
        ResilientPipelineStage(
            name="code_reviewer",
            agent_func=code_reviewer_agent,
            max_retries=5,  # More retries for critical stage
            circuit_breaker_config={
                'failure_threshold': 5,
                'recovery_timeout': 180  # 3 minutes
            }
        ),
    ]
    
    return ResilientPipeline(stages, state_manager)

# Process task function that integrates with main orchestrator
async def process_task_integrated(task, state_manager, logger):
    """Process task using sequential pipeline with proper integration"""
    from datetime import datetime

    # Convert Task object to pipeline context
    pipeline_context = {
        'pipeline_id': f"pipeline_{task.id}_{datetime.now().timestamp()}",
        'task_id': task.id,
        'agent': task.agent,
        'project': task.project,
        'context': task.context,
        'work_dir': f"./projects/{task.project}",
        'completed_work': [],
        'decisions': [],
        'metrics': {},
        'validation': {}
    }

    # Create pipeline with single business analyst agent for now
    from pipeline.base import PipelineStage
    from pipeline.orchestrator import SequentialPipeline

    class BusinessAnalystStage(PipelineStage):
        def __init__(self):
            super().__init__("business_analyst")

        async def execute(self, context):
            return await business_analyst_agent(context)

    stages = [BusinessAnalystStage()]
    pipeline = SequentialPipeline(stages, state_manager)

    try:
        logger.log_info(f"Starting pipeline execution for task {task.id}")
        result = await pipeline.execute(pipeline_context)
        logger.log_info(f"Pipeline completed for task {task.id}")
        return result

    except Exception as e:
        logger.log_error(f"Pipeline execution failed for task {task.id}: {e}")
        raise