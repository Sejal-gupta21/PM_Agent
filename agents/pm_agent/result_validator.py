"""
Modern Validator Architecture - Unified Intent-Plan-Result Alignment Engine.

Implements a structured validation pipeline with strict retry feedback loop
and circuit breaker enforcement for the PM Agent.

Validation Stages:
    A. Intent-Plan Alignment  -> Does the plan represent user intent?
    B. Plan Structural         -> Is the plan executable (no placeholders, all params)?
    C. Execution Result        -> Do results satisfy planned constraints?
    D. Retry Feedback Loop     -> Max 3 attempts with structured feedback to planner
    E. Circuit Breaker         -> Hard stop after 3 failures, no silent fallback

Architecture Position:
    Intent Extraction -> Plan Generation -> [ValidationOrchestrator] -> Execution -> Synthesis

All stages are fully dynamic (no hardcoded queries, no pattern matching, no static routing).
All stages emit structured Langfuse traces for full observability.

Usage:
    from agents.pm_agent.result_validator import (
        ValidationOrchestrator,
        IntentRepresentation,
        PlanValidationResult,
        ExecutionValidationResult,
        CircuitBreakerError,
    )

    orchestrator = ValidationOrchestrator(tools_cache=mcp_tools, mcp_connector=connector)

    # Pre-execution: validate plan against intent
    plan_result = orchestrator.validate_plan(query, plan)
    if not plan_result.is_valid:
        feedback = plan_result.feedback  # structured feedback for planner

    # Post-execution: validate results
    exec_result = orchestrator.validate_execution(query, plan, tool_result)

    # Full retry loop (called from agent.py)
    final_plan = await orchestrator.validate_with_retry(query, plan, plan_generator_fn)
"""

import json
import logging
import re
import time
from typing import Dict, Any, Optional, List, Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("agents.pm_agent.result_validator")


# ==============================================================================
# ENUMS & DATA CLASSES
# ==============================================================================

