# pipeline/orchestrator.py
import asyncio
from typing import List, Dict, Any
from datetime import datetime
from pipeline.base import PipelineStage

class SequentialPipeline:
    def __init__(self, stages: List[PipelineStage], state_manager):
        self.stages = stages
        self.state_manager = state_manager
        self.current_stage_index = 0
        
    async def execute(self, initial_context: Dict[str, Any]) -> Dict[str, Any]:
        context = initial_context.copy()
        context['pipeline_id'] = f"pipeline_{datetime.now().isoformat()}"
        
        # Load from checkpoint if resuming
        checkpoint = await self.state_manager.get_latest_checkpoint(context['pipeline_id'])
        if checkpoint:
            self.current_stage_index = checkpoint['stage_index']
            context = checkpoint['context']
        
        while self.current_stage_index < len(self.stages):
            stage = self.stages[self.current_stage_index]
            
            try:
                # Create checkpoint before stage execution
                await self.state_manager.checkpoint(
                    pipeline_id=context['pipeline_id'],
                    stage_index=self.current_stage_index,
                    context=context
                )
                
                # Execute stage with circuit breaker
                context = await stage.run_with_circuit_breaker(context)
                
                # Log successful completion
                await self.state_manager.log_stage_completion(
                    pipeline_id=context['pipeline_id'],
                    stage_name=stage.name,
                    context=context
                )
                
                self.current_stage_index += 1
                
            except Exception as e:
                await self.state_manager.log_stage_failure(
                    pipeline_id=context['pipeline_id'],
                    stage_name=stage.name,
                    error=str(e)
                )
                
                # Determine if we should retry or fail
                if stage.circuit_breaker.state == "open":
                    raise Exception(f"Pipeline halted: {stage.name} circuit breaker open")
                
                # Allow for retry logic here
                await asyncio.sleep(5)
        
        return context