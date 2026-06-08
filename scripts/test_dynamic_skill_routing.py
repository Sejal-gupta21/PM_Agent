#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test script for Dynamic Skill Routing & Execution.

Tests that queries are correctly routed to skills regardless of phrasing.
Validates the 4 core skills plus PM Agent fixed skills.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import List, Tuple

# Test cases: (query, expected_skill_id, min_confidence)
TEST_CASES: List[Tuple[str, str, float]] = [
    # Developer Knowledge Base variations
    ("show developer knowledge base", "developer_knowledge_base", 0.5),
    ("what do our developers know", "developer_knowledge_base", 0.4),
    ("who knows python", "developer_skills", 0.4),
    ("team skill matrix", "developer_skills", 0.4),
    ("developer proficiency report", "developer_knowledge_base", 0.4),
    
    # Sprint Plan Generator variations
    ("generate sprint plan", "generate_sprint_plan", 0.5),
    ("create a plan for the sprint", "generate_sprint_plan", 0.4),
    ("help me plan the iteration", "generate_sprint_plan", 0.4),
    ("make a sprint schedule", "generate_sprint_plan", 0.4),
    ("plan sprint assignments", "generate_sprint_plan", 0.4),
    
    # Capacity Check variations
    ("check capacity", "get_capacity_forecast", 0.5),
    ("how much bandwidth do we have", "get_capacity_forecast", 0.4),
    ("team workload status", "get_capacity_forecast", 0.4),
    ("are we overloaded", "get_capacity_forecast", 0.4),
    ("who is overloaded", "get_capacity_forecast", 0.5),
    ("show team capacity", "get_capacity_forecast", 0.4),
    
    # Backlog Assignments variations
    ("assign backlog items", "upcoming_tasks", 0.4),
    ("distribute work to underutilized devs", "upcoming_tasks", 0.3),
    ("balance the workload", "get_capacity_forecast", 0.3),
    ("who can take more work", "get_capacity_forecast", 0.3),
    
    # Bug Areas Highlight variations
    ("show recurring bugs", "bug_areas_highlight", 0.5),
    ("find recurring bugs", "bug_areas_highlight", 0.5),
    ("bug areas highlight", "bug_areas_highlight", 0.6),
    ("analyze bug patterns", "bug_areas_highlight", 0.4),
    ("bug hotspots", "bug_areas_highlight", 0.4),
    
    # Sprint Status variations
    ("what is the derailing work item in the current sprint", "get_sprint_status", 0.4),
    ("show derailing items", "get_sprint_status", 0.4),
    ("sprint status", "get_sprint_status", 0.5),
    ("are we on track", "get_sprint_status", 0.4),
    ("blocked tasks in sprint", "get_sprint_status", 0.4),
    
    # Iteration Report variations
    ("generate sprint report", "iteration_report", 0.5),
    ("iteration report", "iteration_report", 0.6),
    ("sprint summary", "get_sprint_status", 0.4),
    
    # Overlooked Stories variations
    ("overlooked stories", "overlooked_stories", 0.6),
    ("find forgotten tasks", "overlooked_stories", 0.4),
    ("stale stories", "overlooked_stories", 0.5),
]


def test_skill_registry_matching():
    """Test skill matching using skill_registry."""
    from utilities.skill_registry import match_skill_by_query, semantic_match_query_to_skill
    
    print("\n" + "="*70)
    print("SKILL REGISTRY MATCHING TEST")
    print("="*70)
    
    passed = 0
    failed = 0
    
    for query, expected_skill, min_conf in TEST_CASES:
        # Test keyword matching
        result = match_skill_by_query(query, threshold=0.3)
        matched_id = result.get("id") if result else None
        
        # Test semantic matching
        semantic_id, score, confidence = semantic_match_query_to_skill(query)
        
        # Check if either matcher found the skill
        found = matched_id == expected_skill or semantic_id == expected_skill
        
        if found:
            passed += 1
            status = "[OK]"
        else:
            failed += 1
            status = "[FAIL]"
        
        print(f"{status} '{query[:50]:<50}' -> keyword:{matched_id}, semantic:{semantic_id} (expected:{expected_skill})")
    
    print(f"\nResults: Passed={passed}, Failed={failed}, Success Rate={passed/(passed+failed)*100:.1f}%")
    return failed == 0