class ValidationSeverity(Enum):
    """Severity levels for validation issues."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationStage(Enum):
    """Stages of the validation pipeline."""
    INTENT_PLAN_ALIGNMENT = "intent_plan_alignment"
    PLAN_STRUCTURAL = "plan_structural"
    EXECUTION_RESULT = "execution_result"


@dataclass
class ValidationIssue:
    """Single validation issue found during any stage."""
    severity: ValidationSeverity
    stage: ValidationStage
    code: str
    message: str
    field: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity.value,
            "stage": self.stage.value,
            "code": self.code,
            "message": self.message,
            "field": self.field,
        }


@dataclass
class IntentRepresentation:
    """
    Structured representation of user intent extracted from query.

    Fully dynamic -- no hardcoded entity types, sprint formats, or ratio patterns.
    Captures the structural properties of the intent that the plan must satisfy.
    """
    raw_query: str
    # Extracted entities the plan must address
    entities: Dict[str, Any] = field(default_factory=dict)
    # Required constraints (filters, scopes) from the query
    constraints: List[Dict[str, str]] = field(default_factory=list)
    # Expected output characteristics (aggregation, list, single item, etc.)
    expected_output_type: Optional[str] = None
    # Whether the query implies a specific scope (team, area, sprint)
    requires_scope: bool = False
    scope_details: Dict[str, Any] = field(default_factory=dict)
    # Whether identity resolution is needed
    requires_identity: bool = False
    identity_hints: List[str] = field(default_factory=list)
    # Whether the query implies temporal constraints
    requires_temporal: bool = False
    temporal_details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_query": self.raw_query[:200],
            "entities": self.entities,
            "constraints": self.constraints,
            "expected_output_type": self.expected_output_type,
            "requires_scope": self.requires_scope,
            "scope_details": self.scope_details,
            "requires_identity": self.requires_identity,
            "identity_hints": self.identity_hints,
            "requires_temporal": self.requires_temporal,
            "temporal_details": self.temporal_details,
        }


@dataclass
class PlanValidationResult:
    """Result of pre-execution plan validation (stages A + B)."""
    is_valid: bool
    stage: ValidationStage
    issues: List[ValidationIssue] = field(default_factory=list)
    feedback: Optional[Dict[str, Any]] = None
    sanitized_plan: Optional[Dict[str, Any]] = None
    intent: Optional[IntentRepresentation] = None
    alignment_score: float = 1.0
    retry_hint: Optional[str] = None
    # Backward-compat fields expected by agent.py
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    intent_alignment: Optional[Dict[str, Any]] = None

    @property
    def error_message(self) -> Optional[str]:
        errors = [i for i in self.issues if i.severity == ValidationSeverity.ERROR]
        if not errors:
            return self.error
        return "; ".join(e.message for e in errors)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "stage": self.stage.value,
            "issues": [i.to_dict() for i in self.issues],
            "alignment_score": self.alignment_score,
            "error_message": self.error_message,
            "retry_hint": self.retry_hint,
        }


@dataclass
class ExecutionValidationResult:
    """Result of post-execution result validation (stage C)."""
    is_valid: bool
    tool_name: str
    issues: List[ValidationIssue] = field(default_factory=list)
    sanitized_result: Optional[Any] = None
    result_count: int = 0
    feedback: Optional[Dict[str, Any]] = None

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == ValidationSeverity.ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == ValidationSeverity.WARNING]

    @property
    def info(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == ValidationSeverity.INFO]

    @property
    def error_message(self) -> Optional[str]:
        errs = self.errors
        if not errs:
            return None
        return "; ".join(e.message for e in errs)

    @property
    def warning_messages(self) -> List[str]:
        return [w.message for w in self.warnings]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "tool_name": self.tool_name,
            "issues": [i.to_dict() for i in self.issues],
            "result_count": self.result_count,
            "error_message": self.error_message,
        }


class CircuitBreakerError(Exception):
    """Raised when circuit breaker activates after max retries."""

    def __init__(self, attempts: int, last_feedback: Dict[str, Any]):
        self.attempts = attempts
        self.last_feedback = last_feedback
        super().__init__(
            f"Plan validation failed after {attempts} attempts. "
            f"The generated plan does not align with user intent."
        )


# ==============================================================================
# INTENT EXTRACTOR
# ==============================================================================

class IntentExtractor:
    """
    Extracts a structured IntentRepresentation from a user query.

    Uses the existing analyze_query_intent infrastructure from
    query_aware_filter for filter/constraint detection, plus additional
    structural analysis for scope, identity, and temporal requirements.

    Fully dynamic -- no hardcoded queries, no example-based handling.
    """

    def extract(self, query: str) -> IntentRepresentation:
        """Extract structured intent from a natural language query."""
        intent = IntentRepresentation(raw_query=query)

        # -- Use existing query_aware_filter for constraint detection --
        try:
            from agents.pm_agent.query_aware_filter import analyze_query_intent
            qaf_intent = analyze_query_intent(query)

            if qaf_intent.filter_by_types:
                intent.constraints.append({
                    "type": "work_item_type",
                    "values": qaf_intent.filter_by_types,
                })
                intent.entities["work_item_types"] = qaf_intent.filter_by_types

            if qaf_intent.filter_by_states:
                intent.constraints.append({
                    "type": "state",
                    "values": qaf_intent.filter_by_states,
                })
                intent.entities["states"] = qaf_intent.filter_by_states

            if qaf_intent.filter_blocked:
                intent.constraints.append({"type": "tag_filter", "values": ["Blocked"]})
            if qaf_intent.filter_at_risk:
                intent.constraints.append({"type": "risk_filter", "values": ["at_risk"]})
            if qaf_intent.filter_unassigned:
                intent.constraints.append({"type": "assignment_filter", "values": ["unassigned"]})
            if qaf_intent.filter_customer_impacting:
                intent.constraints.append({"type": "priority_filter", "values": ["customer_impacting"]})
            if qaf_intent.filter_stale:
                intent.constraints.append({"type": "staleness_filter", "values": ["stale"]})

            if getattr(qaf_intent, "expects_aggregation", False):
                intent.expected_output_type = "aggregation"
            elif getattr(qaf_intent, "expects_list", True):
                intent.expected_output_type = "list"
            elif getattr(qaf_intent, "expects_single", False):
                intent.expected_output_type = "single_item"
            else:
                intent.expected_output_type = "list"

        except (ImportError, AttributeError):
            intent.expected_output_type = "list"

        # -- Scope detection --
        self._detect_scope(query, intent)
        # -- Identity detection --
        self._detect_identity_requirement(query, intent)
        # -- Temporal detection --
        self._detect_temporal_requirement(query, intent)

        return intent

    @staticmethod
    def _detect_scope(query: str, intent: IntentRepresentation) -> None:
        """Detect if query implies a specific team/area/sprint scope."""
        import re

        sprint_indicators = re.findall(
            r'\b(?:sprint|iteration|current\s+sprint|this\s+sprint|'
            r'previous\s+sprint|last\s+sprint|next\s+sprint)\b',
            query, re.IGNORECASE,
        )
        if sprint_indicators:
            intent.requires_scope = True
            intent.scope_details["iteration"] = True

        _SKIP = {
            "the", "this", "that", "all", "my", "our", "some", "any",
            "each", "current", "last", "next", "previous", "active",
            "new", "open", "every", "a", "an",
        }
        team_match = re.search(
            r'\b(?:for|in|under|within)\s+'
            r'((?:[A-Z][A-Za-z0-9]*(?:\s+(?:[A-Z0-9][A-Za-z0-9]*|\d+))*))',
            query,
        )
        if team_match:
            candidate = team_match.group(1).strip()
            if candidate.split()[0].lower() not in _SKIP:
                intent.requires_scope = True
                intent.scope_details["team_or_area"] = candidate

    @staticmethod
    def _detect_identity_requirement(query: str, intent: IntentRepresentation) -> None:
        """Detect if query references a person / identity."""
        import re
        person_patterns = [
            r"(?:assigned\s+to|created\s+by|modified\s+by|resolved\s+by)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)'s\s+(?:items?|bugs?|tasks?|stories?|work)",
        ]
        for pattern in person_patterns:
            m = re.search(pattern, query)
            if m:
                intent.requires_identity = True
                intent.identity_hints.append(m.group(1).strip())

    @staticmethod
    def _detect_temporal_requirement(query: str, intent: IntentRepresentation) -> None:
        """Detect if query implies a time window."""
        import re
        temporal_match = re.search(
            r'\b(?:last|past|recent|within)\s+(\d+)\s+(days?|weeks?|months?|hours?)\b',
            query, re.IGNORECASE,
        )
        if temporal_match:
            intent.requires_temporal = True
            intent.temporal_details["quantity"] = int(temporal_match.group(1))
            intent.temporal_details["unit"] = temporal_match.group(2).lower().rstrip("s")

        if re.search(r'\b(?:today|yesterday|this\s+week|this\s+month|since)\b', query, re.IGNORECASE):
            intent.requires_temporal = True
            intent.temporal_details["relative"] = True


# ==============================================================================
# STAGE A: INTENT-PLAN ALIGNMENT VALIDATION
# ==============================================================================

class IntentPlanAlignmentValidator:
    """
    Validates whether a generated plan structurally represents user intent.

    Checks:
    - Tool selection appropriateness for the intent
    - Constraint coverage (all user constraints reflected in plan args/criteria)
    - Scope coverage (team, area, sprint if required by intent)
    - Identity coverage (person names resolved if required)
    - Temporal coverage (date filters if required)

    Fully dynamic -- no hardcoded tool lists or query patterns.
    """

    def validate(
        self,
        intent: IntentRepresentation,
        plan: Dict[str, Any],
        tools_cache: Dict[str, Any],
    ) -> PlanValidationResult:
        """Validate plan alignment with intent."""
        result = PlanValidationResult(
            is_valid=True,
            stage=ValidationStage.INTENT_PLAN_ALIGNMENT,
            intent=intent,
        )

        tool = plan.get("tool", "")
        args = plan.get("args", {}) or {}
        analysis_criteria = plan.get("analysis_criteria", {}) or {}
        action = plan.get("action", "")

        if action in ("ask_clarification", "no_tool", "context_data_answer"):
            result.alignment_score = 1.0
            result.sanitized_plan = plan
            return result

        score = 1.0
        feedback_items = []

        # -- Check 1: Tool exists --
        if not tool:
            result.is_valid = False
            result.issues.append(ValidationIssue(
                severity=ValidationSeverity.ERROR,
                stage=ValidationStage.INTENT_PLAN_ALIGNMENT,
                code="NO_TOOL",
                message="Plan has no tool specified",
            ))
            result.alignment_score = 0.0
            result.feedback = {"reason": "no_tool", "items": ["Plan must specify a tool"]}
            return result

        # -- Check 2: Constraint coverage --
        for constraint in intent.constraints:
            c_type = constraint.get("type", "")
            c_values = constraint.get("values", [])
            covered = self._is_constraint_covered(c_type, c_values, args, analysis_criteria, tool)
            if not covered:
                score -= 0.15
                msg = (
                    f"Intent constraint '{c_type}' with values {c_values} is not "
                    f"reflected in plan args or analysis_criteria"
                )
                result.issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    stage=ValidationStage.INTENT_PLAN_ALIGNMENT,
                    code="CONSTRAINT_NOT_COVERED",
                    message=msg,
                ))
                feedback_items.append(msg)

        # -- Check 3: Scope coverage --
        if intent.requires_scope:
            scope_covered = self._is_scope_covered(intent.scope_details, args, analysis_criteria)
            if not scope_covered:
                score -= 0.2
                msg = (
                    f"Intent requires scope {intent.scope_details} but plan "
                    f"does not include corresponding scope parameters"
                )
                result.issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    stage=ValidationStage.INTENT_PLAN_ALIGNMENT,
                    code="SCOPE_NOT_COVERED",
                    message=msg,
                ))
                feedback_items.append(msg)

        # -- Check 4: Identity coverage --
        if intent.requires_identity and intent.identity_hints:
            identity_covered = self._is_identity_covered(intent.identity_hints, args)
            if not identity_covered:
                score -= 0.15
                msg = (
                    f"Intent references person(s) {intent.identity_hints} but plan "
                    f"does not include assignee/person parameters"
                )
                result.issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    stage=ValidationStage.INTENT_PLAN_ALIGNMENT,
                    code="IDENTITY_NOT_COVERED",
                    message=msg,
                ))
                feedback_items.append(msg)

        # -- Check 5: Temporal coverage --
        if intent.requires_temporal:
            temporal_covered = self._is_temporal_covered(intent.temporal_details, args, tool)
            if not temporal_covered:
                score -= 0.15
                msg = (
                    f"Intent implies temporal constraint {intent.temporal_details} "
                    f"but plan does not include date filtering"
                )
                result.issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    stage=ValidationStage.INTENT_PLAN_ALIGNMENT,
                    code="TEMPORAL_NOT_COVERED",
                    message=msg,
                ))
                feedback_items.append(msg)

        # -- Finalize --
        result.alignment_score = max(0.0, min(1.0, score))

        if result.alignment_score < 0.4:
            result.is_valid = False
            result.retry_hint = "Plan has significant alignment gaps with user intent"
            result.feedback = {
                "reason": "low_alignment_score",
                "score": result.alignment_score,
                "items": feedback_items,
                "intent_summary": intent.to_dict(),
            }
        elif feedback_items:
            result.feedback = {
                "reason": "partial_alignment",
                "score": result.alignment_score,
                "items": feedback_items,
            }

        result.sanitized_plan = plan
        return result

    @staticmethod
    def _is_constraint_covered(
        c_type: str,
        c_values: List[str],
        args: Dict[str, Any],
        analysis_criteria: Dict[str, Any],
        tool: str,
    ) -> bool:
        """Check if a constraint from intent is covered in plan args or analysis_criteria."""
        args_str = json.dumps(args, default=str).lower()
        criteria_str = json.dumps(analysis_criteria, default=str).lower()
        combined = args_str + " " + criteria_str

        for val in c_values:
            if val.lower() in combined:
                return True

        type_to_args = {
            "work_item_type": ["workItemType", "workitemtype", "type"],
            "state": ["state", "status", "states"],
            "tag_filter": ["tags", "tag"],
            "assignment_filter": ["assignedTo", "assigned_to"],
            "priority_filter": ["priority", "severity"],
        }
        arg_names = type_to_args.get(c_type, [])
        for arg_name in arg_names:
            if arg_name in args and args[arg_name]:
                return True

        wiql = args.get("wiql", "") or args.get("query", "")
        if wiql:
            wiql_lower = wiql.lower()
            for val in c_values:
                if val.lower() in wiql_lower:
                    return True

        filter_logic = analysis_criteria.get("filter_logic", "")
        if filter_logic:
            for val in c_values:
                if val.lower() in filter_logic.lower():
                    return True

        return False

    @staticmethod
    def _is_scope_covered(
        scope_details: Dict[str, Any],
        args: Dict[str, Any],
        analysis_criteria: Dict[str, Any],
    ) -> bool:
        """Check if scope requirements are covered in plan."""
        if scope_details.get("iteration"):
            has_iter = any(
                k in args and args[k]
                for k in ("iterationId", "iterationPath", "iteration", "timeframe")
            )
            wiql = args.get("wiql", "") or args.get("query", "")
            has_iter_wiql = "iterationpath" in wiql.lower() if wiql else False
            if not has_iter and not has_iter_wiql:
                return False

        if scope_details.get("team_or_area"):
            has_scope = any(
                k in args and args[k]
                for k in ("team", "areaPath", "area_path")
            )
            wiql = args.get("wiql", "") or args.get("query", "")
            scope_name = scope_details["team_or_area"].lower()
            has_scope_wiql = scope_name in wiql.lower() if wiql else False
            if not has_scope and not has_scope_wiql:
                return False

        return True

    @staticmethod
    def _is_identity_covered(identity_hints: List[str], args: Dict[str, Any]) -> bool:
        """Check if identity references are covered in plan."""
        person_args = ["assignedTo", "createdBy", "author", "searchFilter", "assignee"]
        for arg_name in person_args:
            if arg_name in args and args[arg_name]:
                return True

        wiql = args.get("wiql", "") or args.get("query", "")
        if wiql:
            wiql_lower = wiql.lower()
            for hint in identity_hints:
                if hint.lower() in wiql_lower:
                    return True

        search = args.get("searchText", "")
        if search:
            for hint in identity_hints:
                if hint.lower() in search.lower():
                    return True

        return False

    @staticmethod
    def _is_temporal_covered(
        temporal_details: Dict[str, Any],
        args: Dict[str, Any],
        tool: str,
    ) -> bool:
        """Check if temporal constraints are covered in plan."""
        wiql = args.get("wiql", "") or args.get("query", "")
        if wiql:
            wiql_lower = wiql.lower()
            date_tokens = [
                "createddate", "changeddate", "closeddate",
                "resolveddate", "@today", "startdate", "finishdate",
            ]
            if any(t in wiql_lower for t in date_tokens):
                return True

        if any(k in args for k in ("iterationId", "iterationPath", "timeframe")):
            return True

        return False


# ==============================================================================
# STAGE B: PLAN STRUCTURAL VALIDATION
# ==============================================================================

class PlanStructuralValidator:
    """
    Validates that a plan is structurally executable.

    Delegates to the existing PlanValidator from validation.py for:
    - Tool existence in MCP tools cache
    - Required argument presence
    - Confidence threshold
    - Argument type validation

    Adds additional checks:
    - Unresolved placeholder detection
    - Inconsistent cross-references
    """

    PLACEHOLDER_PATTERNS = [
        "{{", "}}", "placeholder", "tbd",
        "todo", "unknown", "n/a", "xxx",
    ]

    # Regex for template-style placeholders like <INSERT_HERE>, <PLACEHOLDER>, <TBD>
    # Excludes WIQL comparison operators (>=, <=, <>, etc.) and HTML-like tags
    _TEMPLATE_RE = re.compile(r"<[A-Z_]{3,}>", re.IGNORECASE)

    def validate(
        self,
        plan: Dict[str, Any],
        tools_cache: Dict[str, Any],
        query: Optional[str] = None,
        mcp_connector: Optional[Any] = None,
    ) -> PlanValidationResult:
        """Validate plan structure using existing PlanValidator + additional checks."""
        result = PlanValidationResult(
            is_valid=True,
            stage=ValidationStage.PLAN_STRUCTURAL,
        )

        action = plan.get("action", "")
        if action in ("ask_clarification", "no_tool", "context_data_answer"):
            result.sanitized_plan = plan
            return result

        # -- Delegate to existing PlanValidator --
        try:
            from agents.pm_agent.validation import validate_plan as _legacy_validate
            legacy_result = _legacy_validate(plan, tools_cache, mcp_connector=mcp_connector, query=query)

            if not legacy_result.is_valid:
                result.is_valid = False
                result.error = legacy_result.error
                result.issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.PLAN_STRUCTURAL,
                    code="STRUCTURAL_INVALID",
                    message=legacy_result.error or "Plan structural validation failed",
                ))
                result.feedback = {
                    "reason": "structural_failure",
                    "error": legacy_result.error,
                }
                return result

            if legacy_result.warnings:
                result.warnings = list(legacy_result.warnings)
                for w in legacy_result.warnings:
                    result.issues.append(ValidationIssue(
                        severity=ValidationSeverity.WARNING,
                        stage=ValidationStage.PLAN_STRUCTURAL,
                        code="STRUCTURAL_WARNING",
                        message=w,
                    ))

            result.sanitized_plan = legacy_result.sanitized_plan or plan

            if legacy_result.intent_alignment:
                result.intent_alignment = legacy_result.intent_alignment
                result.alignment_score = legacy_result.intent_alignment.get("alignment_score", 1.0)

        except ImportError:
            logger.warning("[VALIDATOR] Legacy PlanValidator not available, using minimal structural check")
            result.sanitized_plan = plan

        # -- Additional: Placeholder detection --
        args = (result.sanitized_plan or plan).get("args", {}) or {}
        self._check_placeholders(args, result)

        return result

    def _check_placeholders(self, args: Dict[str, Any], result: PlanValidationResult) -> None:
        """Detect unresolved placeholder values in arguments."""
        for key, value in args.items():
            if value is None:
                continue
            val_str = str(value).lower().strip()
            is_placeholder = False

            # Check keyword patterns (e.g., "placeholder", "tbd", "todo")
            for pattern in self.PLACEHOLDER_PATTERNS:
                if pattern in val_str:
                    is_placeholder = True
                    break

            # Check template-style patterns: <INSERT_HERE>, <PLACEHOLDER>, etc.
            if not is_placeholder and self._TEMPLATE_RE.search(str(value)):
                is_placeholder = True

            if is_placeholder:
                result.issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.PLAN_STRUCTURAL,
                    code="UNRESOLVED_PLACEHOLDER",
                    message=f"Argument '{key}' contains placeholder value: {value}",
                    field=key,
                ))
                result.is_valid = False
                if not result.feedback:
                    result.feedback = {"reason": "unresolved_placeholders", "fields": []}
                result.feedback.setdefault("fields", []).append(key)


# ==============================================================================
# STAGE C: EXECUTION RESULT VALIDATION
# ==============================================================================

class ExecutionResultValidator:
    """
    Validates that tool execution results satisfy the planned constraints.

    Checks:
    - Result structure (dict, has expected fields)
    - Success/failure flags
    - Data presence (items, count)
    - Suspicious empty results when data is expected
    - Planner JSON leak detection
    - Result-intent coherence (scope, types, dates)

    Fully dynamic -- no tool-specific hardcoded logic.
    """

    def validate(
        self,
        tool_name: str,
        result: Any,
        query: str = "",
        plan: Optional[Dict[str, Any]] = None,
        intent: Optional[IntentRepresentation] = None,
    ) -> ExecutionValidationResult:
        """Validate a tool execution result."""
        validation = ExecutionValidationResult(is_valid=True, tool_name=tool_name)

        try:
            # -- Step 1: Null / type check --
            if result is None:
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="NULL_RESULT",
                    message=f"Tool '{tool_name}' returned null/None result",
                ))
                validation.is_valid = False
                return validation

            if isinstance(result, str):
                result = self._parse_string_result(result, validation)
                if not validation.is_valid:
                    return validation

            if isinstance(result, list):
                result = {"items": result, "count": len(result), "success": True}

            if not isinstance(result, dict):
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="INVALID_TYPE",
                    message=f"Expected dict result, got {type(result).__name__}",
                ))
                validation.is_valid = False
                return validation

            # -- Step 2: Success/error flag check --
            success = result.get("success")
            if success is False:
                error_msg = result.get("error") or result.get("message") or "Unknown error"
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="TOOL_FAILED",
                    message=f"Tool execution failed: {error_msg}",
                ))
                validation.is_valid = False
                validation.feedback = {
                    "reason": "tool_execution_failed",
                    "error": str(error_msg),
                    "tool": tool_name,
                }
                return validation

            if "error" in result and result["error"] and success is not True:
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="ERROR_FIELD",
                    message=f"Error in result: {result['error']}",
                ))
                validation.is_valid = False
                return validation

            # -- Step 3: Data presence --
            items = self._extract_items(result)
            if items is not None:
                validation.result_count = len(items)
                result["items"] = items
                result["count"] = len(items)

                if len(items) == 0:
                    validation.issues.append(ValidationIssue(
                        severity=ValidationSeverity.INFO,
                        stage=ValidationStage.EXECUTION_RESULT,
                        code="EMPTY_RESULTS",
                        message="Query returned no matching items",
                    ))

            # -- Step 4: Planner JSON leak detection --
            if self._looks_like_planner_json(result):
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="PLANNER_JSON_LEAK",
                    message="Result contains planner JSON instead of tool output (critical error)",
                ))
                validation.is_valid = False
                return validation

            # -- Step 5: Intent-result coherence --
            if intent and items:
                self._validate_result_coherence(items, intent, plan, validation)

            validation.sanitized_result = result

        except Exception as e:
            validation.issues.append(ValidationIssue(
                severity=ValidationSeverity.ERROR,
                stage=ValidationStage.EXECUTION_RESULT,
                code="VALIDATION_EXCEPTION",
                message=f"Validation failed with exception: {str(e)}",
            ))
            validation.is_valid = False
            logger.exception(f"[VALIDATOR] Exception validating {tool_name}: {e}")

        return validation

    def _parse_string_result(self, result: str, validation: ExecutionValidationResult) -> Optional[Dict]:
        """Parse string result as JSON."""
        if not result or result.strip() == "":
            validation.issues.append(ValidationIssue(
                severity=ValidationSeverity.ERROR,
                stage=ValidationStage.EXECUTION_RESULT,
                code="EMPTY_STRING",
                message="Tool returned empty string",
            ))
            validation.is_valid = False
            return None

        null_responses = {"null", "none", "no results", "no iterations found"}
        if result.strip().lower() in null_responses:
            return {"items": [], "count": 0, "success": True}

        try:
            return json.loads(result)
        except json.JSONDecodeError:
            if "error" in result.lower() or "failed" in result.lower():
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="ERROR_STRING",
                    message=f"Tool returned error: {result[:200]}",
                ))
                validation.is_valid = False
                return None
            return {"raw_text": result, "success": True}

    @staticmethod
    def _extract_items(result: Dict) -> Optional[List]:
        """Extract items list from result (handles various ADO response structures)."""
        for key in ("items", "workItems", "results", "value", "work_items", "iterations"):
            if key in result and isinstance(result[key], list):
                return result[key]
        return None

    @staticmethod
    def _looks_like_planner_json(result: Dict) -> bool:
        """Detect if result is actually planner JSON (not tool output)."""
        planner_signatures = ["action", "tool", "args", "confidence"]
        matches = sum(1 for sig in planner_signatures if sig in result)
        return matches >= 3 or ("action" in result and "tool" in result)

    @staticmethod
    def _check_iteration_coherence(
        items: List[Dict],
        plan: Optional[Dict[str, Any]],
        validation: "ExecutionValidationResult",
    ) -> None:
        """
        Check that result items belong to the sprint/iteration the plan requested.

        Reads the IterationPath filter from the plan's WIQL (``args.wiql`` or
        ``args.query``) and compares it against ``System.IterationPath`` on each
        sampled result item.

        Skips the check when ``@CurrentIteration`` is used (runtime dynamic value).
        Issues an ``ITERATION_MISMATCH`` WARNING when ≥1 sampled items don't match
        the expected sprint leaf segment — non-blocking by design.
        """
        if not plan or not items:
            return

        args = plan.get("args", {}) or {}
        # Support both common key names used by different tools
        wiql = args.get("wiql", "") or args.get("query", "") or ""
        if not wiql:
            return

        # Skip dynamic macro — cannot validate statically
        if "@currentiteration" in wiql.lower():
            return

        # Extract IterationPath value from WIQL.
        # Handles: IterationPath UNDER 'X', IterationPath = 'X', IterationPath IN ('X', …)
        iter_match = re.search(
            r"iterationpath\s+(?:UNDER|=|IN)\s+['\"]([^'\"]+)['\"]",
            wiql,
            re.IGNORECASE,
        )
        if not iter_match:
            return

        expected_iter = iter_match.group(1).strip()
        # Normalise to leaf segment so "Project\Sprint 26.05" → "Sprint 26.05"
        expected_leaf = (
            expected_iter.rsplit("\\", 1)[-1].strip()
            if "\\" in expected_iter
            else expected_iter
        )

        mismatched = 0
        checked = 0
        for item in items[:50]:
            if not isinstance(item, dict):
                continue
            fields = item.get("fields", {})
            actual_iter = (fields.get("System.IterationPath", "") or "").strip()
            if not actual_iter:
                continue
            checked += 1
            if expected_leaf.lower() not in actual_iter.lower():
                mismatched += 1

        if checked > 0 and mismatched > 0:
            pct = round(mismatched / checked * 100)
            validation.issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                stage=ValidationStage.EXECUTION_RESULT,
                code="ITERATION_MISMATCH",
                message=(
                    f"Plan targets iteration '{expected_leaf}' but {pct}% of sampled "
                    f"result items have a different System.IterationPath"
                ),
            ))

    def _validate_result_coherence(
        self,
        items: List[Dict],
        intent: IntentRepresentation,
        plan: Optional[Dict[str, Any]],
        validation: ExecutionValidationResult,
    ) -> None:
        """Validate that result items are coherent with user intent."""
        if not items:
            return

        sample = items[:50]

        # -- Work item type coherence --
        expected_types = intent.entities.get("work_item_types", [])
        if expected_types:
            actual_types = set()
            for item in sample:
                if isinstance(item, dict):
                    fields = item.get("fields", {})
                    wit = fields.get("System.WorkItemType", "")
                    if wit:
                        actual_types.add(wit.lower())

            if actual_types and not actual_types.intersection(
                set(t.lower() for t in expected_types)
            ):
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="TYPE_MISMATCH",
                    message=(
                        f"Query expects types {expected_types} but result "
                        f"contains types {list(actual_types)}"
                    ),
                ))

        # -- Scope coherence --
        scope_name = intent.scope_details.get("team_or_area", "")
        if scope_name:
            scope_lower = scope_name.lower()
            matching = 0
            checked = 0
            for item in sample:
                if isinstance(item, dict):
                    fields = item.get("fields", {})
                    area_path = (fields.get("System.AreaPath", "") or "").lower()
                    if area_path:
                        checked += 1
                        if scope_lower in area_path:
                            matching += 1

            if checked > 0 and matching == 0:
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="SCOPE_MISMATCH",
                    message=(
                        f"Query asks for '{scope_name}' but none of the {checked} "
                        f"sampled items have a matching AreaPath"
                    ),
                ))

        # -- Temporal coherence --
        if intent.requires_temporal and intent.temporal_details.get("quantity"):
            from datetime import datetime, timedelta
            days = intent.temporal_details["quantity"]
            unit = intent.temporal_details.get("unit", "day")
            if unit == "week":
                days *= 7
            elif unit == "month":
                days *= 30
            cutoff = datetime.utcnow() - timedelta(days=days + 1)

            out_of_range = 0
            checked_dates = 0
            for item in sample[:30]:
                if isinstance(item, dict):
                    fields = item.get("fields", {})
                    created = fields.get("System.CreatedDate", "")
                    if created and isinstance(created, str):
                        try:
                            dt = datetime.fromisoformat(
                                created.replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                            checked_dates += 1
                            if dt < cutoff:
                                out_of_range += 1
                        except (ValueError, TypeError):
                            pass

            if checked_dates > 0 and out_of_range > checked_dates * 0.5:
                pct = round(out_of_range / checked_dates * 100)
                validation.issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="DATE_RANGE_MISMATCH",
                    message=(
                        f"Query asks for items from the last {intent.temporal_details['quantity']} "
                        f"{intent.temporal_details.get('unit', 'day')}(s) but {pct}% of "
                        f"sampled items were created outside that range"
                    ),
                ))

        # -- Iteration/sprint coherence (plan-driven) --
        self._check_iteration_coherence(sample, plan, validation)


# ==============================================================================
# VALIDATION ORCHESTRATOR (RETRY LOOP + CIRCUIT BREAKER)
# ==============================================================================

class ValidationOrchestrator:
    """
    Orchestrates the full validation pipeline with retry loop and circuit breaker.

    Flow:
        1. Extract intent from query
        2. Validate plan against intent (Stage A)
        3. Validate plan structure (Stage B)
        4. If validation fails -> return structured feedback to planner
        5. Planner regenerates plan using feedback
        6. Repeat up to MAX_RETRIES (3)
        7. After 3 failures -> circuit breaker activates -> hard stop

    All stages emit Langfuse traces for full observability.
    """

    MAX_RETRIES = 3

    def __init__(
        self,
        tools_cache: Optional[Dict[str, Any]] = None,
        mcp_connector: Optional[Any] = None,
    ):
        self.tools_cache = tools_cache or {}
        self.mcp_connector = mcp_connector
        self.intent_extractor = IntentExtractor()
        self.alignment_validator = IntentPlanAlignmentValidator()
        self.structural_validator = PlanStructuralValidator()
        self.execution_validator = ExecutionResultValidator()

    def validate_plan(
        self,
        query: str,
        plan: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> PlanValidationResult:
        """
        Validate a plan against user intent and structural requirements.

        Returns PlanValidationResult with is_valid, issues, and feedback
        that can be passed back to the planner for re-generation.
        """
        span = self._create_span(
            "plan_validation",
            input_data={"query": query[:200], "tool": plan.get("tool"), "attempt": 1},
            metadata={"stage": "plan_validation"},
            session_id=session_id,
        )

        try:
            # 1. Extract intent
            intent = self.intent_extractor.extract(query)
            logger.info(
                f"[VALIDATOR] Intent extracted: constraints={len(intent.constraints)}, "
                f"scope={intent.requires_scope}, identity={intent.requires_identity}, "
                f"temporal={intent.requires_temporal}"
            )

            # 2. Stage A: Intent-Plan Alignment
            alignment_result = self.alignment_validator.validate(
                intent, plan, self.tools_cache
            )
            logger.info(
                f"[VALIDATOR] Alignment: score={alignment_result.alignment_score:.2f}, "
                f"valid={alignment_result.is_valid}, issues={len(alignment_result.issues)}"
            )

            if not alignment_result.is_valid:
                self._finalize_span(span, output=alignment_result.to_dict(), status="alignment_failed")
                return alignment_result

            # 3. Stage B: Structural Validation
            structural_result = self.structural_validator.validate(
                alignment_result.sanitized_plan or plan,
                self.tools_cache,
                query=query,
                mcp_connector=self.mcp_connector,
            )
            logger.info(
                f"[VALIDATOR] Structural: valid={structural_result.is_valid}, "
                f"issues={len(structural_result.issues)}"
            )

            if not structural_result.is_valid:
                structural_result.issues = alignment_result.issues + structural_result.issues
                structural_result.intent = intent
                structural_result.alignment_score = alignment_result.alignment_score
                self._finalize_span(span, output=structural_result.to_dict(), status="structural_failed")
                return structural_result

            # -- All passed --
            merged = PlanValidationResult(
                is_valid=True,
                stage=ValidationStage.PLAN_STRUCTURAL,
                issues=alignment_result.issues + structural_result.issues,
                sanitized_plan=structural_result.sanitized_plan,
                intent=intent,
                alignment_score=alignment_result.alignment_score,
                warnings=structural_result.warnings,
                intent_alignment=structural_result.intent_alignment,
            )

            self._finalize_span(span, output=merged.to_dict(), status="success")
            return merged

        except Exception as e:
            logger.exception(f"[VALIDATOR] Plan validation error: {e}")
            error_result = PlanValidationResult(
                is_valid=False,
                stage=ValidationStage.PLAN_STRUCTURAL,
                issues=[ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.PLAN_STRUCTURAL,
                    code="VALIDATION_EXCEPTION",
                    message=str(e),
                )],
                error=str(e),
            )
            self._finalize_span(span, output={"error": str(e)}, status="error")
            return error_result

    def validate_execution(
        self,
        query: str,
        plan: Dict[str, Any],
        result: Any,
        session_id: Optional[str] = None,
    ) -> ExecutionValidationResult:
        """Validate execution results against plan and intent."""
        tool_name = plan.get("tool", "unknown")

        span = self._create_span(
            "execution_validation",
            input_data={"tool": tool_name, "query": query[:200]},
            metadata={"stage": "execution_validation"},
            session_id=session_id,
        )

        try:
            intent = self.intent_extractor.extract(query)
            exec_result = self.execution_validator.validate(
                tool_name, result, query=query, plan=plan, intent=intent,
            )

            log_level = "success" if exec_result.is_valid else "warning"
            logger.info(
                f"[VALIDATOR] Execution validation: valid={exec_result.is_valid}, "
                f"count={exec_result.result_count}, errors={len(exec_result.errors)}, "
                f"warnings={len(exec_result.warnings)}"
            )

            self._finalize_span(span, output=exec_result.to_dict(), status=log_level)
            return exec_result

        except Exception as e:
            logger.exception(f"[VALIDATOR] Execution validation error: {e}")
            error_result = ExecutionValidationResult(
                is_valid=False,
                tool_name=tool_name,
                issues=[ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    stage=ValidationStage.EXECUTION_RESULT,
                    code="VALIDATION_EXCEPTION",
                    message=str(e),
                )],
            )
            self._finalize_span(span, output={"error": str(e)}, status="error")
            return error_result

    async def validate_with_retry(
        self,
        query: str,
        initial_plan: Dict[str, Any],
        plan_generator: Callable[..., Awaitable[Dict[str, Any]]],
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validate a plan with retry loop and circuit breaker.

        If validation fails, passes structured feedback to plan_generator
        which should regenerate a plan. Repeats up to MAX_RETRIES times.

        After MAX_RETRIES failures, raises CircuitBreakerError.

        Args:
            query: User query
            initial_plan: First plan attempt
            plan_generator: Async callable(query, feedback_context) -> plan dict
            session_id: For Langfuse trace grouping

        Returns:
            Validated plan dict

        Raises:
            CircuitBreakerError: After MAX_RETRIES failed attempts
        """
        retry_span = self._create_span(
            "validation_retry_loop",
            input_data={"query": query[:200], "max_retries": self.MAX_RETRIES},
            metadata={"stage": "retry_loop"},
            session_id=session_id,
        )

        current_plan = initial_plan
        last_feedback = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            attempt_span = self._create_span(
                f"validation_attempt_{attempt}",
                input_data={
                    "attempt": attempt,
                    "tool": current_plan.get("tool") if current_plan else None,
                    "has_feedback": last_feedback is not None,
                },
                metadata={"attempt": attempt, "stage": "retry_attempt"},
                session_id=session_id,
            )

            logger.info(
                f"[VALIDATOR] Retry loop attempt {attempt}/{self.MAX_RETRIES} "
                f"(tool={current_plan.get('tool') if current_plan else 'none'})"
            )

            result = self.validate_plan(query, current_plan, session_id=session_id)

            if result.is_valid:
                logger.info(
                    f"[VALIDATOR] Plan validated on attempt {attempt} "
                    f"(alignment={result.alignment_score:.2f})"
                )
                self._finalize_span(attempt_span, output={
                    "result": "valid",
                    "attempt": attempt,
                    "alignment_score": result.alignment_score,
                }, status="success")
                self._finalize_span(retry_span, output={
                    "total_attempts": attempt,
                    "result": "valid",
                }, status="success")
                return result.sanitized_plan or current_plan

            # -- Validation failed --
            logger.warning(
                f"[VALIDATOR] Attempt {attempt} failed: {result.error_message}"
            )

            last_feedback = result.feedback or {
                "reason": "validation_failed",
                "error": result.error_message,
                "issues": [i.to_dict() for i in result.issues],
            }
            last_feedback["attempt"] = attempt
            last_feedback["max_retries"] = self.MAX_RETRIES

            self._finalize_span(attempt_span, output={
                "result": "invalid",
                "attempt": attempt,
                "feedback": last_feedback,
            }, status="error")

            # -- Generate new plan if not at max retries --
            if attempt < self.MAX_RETRIES:
                try:
                    logger.info(
                        f"[VALIDATOR] Requesting plan regeneration with feedback "
                        f"(attempt {attempt + 1})"
                    )
                    current_plan = await plan_generator(query, last_feedback)
                    if not current_plan:
                        logger.error("[VALIDATOR] Plan generator returned empty plan")
                        break
                except Exception as e:
                    logger.exception(f"[VALIDATOR] Plan generator failed: {e}")
                    break

        # ==================================================================
        # CIRCUIT BREAKER ACTIVATED
        # ==================================================================
        logger.error(
            f"[VALIDATOR] CIRCUIT BREAKER ACTIVATED after {self.MAX_RETRIES} attempts. "
            f"Last feedback: {json.dumps(last_feedback, default=str)[:500]}"
        )

        self._finalize_span(retry_span, output={
            "total_attempts": self.MAX_RETRIES,
            "result": "circuit_breaker",
            "last_feedback": last_feedback,
        }, status="error")

        raise CircuitBreakerError(
            attempts=self.MAX_RETRIES,
            last_feedback=last_feedback or {},
        )

    @staticmethod
    def _create_span(
        name: str,
        input_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ):
        """Create a Langfuse span (returns None if unavailable)."""
        try:
            from utilities.langfuse_client import create_span as _cs
            return _cs(name=name, input_data=input_data, metadata=metadata, session_id=session_id)
        except Exception:
            return None

    @staticmethod
    def _finalize_span(span, output=None, status="success"):
        """Finalize a Langfuse span (no-op if None)."""
        if not span:
            return
        try:
            from utilities.langfuse_client import finalize_span as _fs
            _fs(span, output=output, status=status)
        except Exception:
            pass


