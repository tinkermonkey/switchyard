from typing import Dict, Any, List, Callable, Optional
from resilience.circuit_breaker import CircuitBreaker
from resilience.retry_manager import RetryManager

class ResilientPipelineStage:
    """Pipeline stage with retry and circuit breaker capabilities"""
    
    def __init__(
        self,
        name: str,
        agent_func: Callable,
        max_retries: int = 3,
        circuit_breaker_config: Optional[Dict] = None
    ):
        self.name = name
        self.agent_func = agent_func
        self.max_retries = max_retries
        
        # Create circuit breaker for this stage
        if circuit_breaker_config:
            self.circuit_breaker = CircuitBreaker(**circuit_breaker_config)
        else:
            self.circuit_breaker = CircuitBreaker()
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute stage with resilience patterns"""
        
        # Wrap the agent function with retry logic
        @RetryManager.with_retry(
            max_attempts=self.max_retries,
            circuit_breaker=self.circuit_breaker
        )
        async def resilient_execution():
            return await self.agent_func(context)
        
        try:
            result = await resilient_execution()
            return result
        except Exception as e:
            print(f"❌ Stage {self.name} failed permanently: {e}")
            raise


class ResilientPipeline:
    """Pipeline with integrated resilience patterns"""
    
    def __init__(self, stages: List[ResilientPipelineStage], state_manager):
        self.stages = stages
        self.state_manager = state_manager
        self.stage_metrics = {}  # Track success/failure rates
    
    async def execute(self, initial_context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute pipeline with resilience"""
        context = initial_context.copy()
        
        for i, stage in enumerate(self.stages):
            stage_key = f"{stage.name}_{i}"
            
            # Initialize metrics
            if stage_key not in self.stage_metrics:
                self.stage_metrics[stage_key] = {
                    'attempts': 0,
                    'successes': 0,
                    'failures': 0,
                    'circuit_breaks': 0
                }
            
            try:
                # Check if circuit is open before attempting
                if stage.circuit_breaker.is_open():
                    self.stage_metrics[stage_key]['circuit_breaks'] += 1
                    print(f"⚡ Circuit breaker OPEN for {stage.name}, skipping...")
                    
                    # You could implement fallback logic here
                    context['skipped_stages'] = context.get('skipped_stages', [])
                    context['skipped_stages'].append(stage.name)
                    continue
                
                # Execute stage
                self.stage_metrics[stage_key]['attempts'] += 1
                context = await stage.execute(context)
                self.stage_metrics[stage_key]['successes'] += 1
                
                # Checkpoint after successful stage
                await self.state_manager.checkpoint(
                    pipeline_id=context.get('pipeline_id'),
                    stage_index=i,
                    context=context
                )
                
            except Exception as e:
                self.stage_metrics[stage_key]['failures'] += 1
                
                # Decide whether to continue or abort pipeline
                if self._should_abort_pipeline(stage, e):
                    print(f"🛑 Aborting pipeline due to critical failure in {stage.name}")
                    raise
                else:
                    print(f"⚠️ Stage {stage.name} failed but continuing pipeline")
                    context['failed_stages'] = context.get('failed_stages', [])
                    context['failed_stages'].append({
                        'stage': stage.name,
                        'error': str(e)
                    })
        
        return context
    
    def _should_abort_pipeline(self, stage: ResilientPipelineStage, error: Exception) -> bool:
        """Determine if pipeline should abort based on failure"""
        # Critical stages that should stop the pipeline
        critical_stages = ['code_reviewer', 'security_scanner']
        
        if stage.name in critical_stages:
            return True
        
        # Abort if circuit is open (too many failures)
        if stage.circuit_breaker.is_open():
            return True
        
        # Continue for non-critical failures
        return False
    
    def get_health_report(self) -> Dict[str, Any]:
        """Get health metrics for all stages"""
        report = {}
        
        for stage_key, metrics in self.stage_metrics.items():
            total = metrics['attempts']
            if total > 0:
                success_rate = metrics['successes'] / total
                report[stage_key] = {
                    'success_rate': f"{success_rate:.2%}",
                    'total_attempts': total,
                    'circuit_breaks': metrics['circuit_breaks'],
                    'health_score': self._calculate_health_score(metrics)
                }
        
        return report
    
    def _calculate_health_score(self, metrics: Dict) -> float:
        """Calculate health score (0-100) for a stage"""
        if metrics['attempts'] == 0:
            return 100.0
        
        success_rate = metrics['successes'] / metrics['attempts']
        circuit_break_penalty = min(metrics['circuit_breaks'] * 10, 50)
        
        return max(0, (success_rate * 100) - circuit_break_penalty)