"""
LLM-based result synthesis and multi-agent output synthesis.

This module provides:
1. summarize_mcp_result() - Synthesize raw MCP tool results into human-readable summaries
   - Handles skill results (iteration_report, bug_areas_highlight)
   - Handles single work items (wit_get_work_item)
   - Handles list results (search, WIQL queries)
   - Supports intelligent synthesis with analysis criteria
   - Supports team enrichment for dynamic assignments

2. synthesize_agent_outputs() - Synthesize outputs from multiple agents
   - Used by orchestrator for final user-facing responses
   - Accepts generic agent result dicts
   - Falls back to deterministic composition if LLM unavailable

Moved from agents/pm_agent/llm.py to utilities/llm/ for reusability.
Generic, agent-agnostic implementation - no hardcoded tool/agent names.

CRITICAL: Uses OpenAI API configured via config.yaml (api_keys.openai_api_key)
All LLM calls create spans within the current trace for unified observability.
"""

import asyncio
import json
import re
import logging
import time
from typing import Optional, Dict, Any, List

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from utilities.langfuse_client import (
    get_langfuse_client, create_span, finalize_span
)

from .utils import (
    sanitize_json_text,
    run_in_executor_with_context,
    extract_work_item_metadata,
)

from .instructions import load_synthesizer_instructions, load_kb_instructions

logger = logging.getLogger(__name__)
_langfuse_client = get_langfuse_client()


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC STATE CATEGORIES INJECTION
# ══════════════════════════════════════════════════════════════════════════════

def _build_state_categories_text() -> str:
    """Build state categories text from centralized config for LLM prompts.

    Reads from config.yaml → state_categories so that adding a new state
    in config automatically propagates to every LLM synthesis call.

    Returns a block like:
       - Completed: Closed, Resolved, Done, UAT Complete, Removed, Completed
       - In Progress: Active, Design, Code Review, QA, UAT, PRE-PROD, ...
       - Not Started: New, Ready, Proposed
       - Blocked: On Hold, Issues Found, Reopened
    """
    from config import config
    cats = config.get_state_categories()
    lines = []
    for label, states in cats.items():
        lines.append(f"   - {label}: {', '.join(states)}")
    return "\n".join(lines)


def _inject_state_categories(prompt_text: str) -> str:
    """Replace {{STATE_CATEGORIES}} placeholder with dynamic state list."""
    if "{{STATE_CATEGORIES}}" in prompt_text:
        return prompt_text.replace("{{STATE_CATEGORIES}}", _build_state_categories_text())
    return prompt_text


# ══════════════════════════════════════════════════════════════════════════════
# PRE-COMPUTED GROUND TRUTH FACTS
# ══════════════════════════════════════════════════════════════════════════════

