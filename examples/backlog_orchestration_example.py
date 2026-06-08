"""
Example: Backlog Orchestration Flow

This example demonstrates the complete backlog triaging orchestration flow:
1. User query → LLM Planner
2. LLM Planner → Execution Plan (JSON)
3. Execution Plan → MultiToolOrchestrator
4. Orchestrator → ToolDispatcher → Tools
5. Results → Synthesizer (Phase 4, not yet implemented)

Usage:
    python examples/backlog_orchestration_example.py
"""

import asyncio
import json
from typing import Dict, Any

# Note: This is a conceptual example showing the flow
# Full implementation requires MCP server and PM Skill Agent setup


async def main():
    """Demonstrate backlog orchestration flow."""
    
    print("=" * 80)
    print("BACKLOG ORCHESTRATION EXAMPLE")
    print("=" * 80)
    print()
    
    # ============================================================================
    # STEP 1: User Query
    # ============================================================================
    user_query = "How is my backlog health for FracPro-OPS team XOPS 25?"
    print(f"📝 User Query: {user_query}\n")
    
    # ============================================================================
    # STEP 2: LLM Planner generates execution plan
    # ============================================================================
    print("🤖 LLM Planner analyzing query...\n")
    
    # This would come from: backlog_planner.plan_backlog_query()
    llm_generated_plan = {
        "intent": "full backlog health report",
        "plan_type": "full",
        "can_parallelize": True,
        "steps": [
            {
                "step_id": "step1",
                "tool": "get_team_area_path",
                "tool_type": "pm_skill",
                "args": {
                    "project": "FracPro-OPS",
                    "team": "XOPS 25"
                },
                "parallel_group": 1,
                "output_var": "area_path",
                "required": True,
                "description": "Get team's area path for backlog filtering"
            },
            {
                "step_id": "step2a",
                "tool": "get_backlog_items",
                "tool_type": "pm_skill",
                "args": {
                    "project": "FracPro-OPS",
                    "area_path": "${area_path}",
                    "include_estimates": False
                },
                "parallel_group": 2,
                "output_var": "backlog_items",
                "required": True,
                "description": "Fetch backlog items for team"
            },
            {
                "step_id": "step2b",
                "tool": "calculate_team_velocity",
                "tool_type": "pm_skill",
                "args": {
                    "project": "FracPro-OPS",
                    "team": "XOPS 25"
                },
                "parallel_group": 2,
                "output_var": "velocity_data",
                "required": True,
                "description": "Calculate team velocity from recent sprints"
            },
            {
                "step_id": "step3",
                "tool": "estimate_backlog_items",
                "tool_type": "pm_skill",
                "args": {
                    "backlog_items": "${backlog_items}",
                    "velocity": "${velocity_data.average_velocity}"
                },
                "parallel_group": 3,
                "output_var": "estimated_items",
                "required": False,
                "description": "Use LLM to estimate unestimated items"
            },
            {
                "step_id": "step4",
                "tool": "calculate_backlog_health",
                "tool_type": "pm_skill",
                "args": {
                    "backlog_items": "${estimated_items}",
                    "velocity": "${velocity_data.average_velocity}"
                },
                "parallel_group": 4,
                "output_var": "health_metrics",
                "required": True,
                "description": "Calculate health: THIN/HEALTHY/OVERSTOCKED"
            },
            {
                "step_id": "step5",
                "tool": "generate_backlog_recommendations",
                "tool_type": "pm_skill",
                "args": {
                    "health_metrics": "${health_metrics}",
                    "backlog_items": "${estimated_items}",
                    "velocity_data": "${velocity_data}"
                },
                "parallel_group": 5,
                "output_var": "recommendations",
                "required": False,
                "description": "Generate actionable recommendations"
            }
        ],
        "final_output_vars": [
            "health_metrics",
            "recommendations",
            "velocity_data"
        ],
        "planner_metadata": {
            "confidence": 0.95,
            "model": "gpt-4o",
            "reasoning": "Query asks for health report - need full analysis with velocity, estimation, health calc, and recommendations"
        }
    }
    
    print("📋 Generated Execution Plan:")
    print(f"   Intent: {llm_generated_plan['intent']}")
    print(f"   Plan Type: {llm_generated_plan['plan_type']}")
    print(f"   Total Steps: {len(llm_generated_plan['steps'])}")
    print(f"   Parallel Groups: {max(step['parallel_group'] for step in llm_generated_plan['steps'])}")
    print(f"   Confidence: {llm_generated_plan['planner_metadata']['confidence']}")
    print()
    
    # ============================================================================
    # STEP 3: Show execution flow with parallel groups
    # ============================================================================
    print("🔄 Execution Flow:\n")
    
    # Group by parallel_group
    from collections import defaultdict
    groups = defaultdict(list)
    for step in llm_generated_plan["steps"]:
        groups[step["parallel_group"]].append(step)
    
    for group_num in sorted(groups.keys()):
        group_steps = groups[group_num]
        print(f"   Group {group_num} {'(parallel)' if len(group_steps) > 1 else '(sequential)'}:")
        for step in group_steps:
            required = "✓ required" if step["required"] else "○ optional"
            print(f"      • {step['step_id']}: {step['tool']} [{required}]")
            print(f"        Output: ${step['output_var']}")
            print(f"        {step['description']}")
        print()
    
    # ============================================================================
    # STEP 4: Variable resolution demonstration
    # ============================================================================
    print("🔗 Variable Resolution Examples:\n")
    
    examples = [
        ("${area_path}", "step1.area_path", "FracPro-OPS\\Global Management\\XOPS 25"),
        ("${backlog_items}", "step2a.backlog_items", "[{id: 1, ...}, {id: 2, ...}]"),
        ("${velocity_data.average_velocity}", "step2b.velocity_data.average_velocity", "30.5"),
        ("${health_metrics.status}", "step4.health_metrics.status", "HEALTHY"),
    ]
    
    for var_ref, resolution_path, example_value in examples:
        print(f"   {var_ref}")
        print(f"      → Resolves to: {resolution_path}")
        print(f"      → Example value: {example_value}")
        print()
    
    # ============================================================================
    # STEP 5: Expected output structure
    # ============================================================================
    print("📊 Expected Execution Result:\n")
    
    expected_result = {
        "success": True,
        "execution_time_ms": 3500,
        "results": {
            "step1": {"success": True, "result": {"area_path": "FracPro-OPS\\XOPS 25"}},
            "step2a": {"success": True, "result": {"items": [], "count": 12}},
            "step2b": {"success": True, "result": {"average_velocity": 30.5}},
            "step3": {"success": True, "result": {"estimated_count": 3}},
            "step4": {"success": True, "result": {"status": "HEALTHY"}},
            "step5": {"success": True, "result": {"recommendations": []}}
        },
        "final_output": {
            "health_metrics": {
                "status": "HEALTHY",
                "total_items": 12,
                "total_effort": 360,
                "sprints_worth": 12.0,
                "velocity": 30.5
            },
            "recommendations": [
                "Your backlog is healthy with 12 sprints of work",
                "Consider reviewing items older than 90 days"
            ],
            "velocity_data": {
                "average_velocity": 30.5,
                "sprint_count": 6,
                "trend": "stable"
            }
        },
        "errors": []
    }
    
    print(json.dumps(expected_result, indent=2))
    print()
    
    # ============================================================================
    # STEP 6: Synthesizer (Phase 4 - not yet implemented)
    # ============================================================================
    print("=" * 80)
    print("NEXT PHASE: Synthesizer")
    print("=" * 80)
    print()
    print("The synthesizer will convert execution results into a user-friendly report:")
    print()
    print("Example Output:")
    print("─" * 80)
    print("""
## 📊 Backlog Health Report for XOPS 25

**Status:** ✅ HEALTHY

### Key Metrics
- **Total Items:** 12
- **Total Effort:** 360 story points
- **Team Velocity:** 30.5 points/sprint
- **Backlog Runway:** ~12 sprints

### 💡 Recommendations
1. Your backlog is healthy with sufficient work for the next 12 sprints
2. Consider reviewing 3 items that are older than 90 days
3. Team velocity has been stable over the last 6 sprints

### 📈 Velocity Trend
Average: 30.5 points/sprint (last 6 sprints)
Trend: Stable

*Generated on 2026-01-08 at 14:30 UTC*
    """)
    print("─" * 80)
    print()
    
    # ============================================================================
    # STEP 7: Architecture Summary
    # ============================================================================
    print("=" * 80)
    print("ARCHITECTURE SUMMARY")
    print("=" * 80)
    print()
    print("Component Interactions:")
    print()
    print("   User Query")
    print("       ↓")
    print("   LLM Planner (backlog_planner.py)")
    print("       ↓")
    print("   Execution Plan (JSON with dependencies)")
    print("       ↓")
    print("   MultiToolOrchestrator.execute_backlog_plan()")
    print("       ↓")
    print("   ToolDispatcher (routes to PM Skill or MCP)")
    print("       ├─→ PM Skill Tools (backlog_handlers.py)")
    print("       └─→ MCP Tools (ADO API)")
    print("       ↓")
    print("   Execution Results (Dict)")
    print("       ↓")
    print("   Synthesizer (backlog_synthesizer.py - Phase 4)")
    print("       ↓")
    print("   User-Friendly Report (Markdown)")
    print()
    print("=" * 80)
    print()
    
    print("✅ Phase 3 Complete: Orchestrator can execute structured backlog plans")
    print("🔜 Next: Implement Synthesizer (Phase 4)")
    print()


if __name__ == "__main__":
    asyncio.run(main())
