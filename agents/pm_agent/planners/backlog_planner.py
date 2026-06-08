"""
Backlog Planner - LLM-based query planning for backlog triaging.

This module uses GPT-4 to break down backlog health queries into structured
execution plans with optimal tool selection and dependency management.
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path

logger = logging.getLogger("pm_agent.backlog_planner")


async def plan_backlog_query(
    query: str,
    project: str,
    team: str,
    tool_registry_mcp: Dict[str, Dict[str, Any]],
    tool_registry_skills: Dict[str, Dict[str, Any]],
    model: str = "gpt-4o"
) -> Dict[str, Any]:
    """
    Generate execution plan for backlog triaging queries using LLM.
    
    Args:
        query: User's natural language query
        project: ADO project name
        team: Team name
        tool_registry_mcp: MCP tool registry (from tool_registry.py)
        tool_registry_skills: PM Skill tool registry (from skills.py)
        model: LLM model to use
    
    Returns:
        Structured execution plan with steps, dependencies, and metadata
    """
    try:
        from openai import OpenAI
        
        # Load planning prompt template
        prompt_path = Path(__file__).parent.parent / "prompts" / "backlog_planning.md"
        with open(prompt_path, "r", encoding="utf-8") as f:
            prompt_template = f.read()
        
        # Build tool documentation for prompt
        skill_tools_doc = _format_skill_tools_for_prompt(tool_registry_skills)
        mcp_tools_doc = _format_mcp_tools_for_prompt(tool_registry_mcp)
        
        # Inject context into prompt
        prompt = prompt_template.replace("{{SKILL_TOOLS}}", skill_tools_doc)
        prompt = prompt.replace("{{MCP_TOOLS}}", mcp_tools_doc)
        prompt = prompt.replace("{{project}}", project)
        prompt = prompt.replace("{{team}}", team)
        prompt = prompt.replace("{{user_query}}", query)
        
        logger.info(f"Planning backlog query: '{query}' for {team} in {project}")
        
        # Call LLM to generate plan
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert query planner for Azure DevOps backlog analysis. Generate efficient, dependency-aware execution plans."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.2  # Lower temperature for more deterministic planning
        )
        
        # Parse plan
        plan = json.loads(response.choices[0].message.content)
        
        # Validate plan
        is_valid, validation_errors = _validate_plan(plan)
        if not is_valid:
            logger.warning(f"Plan validation failed: {validation_errors}")
            # Try to autocorrect common issues
            plan = _autocorrect_plan(plan, validation_errors)
        
        # Add metadata
        plan["planner_metadata"] = {
            "model": model,
            "tokens_used": response.usage.total_tokens,
            "project": project,
            "team": team,
            "original_query": query
        }
        
        logger.info(
            f"Generated plan: {plan.get('intent')} with {len(plan.get('steps', []))} steps "
            f"(confidence: {plan.get('confidence', 0):.2f})"
        )
        
        return plan
        
    except Exception as e:
        logger.error(f"Error generating backlog plan: {e}", exc_info=True)
        
        # Fallback: return default plan template
        logger.warning("Falling back to default backlog health plan template")
        from agents.pm_skill_agent.backlog_tools import get_backlog_health_plan_template
        
        template = get_backlog_health_plan_template()
        
        # Inject project/team into args
        for step in template["steps"]:
            for arg_name, arg_value in step.get("args_template", {}).items():
                if arg_value == "${project}":
                    step["args_template"][arg_name] = project
                elif arg_value == "${team}":
                    step["args_template"][arg_name] = team
        
        # Convert to plan format
        fallback_plan = {
            "intent": "backlog_health_check",
            "confidence": 0.5,
            "plan_type": "full",
            "steps": [
                {
                    "step_id": step["step_id"],
                    "tool": step["tool"],
                    "tool_type": "pm_skill",  # Assume PM skill for template tools
                    "args": step["args_template"],
                    "purpose": step["purpose"],
                    "output_var": step["output_var"],
                    "dependencies": step["dependencies"],
                    "parallel_group": step["parallel_group"],
                    "required": True
                }
                for step in template["steps"]
            ],
            "expected_duration_seconds": template["expected_duration_seconds"],
            "can_parallelize": template["can_parallelize"],
            "final_output_vars": template["final_output_vars"],
            "synthesis_required": template["synthesis_required"],
            "reasoning": "Fallback plan due to LLM planner error",
            "planner_metadata": {
                "fallback_used": True,
                "error": str(e)
            }
        }
        
        return fallback_plan


def _format_skill_tools_for_prompt(tool_registry: Dict[str, Dict[str, Any]]) -> str:
    """Format PM Skill tools for prompt injection."""
    # Filter to backlog-related skills only
    backlog_skills = {
        name: info for name, info in tool_registry.items()
        if any(keyword in name for keyword in [
            "backlog", "velocity", "estimate", "area_path", "team_area"
        ])
    }
    
    lines = []
    for tool_name, tool_info in backlog_skills.items():
        lines.append(f"**{tool_name}**")
        lines.append(f"  - Description: {tool_info.get('description', 'N/A')}")
        lines.append(f"  - Required: {tool_info.get('required_params', [])}")
        lines.append(f"  - Optional: {list(tool_info.get('optional_params', {}).keys())}")
        lines.append(f"  - Use cases: {', '.join(tool_info.get('use_cases', [])[:3])}")
        lines.append("")
    
    return "\n".join(lines) if lines else "(No backlog-related PM Skill tools available)"


def _format_mcp_tools_for_prompt(tool_registry: Dict[str, Dict[str, Any]]) -> str:
    """Format MCP tools for prompt injection (limited to most relevant)."""
    # Select only the most relevant MCP tools for backlog operations
    relevant_tools = [
        "search_workitem",
        "wit_get_work_items_batch_by_ids",
        "work_list_team_iterations",
        "work_get_team_capacity",
        "core_get_team_area_path"
    ]
    
    lines = []
    for tool_name in relevant_tools:
        if tool_name in tool_registry:
            tool_info = tool_registry[tool_name]
            lines.append(f"**{tool_name}**")
            lines.append(f"  - Description: {tool_info.get('description', 'N/A')}")
            lines.append(f"  - Required: {tool_info.get('required_args', [])}")
            lines.append("")
    
    return "\n".join(lines) if lines else "(No relevant MCP tools available)"


def _validate_plan(plan: Dict[str, Any]) -> tuple[bool, List[str]]:
    """
    Validate execution plan structure and dependencies.
    
    Returns:
        (is_valid, list_of_errors)
    """
    errors = []
    
    # Check required top-level fields
    required_fields = ["intent", "confidence", "steps"]
    for field in required_fields:
        if field not in plan:
            errors.append(f"Missing required field: {field}")
    
    # Validate steps
    if "steps" in plan:
        steps = plan["steps"]
        if not isinstance(steps, list) or len(steps) == 0:
            errors.append("Steps must be a non-empty list")
        else:
            step_ids = {step.get("step_id") for step in steps}
            
            for i, step in enumerate(steps):
                # Check required step fields
                step_required = ["step_id", "tool", "args", "purpose", "output_var", "dependencies"]
                for field in step_required:
                    if field not in step:
                        errors.append(f"Step {i}: Missing required field '{field}'")
                
                # Validate dependencies reference valid step IDs
                deps = step.get("dependencies", [])
                for dep in deps:
                    if dep not in step_ids:
                        errors.append(f"Step {step.get('step_id')}: Invalid dependency '{dep}'")
    
    # Validate confidence range
    confidence = plan.get("confidence", 0)
    if not (0.0 <= confidence <= 1.0):
        errors.append(f"Confidence must be between 0 and 1, got {confidence}")
    
    is_valid = len(errors) == 0
    return is_valid, errors


def _autocorrect_plan(plan: Dict[str, Any], errors: List[str]) -> Dict[str, Any]:
    """
    Attempt to auto-correct common plan issues.
    
    Args:
        plan: The plan with validation errors
        errors: List of validation errors
    
    Returns:
        Corrected plan (best effort)
    """
    corrected = plan.copy()
    
    # Add missing top-level fields with defaults
    if "intent" not in corrected:
        corrected["intent"] = "backlog_health_check"
    
    if "confidence" not in corrected:
        corrected["confidence"] = 0.5
    
    if "plan_type" not in corrected:
        corrected["plan_type"] = "full"
    
    if "steps" not in corrected or not corrected["steps"]:
        # Critical error - can't autocorrect empty steps
        logger.error("Cannot autocorrect plan with no steps")
        return corrected
    
    # Fix step fields
    for i, step in enumerate(corrected.get("steps", [])):
        if "step_id" not in step:
            step["step_id"] = str(i + 1)
        
        if "dependencies" not in step:
            step["dependencies"] = []
        
        if "parallel_group" not in step:
            step["parallel_group"] = i + 1
        
        if "required" not in step:
            step["required"] = True
        
        if "tool_type" not in step:
            # Guess based on tool name
            if step.get("tool", "").startswith("wit_") or step.get("tool", "").startswith("work_") or step.get("tool", "").startswith("core_"):
                step["tool_type"] = "mcp"
            else:
                step["tool_type"] = "pm_skill"
    
    # Fix confidence range
    if "confidence" in corrected:
        corrected["confidence"] = max(0.0, min(1.0, corrected["confidence"]))
    
    # Add missing metadata
    if "synthesis_required" not in corrected:
        # If last step is generate_recommendations, synthesis is needed
        last_tool = corrected["steps"][-1].get("tool", "")
        corrected["synthesis_required"] = "recommendation" in last_tool.lower()
    
    if "can_parallelize" not in corrected:
        # Check if any steps have parallel_group > 1
        parallel_groups = {step.get("parallel_group", 1) for step in corrected["steps"]}
        corrected["can_parallelize"] = len(parallel_groups) > 1
    
    logger.info("Autocorrected plan with best-effort fixes")
    return corrected


# Export
__all__ = ["plan_backlog_query"]