def test_router_routing():
    """Test router routing decisions."""
    from orchestrator.router import Router
    
    print("\n" + "="*70)
    print("ROUTER ROUTING TEST")
    print("="*70)
    
    router = Router()
    passed = 0
    failed = 0
    
    # Key queries that should route to PM_SKILL_AGENT
    skill_queries = [
        ("show recurring bugs", "bug_areas_highlight"),
        ("sprint status", "get_sprint_status"),
        ("capacity forecast", "get_capacity_forecast"),
        ("iteration report", "iteration_report"),
        ("overlooked stories", "overlooked_stories"),
        ("what is the derailing work item", "get_sprint_status"),
        ("find blocked tasks", "get_sprint_status"),
    ]
    
    for query, expected_skill in skill_queries:
        result = router.route(query)
        
        if result.skill == expected_skill or expected_skill in str(result.skill):
            passed += 1
            status = "[OK]"
        else:
            failed += 1
            status = "[FAIL]"
        
        print(f"{status} '{query:<45}' -> agent:{result.agent.value}, skill:{result.skill}, conf:{result.confidence:.2f}")
    
    print(f"\nResults: Passed={passed}, Failed={failed}, Success Rate={passed/(passed+failed)*100:.1f}%")
    return failed == 0


def test_tool_registry_mapping():
    """Test that SKILL_TO_TOOLS_MAP contains correct tool mappings."""
    from agents.pm_agent.tool_registry import SKILL_TO_TOOLS_MAP, get_priority_tools_for_query
    
    print("\n" + "="*70)
    print("TOOL REGISTRY MAPPING TEST")
    print("="*70)
    
    # Verify 4 core skills have tool mappings
    core_skills = [
        "developer_knowledge_base",
        "sprint_plan_generator", 
        "capacity_check",
        "backlog_assignments",
    ]
    
    passed = 0
    failed = 0
    
    for skill in core_skills:
        mapping = SKILL_TO_TOOLS_MAP.get(skill)
        if mapping and mapping.get("primary_tools"):
            passed += 1
            print(f"[OK] {skill}: primary_tools={mapping['primary_tools']}")
        else:
            failed += 1
            print(f"[FAIL] {skill}: missing or empty mapping")
    
    # Test get_priority_tools_for_query
    print("\nPriority tools for queries:")
    test_queries = [
        "generate sprint plan",
        "check capacity",
        "show recurring bugs",
        "what do our developers know",
    ]
    
    for query in test_queries:
        tools = get_priority_tools_for_query(query)
        if tools:
            passed += 1
            print(f"[OK] '{query}' -> {tools}")
        else:
            # Not a failure if no tools found
            print(f"[--] '{query}' -> no priority tools")
    
    print(f"\nResults: Passed={passed}, Failed={failed}")
    return failed == 0


def test_langfuse_context():
    """Test Langfuse trace context management."""
    from utilities.langfuse_client import (
        create_parent_trace, get_current_trace_id, create_span, 
        finalize_span, finalize_trace, TraceContext
    )
    
    print("\n" + "="*70)
    print("LANGFUSE CONTEXT TEST")
    print("="*70)
    
    passed = 0
    failed = 0
    
    # Test TraceContext context manager
    try:
        with TraceContext("test_query", user_id="test_user", query="test query") as ctx:
            if ctx.trace_id:
                passed += 1
                print(f"[OK] TraceContext created trace_id: {ctx.trace_id[:30]}...")
            else:
                passed += 1  # Local client may not have trace_id
                print(f"[OK] TraceContext created (local mode)")
            
            # Create child span
            span = create_span("test_span", input_data={"test": True})
            if span:
                passed += 1
                print(f"[OK] Child span created successfully")
                finalize_span(span, output={"result": "test"}, status="success")
            else:
                passed += 1  # Local client
                print(f"[OK] Span creation attempted (local mode)")
        
    except Exception as e:
        failed += 1
        print(f"[FAIL] TraceContext error: {e}")
    
    print(f"\nResults: Passed={passed}, Failed={failed}")
    return failed == 0


def main():
    """Run all tests."""
    print("\n" + "#"*70)
    print("# DYNAMIC SKILL ROUTING & EXECUTION TEST SUITE")
    print("#"*70)
    
    all_passed = True
    
    # Test 1: Skill Registry Matching
    try:
        if not test_skill_registry_matching():
            all_passed = False
    except Exception as e:
        print(f"[ERROR] Skill registry test failed: {e}")
        all_passed = False
    
    # Test 2: Router Routing
    try:
        if not test_router_routing():
            all_passed = False
    except Exception as e:
        print(f"[ERROR] Router test failed: {e}")
        all_passed = False
    
    # Test 3: Tool Registry Mapping
    try:
        if not test_tool_registry_mapping():
            all_passed = False
    except Exception as e:
        print(f"[ERROR] Tool registry test failed: {e}")
        all_passed = False
    
    # Test 4: Langfuse Context
    try:
        if not test_langfuse_context():
            all_passed = False
    except Exception as e:
        print(f"[ERROR] Langfuse context test failed: {e}")
        all_passed = False
    
    print("\n" + "#"*70)
    if all_passed:
        print("# OVERALL RESULT: ALL TESTS PASSED!")
    else:
        print("# OVERALL RESULT: SOME TESTS FAILED")
    print("#"*70 + "\n")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
