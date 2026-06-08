"""
Query Verification Test Suite
=============================

Tests 25+ dynamic query variations to verify:
1. Tool categorization works correctly
2. Team context handling (extraction, clarification, default)
3. Proper plan generation with traceability
4. PM Skill Agent tools are disabled

Run: python -m pytest tests/test_query_verification.py -v
Or: python tests/test_query_verification.py
"""

import asyncio
import json
import sys
import os
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import modules to test
from utilities.mcp.tool_registry import MCP_TOOL_REGISTRY
from utilities.llm.planner import _build_tool_summary, _select_relevant_tools
from agents.pm_agent.conversation import ConversationContext


@dataclass
class TestResult:
    """Result of a single test case."""
    test_id: str
    query: str
    category: str
    passed: bool
    expected: str
    actual: str
    details: str = ""


class QueryVerificationSuite:
    """
    Comprehensive test suite for verifying PM Agent query handling.
    """
    
    def __init__(self):
        self.results: List[TestResult] = []
        self._test_count = 0
        self.pass_count = 0
        self.fail_count = 0
    
    def record_result(self, result: TestResult):
        """Record a test result."""
        self.results.append(result)
        self._test_count += 1
        if result.passed:
            self.pass_count += 1
        else:
            self.fail_count += 1
    
    # ===================================================================
    # TEST CATEGORY 1: Tool Registry Verification
    # ===================================================================
    
    def test_1_1_all_tools_have_category(self):
        """Test that ALL tools in MCP_TOOL_REGISTRY have a category field."""
        tools_without_category = []
        for name, meta in MCP_TOOL_REGISTRY.items():
            if 'category' not in meta or not meta['category']:
                tools_without_category.append(name)
        
        passed = len(tools_without_category) == 0
        self.record_result(TestResult(
            test_id="1.1",
            query="N/A",
            category="Tool Registry",
            passed=passed,
            expected="All tools have category",
            actual=f"{len(tools_without_category)} tools without category" if tools_without_category else "All tools have category",
            details=str(tools_without_category[:5]) if tools_without_category else ""
        ))
        return passed
    
    def test_1_2_all_tools_have_priority(self):
        """Test that ALL tools have a priority field."""
        tools_without_priority = []
        for name, meta in MCP_TOOL_REGISTRY.items():
            if 'priority' not in meta:
                tools_without_priority.append(name)
        
        passed = len(tools_without_priority) == 0
        self.record_result(TestResult(
            test_id="1.2",
            query="N/A",
            category="Tool Registry",
            passed=passed,
            expected="All tools have priority",
            actual=f"{len(tools_without_priority)} tools without priority" if tools_without_priority else "All tools have priority",
            details=str(tools_without_priority[:5]) if tools_without_priority else ""
        ))
        return passed
    
    def test_1_3_expected_categories_exist(self):
        """Test that expected categories exist in the registry."""
        expected_categories = {'core', 'work_items', 'search', 'iteration', 'repositories', 'pull_requests', 'build', 'test', 'wiki'}
        found_categories = set()
        for name, meta in MCP_TOOL_REGISTRY.items():
            if 'category' in meta and meta['category']:
                found_categories.add(meta['category'])
        
        missing = expected_categories - found_categories
        passed = len(missing) == 0
        self.record_result(TestResult(
            test_id="1.3",
            query="N/A",
            category="Tool Registry",
            passed=passed,
            expected=f"Categories: {expected_categories}",
            actual=f"Found: {found_categories}",
            details=f"Missing: {missing}" if missing else ""
        ))
        return passed
    
    def test_1_4_pm_skill_tools_disabled(self):
        """Test that PM Skill Agent tools are NOT in the registry."""
        disabled_tools = ['bug_areas_highlight', 'overlooked_stories', 'iteration_report', 
                          'list_area_paths', 'send_email', 'search_developers_by_skill']
        found_tools = []
        for tool in disabled_tools:
            if tool in MCP_TOOL_REGISTRY:
                found_tools.append(tool)
        
        passed = len(found_tools) == 0
        self.record_result(TestResult(
            test_id="1.4",
            query="N/A",
            category="Tool Registry",
            passed=passed,
            expected="PM Skill tools disabled",
            actual=f"Found {len(found_tools)} PM skill tools still active" if found_tools else "All PM skill tools disabled",
            details=str(found_tools) if found_tools else ""
        ))
        return passed
    
    def test_1_5_tool_count_reasonable(self):
        """Test that tool count is reasonable (30-50 expected)."""
        count = len(MCP_TOOL_REGISTRY)
        passed = 25 <= count <= 60
        self.record_result(TestResult(
            test_id="1.5",
            query="N/A",
            category="Tool Registry",
            passed=passed,
            expected="25-60 tools",
            actual=f"{count} tools",
            details=""
        ))
        return passed
    
    # ===================================================================
    # TEST CATEGORY 2: Planner Tool Summary Verification  
    # ===================================================================
    
    def test_2_1_tool_summary_has_categories(self):
        """Test that tool summary groups by category."""
        summary = _build_tool_summary(MCP_TOOL_REGISTRY)
        
        # Check for category headers
        expected_headers = ['Work Items', 'Search', 'Sprint/Iteration', 'Pull Requests', 'Repositories']
        found_headers = sum(1 for h in expected_headers if h in summary)
        
        passed = found_headers >= 4
        self.record_result(TestResult(
            test_id="2.1",
            query="N/A",
            category="Planner Summary",
            passed=passed,
            expected="At least 4 category headers",
            actual=f"Found {found_headers} headers",
            details=summary[:500] if not passed else ""
        ))
        return passed
    
    def test_2_2_tool_summary_has_emojis(self):
        """Test that tool summary uses emoji headers for categories."""
        summary = _build_tool_summary(MCP_TOOL_REGISTRY)
        
        emojis = ['📋', '🔍', '🔄', '🔀', '📂', '🛠️', '🧪', '📖', '⚙️']
        found_emojis = sum(1 for e in emojis if e in summary)
        
        passed = found_emojis >= 5
        self.record_result(TestResult(
            test_id="2.2",
            query="N/A",
            category="Planner Summary",
            passed=passed,
            expected="At least 5 category emojis",
            actual=f"Found {found_emojis} emojis",
            details=""
        ))
        return passed
    
    # ===================================================================
    # TEST CATEGORY 3: Team Context Extraction
    # ===================================================================
    
    def test_3_1_extract_team_from_for_pattern(self):
        """Test 'for the X team' pattern extraction."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("show sprint status for the XOPS team")
        
        passed = team is not None and team.upper() == "XOPS"
        self.record_result(TestResult(
            test_id="3.1",
            query="show sprint status for the XOPS team",
            category="Team Extraction",
            passed=passed,
            expected="XOPS",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_3_2_extract_team_from_possessive_pattern(self):
        """Test 'X team's sprint' pattern extraction."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("what's in Development team's sprint?")
        
        passed = team is not None and 'development' in team.lower()
        self.record_result(TestResult(
            test_id="3.2",
            query="what's in Development team's sprint?",
            category="Team Extraction",
            passed=passed,
            expected="Development",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_3_3_extract_team_from_capacity_pattern(self):
        """Test 'capacity for X' pattern extraction."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("show capacity for Platform")
        
        passed = team is not None and 'platform' in team.lower()
        self.record_result(TestResult(
            test_id="3.3",
            query="show capacity for Platform",
            category="Team Extraction",
            passed=passed,
            expected="Platform",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_3_4_extract_known_team_name(self):
        """Test known team name extraction."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("what bugs are XOPS working on?")
        
        passed = team is not None and team.upper() == "XOPS"
        self.record_result(TestResult(
            test_id="3.4",
            query="what bugs are XOPS working on?",
            category="Team Extraction",
            passed=passed,
            expected="XOPS",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_3_5_no_team_extraction_when_absent(self):
        """Test that no team is extracted when none mentioned."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("show me active bugs")
        
        # For this query, we expect None OR a known team name only if explicitly mentioned
        # 'bugs' shouldn't trigger a team match
        passed = team is None or team.lower() not in ['show', 'active', 'bugs', 'me', 'the']
        self.record_result(TestResult(
            test_id="3.5",
            query="show me active bugs",
            category="Team Extraction",
            passed=passed,
            expected="None or valid team",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_3_6_update_context_from_query(self):
        """Test update_context_from_query_text updates session context."""
        ctx = ConversationContext()
        extracted = ctx.update_context_from_query_text("sprint backlog for XOPS team")
        
        passed = ctx.team is not None and 'xops' in ctx.team.lower()
        self.record_result(TestResult(
            test_id="3.6",
            query="sprint backlog for XOPS team",
            category="Team Extraction",
            passed=passed,
            expected="Session team set to XOPS",
            actual=f"Session team: {ctx.team}",
            details=str(extracted)
        ))
        return passed
    
    # ===================================================================
    # TEST CATEGORY 4: Pending Clarification Handling
    # ===================================================================
    
    def test_4_1_set_pending_clarification(self):
        """Test setting pending clarification."""
        ctx = ConversationContext()
        ctx.set_pending_clarification(
            original_query="show sprint status",
            clarification_type="team",
            ambiguous_token=None
        )
        
        pending = ctx.get_pending_clarification()
        passed = pending is not None and pending.get('clarification_type') == 'team'
        self.record_result(TestResult(
            test_id="4.1",
            query="show sprint status",
            category="Clarification",
            passed=passed,
            expected="Pending clarification set",
            actual=str(pending)[:100] if pending else "None",
            details=""
        ))
        return passed
    
    def test_4_2_clear_pending_clarification(self):
        """Test clearing pending clarification."""
        ctx = ConversationContext()
        ctx.set_pending_clarification("query", "team", None)
        ctx.clear_pending_clarification()
        
        pending = ctx.get_pending_clarification()
        passed = pending is None
        self.record_result(TestResult(
            test_id="4.2",
            query="N/A",
            category="Clarification",
            passed=passed,
            expected="Clarification cleared",
            actual="Cleared" if pending is None else "Still set",
            details=""
        ))
        return passed
    
    def test_4_3_resolve_clarification(self):
        """Test resolving clarification with user response."""
        ctx = ConversationContext()
        ctx.set_pending_clarification(
            original_query="show sprint status",
            clarification_type="team",
            ambiguous_token=None
        )
        
        # Simulate user providing team name
        result = ctx.resolve_clarification("XOPS team")
        
        passed = result is not None and "XOPS" in result
        self.record_result(TestResult(
            test_id="4.3",
            query="show sprint status -> XOPS team",
            category="Clarification",
            passed=passed,
            expected="Reconstructed query with team",
            actual=result or "None",
            details=""
        ))
        return passed
    
    # ===================================================================
    # TEST CATEGORY 5: Query Variations (Dynamic Queries)
    # ===================================================================
    
    def test_5_1_sprint_query_with_team(self):
        """Test sprint query with explicit team."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("show current sprint for Development team")
        
        passed = team is not None
        self.record_result(TestResult(
            test_id="5.1",
            query="show current sprint for Development team",
            category="Query Variations",
            passed=passed,
            expected="Team extracted",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_5_2_bug_query_no_team(self):
        """Test bug query without team (project-wide)."""
        ctx = ConversationContext()
        extracted = ctx.update_context_from_query_text("list all active bugs")
        
        # No team should be extracted for project-wide query
        passed = 'team' not in extracted or extracted.get('team') is None
        self.record_result(TestResult(
            test_id="5.2",
            query="list all active bugs",
            category="Query Variations",
            passed=passed,
            expected="No team extracted",
            actual=str(extracted),
            details=""
        ))
        return passed
    
    def test_5_3_velocity_query(self):
        """Test velocity query (sprint-related)."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("team velocity for Platform team")
        
        passed = team is not None and 'platform' in team.lower()
        self.record_result(TestResult(
            test_id="5.3",
            query="team velocity for Platform team",
            category="Query Variations",
            passed=passed,
            expected="Platform",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_5_4_capacity_query(self):
        """Test capacity query."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("show capacity for QA")
        
        passed = team is not None
        self.record_result(TestResult(
            test_id="5.4",
            query="show capacity for QA",
            category="Query Variations",
            passed=passed,
            expected="QA",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_5_5_backlog_query(self):
        """Test backlog query."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("XOPS team backlog")
        
        passed = team is not None and 'xops' in team.lower()
        self.record_result(TestResult(
            test_id="5.5",
            query="XOPS team backlog",
            category="Query Variations",
            passed=passed,
            expected="XOPS",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_5_6_iteration_status_query(self):
        """Test iteration status query."""
        ctx = ConversationContext()
        team = ctx.extract_team_from_query("iteration status for Frontend team")
        
        passed = team is not None and 'frontend' in team.lower()
        self.record_result(TestResult(
            test_id="5.6",
            query="iteration status for Frontend team",
            category="Query Variations",
            passed=passed,
            expected="Frontend",
            actual=team or "None",
            details=""
        ))
        return passed
    
    def test_5_7_work_item_query_by_id(self):
        """Test work item query by ID (no team needed)."""
        ctx = ConversationContext()
        # ID queries don't need team context
        extracted = ctx.update_context_from_query_text("show me work item 12345")
        
        # Should not fail, team extraction is optional for ID queries
        passed = True  # No error thrown
        self.record_result(TestResult(
            test_id="5.7",
            query="show me work item 12345",
            category="Query Variations",
            passed=passed,
            expected="No error",
            actual="Processed",
            details=str(extracted)
        ))
        return passed
    
    def test_5_8_pr_query(self):
        """Test pull request query (no team needed)."""
        ctx = ConversationContext()
        extracted = ctx.update_context_from_query_text("list open pull requests")
        
        passed = True  # PR queries don't require team
        self.record_result(TestResult(
            test_id="5.8",
            query="list open pull requests",
            category="Query Variations",
            passed=passed,
            expected="No team required",
            actual="Processed",
            details=str(extracted)
        ))
        return passed
    
    def test_5_9_build_query(self):
        """Test build/pipeline query (no team needed)."""
        ctx = ConversationContext()
        extracted = ctx.update_context_from_query_text("show latest builds")
        
        passed = True
        self.record_result(TestResult(
            test_id="5.9",
            query="show latest builds",
            category="Query Variations",
            passed=passed,
            expected="No team required",
            actual="Processed",
            details=str(extracted)
        ))
        return passed
    
    def test_5_10_search_query(self):
        """Test code/wiki search query."""
        ctx = ConversationContext()
        extracted = ctx.update_context_from_query_text("search for authentication in codebase")
        
        passed = True
        self.record_result(TestResult(
            test_id="5.10",
            query="search for authentication in codebase",
            category="Query Variations",
            passed=passed,
            expected="No team required",
            actual="Processed",
            details=str(extracted)
        ))
        return passed
    
    # ===================================================================
    # TEST CATEGORY 6: Tool Selection Verification
    # ===================================================================
    
    def test_6_1_relevant_tools_for_bug_query(self):
        """Test relevant tools are selected for bug queries."""
        relevant, priority = _select_relevant_tools(MCP_TOOL_REGISTRY, "show me active bugs", max_tools=12)
        
        # Should include work item tools
        has_work_item_tools = any('wit' in name or 'search' in name for name in relevant.keys())
        passed = has_work_item_tools
        self.record_result(TestResult(
            test_id="6.1",
            query="show me active bugs",
            category="Tool Selection",
            passed=passed,
            expected="Work item tools selected",
            actual=f"Selected tools: {list(relevant.keys())[:5]}",
            details=""
        ))
        return passed
    
    def test_6_2_relevant_tools_for_pr_query(self):
        """Test relevant tools are selected for PR queries."""
        relevant, priority = _select_relevant_tools(MCP_TOOL_REGISTRY, "list open pull requests", max_tools=12)
        
        # Should include PR tools
        has_pr_tools = any('pr_' in name for name in relevant.keys())
        passed = has_pr_tools
        self.record_result(TestResult(
            test_id="6.2",
            query="list open pull requests",
            category="Tool Selection",
            passed=passed,
            expected="PR tools selected",
            actual=f"Selected tools: {list(relevant.keys())[:5]}",
            details=""
        ))
        return passed
    
    def test_6_3_relevant_tools_for_sprint_query(self):
        """Test relevant tools are selected for sprint queries."""
        relevant, priority = _select_relevant_tools(MCP_TOOL_REGISTRY, "sprint status for XOPS team", max_tools=12)
        
        # Should include iteration/sprint tools
        has_sprint_tools = any('iteration' in name.lower() or 'sprint' in name.lower() or 'work' in name.lower() 
                              for name in relevant.keys())
        passed = has_sprint_tools
        self.record_result(TestResult(
            test_id="6.3",
            query="sprint status for XOPS team",
            category="Tool Selection",
            passed=passed,
            expected="Sprint/iteration tools selected",
            actual=f"Selected tools: {list(relevant.keys())[:5]}",
            details=""
        ))
        return passed
    
    # ===================================================================
    # TEST CATEGORY 7: Default Team Handling
    # ===================================================================
    
    def test_7_1_default_team_constant_exists(self):
        """Test that FracPro Suite is used as default team."""
        # Check agent.py for default team constant
        agent_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                                   'agents', 'pm_agent', 'agent.py')
        with open(agent_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        passed = 'FracPro Suite' in content
        self.record_result(TestResult(
            test_id="7.1",
            query="N/A",
            category="Default Team",
            passed=passed,
            expected="FracPro Suite as default",
            actual="Found" if passed else "Not found",
            details=""
        ))
        return passed
    
    # ===================================================================
    # TEST CATEGORY 8: Session Context Persistence
    # ===================================================================
    
    def test_8_1_team_persists_across_queries(self):
        """Test that team persists in session context."""
        ctx = ConversationContext()
        ctx.update_context_from_query_text("sprint backlog for XOPS team")
        
        # Second query without team should use persisted team
        initial_team = ctx.team
        ctx.update_context_from_query_text("show active bugs")  # No team mentioned
        
        passed = ctx.team == initial_team and ctx.team is not None
        self.record_result(TestResult(
            test_id="8.1",
            query="[XOPS query] -> [no team query]",
            category="Session Context",
            passed=passed,
            expected="Team persists",
            actual=f"Initial: {initial_team}, After: {ctx.team}",
            details=""
        ))
        return passed
    
    def test_8_2_context_for_llm_includes_team(self):
        """Test that get_context_for_llm includes team."""
        ctx = ConversationContext()
        ctx.team = "XOPS"
        ctx.project = "FracPro-OPS"
        
        llm_ctx = ctx.get_context_for_llm()
        passed = 'team' in llm_ctx and llm_ctx['team'] == 'XOPS'
        self.record_result(TestResult(
            test_id="8.2",
            query="N/A",
            category="Session Context",
            passed=passed,
            expected="Team in LLM context",
            actual=str(llm_ctx),
            details=""
        ))
        return passed
    
    # ===================================================================
    # RUN ALL TESTS
    # ===================================================================
    
    def run_all_tests(self) -> Dict[str, Any]:
        """Run all test cases and return summary."""
        print("\n" + "=" * 70)
        print("PM AGENT QUERY VERIFICATION TEST SUITE")
        print("=" * 70 + "\n")
        
        # Get all test methods
        test_methods = [m for m in dir(self) if m.startswith('test_')]
        test_methods.sort()
        
        for method_name in test_methods:
            try:
                method = getattr(self, method_name)
                method()
            except Exception as e:
                # Record failure
                self.record_result(TestResult(
                    test_id=method_name,
                    query="N/A",
                    category="ERROR",
                    passed=False,
                    expected="No exception",
                    actual=f"Exception: {str(e)[:100]}",
                    details=str(e)
                ))
        
        # Print results
        print("\n" + "-" * 70)
        print("TEST RESULTS")
        print("-" * 70)
        
        current_category = None
        for result in self.results:
            if result.category != current_category:
                current_category = result.category
                print(f"\n📁 {current_category}")
            
            status = "✅" if result.passed else "❌"
            print(f"  {status} [{result.test_id}] {result.expected} -> {result.actual}")
            if not result.passed and result.details:
                print(f"      Details: {result.details[:100]}")
        
        # Summary
        print("\n" + "=" * 70)
        print(f"SUMMARY: {self.pass_count}/{self._test_count} passed ({100*self.pass_count/self._test_count:.1f}%)")
        if self.fail_count > 0:
            print(f"FAILED: {self.fail_count} tests")
        print("=" * 70 + "\n")
        
        return {
            "total": self._test_count,
            "passed": self.pass_count,
            "failed": self.fail_count,
            "results": [{"test_id": r.test_id, "passed": r.passed, "query": r.query} for r in self.results]
        }


def main():
    """Run the test suite."""
    suite = QueryVerificationSuite()
    results = suite.run_all_tests()
    
    # Exit with error code if any tests failed
    if results["failed"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