# ==============================================================================
# BACKWARD-COMPATIBLE EXPORTS
# ==============================================================================

class ToolResultValidator:
    """Backward-compatible wrapper around ExecutionResultValidator."""

    def __init__(self):
        self._inner = ExecutionResultValidator()
        self._intent_extractor = IntentExtractor()

    def validate(
        self,
        tool_name: str,
        result: Any,
        query: str = None,
        plan: dict = None,
    ) -> ExecutionValidationResult:
        intent = self._intent_extractor.extract(query) if query else None
        return self._inner.validate(tool_name, result, query=query or "", plan=plan, intent=intent)


# Legacy alias used in agent.py
ValidationResult = ExecutionValidationResult

_validator: Optional[ToolResultValidator] = None


def get_result_validator() -> ToolResultValidator:
    """Get singleton validator instance."""
    global _validator
    if _validator is None:
        _validator = ToolResultValidator()
    return _validator


def validate_tool_result(
    tool_name: str, result: Any, query: str = None, plan: dict = None,
) -> ExecutionValidationResult:
    """Convenience function to validate a tool result."""
    return get_result_validator().validate(tool_name, result, query=query, plan=plan)


_orchestrator: Optional[ValidationOrchestrator] = None


def get_validation_orchestrator(
    tools_cache: Optional[Dict[str, Any]] = None,
    mcp_connector: Optional[Any] = None,
) -> ValidationOrchestrator:
    """Get or create the singleton ValidationOrchestrator."""
    global _orchestrator
    if _orchestrator is None or tools_cache is not None:
        _orchestrator = ValidationOrchestrator(
            tools_cache=tools_cache or {},
            mcp_connector=mcp_connector,
        )
    return _orchestrator

