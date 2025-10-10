"""
Decision Analytics Module

Provides analytics and reporting capabilities for orchestrator decision events:
- Metrics aggregation (event counts, success rates, timing)
- Pattern detection (common sequences, anomalies)
- Bottleneck identification
- Performance analysis
- Reporting API for dashboards and alerts
"""

import logging
import json
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from dataclasses import dataclass, asdict
import redis

from monitoring.observability import EventType, get_observability_manager

logger = logging.getLogger(__name__)


@dataclass
class MetricsSummary:
    """Summary of metrics for a time period"""
    time_period: str
    total_events: int
    events_by_type: Dict[str, int]
    events_by_category: Dict[str, int]
    success_rate: float
    error_count: int
    avg_events_per_minute: float


@dataclass
class ReviewCycleMetrics:
    """Metrics for review cycles"""
    total_cycles: int
    avg_iterations: float
    escalation_rate: float
    success_rate: float
    avg_duration_minutes: Optional[float]


@dataclass
class RoutingMetrics:
    """Metrics for agent routing decisions"""
    total_decisions: int
    agents_selected: Dict[str, int]
    null_selections: int
    avg_alternatives_considered: float


@dataclass
class ErrorMetrics:
    """Metrics for error handling"""
    total_errors: int
    errors_by_type: Dict[str, int]
    recovery_rate: float
    circuit_breaker_trips: int
    avg_retries_to_success: float


@dataclass
class Pattern:
    """Detected pattern in events"""
    pattern_type: str
    description: str
    occurrences: int
    severity: str  # "info", "warning", "critical"
    first_seen: datetime
    last_seen: datetime
    example_event_ids: List[str]


