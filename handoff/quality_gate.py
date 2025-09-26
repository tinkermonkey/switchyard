# handoff/quality_gate.py
from typing import Dict, Any, List, Tuple
from .protocol import HandoffPackage

class QualityGate:
    """Enforces quality standards before allowing handoffs"""
    
    def __init__(self, thresholds: Dict[str, float]):
        self.thresholds = thresholds
    
    def evaluate(self, handoff: HandoffPackage) -> Tuple[bool, List[str]]:
        """Evaluate if handoff meets quality standards"""
        issues = []
        
        # Check quality metrics against thresholds
        for metric, threshold in self.thresholds.items():
            if metric in handoff.quality_metrics:
                if handoff.quality_metrics[metric] < threshold:
                    issues.append(
                        f"{metric} ({handoff.quality_metrics[metric]:.2f}) "
                        f"below threshold ({threshold:.2f})"
                    )
            else:
                issues.append(f"Missing required metric: {metric}")
        
        # Check validation results
        for check, passed in handoff.validation_results.items():
            if not passed:
                issues.append(f"Validation failed: {check}")
        
        # Check artifact completeness
        required_artifacts = ['requirements', 'design', 'tests']
        for artifact in required_artifacts:
            if artifact not in handoff.artifacts:
                issues.append(f"Missing required artifact: {artifact}")
        
        passed = len(issues) == 0
        return passed, issues