"""
Context Preservation for Multi-Tool Flows.

This module provides context preservation and variable resolution for multi-tool
execution plans, enabling:
1. Step-to-step result passing via variables (${step_1.items})
2. Execution context accumulation across steps
3. Dynamic argument resolution from previous outputs

Architecture Position:
    MultiToolOrchestrator → ContextResolver → ToolStep execution

Variable Syntax:
- ${step_1.items}       - Items list from step 1
- ${step_1.count}       - Count field from step 1
- ${step_2.result.field} - Nested field access
- ${context.project}    - Value from execution context
- ${this.result}        - Reserved for rollback (result of current step)

Usage:
    from agents.pm_agent.context_resolver import ContextResolver
    
    resolver = ContextResolver()
    resolved_args = resolver.resolve_step_args(
        args={"ids": "${step_1.items}"},
        context=plan.context,
        step_outputs=plan.intermediate_results
    )
"""

import re
import json
import logging
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass

logger = logging.getLogger("agents.pm_agent.context_resolver")


@dataclass
class ResolutionResult:
    """Result of variable resolution."""
    success: bool
    resolved_value: Any = None
    error: Optional[str] = None
    variables_resolved: List[str] = None
    
    def __post_init__(self):
        if self.variables_resolved is None:
            self.variables_resolved = []


