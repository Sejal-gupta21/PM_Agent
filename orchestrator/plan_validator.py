"""
Plan Validator - Legacy module for validating Light LLM plans.

⚠️ NOTE: This module is NOT ACTIVELY USED in the application.
The validation system has been replaced by agents/pm_agent/validation.py
which uses dynamically-generated metadata from the live MCP server.

This module is maintained for backward compatibility but all validation
is performed by the active system in agents/pm_agent/validation.py.

To validate plans, use:
    from agents.pm_agent.validation import validate_plan
    result = validate_plan(plan, tools_cache)
"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger("orchestrator.plan_validator")


# ⚠️ DEPRECATED: Static tool schemas have been removed.
# Tool metadata is now generated dynamically from the live MCP server.
# See utilities/mcp/tool_registry_generator.py for the current system.


@dataclass
class ValidationResult:
    """Result of plan validation."""
    is_valid: bool = True
    is_complete: bool = False  # Has all required params with concrete values
    completeness_score: float = 0.0  # 0.0 to 1.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_params: List[str] = field(default_factory=list)
    placeholder_params: List[str] = field(default_factory=list)
    enhanced_plan: Optional[Dict[str, Any]] = None  # Plan with filled params


class PlanValidator:
    """
    Validate and enhance Light LLM execution plans.
    
    ⚠️ DEPRECATED: This class is NOT ACTIVELY USED.
    Use agents/pm_agent/validation.py:validate_plan() instead.
    
    This class:
    1. Validates tool names against known registry
    2. Checks required parameters are present
    3. Validates parameter types
    4. Detects placeholder values
    5. Fills missing parameters from context
    6. Calculates completeness score
    """
    
    def __init__(self, tool_registry: Optional[Dict[str, Any]] = None):
        """Initialize the plan validator.
        
        Args:
            tool_registry: Optional override for tool schemas
                          (deprecated - use dynamic registry instead)
        """
        # Use provided registry or empty dict (legacy code, not actively used)
        self.tool_schemas = tool_registry or {}
    
    def validate(
        self,
        plan: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> ValidationResult:
        """
        Validate an execution plan from Light LLM.
        
        Args:
            plan: The plan dict from Light LLM
            context: Execution context with project, team, etc.
            
        Returns:
            ValidationResult with validation status and enhanced plan
        """
        context = context or {}
        result = ValidationResult()
        
        # Check if plan has the expected structure
        if not isinstance(plan, dict):
            result.is_valid = False
            result.errors.append("Plan is not a dictionary")
            return result
        
        # Handle clarification requests
        if plan.get("needs_clarification"):
            result.is_valid = True
            result.is_complete = False
            result.completeness_score = 0.0
            result.enhanced_plan = plan
            return result
        
        # Get plan steps
        plan_obj = plan.get("plan")
        if not plan_obj:
            # Check if this is an old-style plan with direct tool/args
            if plan.get("tool") or plan.get("action"):
                # Convert to new style
                plan_obj = {
                    "type": "single",
                    "steps": [{
                        "action": plan.get("action", "call_tool"),
                        "tool": plan.get("tool"),
                        "args": plan.get("args", {}),
                        "description": plan.get("description", ""),
                    }]
                }
                plan["plan"] = plan_obj
            else:
                result.is_valid = False
                result.errors.append("Plan has no 'plan' object or steps")
                return result
        
        steps = plan_obj.get("steps", [])
        if not steps:
            result.is_valid = False
            result.errors.append("Plan has no steps")
            return result
        
        # Validate each step
        total_completeness = 0.0
        enhanced_steps = []
        
        for idx, step in enumerate(steps):
            step_result = self._validate_step(step, context, idx)
            
            if not step_result.is_valid:
                result.is_valid = False
                result.errors.extend(step_result.errors)
            
            result.warnings.extend(step_result.warnings)
            result.missing_params.extend(step_result.missing_params)
            result.placeholder_params.extend(step_result.placeholder_params)
            
            total_completeness += step_result.completeness_score
            
            if step_result.enhanced_plan:
                enhanced_steps.append(step_result.enhanced_plan)
            else:
                enhanced_steps.append(step)
        
        # Calculate overall completeness
        result.completeness_score = total_completeness / len(steps) if steps else 0.0
        result.is_complete = result.completeness_score >= 0.9 and not result.missing_params
        
        # Build enhanced plan
        result.enhanced_plan = {
            **plan,
            "plan": {
                **plan_obj,
                "steps": enhanced_steps,
            },
            "has_full_plan": result.is_complete,
        }
        
        logger.info(f"[PLAN_VALIDATOR] Validation: valid={result.is_valid}, "
                   f"complete={result.is_complete}, score={result.completeness_score:.2f}, "
                   f"errors={len(result.errors)}, warnings={len(result.warnings)}")
        
        return result
    
    def _validate_step(
        self,
        step: Dict[str, Any],
        context: Dict[str, Any],
        step_idx: int
    ) -> ValidationResult:
        """Validate a single plan step."""
        result = ValidationResult()
        
        action = step.get("action", "")
        tool = step.get("tool", "")
        args = step.get("args", {})
        
        # Validate action
        if action not in ("call_tool", "call_skill", "synthesize"):
            result.warnings.append(f"Step {step_idx}: Unknown action '{action}'")
        
        # Synthesize steps don't need tool validation
        if action == "synthesize":
            result.is_valid = True
            result.completeness_score = 1.0
            result.enhanced_plan = step
            return result
        
        # Validate tool name
        if not tool:
            result.is_valid = False
            result.errors.append(f"Step {step_idx}: No tool specified")
            return result
        
        # Get tool schema
        schema = self.tool_schemas.get(tool)
        if not schema:
            result.warnings.append(f"Step {step_idx}: Unknown tool '{tool}', cannot validate")
            result.is_valid = True  # Don't fail on unknown tools
            result.completeness_score = 0.7
            result.enhanced_plan = step
            return result
        
        # Validate required parameters
        required = schema.get("required", [])
        optional = schema.get("optional", [])
        param_types = schema.get("param_types", {})
        param_validators = schema.get("param_validators", {})
        
        enhanced_args = dict(args)
        params_valid = 0
        params_total = len(required) + len([o for o in optional if o in args])
        
        for param in required:
            value = args.get(param)
            
            # Check if missing
            if value is None:
                # Try to fill from context
                filled = self._fill_from_context(param, context)
                if filled is not None:
                    enhanced_args[param] = filled
                    params_valid += 1
                else:
                    result.missing_params.append(f"{tool}.{param}")
                continue
            
            # Check for placeholder
            if self._is_placeholder(value):
                # Try to fill from context
                filled = self._fill_from_context(param, context)
                if filled is not None:
                    enhanced_args[param] = filled
                    result.placeholder_params.append(f"{tool}.{param} (filled)")
                    params_valid += 1
                else:
                    result.placeholder_params.append(f"{tool}.{param}")
                continue
            
            # Validate type
            expected_type = param_types.get(param)
            if expected_type and not self._check_type(value, expected_type):
                result.warnings.append(f"Step {step_idx}: {param} has wrong type")
            
            # Run custom validator
            validator = param_validators.get(param)
            if validator:
                try:
                    if not validator(value):
                        result.warnings.append(f"Step {step_idx}: {param} failed validation")
                except Exception:
                    pass
            
            params_valid += 1
        
        # Check optional parameters for placeholders
        for param in optional:
            value = args.get(param)
            if value is not None:
                if self._is_placeholder(value):
                    filled = self._fill_from_context(param, context)
                    if filled is not None:
                        enhanced_args[param] = filled
                        result.placeholder_params.append(f"{tool}.{param} (filled)")
                        params_valid += 1
                    else:
                        result.placeholder_params.append(f"{tool}.{param}")
                else:
                    params_valid += 1
        
        # Calculate completeness
        result.completeness_score = params_valid / params_total if params_total > 0 else 1.0
        
        # Set validity
        result.is_valid = len(result.missing_params) == 0
        result.is_complete = result.completeness_score >= 0.9
        
        # Build enhanced step
        result.enhanced_plan = {
            **step,
            "args": enhanced_args,
        }
        
        return result
    
    def _is_placeholder(self, value: Any) -> bool:
        """Check if a value is a placeholder."""
        if value is None:
            return True
        
        if isinstance(value, str):
            value_lower = value.strip().lower()
            # Check for common placeholder patterns
            common_placeholders = [
                "placeholder", "tbd", "todo", "unknown", "n/a",
                "none", "null", "xxx", "temp"
            ]
            for placeholder in common_placeholders:
                if value_lower == placeholder.lower():
                    return True
            # Also check for partial matches
            if "<" in value and ">" in value:
                return True
            if "unknown" in value_lower:
                return True
        
        return False
    
    def _fill_from_context(self, param: str, context: Dict[str, Any]) -> Optional[Any]:
        """Try to fill a parameter from context."""
        # Direct mapping
        if param in context and context[param]:
            return context[param]
        
        # Alternative keys
        key_mappings = {
            "project": ["project", "ado_project", "project_name"],
            "team": ["team", "ado_team", "team_name"],
            "iterationId": ["iterationId", "iteration", "iterationPath", "sprint"],
            "iterationPath": ["iterationPath", "iteration", "iterationId"],
            "areaPath": ["areaPath", "area_path", "area"],
        }
        
        alternatives = key_mappings.get(param, [])
        for alt in alternatives:
            if alt in context and context[alt]:
                return context[alt]
        
        return None
    
    def _check_type(self, value: Any, expected: Any) -> bool:
        """Check if value matches expected type(s)."""
        if isinstance(expected, tuple):
            return isinstance(value, expected)
        return isinstance(value, expected)
    
    def convert_to_executable_plan(
        self,
        light_plan: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Convert a Light LLM plan to an executable format for PM Agent.
        
        This bridges the gap between Light LLM output and what PM Agent expects.
        
        Args:
            light_plan: Plan from Light LLM Planner
            context: Execution context
            
        Returns:
            Executable plan in PM Agent format
        """
        # First validate and enhance
        validation = self.validate(light_plan, context)
        
        if not validation.is_valid:
            return {
                "action": "ask_clarification",
                "message": f"I couldn't create a valid execution plan: {', '.join(validation.errors)}",
                "errors": validation.errors,
            }
        
        plan = validation.enhanced_plan or light_plan
        plan_obj = plan.get("plan", {})
        steps = plan_obj.get("steps", [])
        
        if not steps:
            return {
                "action": "ask_clarification",
                "message": "I need more information to process your request.",
            }
        
        # Check for clarification needed
        if plan.get("needs_clarification"):
            questions = plan.get("clarification_questions", [])
            return {
                "action": "ask_clarification",
                "message": questions[0] if questions else "Could you provide more details?",
                "clarification_questions": questions,
            }
        
        # Single step plan
        if len(steps) == 1:
            step = steps[0]
            action = step.get("action", "call_tool")
            
            if action == "call_skill":
                # Route to skill
                return {
                    "action": "call_skill",
                    "skill": step.get("tool"),
                    "args": step.get("args", {}),
                    "confidence": plan.get("confidence", 0.8),
                }
            else:
                # Single tool call
                return {
                    "action": "call_tool",
                    "tool": step.get("tool"),
                    "args": step.get("args", {}),
                    "confidence": plan.get("confidence", 0.8),
                }
        
        # Multi-step plan
        execution_plan = {
            "steps": [],
            "query": context.get("query", ""),
            "context": context,
        }
        
        for idx, step in enumerate(steps):
            action = step.get("action", "call_tool")
            
            if action == "synthesize":
                # Synthesis step
                execution_plan["steps"].append({
                    "step_id": f"step_{idx + 1}",
                    "type": "synthesize",
                    "instruction": step.get("args", {}).get("instruction", ""),
                    "description": step.get("description", ""),
                })
            else:
                # Tool/skill call
                execution_plan["steps"].append({
                    "step_id": f"step_{idx + 1}",
                    "type": "call_tool" if action == "call_tool" else "call_skill",
                    "tool_name": step.get("tool"),
                    "args": step.get("args", {}),
                    "description": step.get("description", ""),
                })
        
        return {
            "action": "execute_plan",
            "execution_plan": execution_plan,
            "confidence": plan.get("confidence", 0.8),
            "analysis_summary": plan.get("analysis_summary", ""),
        }


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON ACCESS
# ══════════════════════════════════════════════════════════════════════════════

_plan_validator: Optional[PlanValidator] = None


def get_plan_validator() -> PlanValidator:
    """Get the global plan validator instance."""
    global _plan_validator
    if _plan_validator is None:
        _plan_validator = PlanValidator()
    return _plan_validator


__all__ = [
    "PlanValidator",
    "ValidationResult",
    "get_plan_validator",
]
