"""
Centralized Metrics Collection for Architectural Observability.

This module provides a unified metrics layer for tracking key system behaviors:
- Multi-tool execution rates
- Agent switching events
- Plan compliance verification
- Hybrid flow triggers
- Tool success/failure rates
- Execution latencies

Architecture Position:
    All components (Orchestrator, Agents, Multi-Tool Orchestrator) → MetricsCollector → [Storage/Export]

Integration Points:
1. Langfuse: Metrics are attached as metadata to spans for unified observability
2. Logging: Structured logs capture all metric events for analysis
3. Export: Optional Prometheus/StatsD integration point (future)

Usage:
    from utilities.metrics import get_metrics_collector, MetricType
    
    metrics = get_metrics_collector()
    metrics.increment("orchestrator.query.total")
    metrics.increment("agent.multi_tool.executed", {"steps": "3"})
    metrics.record("tool.execution.latency_ms", 250, {"tool": "execute_wiql"})
"""

import time
import logging
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Callable
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger("utilities.metrics")


class MetricType(Enum):
    """Types of metrics supported."""
    COUNTER = "counter"      # Monotonically increasing count
    GAUGE = "gauge"          # Current value (can go up/down)
    HISTOGRAM = "histogram"  # Distribution of values
    TIMER = "timer"          # Duration measurements


@dataclass
class Metric:
    """Single metric data point."""
    name: str
    type: MetricType
    value: float
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Langfuse/logging."""
        return {
            "name": self.name,
            "type": self.type.value,
            "value": self.value,
            "labels": self.labels,
            "timestamp": self.timestamp
        }


@dataclass
class MetricSummary:
    """Aggregated metric summary for a session/request."""
    total_queries: int = 0
    multi_tool_count: int = 0
    single_tool_count: int = 0
    hybrid_flow_count: int = 0
    agent_switches: int = 0
    replan_count: int = 0
    fixed_skill_count: int = 0
    plan_compliance_success: int = 0
    plan_compliance_violations: int = 0
    tool_executions: int = 0
    tool_failures: int = 0
    validation_failures: int = 0
    total_latency_ms: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for reporting."""
        return {
            "total_queries": self.total_queries,
            "multi_tool_rate": (self.multi_tool_count / max(1, self.total_queries)) * 100,
            "hybrid_flow_rate": (self.hybrid_flow_count / max(1, self.total_queries)) * 100,
            "agent_switch_rate": (self.agent_switches / max(1, self.total_queries)) * 100,
            "replan_rate": (self.replan_count / max(1, self.total_queries)) * 100,
            "fixed_skill_rate": (self.fixed_skill_count / max(1, self.total_queries)) * 100,
            "plan_compliance_rate": (self.plan_compliance_success / max(1, self.plan_compliance_success + self.plan_compliance_violations)) * 100,
            "tool_success_rate": ((self.tool_executions - self.tool_failures) / max(1, self.tool_executions)) * 100,
            "avg_latency_ms": self.total_latency_ms / max(1, self.total_queries),
            "validation_failure_rate": (self.validation_failures / max(1, self.tool_executions)) * 100
        }