class ContextResolver:
    """
    Resolves variable references in step arguments from execution context.
    
    Supports:
    - Step output references: ${step_1.items}, ${step_1.count}
    - Context references: ${context.project}, ${context.iteration}
    - Nested field access: ${step_1.result.fields.title}
    - Array indexing: ${step_1.items[0].id}
    """
    
    # Variable pattern: ${variable.path} or ${variable.path[index].field}
    VARIABLE_PATTERN = re.compile(r'\$\{([^}]+)\}')
    
    def __init__(self):
        """Initialize the resolver."""
        pass
    
    def resolve_step_args(
        self,
        args: Dict[str, Any],
        context: Dict[str, Any],
        step_outputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Resolve all variable references in step arguments.
        
        Args:
            args: Step arguments dict (may contain ${var} references)
            context: Execution context (project, team, etc.)
            step_outputs: Results from previous steps {step_id: result}
            
        Returns:
            Dict with all variables resolved to actual values
        """
        if not args:
            return {}
        
        resolved = {}
        
        for key, value in args.items():
            resolved[key] = self._resolve_value(value, context, step_outputs)
        
        return resolved
    
    def _resolve_value(
        self,
        value: Any,
        context: Dict[str, Any],
        step_outputs: Dict[str, Any]
    ) -> Any:
        """
        Resolve a single value (may be string, dict, list, or primitive).
        """
        if value is None:
            return None
        
        # String - check for variable pattern
        if isinstance(value, str):
            return self._resolve_string(value, context, step_outputs)
        
        # Dict - resolve recursively
        if isinstance(value, dict):
            return {k: self._resolve_value(v, context, step_outputs) for k, v in value.items()}
        
        # List - resolve each element
        if isinstance(value, list):
            return [self._resolve_value(v, context, step_outputs) for v in value]
        
        # Primitive - return as-is
        return value
    
    def _resolve_string(
        self,
        value: str,
        context: Dict[str, Any],
        step_outputs: Dict[str, Any]
    ) -> Any:
        """
        Resolve variable references in a string.
        
        If the entire string is a variable (e.g., "${step_1.items}"), 
        returns the resolved value directly (preserving type).
        
        If the string contains embedded variables (e.g., "ID is ${step_1.id}"),
        returns a string with variables substituted.
        """
        # Check if entire string is a single variable
        match = self.VARIABLE_PATTERN.fullmatch(value)
        if match:
            # Single variable - return resolved value directly (preserve type)
            var_path = match.group(1)
            result = self._resolve_variable(var_path, context, step_outputs)
            if result.success:
                logger.debug(f"[CONTEXT] Resolved ${{{var_path}}} → {type(result.resolved_value).__name__}")
                return result.resolved_value
            else:
                logger.warning(f"[CONTEXT] Failed to resolve ${{{var_path}}}: {result.error}")
                return value  # Return original on failure
        
        # String with embedded variables - substitute as strings
        def replace_var(m):
            var_path = m.group(1)
            result = self._resolve_variable(var_path, context, step_outputs)
            if result.success:
                val = result.resolved_value
                # Convert to string for embedding
                if isinstance(val, (dict, list)):
                    return json.dumps(val)
                return str(val)
            return m.group(0)  # Keep original on failure
        
        resolved_str = self.VARIABLE_PATTERN.sub(replace_var, value)
        return resolved_str
    
    def _resolve_variable(
        self,
        var_path: str,
        context: Dict[str, Any],
        step_outputs: Dict[str, Any]
    ) -> ResolutionResult:
        """
        Resolve a single variable path.
        
        Supported paths:
        - step_1.items → step_outputs["step_1"]["items"]
        - step_1.count → step_outputs["step_1"]["count"]
        - context.project → context["project"]
        - step_1.result.fields.title → nested access
        """
        parts = self._parse_path(var_path)
        if not parts:
            return ResolutionResult(success=False, error=f"Invalid variable path: {var_path}")
        
        root = parts[0]
        rest = parts[1:]
        
        # Determine root object
        if root == "context":
            # Context variable
            obj = context
        elif root.startswith("step_"):
            # Step output variable
            if root not in step_outputs:
                return ResolutionResult(success=False, error=f"Step '{root}' not found in outputs")
            obj = step_outputs[root]
        else:
            # Try context first, then step outputs
            if root in context:
                obj = context[root]
            elif root in step_outputs:
                obj = step_outputs[root]
            else:
                return ResolutionResult(success=False, error=f"Unknown root '{root}'")
        
        # Navigate path
        try:
            for part in rest:
                if obj is None:
                    return ResolutionResult(success=False, error=f"Null value at '{part}'")
                
                if isinstance(part, int):
                    # Array index
                    if not isinstance(obj, list):
                        return ResolutionResult(success=False, error=f"Cannot index non-list with [{part}]")
                    if part >= len(obj):
                        return ResolutionResult(success=False, error=f"Index {part} out of bounds (len={len(obj)})")
                    obj = obj[part]
                elif isinstance(obj, dict):
                    if part not in obj:
                        # Try alternative field names
                        alt = self._find_alternative(obj, part)
                        if alt is not None:
                            obj = alt
                        else:
                            return ResolutionResult(success=False, error=f"Field '{part}' not found")
                    else:
                        obj = obj[part]
                else:
                    return ResolutionResult(success=False, error=f"Cannot access '{part}' on {type(obj).__name__}")
            
            return ResolutionResult(success=True, resolved_value=obj, variables_resolved=[var_path])
            
        except Exception as e:
            return ResolutionResult(success=False, error=str(e))
    
    def _parse_path(self, var_path: str) -> Optional[List[Union[str, int]]]:
        """
        Parse variable path into parts.
        
        Examples:
        - "step_1.items" → ["step_1", "items"]
        - "step_1.items[0].id" → ["step_1", "items", 0, "id"]
        - "context.project" → ["context", "project"]
        """
        if not var_path:
            return None
        
        parts = []
        current = ""
        i = 0
        
        while i < len(var_path):
            char = var_path[i]
            
            if char == ".":
                if current:
                    parts.append(current)
                    current = ""
            elif char == "[":
                if current:
                    parts.append(current)
                    current = ""
                # Parse index
                j = i + 1
                while j < len(var_path) and var_path[j] != "]":
                    j += 1
                if j >= len(var_path):
                    return None  # Unclosed bracket
                index_str = var_path[i+1:j]
                try:
                    parts.append(int(index_str))
                except ValueError:
                    return None  # Invalid index
                i = j
            elif char == "]":
                pass  # Skip closing bracket
            else:
                current += char
            
            i += 1
        
        if current:
            parts.append(current)
        
        return parts if parts else None
    
    def _find_alternative(self, obj: Dict, field: str) -> Any:
        """Find alternative field names."""
        alternatives = {
            "items": ["workItems", "results", "value", "work_items"],
            "count": ["totalCount", "total", "resultCount"],
            "id": ["System.Id", "workItemId"],
        }
        
        for alt in alternatives.get(field, []):
            if alt in obj:
                return obj[alt]
        
        return None
    
    def extract_step_data(
        self,
        step_result: Dict[str, Any],
        step_id: str,
        execution_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Extract and normalize key data from step result into execution context.
        
        Called after each step completes to make data available to subsequent steps.
        
        Args:
            step_result: Result from the completed step
            step_id: ID of the completed step
            execution_context: Mutable execution context dict
            
        Returns:
            Updated execution context
        """
        if not step_result or not isinstance(step_result, dict):
            return execution_context
        
        # Extract items list
        items = None
        for key in ["items", "workItems", "results", "value", "work_items"]:
            if key in step_result and isinstance(step_result[key], list):
                items = step_result[key]
                break
        
        if items is not None:
            execution_context[f"{step_id}_items"] = items
            execution_context[f"{step_id}_count"] = len(items)
            
            # Extract IDs for convenience
            ids = []
            for item in items:
                if isinstance(item, dict):
                    item_id = item.get("id") or item.get("System.Id")
                    if item_id:
                        ids.append(item_id)
            if ids:
                execution_context[f"{step_id}_ids"] = ids
        
        # Extract count
        count = step_result.get("count") or step_result.get("totalCount")
        if count is not None:
            execution_context[f"{step_id}_count"] = count
        
        # Extract any scalar values
        for key, value in step_result.items():
            if isinstance(value, (str, int, float, bool)):
                execution_context[f"{step_id}_{key}"] = value
        
        logger.debug(f"[CONTEXT] Extracted data from {step_id}: {list(k for k in execution_context if k.startswith(step_id))}")
        
        return execution_context


# Singleton instance
_resolver: Optional[ContextResolver] = None


def get_context_resolver() -> ContextResolver:
    """Get singleton resolver instance."""
    global _resolver
    if _resolver is None:
        _resolver = ContextResolver()
    return _resolver


def resolve_step_args(
    args: Dict[str, Any],
    context: Dict[str, Any],
    step_outputs: Dict[str, Any]
) -> Dict[str, Any]:
    """Convenience function to resolve step arguments."""
    return get_context_resolver().resolve_step_args(args, context, step_outputs)
