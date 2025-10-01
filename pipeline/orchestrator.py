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
        import logging
        logger = logging.getLogger(__name__)

        logger.info("SequentialPipeline.execute() called")
        logger.info(f"Number of stages: {len(self.stages)}")

        context = initial_context.copy()
        context['pipeline_id'] = f"pipeline_{datetime.now().isoformat()}"

        logger.info(f"Pipeline ID: {context['pipeline_id']}")

        # Load from checkpoint if resuming
        logger.info("Checking for checkpoint")
        checkpoint = await self.state_manager.get_latest_checkpoint(context['pipeline_id'])
        if checkpoint:
            logger.info(f"Resuming from checkpoint at stage {checkpoint['stage_index']}")
            self.current_stage_index = checkpoint['stage_index']
            context = checkpoint['context']
        else:
            logger.info("No checkpoint found, starting from beginning")

        while self.current_stage_index < len(self.stages):
            stage = self.stages[self.current_stage_index]
            logger.info(f"Executing stage {self.current_stage_index}: {stage.name}")

            try:
                # Create checkpoint before stage execution
                logger.info("Creating checkpoint")
                await self.state_manager.checkpoint(
                    pipeline_id=context['pipeline_id'],
                    stage_index=self.current_stage_index,
                    context=context
                )
                logger.info("Checkpoint created")

                # Execute stage with circuit breaker
                logger.info(f"Running stage with circuit breaker: {stage.name}")
                context = await stage.run_with_circuit_breaker(context)
                logger.info(f"Stage {stage.name} completed successfully")
                
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