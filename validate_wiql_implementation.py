"""
WIQL Skill End-to-End Validation Script

This script demonstrates the complete WIQL skill integration by testing
various query types and showing the routing decisions and results.

Run this to validate the implementation is working correctly.
"""

import asyncio
import json
from datetime import datetime
from typing import Dict, Any


async def demonstrate_wiql_skill():
    """Demonstrate WIQL skill routing and execution."""
    print("="*100)
    print("WIQL SKILL END-TO-END DEMONSTRATION")
    print("="*100)
    print(f"Test Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*100)
    
    from utilities.llm.light_planner import call_light_planner_for_routing
    from config import config
    
    context = {
        "project": config.ado_project,
        "team": "XOPS 25",
        "session_id": "demo_session",
        "turn_number": 1
    }
    
    test_cases = [
        {
            "category": "STANDARD (Non-WIQL)",
            "query": "show bugs for team XOPS 25",
            "expected_tool": "wit_get_work_items_for_iteration",
            "should_use_wiql": False,
            "description": "Standard team query with no advanced filtering"
        },
        {
            "category": "DATE-BASED (WIQL)",
            "query": "bugs created last week",
            "expected_tool": "wiql_query",
            "should_use_wiql": True,
            "expected_params": ["date_filter"],
            "description": "Created date filtering - not supported by search_workitem"
        },
        {
            "category": "PRIORITY (WIQL)",  
            "query": "high priority bugs",
            "expected_tool": "wiql_query",
            "should_use_wiql": True,
            "expected_params": ["priority"],
            "description": "Priority filtering - not supported by search_workitem"
        },
        {
            "category": "COMPLEX (WIQL)",
            "query": "P1 bugs created this month for team XOPS 25",
            "expected_tool": "wiql_query",
            "should_use_wiql": True,
            "expected_params": ["priority", "date_filter", "work_item_type", "team"],
            "description": "Combined priority + date + team filtering"
        }
    ]
    
    results = []
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n{'-'*100}")
        print(f"TEST {i}: {test_case['category']}")
        print(f"{'-'*100}")
        print(f"Query: \"{test_case['query']}\"")
        print(f"Description: {test_case['description']}")
        print(f"Expected Tool: {test_case['expected_tool']}")
        print()
        
        try:
            # Call light planner
            result = await call_light_planner_for_routing(
                query=test_case["query"],
                context=context
            )
            
            # Extract routing decision
            if "plan" in result and "steps" in result["plan"] and result["plan"]["steps"]:
                step = result["plan"]["steps"][0]
                actual_tool = step.get("tool")
                actual_action = step.get("action")
                args = step.get("args", {})
                
                # Check if routing was correct
                routing_correct = actual_tool == test_case["expected_tool"]
                wiql_used = (actual_tool == "wiql_query" and actual_action == "call_skill")
                
                # Display results
                print(f"Routing Decision:")
                print(f"  Action: {actual_action}")
                print(f"  Tool: {actual_tool}")
                print(f"  Arguments:")
                for key, value in args.items():
                    print(f"    - {key}: {value}")
                
                # Validation
                print()
                if routing_correct:
                    print(f"✅ ROUTING CORRECT: Routed to {actual_tool}")
                else:
                    print(f"❌ ROUTING INCORRECT: Expected {test_case['expected_tool']}, got {actual_tool}")
                
                if test_case["should_use_wiql"]:
                    if wiql_used:
                        print(f"✅ WIQL SKILL: Correctly identified as WIQL-required query")
                        
                        # Check expected params
                        if "expected_params" in test_case:
                            missing_params = [p for p in test_case["expected_params"] if p not in args or not args[p]]
                            if missing_params:
                                print(f"⚠️  WARNING: Missing expected parameters: {missing_params}")
                            else:
                                print(f"✅ PARAMETERS: All expected parameters present")
                    else:
                        print(f"❌ WIQL SKILL: Should use WIQL skill but routed to {actual_tool}")
                else:
                    if not wiql_used:
                        print(f"✅ STANDARD TOOL: Correctly used standard tool (not WIQL)")
                    else:
                        print(f"⚠️  WARNING: Standard query routed to WIQL (may be okay but inefficient)")
                
                # Record result
                results.append({
                    "test": i,
                    "category": test_case["category"],
                    "query": test_case["query"],
                    "expected_tool": test_case["expected_tool"],
                    "actual_tool": actual_tool,
                    "routing_correct": routing_correct,
                    "wiql_used": wiql_used,
                    "args": args
                })
                
            else:
                print(f"❌ ERROR: No plan or steps in result")
                results.append({
                    "test": i,
                    "category": test_case["category"],
                    "query": test_case["query"],
                    "error": "No plan or steps"
                })
                
        except Exception as e:
            print(f"❌ EXCEPTION: {str(e)}")
            results.append({
                "test": i,
                "category": test_case["category"],
                "query": test_case["query"],
                "error": str(e)
            })
    
    # Summary
    print(f"\n{'='*100}")
    print("SUMMARY")
    print(f"{'='*100}")
    
    total = len(results)
    correct = sum(1 for r in results if r.get("routing_correct", False))
    wiql_correctly_used = sum(1 for r in results if r.get("wiql_used", False) and "WIQL" in r.get("category", ""))
    
    print(f"Total Tests: {total}")
    print(f"Routing Correct: {correct}/{total} ({correct/total*100:.1f}%)")
    print(f"WIQL Correctly Identified: {wiql_correctly_used}/{sum(1 for tc in test_cases if tc['should_use_wiql'])}")
    print()
    
    # Save detailed results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"wiql_validation_results_{timestamp}.json"
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": timestamp,
            "summary": {
                "total_tests": total,
                "routing_correct": correct,
                "success_rate": f"{correct/total*100:.1f}%"
            },
            "test_results": results
        }, f, indent=2)
    
    print(f"Detailed results saved to: {output_file}")
    print()
    
    if correct == total:
        print("="*100)
        print("🎉 ALL TESTS PASSED! WIQL skill integration is working correctly.")
        print("="*100)
    else:
        print("="*100)
        print(f"⚠️  {total - correct} test(s) failed. Review the results above for details.")
        print("="*100)


if __name__ == "__main__":
    asyncio.run(demonstrate_wiql_skill())