def _precompute_summary_facts(
    results: List[Dict[str, Any]],
    query: str,
    analysis_criteria: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Deterministically compute ALL factual/numerical data from raw work-item
    results. The output is injected as GROUND TRUTH into the LLM prompt so
    the model never needs to count, classify, or compute percentages itself.

    This function is the primary defence against hallucination: every number
    the LLM presents comes from here, not from LLM arithmetic on raw JSON.

    Args:
        results: Pre-filtered list of work-item dicts
        query:   Original user query
        analysis_criteria: Optional planner analysis criteria

    Returns:
        Dict with keys: total_count, by_state, by_state_category, by_type,
        by_assignee, by_area_path, by_priority, unassigned_count,
        completion_rate, stuck_items, items_compact
    """
    from config import config as _cfg

    facts: Dict[str, Any] = {
        "total_count": len(results),
        "by_state": {},
        "by_state_category": {},
        "by_type": {},
        "by_assignee": {},
        "by_area_path": {},
        "by_priority": {},
        "unassigned_count": 0,
        "completion_rate": None,
        "stuck_items": [],
        "items_compact": [],
    }

    # Determine stuck-days threshold from query (default 5)
    _stuck_days = 5
    import re as _re_facts
    _m = _re_facts.search(r'(?:more than|over|>)\s*(\d+)\s*days?', query.lower())
    if _m:
        _stuck_days = int(_m.group(1))

    completed_labels = {"Completed"}

    for item in results:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields", {})
        if not fields or not isinstance(fields, dict):
            fields = item

        # --- State ---
        state = (
            fields.get("System.State")
            or fields.get("system.state")
            or "Unknown"
        )
        facts["by_state"][state] = facts["by_state"].get(state, 0) + 1
        category = _cfg.classify_state(state)
        facts["by_state_category"][category] = facts["by_state_category"].get(category, 0) + 1

        # --- Type ---
        wi_type = (
            fields.get("System.WorkItemType")
            or fields.get("system.workitemtype")
            or "Unknown"
        )
        facts["by_type"][wi_type] = facts["by_type"].get(wi_type, 0) + 1

        # --- Assignee ---
        assigned_to = (
            fields.get("System.AssignedTo")
            or fields.get("system.assignedto")
        )
        name = ""
        if assigned_to:
            if isinstance(assigned_to, dict):
                name = (assigned_to.get("displayName") or assigned_to.get("uniqueName", "")).strip()
            elif isinstance(assigned_to, str):
                name = assigned_to.strip()
        if name:
            facts["by_assignee"][name] = facts["by_assignee"].get(name, 0) + 1
        else:
            facts["unassigned_count"] += 1

        # --- Area path ---
        area = (
            fields.get("System.AreaPath")
            or fields.get("system.areapath")
            or "Uncategorized"
        )
        facts["by_area_path"][area] = facts["by_area_path"].get(area, 0) + 1

        # --- Priority ---
        priority = (
            fields.get("Microsoft.VSTS.Common.Priority")
            or fields.get("microsoft.vsts.common.priority")
            or "Unset"
        )
        facts["by_priority"][str(priority)] = facts["by_priority"].get(str(priority), 0) + 1

        # --- Compact item summary ---
        item_id = item.get("id") or fields.get("System.Id") or fields.get("system.id", "?")
        title = (fields.get("System.Title") or fields.get("system.title", ""))[:80]
        days = fields.get("_days_in_state", "?")

        facts["items_compact"].append(
            f"#{item_id} | {wi_type} | {state} | {name or 'Unassigned'} | P{priority} | {days}d | {title}"
        )

        # --- Stuck items ---
        if isinstance(days, (int, float)) and days >= _stuck_days and category == "In Progress":
            facts["stuck_items"].append({
                "id": item_id,
                "title": title,
                "state": state,
                "days_in_state": days,
                "assignee": name or "Unassigned",
            })

    # --- Completion rate ---
    completed_count = facts["by_state_category"].get("Completed", 0)
    total = facts["total_count"]
    if total > 0:
        facts["completion_rate"] = {
            "completed": completed_count,
            "total": total,
            "percentage": round(completed_count / total * 100, 1),
        }

    logger.info(
        "[GROUND_TRUTH] Pre-computed: total=%d, states=%s, types=%s, unassigned=%d, completion=%.1f%%",
        total,
        facts["by_state"],
        facts["by_type"],
        facts["unassigned_count"],
        facts["completion_rate"]["percentage"] if facts["completion_rate"] else 0,
    )
    return facts


def _format_ground_truth_block(facts: Dict[str, Any]) -> str:
    """
    Format pre-computed facts into a plain-text block for LLM injection.

    The block is clearly demarcated so the LLM recognises it as authoritative.
    """
    lines = [
        "",
        "═══════════════════════════════════════════════════",
        "GROUND TRUTH (pre-computed by system — AUTHORITATIVE, DO NOT override):",
        "═══════════════════════════════════════════════════",
        f"Total items: {facts['total_count']}",
    ]

    # State breakdown
    if facts["by_state"]:
        state_parts = [f"{s}={c}" for s, c in sorted(facts["by_state"].items(), key=lambda x: -x[1])]
        lines.append(f"By State: {', '.join(state_parts)}")

    # State category breakdown
    if facts["by_state_category"]:
        cat_parts = [f"{c}={n}" for c, n in sorted(facts["by_state_category"].items(), key=lambda x: -x[1])]
        lines.append(f"By Category: {', '.join(cat_parts)}")

    # Type breakdown
    if facts["by_type"]:
        type_parts = [f"{t}={c}" for t, c in sorted(facts["by_type"].items(), key=lambda x: -x[1])]
        lines.append(f"By Type: {', '.join(type_parts)}")

    # Unassigned count
    lines.append(f"Unassigned items: {facts['unassigned_count']}")

    # Completion rate
    cr = facts.get("completion_rate")
    if cr:
        lines.append(f"Completion Rate: {cr['completed']}/{cr['total']} = {cr['percentage']}%")

    # Top assignees (top 10)
    if facts["by_assignee"]:
        sorted_assignees = sorted(facts["by_assignee"].items(), key=lambda x: -x[1])[:10]
        assignee_parts = [f"{name}={cnt}" for name, cnt in sorted_assignees]
        lines.append(f"Top Assignees: {', '.join(assignee_parts)}")

    # Priority breakdown
    if facts["by_priority"]:
        prio_parts = [f"P{p}={c}" for p, c in sorted(facts["by_priority"].items())]
        lines.append(f"By Priority: {', '.join(prio_parts)}")

    # Area path breakdown (top 10)
    if facts["by_area_path"]:
        sorted_areas = sorted(facts["by_area_path"].items(), key=lambda x: -x[1])[:10]
        area_parts = [f"{a}={c}" for a, c in sorted_areas]
        lines.append(f"Top Areas: {', '.join(area_parts)}")

    # Stuck items
    if facts["stuck_items"]:
        lines.append(f"Stuck items (>{len(facts['stuck_items'])} in progress for extended days):")
        for si in facts["stuck_items"][:10]:
            lines.append(f"  #{si['id']} ({si['state']}, {si['days_in_state']}d) — {si['assignee']} — {si['title']}")

    lines.append("═══════════════════════════════════════════════════")
    lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# DEFAULT SYNTHESIZER SYSTEM PROMPT (used when file not found)
# ══════════════════════════════════════════════════════════════════════════════


async def derive_style_guidelines(user_query: str, analysis_criteria: Optional[Dict[str, Any]] = None, step_context: Optional[List[str]] = None, max_output_tokens: int = 200) -> str:
    """
    Derive concise style guidelines for the synthesizer given a user query and
    optional planner-provided analysis_criteria or step context.

    Behavior:
    - If `analysis_criteria` exists, convert it directly to guidance (deterministic).
    - Otherwise, call the LLM (when available) with a brief prompt to produce
      concise, non-tool-specific synthesis instructions.
    - Falls back to a small heuristic when LLM is not available.
    """
    from config import config

    # Prefer explicit planner guidance
    if analysis_criteria:
        parts = []
        intent = analysis_criteria.get("intent") or analysis_criteria.get("goal") or ""
        if intent:
            parts.append(f"User intent: {intent}")
        filter_logic = analysis_criteria.get("filter_logic", "")
        if filter_logic:
            parts.append(f"Filter/analysis logic: {filter_logic}")
        evidence_fields = analysis_criteria.get("evidence_fields", [])
        if evidence_fields:
            parts.append(f"Key fields to analyze: {', '.join(evidence_fields)}")
        if step_context:
            parts.append("Step context:\n" + "\n".join(step_context))
        return "\n".join(parts)

    # If no criteria, return empty (no LLM available means no synthesis guidance)
    if not OPENAI_AVAILABLE or not config.openai_api_key:
        return ""

    # Build a compact prompt for LLM
    prompt_lines = [
        "You are an assistant that produces concise synthesis instructions for a downstream synthesizer.",
        "Given the user's query and optional step context, output 3-6 short guideline lines that describe what the synthesizer should do (format, key metrics, calculations, grouping).",
        "Do NOT reference tools, APIs, or any internal implementation details. Keep it generic and focused on the user's intent.",
        "Output only the guideline lines, one per line."
    ]
    prompt_lines.append(f"User query: {user_query}")
    if step_context:
        prompt_lines.append("Step context:")
        prompt_lines.extend(step_context[:10])

    prompt = "\n\n".join(prompt_lines)

    def _call_openai_style(p: str) -> str:
        import httpx
        client = OpenAI(api_key=config.openai_api_key, timeout=httpx.Timeout(20.0, connect=5.0))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You produce concise synthesis guidelines for data summarization."},
                {"role": "user", "content": p}
            ],
            temperature=0.0,
            max_tokens=int(max_output_tokens)
        )
        return response.choices[0].message.content

    try:
        derived = await asyncio.wait_for(run_in_executor_with_context(_call_openai_style, prompt), timeout=20.0)
        return (derived or "").strip()
    except Exception:
        # If LLM call fails, return empty guidance (no hardcoded fallback)
        return ""


DEFAULT_SYNTHESIZER_PROMPT = """You are an intelligent PM assistant that analyzes and formats agent outputs based on user intent.

CORE PRINCIPLES:
1. **Read the user's query carefully** - understand what they're asking for
2. **Analyze when needed** - if the query asks for filtering, alignment checks, or specific insights, DO IT
3. **Format clearly** - present information in clean markdown with proper structure
4. **Be evidence-based** - only filter/flag items based on objective criteria from the data

ADAPTIVE BEHAVIOR:
- If query asks for "alignment with goal", "relevant items", "related to X": Filter and explain why items match/don't match
- If query asks for "issues", "problems", "blockers": Highlight problematic items with reasoning
- If query is neutral ("show me", "list", "get"): Format all items cleanly without filtering
- If query asks for analysis ("what's slowing", "at risk", "don't align"): Analyze the data and provide insights

SPECIAL CASE - TEAM/ITERATION QUERIES:
- If query asks "which teams", "what teams", "all teams": List ALL teams from the data
- If data contains team lists/iterations: Present them clearly in a table format
- For team iteration queries: Show which teams have active iterations
- DO NOT default to a single team when user asks for "all teams"

FORMATTING RULES:
1. Extract meaningful data (ignore metadata like execution_time, workflow)
2. Use markdown: headers, bullet points, numbered lists, bold/italic for emphasis
3. For long lists (>20 items): Group by category or show summary + top items
4. For reports: Start with key metrics, then details
5. Do NOT return raw JSON - convert to readable text
6. Do NOT create plans or tool calls - work with provided data only

ANALYSIS RULES (when query requires filtering):
- Look for objective indicators in titles/descriptions
- Common non-product work indicators: DevOps, migration, admin, access, account, setup, legal, accounting, infrastructure
- Common product work indicators: feature names, UI/UX, bug fixes related to features, user-facing functionality
- When unsure, include the item but note uncertainty
- Always explain your reasoning for filtering

WORK ITEM ID FORMAT (CRITICAL):
- ALWAYS use numeric IDs with clickable links: [#73944](https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/73944)
- NEVER use GUIDs: #[59de9a9f-...]
- Extract from: item['id'] or item['fields']['System.Id']
- Link format: [#ID](https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/ID)"""


# ══════════════════════════════════════════════════════════════════════════════
# WORK ITEM ID VALIDATION ADDENDUM
# ══════════════════════════════════════════════════════════════════════════════

ID_VALIDATION_ADDENDUM = """

CRITICAL WORK ITEM ID RULES (HIGHEST PRIORITY):
1. ALWAYS use numeric IDs with clickable ADO links: [#73944](https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/73944)
2. Extract from: item['id'] OR item['fields']['System.Id']
3. NEVER use: item['workItemId'] (it's a GUID, not a work item ID)
4. If you see a GUID, extract the numeric ID from fields - don't display the GUID
5. Link format: [**#73944**](https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/73944)

Example work item data:
```json
{"id": 73944, "fields": {"System.Id": 73944, "System.Title": "Bug"}, "workItemId": "59de9a9f-..."}
```
Your output: [**#73944**](https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/73944) ✅ NOT: #[59de9a9f-...] ❌
"""


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARIZE MCP RESULT
# ══════════════════════════════════════════════════════════════════════════════

async def summarize_mcp_result(
    query: str,
    mcp_result: str,
    system_prompt: str = "",
    session_id: str = None,
    plan: Optional[Dict[str, Any]] = None,
    team_enrichment: Optional[Dict[str, Any]] = None,
    sprint_context: Optional[Dict[str, Any]] = None
) -> str:
    """
    Use LLM (or heuristics as fallback) to summarize raw MCP JSON result.
    
    Handles:
    - Skill results (iteration_report, bug_areas_highlight)
    - Single work items (wit_get_work_item)
    - List results (search_workitem, WIQL queries)
    - Intelligent synthesis with analysis_criteria from planner
    - Team enrichment for dynamic work item assignments
    
    Generic approach: Detects result shape to determine handling strategy.
    No hardcoded tool-specific logic.

    Args:
        query: Original user query
        mcp_result: Raw MCP JSON response (as string)
        system_prompt: System instructions (kept for backward compatibility)
        session_id: Session ID for unified trace tracking
        plan: Optional planner output with analysis_criteria for evidence-based synthesis
        team_enrichment: Optional team data including capacity and developer skills

    Returns:
        Human-readable summary string
    """
    if not query or not mcp_result:
        return mcp_result

    # Create span for tracing
    synth_span = create_span(
        name="llm_summarize_mcp_result",
        input_data={"query": query[:200], "result_length": len(mcp_result)},
        metadata={
            "step": "summarize",
            "layer": "shared",
            "has_analysis_criteria": bool(plan and plan.get('analysis_criteria'))
        },
        session_id=session_id
    )

    # Set this span as current trace context so child spans (like llm_intelligent_synthesis) nest properly
    from utilities.langfuse_client import set_current_trace, get_current_trace
    previous_trace = get_current_trace()
    set_current_trace(synth_span)

    try:
        # Try to parse MCP result as JSON
        try:
            result_obj = json.loads(mcp_result)
        except (json.JSONDecodeError, TypeError):
            # Not JSON; return as-is
            finalize_span(synth_span, output={"status": "passthrough"}, status="success")
            return mcp_result

        # ══════════════════════════════════════════════════════════════
        # Handle skill results (from local skills like iteration_report)
        # These have 'success', 'result', and optionally 'metadata' keys
        # ══════════════════════════════════════════════════════════════
        if isinstance(result_obj, dict) and "success" in result_obj and "result" in result_obj:
            result = _format_skill_result(result_obj)
            finalize_span(synth_span, output={"status": "skill_result", "synthesized_output": result[:1000]}, status="success")
            return result

        # ══════════════════════════════════════════════════════════════
        # Handle single work item result (from wit_get_work_item)
        # ══════════════════════════════════════════════════════════════
        if isinstance(result_obj, dict) and "id" in result_obj and "fields" in result_obj:
            result = _format_single_work_item(result_obj)
            finalize_span(synth_span, output={"status": "single_work_item", "synthesized_output": result[:1000]}, status="success")
            return result

        # ══════════════════════════════════════════════════════════════
        # Handle list results (from search or WIQL)
        # ══════════════════════════════════════════════════════════════
        if isinstance(result_obj, dict):
            result = await _format_list_results(
                result_obj, query, plan, team_enrichment, session_id, sprint_context
            )
            finalize_span(synth_span, output={"status": "list_result", "synthesized_output": result[:1000]}, status="success")
            return result

        # Fallback: return formatted JSON
        result_text = json.dumps(result_obj, indent=2)[:500]
        finalize_span(synth_span, output={"status": "json_fallback", "synthesized_output": result_text}, status="success")
        return result_text
        
    except Exception as e:
        logger.error("[LLM_SYNTHESIZER] summarize_mcp_result failed: %s", e)
        finalize_span(synth_span, output={"error": str(e)}, status="error", level="ERROR")
        return mcp_result
    finally:
        # Restore previous trace context
        if previous_trace:
            set_current_trace(previous_trace)


def _format_skill_result(result_obj: Dict[str, Any]) -> str:
    """Format skill execution results."""
    skill_result = result_obj.get("result", {})
    metadata = result_obj.get("metadata", {})
    
    if not result_obj.get("success"):
        error = result_obj.get("error", "Unknown error")
        return f"❌ Skill execution failed: {error}"
    
    # Handle iteration_report result
    if isinstance(skill_result, dict):
        # Check for iteration_report specific fields
        if "csv_file" in skill_result or "html_file" in skill_result or "total_items" in skill_result:
            total = skill_result.get("total_items", 0)
            filtered = skill_result.get("filtered_items", 0)
            csv_file = skill_result.get("csv_file", "")
            html_file = skill_result.get("html_file", "")
            email_sent = skill_result.get("email_sent", False)
            
            project = metadata.get("project", "Unknown")
            iteration = metadata.get("iteration", "Unknown")
            
            lines = [
                f"📊 **Iteration Report Generated**",
                f"",
                f"**Project:** {project}",
                f"**Iteration:** {iteration}",
                f"**Total Items:** {total}",
            ]
            
            if filtered and filtered != total:
                lines.append(f"**Filtered Items:** {filtered}")
            
            if csv_file:
                lines.append(f"")
                lines.append(f"📁 CSV Report: `{csv_file}`")
            
            if html_file:
                lines.append(f"📄 HTML Report: `{html_file}`")
            
            if email_sent:
                lines.append(f"")
                lines.append(f"✉️ Email sent successfully")
            
            return "\n".join(lines)
        
        # Handle bug_areas_highlight result
        if "areas_analyzed" in skill_result or "recurring_bugs" in skill_result:
            areas = skill_result.get("areas_analyzed", 0)
            recurring = skill_result.get("recurring_bugs", 0)
            report_file = skill_result.get("report_file", "")
            email_sent = skill_result.get("email_sent", False)
            
            lines = [
                f"🐛 **Bug Areas Analysis Complete**",
                f"",
                f"**Areas Analyzed:** {areas}",
                f"**Recurring Bugs Found:** {recurring}",
            ]
            
            if report_file:
                lines.append(f"")
                lines.append(f"📄 Report: `{report_file}`")
            
            if email_sent:
                lines.append(f"✉️ Email sent successfully")
            
            return "\n".join(lines)
        
        # Generic skill result - show as formatted summary
        lines = ["✅ **Skill executed successfully**", ""]
        for key, value in skill_result.items():
            if isinstance(value, (str, int, float, bool)):
                lines.append(f"**{key.replace('_', ' ').title()}:** {value}")
        return "\n".join(lines)
    
    # Fallback for non-dict skill results
    return f"✅ Skill completed: {skill_result}"


def _format_single_work_item(result_obj: Dict[str, Any]) -> str:
    """Format a single work item result."""
    fields = result_obj.get("fields", {})
    wi_id = result_obj.get("id")
    wi_type = fields.get("System.WorkItemType", "Work Item")
    title = fields.get("System.Title", "No title")
    state = fields.get("System.State", "Unknown")
    
    assigned_to = fields.get("System.AssignedTo", {})
    if isinstance(assigned_to, dict):
        assigned_to = assigned_to.get("displayName", assigned_to.get("uniqueName", "Unassigned"))
    elif not assigned_to:
        assigned_to = "Unassigned"
    
    description = fields.get("System.Description", "")
    # Strip HTML tags from description
    if description:
        description = re.sub(r'<[^>]+>', '', description)
        description = description[:500] + "..." if len(description) > 500 else description
    
    area_path = fields.get("System.AreaPath", "")
    iteration_path = fields.get("System.IterationPath", "")
    created_date = fields.get("System.CreatedDate", "")
    changed_date = fields.get("System.ChangedDate", "")
    
    created_by = fields.get("System.CreatedBy", {})
    if isinstance(created_by, dict):
        created_by = created_by.get("displayName", created_by.get("uniqueName", ""))
    
    # Build formatted output
    ado_link = f"https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/{wi_id}"
    lines = [
        f"📋 **{wi_type} [#{wi_id}]({ado_link})**: {title}",
        f"",
        f"**State:** {state}",
        f"**Assigned To:** {assigned_to}",
        f"**Area Path:** {area_path}",
        f"**Iteration:** {iteration_path}",
    ]
    
    if created_by:
        lines.append(f"**Created By:** {created_by}")
    if created_date:
        lines.append(f"**Created:** {created_date[:10] if len(created_date) >= 10 else created_date}")
    if changed_date:
        lines.append(f"**Last Updated:** {changed_date[:10] if len(changed_date) >= 10 else changed_date}")
    
    # Add type-specific fields
    if wi_type == "Bug":
        repro_steps = fields.get("Microsoft.VSTS.TCM.ReproSteps", "")
        if repro_steps:
            repro_steps = re.sub(r'<[^>]+>', '', repro_steps)
            repro_steps = repro_steps[:300] + "..." if len(repro_steps) > 300 else repro_steps
            lines.append(f"")
            lines.append(f"**Repro Steps:** {repro_steps}")
        
        severity = fields.get("Microsoft.VSTS.Common.Severity", "")
        priority = fields.get("Microsoft.VSTS.Common.Priority", "")
        if severity:
            lines.append(f"**Severity:** {severity}")
        if priority:
            lines.append(f"**Priority:** {priority}")
    
    if description:
        lines.append(f"")
        lines.append(f"**Description:**")
        lines.append(description)
    
    return "\n".join(lines)


def _apply_filter_logic(
    results: List[Dict[str, Any]],
    filter_logic: str,
    intent: str,
    plan: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Pre-filter results based on analysis_criteria.filter_logic.
    
    This function applies programmatic filtering BEFORE passing to LLM,
    ensuring the synthesizer only sees relevant items.
    
    Args:
        results: List of work item dicts
        filter_logic: Human-readable filter description from planner
        intent: The intent type (all, filtered, blocked, etc.)
        
    Returns:
        Filtered list of work items
    """
    if not results:
        return results
    
    # If intent is "all", return everything
    if intent.lower() == "all":
        logger.info("[SYNTHESIZER] Intent='all' - no filtering applied, returning all %d items", len(results))
        return results
    
    # Parse filter_logic to extract conditions
    filter_lower = filter_logic.lower() if filter_logic else ""
    
    # Extract workItemType filter
    work_item_type_filter = None
    for wi_type in ["bug", "user story", "task", "feature", "epic"]:
        if wi_type in filter_lower:
            work_item_type_filter = wi_type
            break
    
    # Extract state filter from centralized config
    state_filter = None
    from config import config as _synth_cfg
    _all_known_states = [s.lower() for s in _synth_cfg.get_all_known_states()]
    for state in _all_known_states:
        if state in filter_lower:
            state_filter = state
            break
    
    # OVERRIDE WITH PLAN ARGS IF AVAILABLE (MORE ACCURATE)
    # CRITICAL FIX: Support MULTIPLE states and types (not just single-element arrays)
    work_item_types_filter = []  # List of types to accept
    states_filter = []  # List of states to accept
    
    if plan and isinstance(plan, dict):
        args = plan.get('args', {})
        if args:
            # Check for workItemType override - accept multiple types
            p_types = args.get('workItemType')
            if p_types:
                if isinstance(p_types, list):
                    work_item_types_filter = [t.lower() for t in p_types]
                elif isinstance(p_types, str):
                    work_item_types_filter = [p_types.lower()]
            
            # Check for state override - accept multiple states
            p_state = args.get('state')
            if p_state:
                if isinstance(p_state, list):
                    states_filter = [s.lower() for s in p_state]
                elif isinstance(p_state, str):
                    states_filter = [p_state.lower()]
    
    # Check for blocked intent
    is_blocked_filter = "blocked" in filter_lower or intent.lower() == "blocked"
    
    # Check for unassigned filter
    is_unassigned_filter = "unassigned" in filter_lower or "null" in filter_lower
    
    filtered_results = []
    
    for item in results:
        fields = item.get("fields", {})
        if not fields and isinstance(item, dict):
            # Sometimes fields are at top level
            fields = item
        
        # Build case-insensitive field lookup since search_workitem returns
        # lowercase keys (system.state) while other APIs use PascalCase (System.State)
        fields_lower = {k.lower(): v for k, v in fields.items()} if fields else {}
        
        item_type = (fields.get("System.WorkItemType") or fields_lower.get("system.workitemtype", "")).lower()
        item_state = (fields.get("System.State") or fields_lower.get("system.state", "")).lower()
        item_tags = (fields.get("System.Tags") or fields_lower.get("system.tags", "")).lower()
        item_assigned_to = fields.get("System.AssignedTo") or fields_lower.get("system.assignedto", {})
        
        # Handle different formats of AssignedTo
        if isinstance(item_assigned_to, dict):
            assignee_name = item_assigned_to.get("displayName", "")
        elif isinstance(item_assigned_to, str):
            assignee_name = item_assigned_to
        else:
            assignee_name = ""
        
        # Apply work item type filter (accept if in list of types)
        if work_item_types_filter:
            if item_type not in work_item_types_filter:
                continue
        
        # Apply state filter (accept if in list of states)
        if states_filter:
            if item_state not in states_filter:
                continue
        
        # Apply blocked filter
        if is_blocked_filter:
            _blocked_states = [s.lower() for s in _synth_cfg.get_states_for_category('blocked')]
            if "blocked" not in item_tags and item_state not in _blocked_states:
                continue
        
        # Apply unassigned filter
        if is_unassigned_filter:
            if assignee_name:
                continue
        
        filtered_results.append(item)
    
    logger.info("[SYNTHESIZER] Filtered %d → %d items (types=%s, states=%s, blocked=%s, unassigned=%s)",
                len(results), len(filtered_results), work_item_types_filter, states_filter,
                is_blocked_filter, is_unassigned_filter)
    
    return filtered_results


async def _format_list_results(
    result_obj: Dict[str, Any],
    query: str,
    plan: Optional[Dict[str, Any]],
    team_enrichment: Optional[Dict[str, Any]],
    session_id: str = None,
    sprint_context: Optional[Dict[str, Any]] = None
) -> str:
    """Format list results from search or WIQL queries."""
    # ════════════════════════════════════════════════════════════════════
    # COMPREHENSIVE LOGGING FOR DEBUGGING SYNTHESIS FLOW
    # ════════════════════════════════════════════════════════════════════
    logger.info("[SYNTHESIZER] _format_list_results called")
    logger.info("[SYNTHESIZER] Plan type: %s, Plan: %s", type(plan), str(plan)[:500] if plan else "None")
    
    count = result_obj.get("count", 0)
    # ToolExecutor uses "items", older code used "results", WIQL uses "workItems"
    results = result_obj.get("items") or result_obj.get("results") or result_obj.get("workItems", [])
    
    logger.info("[SYNTHESIZER] Extracted %d items, count=%d", len(results) if results else 0, count)

    if count == 0 and not results:
        return f"No items found matching '{query}'.'"

    if isinstance(results, list) and len(results) > 0:
        # Check if planner provided analysis_criteria for intelligent synthesis
        analysis_criteria = plan.get('analysis_criteria') if plan else None
        
        logger.info("[SYNTHESIZER] analysis_criteria: %s", json.dumps(analysis_criteria) if analysis_criteria else None)
        logger.info("[SYNTHESIZER] OPENAI_AVAILABLE: %s", OPENAI_AVAILABLE)
        
        # ════════════════════════════════════════════════════════════════════
        # CRITICAL: Pre-filter results based on analysis_criteria.filter_logic
        # This ensures only relevant items are passed to synthesis
        # ════════════════════════════════════════════════════════════════════
        if analysis_criteria:
            intent = analysis_criteria.get('intent', 'all')
            filter_logic = analysis_criteria.get('filter_logic', '')
            
            # Apply programmatic filtering BEFORE LLM synthesis
            original_count = len(results)
            results = _apply_filter_logic(results, filter_logic, intent, plan)
            
            logger.info("[SYNTHESIZER] Pre-filtering applied: %d → %d items (intent=%s)",
                        original_count, len(results), intent)
            
            # Update count for display
            count = len(results)
            
            # If filtering resulted in no items, return informative message
            if count == 0 and original_count > 0:
                # Provide context about what was filtered out
                return f"Found {original_count} items in the data source, but none match the filter criteria: {filter_logic}. All {original_count} items were filtered out."
            elif count == 0:
                return f"No items found matching the filter: {filter_logic}"
        
        # If analysis_criteria is provided, use LLM for intelligent synthesis
        if analysis_criteria and OPENAI_AVAILABLE:
            from config import config
            logger.info("[SYNTHESIZER] OpenAI API key available: %s", bool(config.openai_api_key))
            if config.openai_api_key:
                try:
                    logger.info("[SYNTHESIZER] >>> Calling _intelligent_synthesis with %d pre-filtered items", len(results))
                    synthesized = await _intelligent_synthesis(
                        query, results, analysis_criteria, team_enrichment, sprint_context, session_id
                    )
                    logger.info("[SYNTHESIZER] _intelligent_synthesis returned: %s chars", len(synthesized) if synthesized else 0)
                    if synthesized:
                        logger.info("[SYNTHESIZER] SUCCESS: Returning synthesized response")
                        return synthesized
                    else:
                        logger.warning("[SYNTHESIZER] _intelligent_synthesis returned None/empty")
                except Exception as e:
                    logger.warning("[LLM_SYNTHESIZER] Intelligent synthesis failed: %s", e)
                    import traceback
                    logger.warning("[LLM_SYNTHESIZER] Traceback: %s", traceback.format_exc())
                    # Fall through to simple formatting
            else:
                logger.warning("[SYNTHESIZER] No OpenAI API key configured")
        else:
            logger.warning(
                "[SYNTHESIZER] ⚠️ SKIPPING INTELLIGENT SYNTHESIS: "
                "analysis_criteria=%s, OPENAI_AVAILABLE=%s. "
                "Using fallback formatting. Ensure planner provides analysis_criteria with intent, filter_logic, and evidence_fields for dynamic synthesis.",
                bool(analysis_criteria), OPENAI_AVAILABLE
            )
        
        # Fallback: Format results as human-readable text (simple list)
        logger.info("[SYNTHESIZER] FALLBACK: Using _simple_list_format for %d results", len(results))
        return _simple_list_format(results)

    # Fallback: return formatted JSON
    return json.dumps(result_obj, indent=2)[:500]


async def _intelligent_synthesis(
    query: str,
    results: List[Dict[str, Any]],
    analysis_criteria: Dict[str, Any],
    team_enrichment: Optional[Dict[str, Any]],
    sprint_context: Optional[Dict[str, Any]] = None,
    session_id: str = None
) -> Optional[str]:
    """Use LLM for intelligent synthesis with analysis criteria."""
    from config import config
    
    # Load synthesizer instructions
    synthesizer_instructions = load_synthesizer_instructions()
    if not synthesizer_instructions:
        synthesizer_instructions = DEFAULT_SYNTHESIZER_PROMPT
    
    # Inject dynamic state categories from config
    synthesizer_instructions = _inject_state_categories(synthesizer_instructions)
    
    # Build analysis context with criteria
    analysis_context = f"""
**Analysis Intent:** {analysis_criteria.get('intent', 'all')}
**Filter Logic:** {analysis_criteria.get('filter_logic', 'No specific filtering')}
**Evidence Fields:** {', '.join(analysis_criteria.get('evidence_fields', []))}
**Sort Priority:** {analysis_criteria.get('sort_priority', 'No specific sorting')}
"""
    
    # Build team enrichment context if available
    team_context = ""
    if team_enrichment:
        try:
            from agents.pm_agent.team_enrichment import format_enrichment_for_prompt
            team_context = format_enrichment_for_prompt(team_enrichment)
            logger.info("[LLM_SYNTHESIZER] Added team enrichment context (%d chars)", len(team_context))
        except (ImportError, Exception) as e:
            logger.debug("[LLM_SYNTHESIZER] Team enrichment unavailable: %s", e)

    # Build PM knowledge base block
    kb_content = load_kb_instructions()
    kb_block = f"\n\nPM KNOWLEDGE BASE:\n{kb_content}" if kb_content else ""
    if kb_content:
        logger.debug("[LLM_SYNTHESIZER] Injecting KB (%d chars) into synthesis prompt", len(kb_content))

    # Build sprint context block if available
    sprint_context_block = ""
    if sprint_context:
        from datetime import datetime
        sprint_name = sprint_context.get("resolved_iteration_name") or sprint_context.get("sprint", "")
        sprint_finish = sprint_context.get("resolved_iteration_finish_date", "")
        team_name = sprint_context.get("team", "")
        project_name = sprint_context.get("project", "")
        days_remaining = None
        if sprint_finish:
            try:
                finish_dt = datetime.fromisoformat(sprint_finish.replace("Z", "+00:00"))
                days_remaining = (finish_dt.replace(tzinfo=None) - datetime.now()).days
            except (ValueError, TypeError):
                pass
        sc_lines = []
        if sprint_name:
            sc_lines.append(f"Sprint: {sprint_name}")
        if team_name:
            sc_lines.append(f"Team: {team_name}")
        if project_name:
            sc_lines.append(f"Project: {project_name}")
        if sprint_finish:
            sc_lines.append(f"Sprint end date: {sprint_finish[:10]}")
        if days_remaining is not None:
            if days_remaining >= 0:
                sc_lines.append(f"Days remaining in sprint: {days_remaining}")
            else:
                sc_lines.append(f"Sprint overdue by {abs(days_remaining)} day(s)")
        if sc_lines:
            sprint_context_block = "\n\nSPRINT CONTEXT:\n" + "\n".join(sc_lines)
            logger.debug("[LLM_SYNTHESIZER] Injecting sprint context: %s", sprint_context_block.strip())
    
    # ══════════════════════════════════════════════════════════════════════════
    # PRE-PROCESSING: Compute days_in_state and pre-aggregate for group queries
    # ══════════════════════════════════════════════════════════════════════════
    from datetime import datetime as _dt
    _now = _dt.utcnow()
    
    for item in results:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields", {})
        if not fields or not isinstance(fields, dict):
            continue
        
        # --- Compute days_in_state from StateChangeDate ---
        state_change_raw = (
            fields.get("Microsoft.VSTS.Common.StateChangeDate")
            or fields.get("microsoft.vsts.common.statechangedate")
        )
        changed_raw = (
            fields.get("System.ChangedDate")
            or fields.get("system.changeddate")
        )
        date_str = state_change_raw or changed_raw
        if date_str and isinstance(date_str, str):
            try:
                dt = _dt.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
                days_diff = (_now - dt).days
                fields["_days_in_state"] = days_diff
                fields["_state_change_source"] = "StateChangeDate" if state_change_raw else "ChangedDate (fallback)"
            except (ValueError, TypeError):
                pass
    
    # --- Pre-filter: Remove assigned items for "unassigned" queries ---
    query_lower = query.lower()
    if "unassigned" in query_lower and results:
        pre_filter_count = len(results)
        filtered_results = []
        for item in results:
            if not isinstance(item, dict):
                continue
            fields = item.get("fields", {})
            if not isinstance(fields, dict):
                fields = item
            assigned_to = (
                fields.get("System.AssignedTo")
                or fields.get("system.assignedto")
            )
            # Item is unassigned if AssignedTo is empty/None or has no displayName
            is_assigned = False
            if assigned_to:
                if isinstance(assigned_to, dict):
                    name = (assigned_to.get("displayName") or assigned_to.get("uniqueName", "")).strip()
                    is_assigned = bool(name)
                elif isinstance(assigned_to, str):
                    is_assigned = bool(assigned_to.strip())
            if not is_assigned:
                filtered_results.append(item)
        results = filtered_results
        logger.info(f"[LLM_SYNTHESIZER] Unassigned pre-filter: {pre_filter_count} → {len(results)} items")

    # --- De-duplicate items across steps (prevent same item from multiple steps) ---
    if results:
        seen_ids = set()
        unique_results = []
        for item in results:
            if not isinstance(item, dict):
                unique_results.append(item)
                continue
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            item_id = (
                item.get("id")
                or (fields.get("System.Id") if fields else None)
                or (fields.get("system.id") if fields else None)
            )
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            unique_results.append(item)
        if len(unique_results) < len(results):
            logger.info(f"[LLM_SYNTHESIZER] De-duplicated: {len(results)} → {len(unique_results)} items")
            results = unique_results

    # ══════════════════════════════════════════════════════════════════════════
    # PRE-COMPUTE GROUND TRUTH — the single most important anti-hallucination step
    # ══════════════════════════════════════════════════════════════════════════
    facts = _precompute_summary_facts(results, query, analysis_criteria)
    ground_truth_block = _format_ground_truth_block(facts)

    # --- Pre-aggregate for "group by" / "top N areas" type queries ---
    aggregation_block = ""
    _needs_aggregation = any(kw in query_lower for kw in [
        "top", "areas with", "group by area", "by area path",
        "most bugs", "most open", "most items", "grouped by area",
    ])
    if _needs_aggregation and results:
        # Build aggregation by area path
        from collections import Counter
        area_counter = Counter()
        area_items = {}  # area_path → list of items
        for item in results:
            fields = item.get("fields", {})
            if not fields:
                fields = item
            area = (
                fields.get("System.AreaPath")
                or fields.get("system.areapath")
                or "Uncategorized"
            )
            area_counter[area] += 1
            if area not in area_items:
                area_items[area] = []
            item_id = item.get("id") or fields.get("System.Id") or fields.get("system.id", "?")
            item_title = fields.get("System.Title") or fields.get("system.title", "")
            item_state = fields.get("System.State") or fields.get("system.state", "")
            area_items[area].append({"id": item_id, "title": item_title, "state": item_state})
        
        # Build aggregation summary (sorted by count descending)
        sorted_areas = area_counter.most_common(20)
        agg_lines = [f"\nPRE-COMPUTED AGGREGATION BY AREA PATH (Total items: {len(results)}, Unique areas: {len(area_counter)}):"]
        agg_lines.append("Rank | Area Path | Count | Sample Work Items")
        agg_lines.append("-----|-----------|-------|------------------")
        for rank, (area, count) in enumerate(sorted_areas, 1):
            samples = area_items[area][:3]
            sample_str = "; ".join([f"#{s['id']} ({s['state']})" for s in samples])
            agg_lines.append(f"{rank}. {area} | {count} | {sample_str}")
        aggregation_block = "\n".join(agg_lines)
        logger.info(f"[LLM_SYNTHESIZER] Pre-computed aggregation: {len(sorted_areas)} areas from {len(results)} items")

    # ══════════════════════════════════════════════════════════════════════════
    # BUILD COMPACT WORK-ITEMS DATA — replaces raw JSON with concise per-item lines
    # Each item is ~100 chars (vs ~500+ chars raw JSON), allowing far more items to fit
    # ══════════════════════════════════════════════════════════════════════════
    compact_items_text = "\n".join(facts["items_compact"])
    # Dynamic limit: compact format fits ~4x more items than raw JSON
    _max_compact_chars = 24000 if _needs_aggregation else 14000

    synth_prompt = f"""USER QUERY: {query}

{analysis_context}{sprint_context_block}
{ground_truth_block}
{aggregation_block}

WORK ITEMS ({facts['total_count']} items, compact format: #ID | Type | State | Assignee | Priority | DaysInState | Title):
{compact_items_text[:_max_compact_chars]}

{team_context}{kb_block}

INSTRUCTIONS:
1. **CRITICAL — USE GROUND TRUTH:** The GROUND TRUTH section above contains pre-computed counts,
   distributions, and completion rates. Use these numbers EXACTLY. NEVER recount from the work items list.
   If the GROUND TRUTH says "Total items: 63", your response must say 63 — not a number you counted yourself.
2. For items matching the query's criteria, provide structured evidence
3. Sort by the priority specified
4. Format as specified in your guidelines for blocking/at-risk items
5. **DYNAMIC ASSIGNMENTS:** If team enrichment data is provided, use it to assign items to
   specific team members by NAME with available capacity/hours and EOD dates.
6. **WORK ITEM LINKS:** Format ALL work item IDs as clickable links:
   [**#ID**](https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/ID)
   Example: [**#73944**](https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/73944)
7. **DAYS IN STATE:** The DaysInState value in each compact item line is pre-computed.
   For "stuck > N days" queries, ONLY include items where that value >= N.
8. **AGGREGATION QUERIES:** If a PRE-COMPUTED AGGREGATION section is provided,
   use it as the AUTHORITATIVE source for grouping/ranking. Do NOT re-count from items.
9. **UNASSIGNED QUERIES:** Use the "Unassigned items" count from GROUND TRUTH.
   Only items with Assignee="Unassigned" in the compact list are truly unassigned.
10. **COMPLETION RATE:** Use the "Completion Rate" from GROUND TRUTH directly.
    NEVER recalculate it yourself.
"""

    # Create span for intelligent synthesis
    synth_span = create_span(
        name="llm_intelligent_synthesis",
        input_data={"query": query[:200], "num_items": len(results)},
        metadata={"model": "gpt-4o-mini", "step": "intelligent_synthesis", "layer": "shared"},
        session_id=session_id
    )

    # Call OpenAI with synthesizer instructions
    def _call_synth_with_criteria(prompt_text: str) -> str:
        api_key = config.openai_api_key
        import httpx
        client = OpenAI(api_key=api_key, timeout=httpx.Timeout(25.0, connect=5.0))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": synthesizer_instructions},
                {"role": "user", "content": prompt_text}
            ],
            temperature=0.0,
            max_tokens=2048
        )
        return response.choices[0].message.content
    
    try:
        # Use context-preserving executor with 30-second timeout
        synthesized = await asyncio.wait_for(
            run_in_executor_with_context(_call_synth_with_criteria, synth_prompt),
            timeout=30.0
        )
        finalize_span(synth_span, output={"status": "success"}, status="success")
        return synthesized
    except asyncio.TimeoutError:
        logger.warning("[LLM_SYNTHESIZER] Intelligent synthesis timed out after 30s")
        finalize_span(synth_span, output={"error": "timeout after 30s"}, status="error", level="WARNING")
        return None
    except Exception as e:
        logger.warning("[LLM_SYNTHESIZER] Intelligent synthesis call failed: %s", e)
        finalize_span(synth_span, output={"error": str(e)}, status="error", level="WARNING")
        return None


def _simple_list_format(results: List[Dict[str, Any]], display_limit: int = 50) -> str:
    """Format results as a simple human-readable list."""
    lines = [f"Found {len(results)} items:"]
    
    for i, item in enumerate(results[:display_limit], 1):
        if isinstance(item, dict):
            item_id, item_title, item_state = extract_work_item_metadata(item)
            state_str = f" ({item_state})" if item_state else ""
            lines.append(f"  {i}. [{item_id}] {item_title}{state_str}")
        else:
            lines.append(f"  {i}. {str(item)[:100]}")

    # Add note if more items exist
    if len(results) > display_limit:
        lines.append(f"\n📋 *Showing first {display_limit} of {len(results)} items. Use filters to narrow results.*")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# DATA EXTRACTION FOR SYNTHESIS (Reduce token size before LLM)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_essential_data(content: Any, max_items: int = 50) -> str:
    """
    Extract essential fields from agent results to reduce prompt size while
    preserving fields necessary for analysis (state, assignee, priority, dates).
    
    Converts large JSON structures into condensed but analysis-ready summaries:
    - Full work item objects → ID, Title, Type, State, AssignedTo, Priority, Tags
    - Iteration lists → ID, Name, Count only  
    - Tool results → Success status, item count, essential item data
    
    CRITICAL: Must preserve enough data for the LLM to perform comparisons,
    filtering, and percentage calculations requested by the user.
    
    Args:
        content: Raw agent result content (dict, list, or string)
        max_items: Maximum number of items to include (truncate list if larger)
        
    Returns:
        Condensed string representation suitable for LLM synthesis
    """
    if isinstance(content, str):
        # Already string; truncate if too long
        return content[:8000] if len(content) > 8000 else content
    
    if isinstance(content, list):
        # List of items (work items, iterations, etc.)
        if not content:
            return "[]"
        
        # Check if these are work items (have 'fields' dict - NOT list)
        if len(content) > 0 and isinstance(content[0], dict) and 'fields' in content[0] and isinstance(content[0]['fields'], dict):
            # Work items - extract essential fields for analysis
            condensed = []
            for item in content[:max_items]:
                fields = item.get('fields', {})
                item_id = item.get('id') or fields.get('System.Id', '?')
                title = fields.get('System.Title', 'No title')
                item_type = fields.get('System.WorkItemType', '?')
                state = fields.get('System.State', '?')
                assigned_to = ''
                assigned_raw = fields.get('System.AssignedTo', '')
                if isinstance(assigned_raw, dict):
                    assigned_to = assigned_raw.get('displayName', assigned_raw.get('uniqueName', ''))
                elif isinstance(assigned_raw, str):
                    assigned_to = assigned_raw
                priority = fields.get('Microsoft.VSTS.Common.Priority', '')
                tags = fields.get('System.Tags', '')
                created_date = fields.get('System.CreatedDate', '')
                changed_date = fields.get('System.ChangedDate', '')
                iteration_path = fields.get('System.IterationPath', '')
                
                parts = [f"#{item_id}: {title} | Type={item_type} | State={state}"]
                if assigned_to:
                    parts.append(f"AssignedTo={assigned_to}")
                if priority:
                    parts.append(f"Priority={priority}")
                if tags:
                    parts.append(f"Tags={tags}")
                if iteration_path:
                    parts.append(f"Iteration={iteration_path}")
                if created_date:
                    parts.append(f"Created={created_date[:10]}")
                if changed_date:
                    parts.append(f"Changed={changed_date[:10]}")
                condensed.append(" | ".join(parts))
            
            if len(content) > max_items:
                condensed.append(f"... and {len(content) - max_items} more items")
            
            return f"Work items ({len(content)} total):\n" + "\n".join(condensed)
        
        # Check if these are iterations (have 'name' and 'path' keys)
        elif len(content) > 0 and isinstance(content[0], dict) and 'name' in content[0]:
            # Iterations - include dates and timeFrame for proper sorting/filtering
            iter_lines = []
            for item in content[:max_items]:
                name = item.get('name', '?')
                path = item.get('path', '')
                attrs = item.get('attributes', {})
                start_date = attrs.get('startDate', '')
                finish_date = attrs.get('finishDate', '')
                time_frame_raw = attrs.get('timeFrame', '')
                # ADO timeFrame: 0=past, 1=current, 2=future
                if isinstance(time_frame_raw, int):
                    tf_label = {0: 'past', 1: 'current', 2: 'future'}.get(time_frame_raw, str(time_frame_raw))
                else:
                    tf_label = str(time_frame_raw) if time_frame_raw else ''
                parts = [f"name={name}"]
                if path:
                    parts.append(f"path={path}")
                if start_date:
                    parts.append(f"startDate={start_date[:10]}")
                if finish_date:
                    parts.append(f"finishDate={finish_date[:10]}")
                if tf_label:
                    parts.append(f"status={tf_label}")
                iter_lines.append(" | ".join(parts))
            return f"Iterations ({len(content)} total):\n" + "\n".join(iter_lines)
        
        # Generic list - just count
        # Try to extract useful info from first item's keys
        first = content[0]
        if isinstance(first, dict):
            # Common ADO data types: repos, pipelines, builds, wiki pages, commits, PRs
            key_fields = []
            for item in content[:max_items]:
                parts = []
                # Try common identifier/name fields
                for key in ('id', 'name', 'title', 'path', 'message', 'status', 'state', 'result', 'sourceBranch'):
                    val = item.get(key)
                    if val is not None and val != '':
                        display_val = str(val)[:120]
                        parts.append(f"{key}={display_val}")
                if parts:
                    key_fields.append(" | ".join(parts))
                else:
                    # Fallback: show first few keys
                    key_fields.append(str({k: str(v)[:50] for k, v in list(item.items())[:4]}))
            result_text = f"List with {len(content)} items:\n" + "\n".join(key_fields)
            if len(content) > max_items:
                result_text += f"\n... and {len(content) - max_items} more items"
            return result_text
        return f"List with {len(content)} items"
    
    if isinstance(content, dict):
        # Tool result object with 'success', 'count', 'items'
        if 'success' in content and 'items' in content:
            success = content.get('success', False)
            count = content.get('count', 0)
            items = content.get('items', [])
            
            # Extract essential data from items including analysis-critical fields
            if items and isinstance(items[0], dict):
                if 'fields' in items[0] and isinstance(items[0]['fields'], dict):
                    # Work items - include full essential fields for analysis
                    condensed = []
                    for item in items[:max_items]:
                        fields = item.get('fields', {})
                        item_id = item.get('id') or fields.get('System.Id', '?')
                        title = fields.get('System.Title', '?')
                        item_type = fields.get('System.WorkItemType', '?')
                        state = fields.get('System.State', '?')
                        assigned_to = ''
                        assigned_raw = fields.get('System.AssignedTo', '')
                        if isinstance(assigned_raw, dict):
                            assigned_to = assigned_raw.get('displayName', assigned_raw.get('uniqueName', ''))
                        elif isinstance(assigned_raw, str):
                            assigned_to = assigned_raw
                        priority = fields.get('Microsoft.VSTS.Common.Priority', '')
                        tags = fields.get('System.Tags', '')
                        iteration_path = fields.get('System.IterationPath', '')
                        
                        parts = [f"#{item_id}: {title} | Type={item_type} | State={state}"]
                        if assigned_to:
                            parts.append(f"AssignedTo={assigned_to}")
                        if priority:
                            parts.append(f"Priority={priority}")
                        if tags:
                            parts.append(f"Tags={tags}")
                        if iteration_path:
                            parts.append(f"Iteration={iteration_path}")
                        condensed.append(" | ".join(parts))
                    
                    if len(items) > max_items:
                        condensed.append(f"... and {len(items) - max_items} more items")
                    
                    return f"Tool result: success={success}, {count} items total:\n" + "\n".join(condensed)
                elif 'name' in items[0]:
                    # Iterations, repos, pipelines, etc.
                    condensed = []
                    for item in items[:max_items]:
                        parts = []
                        for key in ('id', 'name', 'title', 'path', 'status', 'state', 'result', 'url'):
                            val = item.get(key)
                            if val is not None and val != '':
                                parts.append(f"{key}={str(val)[:100]}")
                        condensed.append(" | ".join(parts) if parts else str(item)[:150])
                    result_text = f"Tool result: success={success}, count={count} items:\n" + "\n".join(condensed)
                    if len(items) > max_items:
                        result_text += f"\n... and {len(items) - max_items} more items"
                    return result_text
                else:
                    # Generic items - extract key fields
                    condensed = []
                    for item in items[:max_items]:
                        parts = []
                        for key in ('id', 'name', 'title', 'path', 'message', 'status', 'state', 'result', 'sourceBranch'):
                            val = item.get(key)
                            if val is not None and val != '':
                                parts.append(f"{key}={str(val)[:100]}")
                        condensed.append(" | ".join(parts) if parts else str(item)[:150])
                    result_text = f"Tool result: success={success}, count={count} items:\n" + "\n".join(condensed)
                    if len(items) > max_items:
                        result_text += f"\n... and {len(items) - max_items} more items"
                    return result_text
            
            return f"Tool result: success={success}, count={count} items"
        
        # Fallback: convert to string and truncate
        try:
            return json.dumps(content, indent=2)[:8000]
        except:
            return str(content)[:8000]
    
    return str(content)[:8000]


# ══════════════════════════════════════════════════════════════════════════════
# SYNTHESIZE AGENT OUTPUTS (Multi-agent synthesis)
# ══════════════════════════════════════════════════════════════════════════════

async def synthesize_agent_outputs(
    agent_results: List[Dict[str, Any]],
    user_query: str,
    style_guidelines: str = "",
    max_output_tokens: int = 512,
    session_id: str = None
) -> str:
    """
    Synthesize final user-facing response from structured agent outputs using OpenAI.
    
    Generic synthesis that works with ANY agents by extracting metadata from
    the `_routing` dict in each result. Produces human-readable output from
    structured agent outputs.
    
    Falls back to deterministic composition if OpenAI unavailable.

    IMPORTANT: This function must only use the provided `agent_results` contents
    and must NOT attempt to fetch external data or call tools. The orchestrator
    is responsible for providing the agent outputs.

    Args:
        agent_results: List of agent result dicts (structured outputs from agents)
        user_query: Original user query
        style_guidelines: Optional style guidance for the synthesis LLM
        max_output_tokens: Token limit for LLM (when available)
        session_id: Session ID for unified trace tracking

    Returns:
        Human-readable synthesized string
    """
    if not agent_results:
        return "No agent outputs available to synthesize."

    from config import config
    
    # Create span for synthesis
    synth_span = create_span(
        name="llm_synthesis_multi_agent",
        input_data={"query": user_query[:200], "num_agents": len(agent_results)},
        metadata={"model": "gpt-4o-mini", "step": "synthesis", "layer": "shared"},
        session_id=session_id
    )
    
    # Check if OpenAI is available
    if not OPENAI_AVAILABLE or not config.openai_api_key:
        # Fall back to simple deterministic composer when no LLM available
        logger.info("[LLM_SYNTHESIZER] OpenAI unavailable, using deterministic fallback")
        result = _deterministic_synthesis(agent_results, user_query)
        finalize_span(synth_span, output={"status": "deterministic_fallback"}, status="success")
        return result

    # Build prompt from agent outputs using CONDENSED data (not raw JSON)
    # This dramatically reduces prompt size to prevent OpenAI timeouts
    outputs_summary = []
    for i, r in enumerate(agent_results, 1):
        agent_name = r.get('_routing', {}).get('agent') or r.get('agent') or f'agent_{i}'
        role = r.get('_routing', {}).get('skill') or r.get('skill') or ''
        content = r.get('content') if 'content' in r else r.get('result', r)
        
        # CRITICAL FIX: Extract essential data instead of full JSON
        # This reduces 15KB raw JSON → 3KB condensed text
        condensed_content = _extract_essential_data(content)
        
        outputs_summary.append(f"---\nAgent: {agent_name} Skill: {role}\n{condensed_content}\n")

    # ══════════════════════════════════════════════════════════════════════════
    # DETERMINISTIC STATE GROUPING: Pre-compute state category breakdown so
    # the LLM can't silently drop items with unrecognized states.
    # ══════════════════════════════════════════════════════════════════════════
    state_grouping_text = ""
    try:
        all_items = []
        for r in agent_results:
            content = r.get('content') if 'content' in r else r.get('result', r)
            if isinstance(content, dict) and 'items' in content:
                items = content['items']
                if isinstance(items, list):
                    all_items.extend(items)
            elif isinstance(content, list):
                all_items.extend(content)
        
        # Only inject if we have enriched work items with 'fields'
        work_items_with_fields = [
            it for it in all_items
            if isinstance(it, dict) and 'fields' in it and 'System.State' in it.get('fields', {})
        ]
        if work_items_with_fields:
            state_summary = config.build_state_summary(work_items_with_fields)
            grouping_lines = ["\n═══ PRE-COMPUTED STATE GROUPING (authoritative — use these counts) ═══"]
            for cat, count in state_summary.items():
                if count > 0:
                    # List actual work item IDs in this category
                    ids_in_cat = []
                    for it in work_items_with_fields:
                        st = it.get('fields', {}).get('System.State', '')
                        if config.classify_state(st) == cat:
                            wid = it.get('id') or it.get('fields', {}).get('System.Id', '?')
                            ids_in_cat.append(f"#{wid}")
                    grouping_lines.append(f"  {cat}: {count} items — {', '.join(ids_in_cat)}")
                else:
                    grouping_lines.append(f"  {cat}: 0 items")
            grouping_lines.append(f"  Total: {len(work_items_with_fields)} items")
            grouping_lines.append("Use these EXACT counts in your response. Do NOT omit any category.\n")
            state_grouping_text = "\n".join(grouping_lines)
    except Exception as e:
        logger.debug(f"[LLM_SYNTHESIZER] State grouping pre-computation skipped: {e}")

    # Build prompt WITHOUT the PM Agent planning instructions (which would cause JSON output)
    prompt = f"User query: {user_query}\n\n"
    prompt += "\n\n".join(outputs_summary)
    if state_grouping_text:
        prompt += f"\n{state_grouping_text}"
    if style_guidelines:
        prompt += f"\n\nStyle guidelines:\n{style_guidelines}\n"

    # Load synthesizer instructions from file (with fallback to inline)
    synthesizer_instructions = load_synthesizer_instructions()
    if not synthesizer_instructions:
        synthesizer_instructions = DEFAULT_SYNTHESIZER_PROMPT
    
    # Inject dynamic state categories from config
    synthesizer_instructions = _inject_state_categories(synthesizer_instructions)
    
    # Add critical work item ID format enforcement
    synth_system_prompt = synthesizer_instructions + ID_VALIDATION_ADDENDUM

    # Use OpenAI synthesis
    def _call_openai_synth(p: str) -> str:
        import time as time_module
        api_key = config.openai_api_key
        import httpx
        
        # Log timing to understand where delays occur
        call_start = time_module.time()
        logger.info(f"[LLM_SYNTHESIZER] Starting OpenAI call (prompt size: {len(p)} chars)")
        
        client = OpenAI(api_key=api_key, timeout=httpx.Timeout(25.0, connect=5.0))
        
        init_time = time_module.time()
        logger.info(f"[LLM_SYNTHESIZER] OpenAI client initialized in {init_time - call_start:.2f}s")
        
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": synth_system_prompt},
                    {"role": "user", "content": p}
                ],
                temperature=0.0,
                max_tokens=(int(config.llm_max_output_tokens) if config.llm_max_output_tokens else int(max_output_tokens))
            )
            
            api_time = time_module.time()
            logger.info(f"[LLM_SYNTHESIZER] OpenAI API returned in {api_time - init_time:.2f}s (total: {api_time - call_start:.2f}s)")
            
            return response.choices[0].message.content
        except Exception as e:
            api_time = time_module.time()
            logger.error(f"[LLM_SYNTHESIZER] OpenAI API error after {api_time - call_start:.2f}s: {e}")
            raise

    try:
        # ═══════════════════════════════════════════════════════════════════════════
        # FIX #3: INCREASED TIMEOUT FOR MULTI-STEP SYNTHESIS
        # ═══════════════════════════════════════════════════════════════════════════
        # Multi-step Deep LLM plans with >2 steps need more time for quality synthesis
        # Increased from 40s to 60s to handle:
        # - Larger agent result sets (3+ steps with multiple items each)
        # - More complex synthesis logic (comparisons, aggregations, multi-step narratives)
        # - Network latency variations
        # Still maintains deterministic fallback on timeout for reliability
        # ═══════════════════════════════════════════════════════════════════════════
        synthesis_start = time.time()
        timeout_seconds = 60.0  # Increased from 40s
        logger.info(f"[LLM_SYNTHESIZER] Starting synthesis with asyncio timeout of {timeout_seconds}s")
        logger.info(f"[LLM_SYNTHESIZER] Agent results count: {len(agent_results)}, Query length: {len(user_query)} chars")
        
        synthesized = await asyncio.wait_for(
            run_in_executor_with_context(_call_openai_synth, prompt),
            timeout=timeout_seconds
        )
        
        synthesis_time = time.time() - synthesis_start
        logger.info(f"[LLM_SYNTHESIZER] ✅ Synthesis completed successfully in {synthesis_time:.2f}s")
        
        # CRITICAL: Check if OpenAI returned plan JSON instead of actual content
        synthesized = _validate_synthesis_output(synthesized)
        
        finalize_span(synth_span, output={"status": "success"}, status="success")
        return synthesized
    except asyncio.TimeoutError:
        # ═══════════════════════════════════════════════════════════════════════════
        # FIX #3b: IMPROVED TIMEOUT ERROR HANDLING
        # ═══════════════════════════════════════════════════════════════════════════
        # Don't fail the entire request - return deterministic fallback instead
        # This ensures users get SOME response even if LLM synthesis times out
        # Critical for reliability - better to return formatted data than error
        # ═══════════════════════════════════════════════════════════════════════════
        elapsed = time.time() - synthesis_start
        logger.warning(f"[LLM_SYNTHESIZER] ⚠️  OpenAI synthesis timed out after {elapsed:.2f}s (limit: {timeout_seconds}s)")
        logger.warning(f"[LLM_SYNTHESIZER]    Falling back to deterministic synthesis for reliability")
        logger.warning(f"[LLM_SYNTHESIZER]    Agent results: {len(agent_results)} items, Query: {len(user_query)} chars")
        
        finalize_span(synth_span, output={
            "error": f"timeout after {elapsed:.2f}s",
            "fallback": "deterministic",
            "agent_results_count": len(agent_results)
        }, status="error", level="WARNING")
        
        # CRITICAL: Don't propagate exception - return deterministic fallback
        return _deterministic_synthesis(agent_results, user_query)
    except Exception as e:
        logger.warning("[LLM_SYNTHESIZER] ⚠️  OpenAI synthesis failed: %s", e)
        logger.warning("[LLM_SYNTHESIZER]    Falling back to deterministic synthesis for reliability")
        
        # Deterministic fallback - ensures users always get output
        finalize_span(synth_span, output={
            "error": str(e),
            "fallback": "deterministic",
            "agent_results_count": len(agent_results)
        }, status="error", level="WARNING")
        
        return _deterministic_synthesis(agent_results, user_query)

def _deterministic_synthesis(agent_results: List[Dict[str, Any]], user_query: str) -> str:
    """Deterministic synthesis that produces human-readable markdown from agent results.
    
    This is the fallback when LLM synthesis is unavailable or times out.
    Instead of dumping raw JSON, it formats results into readable markdown
    using _extract_essential_data for proper formatting.
    """
    if not agent_results:
        return f"No results found for: {user_query}"
    
    parts = []
    
    for r in agent_results:
        content = r.get('content') if 'content' in r else r.get('result', r)
        tool_name = r.get('skill') or r.get('_routing', {}).get('skill', '') or ''
        
        # If content is already a formatted string, use it directly
        if isinstance(content, str):
            parts.append(content)
            continue
        
        if isinstance(content, (int, float, bool)):
            parts.append(str(content))
            continue
        
        # Handle dict results (typically tool output with success/count/items)
        if isinstance(content, dict):
            success = content.get('success', True)
            count = content.get('count', 0)
            items = content.get('items', [])
            
            if not success:
                parts.append(f"⚠️ Request failed: {content.get('error', 'Unknown error')}")
                continue
            
            if not items:
                parts.append(f"No results found.")
                continue
            
            # Format items based on their type
            formatted = _format_items_as_markdown(items, tool_name)
            if formatted:
                parts.append(formatted)
            else:
                # Last resort: use _extract_essential_data for condensed but readable output
                parts.append(_extract_essential_data(content))
            continue
        
        # Handle list results directly
        if isinstance(content, list):
            if not content:
                parts.append("No results found.")
                continue
            formatted = _format_items_as_markdown(content, tool_name)
            if formatted:
                parts.append(formatted)
            else:
                parts.append(_extract_essential_data(content))
            continue
        
        # Fallback: use _extract_essential_data
        parts.append(_extract_essential_data(content))
    
    return "\n\n".join(parts) if parts else f"No results found for: {user_query}"


def _format_items_as_markdown(items: list, tool_name: str = "") -> Optional[str]:
    """Format a list of items into human-readable markdown based on their type.
    
    Returns None if items can't be identified/formatted.
    """
    if not items or not isinstance(items, list):
        return None
    
    first = items[0] if items else {}
    if not isinstance(first, dict):
        return None
    
    lines = []
    
    # Detect item type and format accordingly
    if 'fields' in first:
        # Work items
        lines.append(f"**Found {len(items)} work item(s):**\n")
        for i, item in enumerate(items[:50], 1):
            fields = item.get('fields', {})
            item_id = item.get('id') or fields.get('System.Id', '?')
            title = fields.get('System.Title', 'Untitled')
            item_type = fields.get('System.WorkItemType', '')
            state = fields.get('System.State', '')
            assigned_raw = fields.get('System.AssignedTo', '')
            assigned_to = ''
            if isinstance(assigned_raw, dict):
                assigned_to = assigned_raw.get('displayName', assigned_raw.get('uniqueName', ''))
            elif isinstance(assigned_raw, str):
                assigned_to = assigned_raw
            priority = fields.get('Microsoft.VSTS.Common.Priority', '')
            
            line = f"{i}. **#{item_id}** — {title}"
            details = []
            if item_type:
                details.append(f"Type: {item_type}")
            if state:
                details.append(f"State: {state}")
            if assigned_to:
                details.append(f"Assigned: {assigned_to}")
            if priority:
                details.append(f"Priority: {priority}")
            if details:
                line += f"  \n   _{'  |  '.join(details)}_"
            lines.append(line)
        
        if len(items) > 50:
            lines.append(f"\n_...and {len(items) - 50} more items_")
        return "\n".join(lines)
    
    elif 'name' in first and ('path' in first or 'attributes' in first):
        # Iterations / sprints
        lines.append(f"**Found {len(items)} iteration(s)/sprint(s):**\n")
        for i, item in enumerate(items[:50], 1):
            name = item.get('name', '?')
            path = item.get('path', '')
            attrs = item.get('attributes', {})
            start_date = attrs.get('startDate', '')
            finish_date = attrs.get('finishDate', '')
            time_frame = attrs.get('timeFrame', '')
            
            tf_label = ''
            if isinstance(time_frame, int):
                tf_label = {0: '📅 Past', 1: '🟢 Current', 2: '🔮 Future'}.get(time_frame, '')
            
            line = f"{i}. **{name}**"
            details = []
            if path:
                details.append(f"Path: {path}")
            if start_date:
                details.append(f"Start: {start_date[:10]}")
            if finish_date:
                details.append(f"End: {finish_date[:10]}")
            if tf_label:
                details.append(tf_label)
            if details:
                line += f"  \n   _{'  |  '.join(details)}_"
            lines.append(line)
        
        if len(items) > 50:
            lines.append(f"\n_...and {len(items) - 50} more items_")
        return "\n".join(lines)
    
    elif 'name' in first and 'id' in first:
        # Generic named items (repos, pipelines, teams, etc.)
        lines.append(f"**Found {len(items)} item(s):**\n")
        for i, item in enumerate(items[:50], 1):
            name = item.get('name') or item.get('title') or item.get('id', '?')
            desc = item.get('description', '')
            status = item.get('status') or item.get('state') or item.get('result', '')
            
            line = f"{i}. **{name}**"
            details = []
            if status:
                details.append(f"Status: {status}")
            if desc:
                details.append(f"{str(desc)[:100]}")
            if details:
                line += f" — {'  |  '.join(details)}"
            lines.append(line)
        
        if len(items) > 50:
            lines.append(f"\n_...and {len(items) - 50} more items_")
        return "\n".join(lines)
    
    return None


def _validate_synthesis_output(synthesized: str) -> str:
    """
    Validate synthesis output to ensure it's not a plan JSON.
    
    Defense in depth: If OpenAI accidentally returns plan JSON instead of 
    human-readable content, detect and handle it.
    """
    synthesized_stripped = synthesized.strip()
    if synthesized_stripped.startswith("```json"):
        synthesized_stripped = synthesized_stripped[7:]
    if synthesized_stripped.startswith("```"):
        synthesized_stripped = synthesized_stripped[3:]
    if synthesized_stripped.endswith("```"):
        synthesized_stripped = synthesized_stripped[:-3]
    synthesized_stripped = synthesized_stripped.strip()
    
    # Try to parse as JSON and check for plan patterns
    if synthesized_stripped.startswith("{"):
        try:
            parsed = json.loads(synthesized_stripped)
            if isinstance(parsed, dict) and ("action" in parsed or ("tool" in parsed and "args" in parsed)):
                logger.error("[LLM_SYNTHESIZER] CRITICAL: OpenAI returned plan JSON instead of content!")
                logger.error("[LLM_SYNTHESIZER] Bad response: %s", synthesized[:500])
                raise ValueError("OpenAI synthesis returned plan JSON")
        except json.JSONDecodeError:
            pass  # Not JSON - use as-is
    
    return synthesized


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT ALL PUBLIC FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "summarize_mcp_result",
    "synthesize_agent_outputs",
]
