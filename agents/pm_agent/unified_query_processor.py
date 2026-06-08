"""
Unified Query Processor - Central entry point for all ADO query processing.

This module serves as the main orchestration layer that:
1. Analyzes incoming queries to determine type and complexity
2. Routes to appropriate handlers (single tool, multi-tool, or skill)
3. Applies reformulation if needed
4. Aggregates and formats results
5. Handles errors gracefully with fallbacks

Usage:
    from agents.pm_agent.unified_query_processor import UnifiedQueryProcessor
    
    processor = UnifiedQueryProcessor(tool_executor, mcp_connector, context)
    result = await processor.process(user_query)
"""

import re
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

# Internal imports
from utilities.mcp.tool_registry import (
    find_tools_for_query,
    get_tools_by_category,
    get_tool_metadata
)
from .multi_tool_orchestrator import MultiToolOrchestrator, execute_multi_tool_query
from .query_reformulation import (
    get_reformulation_handler,
    attempt_reformulation,
    normalize_time_expression,
    ReformulationStrategy
)
from .dynamic_query_handler import analyze_advanced_intent, QueryDomain
from .query_aware_filter import analyze_query_intent, filter_work_items_by_intent

logger = logging.getLogger(__name__)


class QueryComplexity(Enum):
    """Complexity levels for queries."""
    SIMPLE = "simple"  # Single tool, direct answer
    MODERATE = "moderate"  # Single tool with filtering
    COMPLEX = "complex"  # Multiple tools needed
    SKILL = "skill"  # Requires PM skill execution


class RouteDecision(Enum):
    """Routing decisions for queries."""
    SINGLE_TOOL = "single_tool"
    MULTI_TOOL = "multi_tool"
    PM_SKILL = "pm_skill"
    CLARIFICATION = "clarification"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass
class QueryAnalysis:
    """Complete analysis of a user query."""
    query: str
    complexity: QueryComplexity
    route: RouteDecision
    
    # Detected entities
    work_item_ids: List[int] = field(default_factory=list)
    person_names: List[str] = field(default_factory=list)
    iteration_names: List[str] = field(default_factory=list)
    project_names: List[str] = field(default_factory=list)
    repo_names: List[str] = field(default_factory=list)
    
    # Detected intent
    domain: Optional[QueryDomain] = None
    is_aggregate: bool = False
    is_comparative: bool = False
    is_time_filtered: bool = False
    
    # Time range
    time_from: Optional[datetime] = None
    time_to: Optional[datetime] = None
    
    # Suggested tools
    primary_tool: Optional[str] = None
    secondary_tools: List[str] = field(default_factory=list)
    
    # For PM skills
    skill_name: Optional[str] = None
    
    # Confidence
    confidence: float = 0.8
    needs_clarification: bool = False
    clarification_prompt: Optional[str] = None