class DecisionAnalytics:
    """
    Analytics engine for decision observability events
    
    Provides methods to:
    - Aggregate metrics from event stream
    - Detect patterns and anomalies
    - Identify bottlenecks
    - Generate reports
    """
    
    def __init__(self, redis_client: Optional[redis.Redis] = None):
        """
        Initialize analytics engine
        
        Args:
            redis_client: Redis client (uses obs manager's client if None)
        """
        self.obs = get_observability_manager()
        self.redis = redis_client or self.obs.redis
        self.stream_name = "orchestrator:event_stream"
    
    def get_events(
        self, 
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        event_type: Optional[str] = None,
        max_count: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Retrieve events from Redis stream
        
        Args:
            start_time: Start of time range (default: 1 hour ago)
            end_time: End of time range (default: now)
            event_type: Filter by event type (default: all)
            max_count: Maximum events to return
            
        Returns:
            List of event dictionaries
        """
        if start_time is None:
            start_time = datetime.now() - timedelta(hours=1)
        if end_time is None:
            end_time = datetime.now()
        
        try:
            # Read from Redis stream
            # Convert datetime to Redis stream ID format
            start_id = f"{int(start_time.timestamp() * 1000)}-0"
            end_id = f"{int(end_time.timestamp() * 1000)}-0"
            
            # XRANGE to get events in time range
            entries = self.redis.xrange(self.stream_name, min=start_id, max=end_id, count=max_count)
            
            events = []
            for entry_id, entry_data in entries:
                try:
                    # Parse event data
                    event_json = entry_data.get(b'event', b'{}')
                    event = json.loads(event_json.decode('utf-8'))
                    
                    # Filter by event type if specified
                    if event_type is None or event.get('event_type') == event_type:
                        events.append(event)
                except Exception as e:
                    logger.warning(f"Failed to parse event {entry_id}: {e}")
                    continue
            
            return events
            
        except Exception as e:
            logger.error(f"Failed to retrieve events: {e}")
            return []
    
    def get_metrics_summary(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> MetricsSummary:
        """
        Get summary metrics for a time period
        
        Args:
            start_time: Start of time range (default: 1 hour ago)
            end_time: End of time range (default: now)
            
        Returns:
            MetricsSummary object
        """
        events = self.get_events(start_time, end_time)
        
        if not events:
            return MetricsSummary(
                time_period=f"{start_time} to {end_time}",
                total_events=0,
                events_by_type={},
                events_by_category={},
                success_rate=0.0,
                error_count=0,
                avg_events_per_minute=0.0
            )
        
        # Count by type
        events_by_type = Counter(e['event_type'] for e in events)
        
        # Count by category
        events_by_category = Counter(
            e.get('data', {}).get('decision_category', 'unknown') 
            for e in events
        )
        
        # Calculate success rate
        success_events = [
            e for e in events 
            if e.get('data', {}).get('success') is True
            or 'completed' in e['event_type']
            or 'recovered' in e['event_type']
        ]
        failure_events = [
            e for e in events
            if e.get('data', {}).get('success') is False
            or 'failed' in e['event_type']
            or 'encountered' in e['event_type']
        ]
        
        total_with_status = len(success_events) + len(failure_events)
        success_rate = (len(success_events) / total_with_status * 100) if total_with_status > 0 else 0.0
        
        # Count errors
        error_count = len([e for e in events if 'error' in e['event_type'].lower()])
        
        # Calculate rate
        if start_time and end_time:
            duration_minutes = (end_time - start_time).total_seconds() / 60
            avg_events_per_minute = len(events) / duration_minutes if duration_minutes > 0 else 0.0
        else:
            avg_events_per_minute = 0.0
        
        return MetricsSummary(
            time_period=f"{start_time} to {end_time}",
            total_events=len(events),
            events_by_type=dict(events_by_type),
            events_by_category=dict(events_by_category),
            success_rate=round(success_rate, 2),
            error_count=error_count,
            avg_events_per_minute=round(avg_events_per_minute, 2)
        )
    
    def get_review_cycle_metrics(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> ReviewCycleMetrics:
        """
        Get metrics for review cycles
        
        Args:
            start_time: Start of time range
            end_time: End of time range
            
        Returns:
            ReviewCycleMetrics object
        """
        events = self.get_events(start_time, end_time)
        
        # Filter review cycle events
        review_events = [e for e in events if 'review_cycle' in e['event_type']]
        
        if not review_events:
            return ReviewCycleMetrics(
                total_cycles=0,
                avg_iterations=0.0,
                escalation_rate=0.0,
                success_rate=0.0,
                avg_duration_minutes=None
            )
        
        # Group by issue number to get cycles
        cycles_by_issue = defaultdict(list)
        for event in review_events:
            issue_num = event.get('data', {}).get('issue_number')
            if issue_num:
                cycles_by_issue[issue_num].append(event)
        
        total_cycles = len(cycles_by_issue)
        
        # Calculate iterations
        iterations = []
        for issue_events in cycles_by_issue.values():
            max_iteration = max(
                (e.get('data', {}).get('inputs', {}).get('cycle_iteration', 0) 
                 for e in issue_events),
                default=0
            )
            iterations.append(max_iteration)
        
        avg_iterations = sum(iterations) / len(iterations) if iterations else 0.0
        
        # Calculate escalation rate
        escalated = len([e for e in review_events if e['event_type'] == 'review_cycle_escalated'])
        escalation_rate = (escalated / total_cycles * 100) if total_cycles > 0 else 0.0
        
        # Calculate success rate
        completed = len([e for e in review_events if e['event_type'] == 'review_cycle_completed'])
        success_rate = (completed / total_cycles * 100) if total_cycles > 0 else 0.0
        
        return ReviewCycleMetrics(
            total_cycles=total_cycles,
            avg_iterations=round(avg_iterations, 2),
            escalation_rate=round(escalation_rate, 2),
            success_rate=round(success_rate, 2),
            avg_duration_minutes=None  # Would need to calculate from start/end timestamps
        )
    
    def get_routing_metrics(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> RoutingMetrics:
        """
        Get metrics for agent routing decisions
        
        Args:
            start_time: Start of time range
            end_time: End of time range
            
        Returns:
            RoutingMetrics object
        """
        events = self.get_events(start_time, end_time, event_type='agent_routing_decision')
        
        if not events:
            return RoutingMetrics(
                total_decisions=0,
                agents_selected={},
                null_selections=0,
                avg_alternatives_considered=0.0
            )
        
        # Count agents selected
        agents_selected = Counter(
            e.get('data', {}).get('decision', {}).get('selected_agent', 'null')
            for e in events
        )
        
        # Count null selections
        null_selections = agents_selected.get('null', 0)
        
        # Calculate average alternatives
        alternatives_counts = [
            len(e.get('data', {}).get('reasoning_data', {}).get('alternatives_considered', []))
            for e in events
        ]
        avg_alternatives = sum(alternatives_counts) / len(alternatives_counts) if alternatives_counts else 0.0
        
        return RoutingMetrics(
            total_decisions=len(events),
            agents_selected=dict(agents_selected),
            null_selections=null_selections,
            avg_alternatives_considered=round(avg_alternatives, 2)
        )
    
    def get_error_metrics(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> ErrorMetrics:
        """
        Get metrics for error handling
        
        Args:
            start_time: Start of time range
            end_time: End of time range
            
        Returns:
            ErrorMetrics object
        """
        events = self.get_events(start_time, end_time)
        
        # Filter error-related events
        error_events = [
            e for e in events 
            if any(keyword in e['event_type'] for keyword in ['error', 'circuit_breaker', 'retry'])
        ]
        
        if not error_events:
            return ErrorMetrics(
                total_errors=0,
                errors_by_type={},
                recovery_rate=0.0,
                circuit_breaker_trips=0,
                avg_retries_to_success=0.0
            )
        
        # Count errors by type
        error_encountered = [e for e in error_events if e['event_type'] == 'error_encountered']
        errors_by_type = Counter(
            e.get('data', {}).get('error_type', 'Unknown')
            for e in error_encountered
        )
        
        # Calculate recovery rate
        error_recovered = [e for e in error_events if e['event_type'] == 'error_recovered']
        recovery_rate = (len(error_recovered) / len(error_encountered) * 100) if error_encountered else 0.0
        
        # Count circuit breaker trips
        circuit_breaker_trips = len([
            e for e in error_events 
            if e['event_type'] == 'circuit_breaker_opened'
        ])
        
        # Calculate average retries (simple version - could be more sophisticated)
        retry_events = [e for e in error_events if e['event_type'] == 'retry_attempted']
        avg_retries = len(retry_events) / len(error_recovered) if error_recovered else 0.0
        
        return ErrorMetrics(
            total_errors=len(error_encountered),
            errors_by_type=dict(errors_by_type),
            recovery_rate=round(recovery_rate, 2),
            circuit_breaker_trips=circuit_breaker_trips,
            avg_retries_to_success=round(avg_retries, 2)
        )
    
    def detect_patterns(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        min_occurrences: int = 3
    ) -> List[Pattern]:
        """
        Detect patterns and anomalies in events
        
        Args:
            start_time: Start of time range
            end_time: End of time range
            min_occurrences: Minimum occurrences to report pattern
            
        Returns:
            List of detected patterns
        """
        events = self.get_events(start_time, end_time)
        patterns = []
        
        # Pattern 1: Repeated null agent selections
        null_routing = [
            e for e in events
            if e['event_type'] == 'agent_routing_decision'
            and e.get('data', {}).get('decision', {}).get('selected_agent') == 'null'
        ]
        
        if len(null_routing) >= min_occurrences:
            patterns.append(Pattern(
                pattern_type='routing_failure',
                description=f'Repeated null agent selections ({len(null_routing)} times)',
                occurrences=len(null_routing),
                severity='critical',
                first_seen=self._parse_timestamp(null_routing[0]['timestamp']),
                last_seen=self._parse_timestamp(null_routing[-1]['timestamp']),
                example_event_ids=[e['event_id'] for e in null_routing[:3]]
            ))
        
        # Pattern 2: Frequent review cycle escalations
        escalations = [
            e for e in events
            if e['event_type'] == 'review_cycle_escalated'
        ]
        
        if len(escalations) >= min_occurrences:
            patterns.append(Pattern(
                pattern_type='review_escalation',
                description=f'Frequent review cycle escalations ({len(escalations)} times)',
                occurrences=len(escalations),
                severity='warning',
                first_seen=self._parse_timestamp(escalations[0]['timestamp']),
                last_seen=self._parse_timestamp(escalations[-1]['timestamp']),
                example_event_ids=[e['event_id'] for e in escalations[:3]]
            ))
        
        # Pattern 3: Circuit breaker trips
        circuit_breaks = [
            e for e in events
            if e['event_type'] == 'circuit_breaker_opened'
        ]
        
        if len(circuit_breaks) >= 1:  # Even one is significant
            patterns.append(Pattern(
                pattern_type='circuit_breaker',
                description=f'Circuit breaker opened ({len(circuit_breaks)} times)',
                occurrences=len(circuit_breaks),
                severity='critical',
                first_seen=self._parse_timestamp(circuit_breaks[0]['timestamp']),
                last_seen=self._parse_timestamp(circuit_breaks[-1]['timestamp']),
                example_event_ids=[e['event_id'] for e in circuit_breaks[:3]]
            ))
        
        # Pattern 4: High error rate
        error_events = [
            e for e in events
            if 'error' in e['event_type'] and 'encountered' in e['event_type']
        ]
        
        error_rate = (len(error_events) / len(events) * 100) if events else 0.0
        
        if error_rate > 10.0:  # More than 10% errors
            patterns.append(Pattern(
                pattern_type='high_error_rate',
                description=f'High error rate: {error_rate:.1f}% of events are errors',
                occurrences=len(error_events),
                severity='critical' if error_rate > 20 else 'warning',
                first_seen=self._parse_timestamp(error_events[0]['timestamp']) if error_events else datetime.now(),
                last_seen=self._parse_timestamp(error_events[-1]['timestamp']) if error_events else datetime.now(),
                example_event_ids=[e['event_id'] for e in error_events[:3]]
            ))
        
        # Pattern 5: Feedback not acted upon
        feedback_detected = [
            e for e in events
            if e['event_type'] == 'feedback_detected'
        ]
        
        feedback_ignored = [
            e for e in events
            if e['event_type'] == 'feedback_ignored'
        ]
        
        if len(feedback_ignored) > len(feedback_detected) and len(feedback_ignored) >= min_occurrences:
            patterns.append(Pattern(
                pattern_type='feedback_ignored',
                description=f'More feedback ignored ({len(feedback_ignored)}) than acted upon ({len(feedback_detected)})',
                occurrences=len(feedback_ignored),
                severity='warning',
                first_seen=self._parse_timestamp(feedback_ignored[0]['timestamp']),
                last_seen=self._parse_timestamp(feedback_ignored[-1]['timestamp']),
                example_event_ids=[e['event_id'] for e in feedback_ignored[:3]]
            ))
        
        return patterns
    
    def identify_bottlenecks(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Identify bottlenecks in the system
        
        Args:
            start_time: Start of time range
            end_time: End of time range
            
        Returns:
            List of identified bottlenecks with details
        """
        events = self.get_events(start_time, end_time)
        bottlenecks = []
        
        # Bottleneck 1: Tasks stuck in queue
        queued = [e for e in events if e['event_type'] == 'task_queued']
        dequeued = [e for e in events if e['event_type'] == 'task_dequeued']
        
        if len(queued) > len(dequeued) * 1.5:  # 50% more queued than dequeued
            bottlenecks.append({
                'type': 'task_queue_backlog',
                'severity': 'warning',
                'description': f'{len(queued)} tasks queued but only {len(dequeued)} dequeued',
                'recommendation': 'Check task queue processing, may need more workers'
            })
        
        # Bottleneck 2: Frequent progression failures
        progression_started = [e for e in events if e['event_type'] == 'status_progression_started']
        progression_failed = [e for e in events if e['event_type'] == 'status_progression_failed']
        
        if progression_failed and len(progression_failed) / len(progression_started) > 0.2:
            bottlenecks.append({
                'type': 'status_progression_failures',
                'severity': 'critical',
                'description': f'{len(progression_failed)} / {len(progression_started)} progressions failed',
                'recommendation': 'Check GitHub API connectivity and rate limits'
            })
        
        # Bottleneck 3: Review cycles taking many iterations
        review_metrics = self.get_review_cycle_metrics(start_time, end_time)
        
        if review_metrics.avg_iterations > 2.5:
            bottlenecks.append({
                'type': 'review_cycle_iterations',
                'severity': 'warning',
                'description': f'Average {review_metrics.avg_iterations} iterations per review cycle',
                'recommendation': 'Review agent prompts and acceptance criteria'
            })
        
        # Bottleneck 4: High escalation rate
        if review_metrics.escalation_rate > 30:
            bottlenecks.append({
                'type': 'review_escalations',
                'severity': 'critical',
                'description': f'{review_metrics.escalation_rate}% of review cycles escalate',
                'recommendation': 'Adjust max iterations or improve reviewer criteria'
            })
        
        return bottlenecks
    
    def generate_report(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Generate comprehensive analytics report
        
        Args:
            start_time: Start of time range
            end_time: End of time range
            
        Returns:
            Complete analytics report as dictionary
        """
        summary = self.get_metrics_summary(start_time, end_time)
        review_metrics = self.get_review_cycle_metrics(start_time, end_time)
        routing_metrics = self.get_routing_metrics(start_time, end_time)
        error_metrics = self.get_error_metrics(start_time, end_time)
        patterns = self.detect_patterns(start_time, end_time)
        bottlenecks = self.identify_bottlenecks(start_time, end_time)
        
        return {
            'report_generated': datetime.now().isoformat(),
            'time_range': {
                'start': start_time.isoformat() if start_time else None,
                'end': end_time.isoformat() if end_time else None
            },
            'summary': asdict(summary),
            'review_cycles': asdict(review_metrics),
            'routing': asdict(routing_metrics),
            'errors': asdict(error_metrics),
            'patterns': [asdict(p) for p in patterns],
            'bottlenecks': bottlenecks,
            'health_score': self._calculate_health_score(
                summary, review_metrics, routing_metrics, error_metrics, patterns
            )
        }
    
    def _calculate_health_score(
        self,
        summary: MetricsSummary,
        review_metrics: ReviewCycleMetrics,
        routing_metrics: RoutingMetrics,
        error_metrics: ErrorMetrics,
        patterns: List[Pattern]
    ) -> Dict[str, Any]:
        """
        Calculate overall system health score
        
        Returns:
            Health score (0-100) with breakdown
        """
        score = 100.0
        deductions = []
        
        # Deduct for errors
        if error_metrics.total_errors > 0:
            error_penalty = min(30, error_metrics.total_errors * 2)
            score -= error_penalty
            deductions.append(f"Errors: -{error_penalty}")
        
        # Deduct for low success rate
        if summary.success_rate < 90:
            success_penalty = (90 - summary.success_rate) / 2
            score -= success_penalty
            deductions.append(f"Low success rate: -{success_penalty:.1f}")
        
        # Deduct for null routing
        if routing_metrics.null_selections > 0:
            null_penalty = min(20, routing_metrics.null_selections * 5)
            score -= null_penalty
            deductions.append(f"Null routing: -{null_penalty}")
        
        # Deduct for circuit breaker trips
        if error_metrics.circuit_breaker_trips > 0:
            cb_penalty = error_metrics.circuit_breaker_trips * 15
            score -= cb_penalty
            deductions.append(f"Circuit breaker: -{cb_penalty}")
        
        # Deduct for critical patterns
        critical_patterns = [p for p in patterns if p.severity == 'critical']
        if critical_patterns:
            pattern_penalty = len(critical_patterns) * 10
            score -= pattern_penalty
            deductions.append(f"Critical patterns: -{pattern_penalty}")
        
        score = max(0, score)  # Don't go below 0
        
        # Determine health status
        if score >= 90:
            status = 'excellent'
        elif score >= 75:
            status = 'good'
        elif score >= 50:
            status = 'fair'
        else:
            status = 'poor'
        
        return {
            'score': round(score, 1),
            'status': status,
            'deductions': deductions
        }
    
    def _parse_timestamp(self, timestamp_str: str) -> datetime:
        """Parse ISO format timestamp"""
        try:
            # Handle both with and without 'Z' suffix
            if timestamp_str.endswith('Z'):
                timestamp_str = timestamp_str[:-1] + '+00:00'
            return datetime.fromisoformat(timestamp_str)
        except Exception:
            return datetime.now()


# Singleton getter
_analytics_instance: Optional[DecisionAnalytics] = None


def get_decision_analytics() -> DecisionAnalytics:
    """
    Get or create global DecisionAnalytics instance
    
    Returns:
        DecisionAnalytics instance
    """
    global _analytics_instance
    
    if _analytics_instance is None:
        _analytics_instance = DecisionAnalytics()
    
    return _analytics_instance


# CLI for testing
if __name__ == '__main__':
    import sys
    
    analytics = get_decision_analytics()
    
    # Get last hour
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=1)
    
    print("=== Decision Analytics Report ===")
    print(f"Time Range: {start_time} to {end_time}")
    print()
    
    # Get summary
    summary = analytics.get_metrics_summary(start_time, end_time)
    print(f"Total Events: {summary.total_events}")
    print(f"Success Rate: {summary.success_rate}%")
    print(f"Error Count: {summary.error_count}")
    print(f"Events/minute: {summary.avg_events_per_minute}")
    print()
    
    # Get patterns
    patterns = analytics.detect_patterns(start_time, end_time)
    if patterns:
        print(f"=== Patterns Detected: {len(patterns)} ===")
        for pattern in patterns:
            print(f"- [{pattern.severity.upper()}] {pattern.description}")
        print()
    
    # Get bottlenecks
    bottlenecks = analytics.identify_bottlenecks(start_time, end_time)
    if bottlenecks:
        print(f"=== Bottlenecks: {len(bottlenecks)} ===")
        for bottleneck in bottlenecks:
            print(f"- [{bottleneck['severity'].upper()}] {bottleneck['description']}")
            print(f"  Recommendation: {bottleneck['recommendation']}")
        print()
    
    # Generate full report
    report = analytics.generate_report(start_time, end_time)
    print(f"=== Health Score: {report['health_score']['score']}/100 ({report['health_score']['status'].upper()}) ===")
    if report['health_score']['deductions']:
        print("Deductions:")
        for deduction in report['health_score']['deductions']:
            print(f"  - {deduction}")