class MetricsCollector:
    """
    Centralized metrics collection for architectural observability.
    
    Thread-safe singleton that collects metrics from all system components.
    Metrics are logged structurally and can be attached to Langfuse spans.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._metrics: List[Metric] = []
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._session_summaries: Dict[str, MetricSummary] = {}
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[Metric], None]] = []
        self._initialized = True
        
        logger.info("[METRICS] MetricsCollector initialized")
    
    def increment(self, name: str, labels: Optional[Dict[str, str]] = None, value: float = 1.0) -> None:
        """
        Increment a counter metric.
        
        Args:
            name: Metric name (e.g., "orchestrator.query.total")
            labels: Optional labels for dimensionality (e.g., {"agent": "pm_agent"})
            value: Amount to increment by (default 1.0)
        """
        labels = labels or {}
        
        with self._lock:
            # Create composite key for labeled counters
            key = self._make_key(name, labels)
            self._counters[key] += value
            
            # Create metric record
            metric = Metric(
                name=name,
                type=MetricType.COUNTER,
                value=self._counters[key],
                labels=labels
            )
            self._metrics.append(metric)
        
        # Log for observability
        labels_str = ",".join(f"{k}={v}" for k, v in labels.items()) if labels else ""
        logger.debug(f"[METRIC] {name} +{value} [{labels_str}] = {self._counters[key]}")
        
        # Fire callbacks
        self._fire_callbacks(metric)
    
    def record(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """
        Record a gauge or histogram value.
        
        Args:
            name: Metric name (e.g., "tool.execution.latency_ms")
            value: Value to record
            labels: Optional labels for dimensionality
        """
        labels = labels or {}
        
        with self._lock:
            key = self._make_key(name, labels)
            self._gauges[key] = value
            self._histograms[name].append(value)
            
            metric = Metric(
                name=name,
                type=MetricType.GAUGE,
                value=value,
                labels=labels
            )
            self._metrics.append(metric)
        
        labels_str = ",".join(f"{k}={v}" for k, v in labels.items()) if labels else ""
        logger.debug(f"[METRIC] {name} = {value} [{labels_str}]")
        
        self._fire_callbacks(metric)
    
    def timer_start(self, name: str) -> float:
        """
        Start a timer and return the start time.
        
        Args:
            name: Timer metric name
            
        Returns:
            Start timestamp (use with timer_stop)
        """
        return time.time()
    
    def timer_stop(self, name: str, start_time: float, labels: Optional[Dict[str, str]] = None) -> float:
        """
        Stop a timer and record the duration.
        
        Args:
            name: Timer metric name
            start_time: Start time from timer_start()
            labels: Optional labels
            
        Returns:
            Duration in milliseconds
        """
        duration_ms = (time.time() - start_time) * 1000
        self.record(f"{name}.latency_ms", duration_ms, labels)
        return duration_ms
    
    def update_session_summary(self, session_id: str, **updates) -> None:
        """
        Update session-level summary metrics.
        
        Args:
            session_id: Session identifier
            **updates: Metric updates (e.g., total_queries=1, multi_tool_count=1)
        """
        with self._lock:
            if session_id not in self._session_summaries:
                self._session_summaries[session_id] = MetricSummary()
            
            summary = self._session_summaries[session_id]
            for key, value in updates.items():
                if hasattr(summary, key):
                    current = getattr(summary, key)
                    if isinstance(current, (int, float)):
                        setattr(summary, key, current + value)
    
    def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get summary metrics for a session."""
        with self._lock:
            summary = self._session_summaries.get(session_id)
            return summary.to_dict() if summary else None
    
    def get_counter(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        """Get current counter value."""
        key = self._make_key(name, labels or {})
        return self._counters.get(key, 0.0)
    
    def get_histogram_stats(self, name: str) -> Dict[str, float]:
        """Get histogram statistics (min, max, avg, p50, p95, p99)."""
        values = self._histograms.get(name, [])
        if not values:
            return {}
        
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        
        return {
            "count": n,
            "min": sorted_vals[0],
            "max": sorted_vals[-1],
            "avg": sum(values) / n,
            "p50": sorted_vals[int(n * 0.5)] if n > 0 else 0,
            "p95": sorted_vals[int(n * 0.95)] if n > 0 else 0,
            "p99": sorted_vals[int(n * 0.99)] if n > 0 else 0
        }
    
    def get_recent_metrics(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent metrics as list of dicts."""
        with self._lock:
            return [m.to_dict() for m in self._metrics[-limit:]]
    
    def attach_to_langfuse_span(self, span: Any, prefix: str = "") -> None:
        """
        Attach relevant metrics as metadata to a Langfuse span.
        
        Args:
            span: Langfuse span object
            prefix: Optional prefix for metric names
        """
        if not span:
            return
        
        try:
            metrics_data = {}
            
            # Include key counters
            for key, value in self._counters.items():
                metric_name = f"{prefix}{key}" if prefix else key
                metrics_data[metric_name] = value
            
            # Include recent histogram stats
            for name in self._histograms:
                stats = self.get_histogram_stats(name)
                if stats:
                    for stat_name, stat_value in stats.items():
                        metric_name = f"{prefix}{name}.{stat_name}" if prefix else f"{name}.{stat_name}"
                        metrics_data[metric_name] = stat_value
            
            if hasattr(span, 'update') and callable(span.update):
                span.update(metadata={"metrics": metrics_data})
            
        except Exception as e:
            logger.debug(f"[METRICS] Failed to attach to Langfuse span: {e}")
    
    def add_callback(self, callback: Callable[[Metric], None]) -> None:
        """Add a callback for metric events (e.g., for external export)."""
        self._callbacks.append(callback)
    
    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        with self._lock:
            self._metrics.clear()
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._session_summaries.clear()
        logger.info("[METRICS] All metrics reset")
    
    def _make_key(self, name: str, labels: Dict[str, str]) -> str:
        """Create composite key from name and labels."""
        if not labels:
            return name
        sorted_labels = sorted(labels.items())
        labels_str = ",".join(f"{k}={v}" for k, v in sorted_labels)
        return f"{name}{{{labels_str}}}"
    
    def _fire_callbacks(self, metric: Metric) -> None:
        """Fire all registered callbacks."""
        for callback in self._callbacks:
            try:
                callback(metric)
            except Exception as e:
                logger.debug(f"[METRICS] Callback error: {e}")


# Singleton instance
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    """
    Get the singleton MetricsCollector instance.
    
    Returns:
        MetricsCollector instance
    """
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS - For direct import without accessing the collector
# ═══════════════════════════════════════════════════════════════════════════════

def increment(name: str, labels: Optional[Dict[str, str]] = None, value: float = 1.0) -> None:
    """Increment a counter metric."""
    get_metrics_collector().increment(name, labels, value)


def record(name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
    """Record a gauge/histogram value."""
    get_metrics_collector().record(name, value, labels)


def timer_start(name: str) -> float:
    """Start a timer."""
    return get_metrics_collector().timer_start(name)


def timer_stop(name: str, start_time: float, labels: Optional[Dict[str, str]] = None) -> float:
    """Stop a timer and record duration."""
    return get_metrics_collector().timer_stop(name, start_time, labels)


def update_session(session_id: str, **updates) -> None:
    """Update session summary metrics."""
    get_metrics_collector().update_session_summary(session_id, **updates)


# ═══════════════════════════════════════════════════════════════════════════════
# STANDARD METRIC NAMES - Centralized definitions for consistency
# ═══════════════════════════════════════════════════════════════════════════════

class MetricNames:
    """Standard metric names for the system."""
    
    # Orchestrator metrics
    ORCHESTRATOR_QUERY_TOTAL = "orchestrator.query.total"
    ORCHESTRATOR_HYBRID_FLOW = "orchestrator.hybrid_flow.triggered"
    ORCHESTRATOR_AGENT_SWITCH = "orchestrator.agent_switch"
    ORCHESTRATOR_REPLAN = "orchestrator.replan.triggered"
    ORCHESTRATOR_LIGHT_LLM = "orchestrator.light_llm.invoked"
    
    # Agent metrics
    AGENT_INVOCATION = "agent.invocation"
    AGENT_SUCCESS = "agent.success"
    AGENT_FAILURE = "agent.failure"
    AGENT_MULTI_TOOL = "agent.multi_tool.executed"
    AGENT_SINGLE_TOOL = "agent.single_tool.executed"
    AGENT_FIXED_SKILL = "agent.fixed_skill.used"
    AGENT_PLAN_COMPLIANCE_SUCCESS = "agent.plan_compliance.success"
    AGENT_PLAN_COMPLIANCE_VIOLATION = "agent.plan_compliance.violation"
    AGENT_PLAN_OVERRIDE = "agent.plan_override"
    AGENT_SELF_CORRECTION = "agent.self_correction"
    AGENT_ESCALATION = "agent.escalation.requested"
    
    # Validation metrics
    VALIDATION_FAILURE = "validation.failure"
    VALIDATION_INTENT_ALIGNMENT = "validation.intent_alignment"
    VALIDATION_INTENT_MISMATCH = "validation.intent_mismatch"
    VALIDATION_RESULT_INTENT_CHECK = "validation.result.intent_check"
    
    # Tool metrics
    TOOL_EXECUTION_TOTAL = "tool.execution.total"
    TOOL_EXECUTION_SUCCESS = "tool.execution.success"
    TOOL_EXECUTION_FAILED = "tool.execution.failed"
    TOOL_EXECUTION_LATENCY = "tool.execution"  # .latency_ms added automatically
    TOOL_VALIDATION_FAILED = "tool.validation.failed"
    TOOL_RESULT_INVALID = "tool.result.invalid"
    
    # Multi-tool orchestrator metrics
    MULTI_TOOL_PLAN_CREATED = "multi_tool.plan.created"
    MULTI_TOOL_STEP_SUCCESS = "multi_tool.step.success"
    MULTI_TOOL_STEP_FAILED = "multi_tool.step.failed"
    MULTI_TOOL_STEP_SKIPPED = "multi_tool.step.skipped"
    MULTI_TOOL_PARALLEL_BATCH = "multi_tool.parallel_batch.executed"
    MULTI_TOOL_ROLLBACK = "multi_tool.rollback.triggered"
    MULTI_TOOL_CONTEXT_RESOLUTION = "multi_tool.context.resolved"