@dataclass
class ProcessingResult:
    """Result of query processing."""
    success: bool
    response: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    item_count: int = 0
    
    # Metadata
    query_analysis: Optional[QueryAnalysis] = None
    tools_used: List[str] = field(default_factory=list)
    reformulations_tried: int = 0
    processing_time_ms: int = 0
    
    # Errors
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class UnifiedQueryProcessor:
    """
    Unified query processor that handles all types of ADO queries.
    
    Features:
    - Intelligent query routing
    - Multi-tool orchestration
    - Automatic reformulation on empty results
    - Result aggregation and formatting
    - Error handling with fallbacks
    """
    
    def __init__(self, tool_executor, mcp_connector, context: Dict[str, Any]):
        """
        Initialize the query processor.
        
        Args:
            tool_executor: ToolExecutor instance for running tools
            mcp_connector: MCPConnector for direct MCP calls
            context: Current context (project, team, etc.)
        """
        self.tool_executor = tool_executor
        self.mcp_connector = mcp_connector
        self.context = context
        
        self.reformulation_handler = get_reformulation_handler()
        self.orchestrator = MultiToolOrchestrator(tool_executor, mcp_connector)
        
        # PM skills that should be routed to skill agents
        self.pm_skills = {
            "developer_knowledge_base": ["developer knowledge", "who knows", "expertise", "skill matrix", "developer skills"],
            "sprint_plan_generator": ["generate sprint plan", "create sprint plan", "plan the sprint"],
            "capacity_check": ["check capacity", "team capacity", "workload", "bandwidth", "overloaded"],
            "backlog_assignments": ["assign backlog", "distribute work", "balance workload"],
            "bug_areas_highlight": ["recurring bugs", "bug patterns", "bug hotspots", "problematic areas"],
            "billing_deviation": ["billing deviation", "deviation in billing", "billing report", "over-billing", "under-billing", "billing target", "billing hours", "deviation report", "billing calculation", "billing deviaiton", "deviaiton report"],
        }
    
    async def process(self, query: str, conversation_history: List[Dict] = None) -> ProcessingResult:
        """
        Process a user query end-to-end.
        
        Args:
            query: User's natural language query
            conversation_history: Previous conversation for context
            
        Returns:
            ProcessingResult with response and metadata
        """
        start_time = datetime.utcnow()
        
        try:
            # Step 1: Analyze the query
            analysis = self._analyze_query(query, conversation_history)
            logger.info(f"[PROCESSOR] Query analysis: route={analysis.route.value}, complexity={analysis.complexity.value}")
            
            # Step 2: Route and process
            if analysis.route == RouteDecision.CLARIFICATION:
                result = self._handle_clarification(analysis)
            elif analysis.route == RouteDecision.OUT_OF_SCOPE:
                result = self._handle_out_of_scope(analysis)
            elif analysis.route == RouteDecision.PM_SKILL:
                result = await self._process_pm_skill(analysis)
            elif analysis.route == RouteDecision.MULTI_TOOL:
                result = await self._process_multi_tool(analysis)
            else:
                result = await self._process_single_tool(analysis)
            
            # Step 3: Apply post-processing filters if needed
            if result.success and result.items and analysis.domain == QueryDomain.WORK_ITEMS:
                result = self._apply_query_filters(result, analysis)
            
            result.query_analysis = analysis
            result.processing_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            
            return result
            
        except Exception as e:
            logger.error(f"[PROCESSOR] Error processing query: {e}", exc_info=True)
            return ProcessingResult(
                success=False,
                response=f"I encountered an error processing your query: {str(e)}. Please try rephrasing or simplifying your request.",
                errors=[str(e)],
                processing_time_ms=int((datetime.utcnow() - start_time).total_seconds() * 1000)
            )
    
    def _analyze_query(self, query: str, history: List[Dict] = None) -> QueryAnalysis:
        """Perform comprehensive query analysis."""
        q = query.lower()
        
        analysis = QueryAnalysis(
            query=query,
            complexity=QueryComplexity.SIMPLE,
            route=RouteDecision.SINGLE_TOOL
        )
        
        # Extract entities
        analysis.work_item_ids = self._extract_work_item_ids(query)
        analysis.person_names = self._extract_person_names(query)
        analysis.iteration_names = self._extract_iteration_names(query)
        
        # Check for PM skills
        for skill_name, triggers in self.pm_skills.items():
            if any(trigger in q for trigger in triggers):
                analysis.route = RouteDecision.PM_SKILL
                analysis.skill_name = skill_name
                analysis.complexity = QueryComplexity.SKILL
                return analysis
        
        # Detect domain
        analysis.domain = self._detect_domain(q)
        
        # Check for aggregation
        analysis.is_aggregate = any(w in q for w in ["how many", "count", "total", "sum", "average", "group by", "per"])
        
        # Check for comparison
        analysis.is_comparative = any(w in q for w in ["compare", "versus", "vs", "between", "difference"])
        
        # Check for time filtering
        analysis.time_from, analysis.time_to = normalize_time_expression(q)
        analysis.is_time_filtered = analysis.time_from is not None
        
        # Determine complexity and routing
        complexity_score = 0
        if analysis.is_aggregate:
            complexity_score += 2
        if analysis.is_comparative:
            complexity_score += 3
        if len(analysis.iteration_names) > 1:
            complexity_score += 2
        if len(analysis.person_names) > 0 and analysis.domain == QueryDomain.WORK_ITEMS:
            complexity_score += 1
        
        if complexity_score >= 3:
            analysis.complexity = QueryComplexity.COMPLEX
            analysis.route = RouteDecision.MULTI_TOOL
        elif complexity_score >= 1:
            analysis.complexity = QueryComplexity.MODERATE
        
        # Check if multi-tool orchestration is needed
        if self.orchestrator.requires_multi_tool(query):
            analysis.route = RouteDecision.MULTI_TOOL
            analysis.complexity = QueryComplexity.COMPLEX
        
        # Find best tools
        matching_tool_names = find_tools_for_query(query, top_n=3)
        if matching_tool_names:
            analysis.primary_tool = matching_tool_names[0]
            analysis.secondary_tools = matching_tool_names[1:3]
        
        # Check for ambiguity that needs clarification
        if self._needs_clarification(query, analysis):
            analysis.needs_clarification = True
            analysis.clarification_prompt = self._generate_clarification(query, analysis)
        
        return analysis
    
    def _detect_domain(self, query: str) -> QueryDomain:
        """Detect the domain of the query."""
        domain_patterns = {
            QueryDomain.WORK_ITEMS: ["bug", "story", "task", "work item", "item", "issue", "backlog"],
            QueryDomain.PULL_REQUESTS: ["pr", "pull request", "merge", "review"],
            QueryDomain.BUILDS: ["build", "pipeline", "ci", "deploy", "release"],
            QueryDomain.REPOSITORIES: ["repo", "repository", "branch", "commit", "code"],
            QueryDomain.ITERATIONS: ["sprint", "iteration"],
            QueryDomain.TEST: ["test", "qa", "testing"],
        }
        
        for domain, keywords in domain_patterns.items():
            if any(kw in query for kw in keywords):
                return domain
        
        return QueryDomain.WORK_ITEMS  # Default
    
    def _extract_work_item_ids(self, query: str) -> List[int]:
        """Extract work item IDs from query."""
        # Match patterns like #1234, ID 1234, item 1234, etc.
        patterns = [
            r"#(\d+)",
            r"(?:id|item|bug|story|task)\s*#?(\d+)",
            r"(?:work\s*item|wi)\s*#?(\d+)"
        ]
        
        ids = set()
        for pattern in patterns:
            for match in re.finditer(pattern, query, re.IGNORECASE):
                ids.add(int(match.group(1)))
        
        return list(ids)
    
    def _extract_person_names(self, query: str) -> List[str]:
        """Extract person names from query."""
        # Look for patterns like "assigned to John", "by John Smith", "for Jane"
        patterns = [
            r"(?:assigned\s+to|by|for|from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)'s\s+(?:bugs?|items?|tasks?|work)"
        ]
        
        names = set()
        for pattern in patterns:
            for match in re.finditer(pattern, query):
                name = match.group(1)
                # Filter out common words
                if name.lower() not in ["the", "a", "an", "this", "that", "sprint", "iteration"]:
                    names.add(name)
        
        return list(names)
    
    def _extract_iteration_names(self, query: str) -> List[str]:
        """Extract iteration/sprint names from query."""
        patterns = [
            r"(?:sprint|iteration)\s+(\S+)",
            r"(?:sprint|iteration)\s+\"([^\"]+)\"",
            r"(?:sprint|iteration)\s+'([^']+)'"
        ]
        
        iterations = set()
        for pattern in patterns:
            for match in re.finditer(pattern, query, re.IGNORECASE):
                iterations.add(match.group(1))
        
        # Handle special cases
        if "current sprint" in query.lower() or "this sprint" in query.lower():
            iterations.add("@CurrentIteration")
        
        return list(iterations)
    
    def _needs_clarification(self, query: str, analysis: QueryAnalysis) -> bool:
        """Determine if query needs clarification."""
        q = query.lower()
        
        # Ambiguous entity references
        if any(w in q for w in ["them", "those", "it", "that one"]) and not analysis.work_item_ids:
            return True
        
        # Missing project context for cross-project queries
        if "all projects" in q and not self.context.get("project"):
            return True
        
        return False
    
    def _generate_clarification(self, query: str, analysis: QueryAnalysis) -> str:
        """Generate a clarification question."""
        q = query.lower()
        
        if any(w in q for w in ["them", "those", "it"]):
            return "I'm not sure what you're referring to. Could you please specify the work item ID or describe what you're looking for?"
        
        return "Could you please provide more details about what you're looking for?"
    
    def _handle_clarification(self, analysis: QueryAnalysis) -> ProcessingResult:
        """Handle queries that need clarification."""
        return ProcessingResult(
            success=True,
            response=analysis.clarification_prompt or "Could you please provide more details?",
            warnings=["Query needed clarification"]
        )
    
    def _handle_out_of_scope(self, analysis: QueryAnalysis) -> ProcessingResult:
        """Handle out-of-scope queries."""
        return ProcessingResult(
            success=True,
            response="I'm sorry, but that query doesn't seem to be related to Azure DevOps. I can help you with work items, sprints, pull requests, builds, and other ADO-related questions.",
            warnings=["Query was out of scope"]
        )
    
    async def _process_pm_skill(self, analysis: QueryAnalysis) -> ProcessingResult:
        """Process queries that require PM skills."""
        skill = analysis.skill_name
        logger.info(f"[PROCESSOR] Routing to PM skill: {skill}")
        
        # This would typically invoke the skill agent
        # For now, return a placeholder that indicates skill routing
        return ProcessingResult(
            success=True,
            response=f"[Routing to {skill} skill for processing...]",
            tools_used=[skill],
            warnings=[f"PM skill '{skill}' should be invoked"]
        )
    
    async def _process_multi_tool(self, analysis: QueryAnalysis) -> ProcessingResult:
        """Process queries requiring multiple tools."""
        logger.info("[PROCESSOR] Processing with multi-tool orchestrator")
        
        try:
            result = await execute_multi_tool_query(
                analysis.query,
                self.context,
                self.tool_executor,
                self.mcp_connector
            )
            
            if result:
                return ProcessingResult(
                    success=True,
                    response=result,
                    tools_used=["multi_tool_orchestrator"]
                )
            else:
                # Fallback to single tool processing
                return await self._process_single_tool(analysis)
                
        except Exception as e:
            logger.error(f"[PROCESSOR] Multi-tool processing failed: {e}")
            # Fallback to single tool
            return await self._process_single_tool(analysis)
    
    async def _process_single_tool(self, analysis: QueryAnalysis) -> ProcessingResult:
        """Process queries with a single tool."""
        tool_name = analysis.primary_tool
        
        if not tool_name:
            # Default to WIQL for work items, search for others
            if analysis.domain == QueryDomain.WORK_ITEMS:
                tool_name = "execute_wiql"
            else:
                tool_name = "search_workitem"
        
        logger.info(f"[PROCESSOR] Processing with tool: {tool_name}")
        
        # Build tool arguments
        args = self._build_tool_args(tool_name, analysis)
        
        # Execute with reformulation retry
        max_attempts = 3
        tried_strategies = []
        
        for attempt in range(max_attempts):
            try:
                tool_plan = {
                    "action": "call_tool",
                    "tool": tool_name,
                    "args": args,
                    "confidence": 0.9
                }
                
                result = await self.tool_executor.execute(tool_plan)
                
                if result.get("success"):
                    items = result.get("items", [])
                    count = result.get("count", len(items))
                    
                    if count > 0 or attempt == max_attempts - 1:
                        return ProcessingResult(
                            success=True,
                            response=self._format_result(result, analysis),
                            items=items,
                            item_count=count,
                            tools_used=[tool_name],
                            reformulations_tried=attempt
                        )
                
                # Try reformulation
                reformulation = attempt_reformulation(
                    analysis.query,
                    tool_name,
                    args,
                    result.get("count", 0),
                    attempt + 1,
                    tried_strategies
                )
                
                if reformulation:
                    logger.info(f"[PROCESSOR] Reformulating: {reformulation.explanation}")
                    tried_strategies.append(reformulation.strategy)
                    
                    # Check for alternative tool
                    if "_alternative_tool" in reformulation.reformulated_args:
                        tool_name = reformulation.reformulated_args.pop("_alternative_tool")
                    
                    args = reformulation.reformulated_args
                else:
                    break
                    
            except Exception as e:
                logger.error(f"[PROCESSOR] Tool execution failed: {e}")
                if attempt == max_attempts - 1:
                    return ProcessingResult(
                        success=False,
                        response=f"I couldn't retrieve the requested information. Error: {str(e)}",
                        errors=[str(e)],
                        reformulations_tried=attempt + 1
                    )
        
        return ProcessingResult(
            success=True,
            response="I couldn't find any items matching your query. Try broadening your search criteria.",
            item_count=0,
            tools_used=[tool_name],
            reformulations_tried=max_attempts,
            warnings=["No results found after reformulation attempts"]
        )
    
    def _build_tool_args(self, tool_name: str, analysis: QueryAnalysis) -> Dict[str, Any]:
        """Build arguments for a tool based on query analysis."""
        project = self.context.get("project", "")
        team = self.context.get("team", project)
        
        args = {"project": project}
        
        if tool_name == "wit_get_work_item":
            if analysis.work_item_ids:
                args["id"] = analysis.work_item_ids[0]
        
        elif tool_name == "wit_get_work_items_for_iteration":
            args["team"] = team
            if analysis.iteration_names:
                args["iterationId"] = analysis.iteration_names[0]
            else:
                args["iterationId"] = "@CurrentIteration"
        
        elif tool_name == "execute_wiql":
            args["wiql"] = self._build_wiql(analysis)
        
        elif tool_name == "search_workitem":
            args["project"] = [project]
            args["searchText"] = analysis.query[:50]
            args["top"] = 100
            
            if analysis.domain == QueryDomain.WORK_ITEMS:
                q = analysis.query.lower()
                if "bug" in q:
                    args["workItemType"] = ["Bug"]
                elif "story" in q:
                    args["workItemType"] = ["User Story"]
                elif "task" in q:
                    args["workItemType"] = ["Task"]
        
        elif tool_name == "pr_list_pull_requests":
            q = analysis.query.lower()
            if "active" in q or "open" in q:
                args["status"] = "active"
            elif "completed" in q or "merged" in q:
                args["status"] = "completed"
            else:
                args["status"] = "all"
        
        elif tool_name == "pipelines_get_builds":
            q = analysis.query.lower()
            if "failed" in q or "broken" in q:
                args["resultFilter"] = "failed"
        
        return args
    
    def _build_wiql(self, analysis: QueryAnalysis) -> str:
        """Build a WIQL query from analysis."""
        project = self.context.get("project", "")
        conditions = [f"[System.TeamProject] = '{project}'"]
        
        q = analysis.query.lower()
        
        # Work item type
        if "bug" in q:
            conditions.append("[System.WorkItemType] = 'Bug'")
        elif "story" in q or "user story" in q:
            conditions.append("[System.WorkItemType] = 'User Story'")
        elif "task" in q:
            conditions.append("[System.WorkItemType] = 'Task'")
        
        # State - use centralized config
        from config import config as _cfg
        if "active" in q or "open" in q:
            _ns = _cfg.get_states_for_category('not_started')
            _ip = _cfg.get_states_for_category('in_progress')
            _active_sql = ", ".join(f"'{s}'" for s in (_ns + _ip))
            conditions.append(f"[System.State] IN ({_active_sql})")
        elif "closed" in q or "done" in q:
            _comp = _cfg.get_states_for_category('completed')
            _comp_sql = ", ".join(f"'{s}'" for s in _comp)
            conditions.append(f"[System.State] IN ({_comp_sql})")
        
        # Time filter
        if analysis.time_from:
            days_ago = (datetime.utcnow() - analysis.time_from).days
            conditions.append(f"[System.ChangedDate] >= @Today - {days_ago}")
        
        # Person filter
        if analysis.person_names:
            person = analysis.person_names[0]
            conditions.append(f"[System.AssignedTo] CONTAINS '{person}'")
        
        # Iteration filter
        if analysis.iteration_names:
            iteration = analysis.iteration_names[0]
            if iteration == "@CurrentIteration":
                conditions.append("[System.IterationPath] = @CurrentIteration")
            else:
                conditions.append(f"[System.IterationPath] UNDER '{iteration}'")
        
        where_clause = " AND ".join(conditions)
        
        return f"""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], [System.WorkItemType], [System.ChangedDate]
        FROM WorkItems
        WHERE {where_clause}
        ORDER BY [System.ChangedDate] DESC
        """.strip()
    
    def _apply_query_filters(self, result: ProcessingResult, analysis: QueryAnalysis) -> ProcessingResult:
        """Apply post-processing filters based on query intent."""
        if not result.items:
            return result
        
        try:
            intent = analyze_query_intent(analysis.query)
            filtered_items, summary = filter_work_items_by_intent(result.items, intent)
            
            result.items = filtered_items
            result.item_count = len(filtered_items)
            
            if summary:
                result.response = summary + "\n\n" + result.response
                
        except Exception as e:
            logger.warning(f"[PROCESSOR] Failed to apply query filters: {e}")
        
        return result
    
    def _format_result(self, result: Dict[str, Any], analysis: QueryAnalysis) -> str:
        """Format tool result into user-friendly response."""
        items = result.get("items", [])
        count = result.get("count", len(items))
        
        if count == 0:
            return "No items found matching your query."
        
        lines = [f"Found **{count}** items:\n"]
        
        # Format based on domain
        if analysis.domain == QueryDomain.WORK_ITEMS:
            for i, item in enumerate(items[:15], 1):
                fields = item.get("fields", item)
                item_id = item.get("id", fields.get("System.Id", "?"))
                title = fields.get("System.Title", fields.get("title", "Untitled"))[:60]
                state = fields.get("System.State", fields.get("state", ""))
                wi_type = fields.get("System.WorkItemType", "")
                
                emoji = {"Bug": "🐛", "User Story": "📖", "Task": "✅", "Feature": "⭐"}.get(wi_type, "📋")
                lines.append(f"{i}. {emoji} **[{item_id}]** {title}")
                if state:
                    lines.append(f"   State: {state}")
        
        elif analysis.domain == QueryDomain.PULL_REQUESTS:
            for i, pr in enumerate(items[:10], 1):
                pr_id = pr.get("pullRequestId", "?")
                title = pr.get("title", "Untitled")[:60]
                status = pr.get("status", "")
                lines.append(f"{i}. 🔀 **[PR {pr_id}]** {title} ({status})")
        
        elif analysis.domain == QueryDomain.BUILDS:
            for i, build in enumerate(items[:10], 1):
                build_id = build.get("id", build.get("buildNumber", "?"))
                result_status = build.get("result", build.get("status", ""))
                branch = build.get("sourceBranch", "").replace("refs/heads/", "")
                emoji = "✅" if result_status == "succeeded" else "❌" if result_status == "failed" else "⏳"
                lines.append(f"{i}. {emoji} Build **{build_id}** - {result_status} (branch: {branch})")
        
        if count > 15:
            lines.append(f"\n*...and {count - 15} more items*")
        
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

async def process_query(
    query: str,
    tool_executor,
    mcp_connector,
    context: Dict[str, Any],
    conversation_history: List[Dict] = None
) -> ProcessingResult:
    """
    Convenience function to process a query.
    
    Args:
        query: User's natural language query
        tool_executor: ToolExecutor instance
        mcp_connector: MCPConnector instance
        context: Current context
        conversation_history: Optional conversation history
        
    Returns:
        ProcessingResult
    """
    processor = UnifiedQueryProcessor(tool_executor, mcp_connector, context)
    return await processor.process(query, conversation_history)
