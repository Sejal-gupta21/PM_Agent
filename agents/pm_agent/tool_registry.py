"""
PM Agent Tool Registry - Fixed Skills for LLM Discovery.

This registry defines the PM Agent's fixed skills with metadata that enables
the LLM planner to understand:
1. What each skill does
2. What arguments it requires/accepts
3. Example use cases and queries that should trigger the skill
4. Expected output format

IMPORTANT: This is separate from the MCP/ADO tool registry (utilities/mcp/tool_registry.py).
The MCP registry handles ADO API tools, while this handles PM-specific business skills.

Usage:
    from agents.pm_agent.tool_registry import PM_SKILL_REGISTRY, get_skill_metadata
    
    # Get all skills for LLM planning
    skills = get_skill_metadata()
    
    # Match query to skill
    skill_name = match_query_to_skill("show recurring bugs")
"""

from typing import Dict, Any, List, Optional
import re
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# SKILL TO TOOLS MAPPING
# Maps the 4 core skills to the MCP/ADO tools they need for execution
# =============================================================================

SKILL_TO_TOOLS_MAP: Dict[str, Dict[str, Any]] = {
    "search_developers_by_skill": {
        "primary_tools": ["search_developers_by_skill"],
        "supporting_tools": [],
        "execution_script": None,
        "output_files": [],
        "description": "Search for developers by skills using vector database semantic matching",
        "canonical_queries": [
            "who knows angular",
            "who knows react",
            "who knows java",
            "who knows python",
            "who knows typescript",
            "find angular developers",
            "find react developers",
            "find java developers",
            "find python developers",
            "list angular developers",
            "list all angular developers",
            "list all java developers",
            "list all react developers",
            "list all frontend developers",
            "list all backend developers",
            "developers who know angular",
            "developers who know java",
            "developers with angular experience",
            "developers with java experience",
            "who can work on angular",
            "who can work on java",
            "who can work on react",
            "who can work on frontend",
            "who can work on backend",
            "team members with angular skills",
            "team members with java skills",
            "show me angular developers",
            "show me java developers",
            "show me frontend developers",
            "show me backend developers",
            "who is expert in angular",
            "who is expert in java",
            "who is expert in react",
            "angular expertise",
            "java expertise",
            "react expertise",
            "frontend expertise",
            "backend expertise",
        ],
    },
    "developer_knowledge_base": {
        "primary_tools": ["developer_skills", "search_developers_by_skill", "search_workitem", "execute_wiql"],
        "supporting_tools": ["list_area_paths", "wit_get_work_items"],
        "execution_script": "scripts/build_knowledge_base.py",
        "output_files": ["data/developer_skills.json", "outputs/developer_kb_*.csv"],
        "description": "Analyzes developer expertise from commit history and work item associations",
        "canonical_queries": [
            "show developer knowledge base",
            "what do our developers know",
            "display team expertise",
            "who has what skills",
            "list developer capabilities",
            "team skill matrix",
            "developer proficiency report",
            "what technologies do we have expertise in",
            "show me the skills of each developer",
            "who knows python",
            "who knows react",
            "which developer is best at java",
            "show developer contributions",
            "team technical skills breakdown",
        ],
    },
    "sprint_plan_generator": {
        "primary_tools": ["execute_wiql", "search_workitem", "wit_get_work_items"],
        "supporting_tools": ["list_area_paths", "work_list_team_iterations"],
        "execution_script": "scripts/generate_sprint_plan.py",
        "output_files": ["outputs/sprint_plan_*.csv"],
        "description": "Creates sprint plans with task breakdown and developer assignments",
        "canonical_queries": [
            "generate sprint plan",
            "create a plan for the sprint",
            "help me plan the iteration",
            "make a sprint schedule",
            "build a sprint plan",
            "plan the next sprint",
            "create iteration plan",
            "generate sprint work allocation",
            "plan sprint assignments",
            "create sprint task breakdown",
            "generate sprint schedule",
            "make sprint roadmap",
            "plan sprint capacity",
            "create sprint timeline",
        ],
    },
    "capacity_check": {
        "primary_tools": ["work_list_team_iterations", "execute_wiql"],
        "supporting_tools": ["search_workitem", "wit_get_work_items"],
        "execution_script": "scripts/check_capacity.py",
        "output_files": ["outputs/capacity_report_*.json", "outputs/capacity_report_*.csv"],
        "description": "Analyzes team capacity, utilization, and workload distribution",
        "canonical_queries": [
            "check capacity",
            "how much bandwidth do we have",
            "team workload status",
            "are we overloaded",
            "show team capacity",
            "check team availability",
            "how much work can we take",
            "team bandwidth check",
            "show capacity report",
            "are developers overloaded",
            "check workload distribution",
            "team utilization status",
            "how many hours do we have available",
            "show capacity forecast",
            "check sprint capacity",
            "are we over capacity",
            "team availability report",
            "show remaining capacity",
            "who has capacity for more work",
        ],
    },
    "backlog_assignments": {
        "primary_tools": ["execute_wiql", "search_workitem"],
        "supporting_tools": ["work_list_team_iterations", "wit_get_work_items"],
        "execution_script": "scripts/assign_backlog_to_underutilized.py",
        "output_files": ["outputs/backlog_assignments_*.csv"],
        "description": "Assigns backlog items to underutilized developers based on capacity",
        "canonical_queries": [
            "assign backlog items",
            "distribute work to underutilized devs",
            "balance the workload",
            "who can take more work",
            "assign backlog to available developers",
            "distribute backlog items",
            "balance team workload",
            "assign work to underutilized team members",
            "allocate backlog to developers",
            "distribute work evenly",
            "assign tasks to available devs",
            "balance developer workload",
            "assign backlog based on capacity",
            "distribute work fairly",
            "balance sprint workload",
        ],
    },
    # Additional PM Agent fixed skills
    "bug_areas_highlight": {
        "primary_tools": ["search_workitem", "execute_wiql"],
        "supporting_tools": ["list_area_paths", "wit_get_work_items"],
        "execution_script": None,  # Uses handler directly
        "output_files": ["outputs/bug_areas_*.html", "logs/bug_areas_preview.html"],
        "description": "Detects recurring bugs by area path and generates analysis report",
        "canonical_queries": [
            "show recurring bugs",
            "find recurring bugs",
            "bug areas highlight",
            "analyze bug patterns",
            "where do bugs keep happening",
            "problematic code areas",
            "bug hotspots",
        ],
    },
    "get_sprint_status": {
        "primary_tools": ["execute_wiql", "search_workitem"],
        "supporting_tools": ["work_list_team_iterations"],
        "execution_script": None,
        "output_files": [],
        "description": "Gets current sprint status with tracking metrics and at-risk items",
        "canonical_queries": [
            "what is the derailing work item",
            "show derailing items",
            "at risk items in sprint",
            "blocked tasks in sprint",
            "sprint status",
            "are we on track",
            "sprint progress",
            "planned vs completed",
            "off track items",
            "delayed work items",
        ],
    },
    # NOTE: get_capacity_forecast REMOVED from PM Agent tool registry
    # It's a PM Skill Agent skill, not a PM Agent tool
    # See: agents/pm_skill_agent/skills.py -> SKILL_DEFINITIONS["get_capacity_forecast"]
    "backlog_triaging": {
        "primary_tools": ["execute_wiql", "search_workitem", "wit_get_work_items_batch_by_ids"],
        "supporting_tools": ["core_list_project_teams"],
        "execution_script": None,
        "output_files": ["outputs/backlog_health_*.html"],
        "description": "Analyzes backlog health, generates reports with effort estimates, and identifies thin backlogs",
        "canonical_queries": [
            "backlog health",
            "is backlog healthy",
            "do we have enough backlog",
            "refined items count",
            "thin backlog warning",
            "show backlog health",
            "backlog status",
            "check backlog",
            "backlog health report",
            "backlog health of XOPS",
            "backlog health for team",
            "backlog runway",
            "backlog items",
            "list backlog items",
        ],
    },
}


def get_skill_tools(skill_id: str) -> Optional[Dict[str, Any]]:
    """Get the tools mapping for a specific skill.
    
    Args:
        skill_id: The skill identifier
        
    Returns:
        Dict with primary_tools, supporting_tools, etc. or None if not found
    """
    return SKILL_TO_TOOLS_MAP.get(skill_id)

# =============================================================================
# PM AGENT FIXED SKILL REGISTRY
# =============================================================================

PM_SKILL_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ==========================================================================
    # BILLING DEVIATION TOOLS - Granular breakdown for flexible LLM orchestration
    # These tools enable step-by-step billing deviation analysis with user interaction
    # ==========================================================================
    "parse_billing_query": {
        "pagination": False,
        "required_args": ["query"],
        "optional_args": {},
        "arg_descriptions": {
            "query": "User's natural language query to parse for area path and month"
        },
        "description": "Extract area path and month from user's billing deviation query. First step in billing deviation workflow.",
        "use_cases": [
            "parse billing query", "extract area path", "identify billing period",
            "understand billing request"
        ],
        "examples": [
            {"description": "Parse query with area", "args": {"query": "give billing deviation report for xops 25 for current month"}},
            {"description": "Parse general query", "args": {"query": "show billing deviation for current month"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "billing_deviation.tools_breakdown.parse_billing_query"
    },
    "prompt_for_target_hours": {
        "pagination": False,
        "required_args": [],
        "optional_args": {
            "area_path": "str"
        },
        "arg_descriptions": {
            "area_path": "Optional area path from parsed query (null for default 4000 hours)"
        },
        "description": "Determine target hours strategy: ask user if area path provided, else use default 4000. Returns instructions for LLM on what to do next.",
        "use_cases": [
            "get target hours", "determine billing target", "ask for target hours",
            "default target hours"
        ],
        "examples": [
            {"description": "Area path provided", "args": {"area_path": "xops 25"}},
            {"description": "No area path (use default)", "args": {}}
        ],
        "agent": "pm_skill_agent",
        "handler": "billing_deviation.tools_breakdown.prompt_for_target_hours"
    },
    "fetch_work_items_by_billing_date": {
        "pagination": False,
        "required_args": [],
        "optional_args": {
            "month": "int",
            "year": "int",
            "area_path": "str",
            "state": "str"
        },
        "arg_descriptions": {
            "month": "Month number (1-12), defaults to current month",
            "year": "Year (YYYY), defaults to current year",
            "area_path": "Optional area path to filter by",
            "state": "Work item state filter (default: Closed)"
        },
        "description": "Fetch work items filtered by Estimated Billing Date field (NOT StateChangeDate). Returns closed work items from specified month with completed work hours. CRITICAL: Uses 'Estimated Billing Date' custom field.",
        "use_cases": [
            "fetch billing work items", "get closed items for month", "billing date filter",
            "actual hours for billing", "completed work by month"
        ],
        "examples": [
            {"description": "Current month all areas", "args": {}},
            {"description": "Specific area and month", "args": {"month": 1, "year": 2026, "area_path": "xops 25"}},
            {"description": "Custom month", "args": {"month": 12, "year": 2025}}
        ],
        "agent": "pm_skill_agent",
        "handler": "billing_deviation.tools_breakdown.fetch_work_items_by_billing_date"
    },
    "get_area_paths_for_month": {
        "pagination": False,
        "required_args": [],
        "optional_args": {
            "month": "int",
            "year": "int"
        },
        "arg_descriptions": {
            "month": "Month number (1-12), defaults to current month",
            "year": "Year (YYYY), defaults to current year"
        },
        "description": "List available area paths that have closed work items in the specified month. Useful for validation and area path selection/suggestions.",
        "use_cases": [
            "list available area paths", "get area paths for month", "validate area path",
            "show billing areas", "area path suggestions"
        ],
        "examples": [
            {"description": "Current month areas", "args": {}},
            {"description": "Specific month areas", "args": {"month": 1, "year": 2026}}
        ],
        "agent": "pm_skill_agent",
        "handler": "billing_deviation.tools_breakdown.get_area_paths_for_month"
    },
    "calculate_billing_deviation": {
        "pagination": False,
        "required_args": ["target_hours", "actual_hours"],
        "optional_args": {
            "area_path": "str"
        },
        "arg_descriptions": {
            "target_hours": "Target/planned hours (from user or default 4000)",
            "actual_hours": "Actual completed work hours (sum of Completed Work field)",
            "area_path": "Optional area path context for reporting"
        },
        "description": "Calculate billing deviation using formula: Deviation = Target - Actual. Positive = Under-billing (behind target), Negative = Over-billing (exceeded target). Returns status, percentage, and criticality.",
        "use_cases": [
            "calculate deviation", "compute billing variance", "target vs actual",
            "billing status", "over-billing check", "under-billing check"
        ],
        "examples": [
            {"description": "Calculate deviation", "args": {"target_hours": 4000, "actual_hours": 3850.5}},
            {"description": "With area context", "args": {"target_hours": 2000, "actual_hours": 1850, "area_path": "xops 25"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "billing_deviation.tools_breakdown.calculate_billing_deviation"
    },
    "generate_billing_summary_text": {
        "pagination": False,
        "required_args": ["deviation_result"],
        "optional_args": {
            "work_item_details": "dict"
        },
        "arg_descriptions": {
            "deviation_result": "Result dictionary from calculate_billing_deviation",
            "work_item_details": "Optional work item fetch results for area breakdown"
        },
        "description": "Create formatted text summary for chat display. Includes target, actual, deviation, status with emoji, and optional area breakdown.",
        "use_cases": [
            "format billing summary", "generate text report", "display billing status",
            "billing deviation text"
        ],
        "examples": [
            {"description": "Basic summary", "args": {"deviation_result": {"target_hours": 4000, "actual_hours": 3850, "deviation": 150, "status": "Under-billing"}}}
        ],
        "agent": "pm_skill_agent",
        "handler": "billing_deviation.tools_breakdown.generate_billing_summary_text"
    },
    "generate_detailed_billing_report": {
        "pagination": False,
        "required_args": ["deviation_result"],
        "optional_args": {
            "work_item_details": "dict",
            "format": "str"
        },
        "arg_descriptions": {
            "deviation_result": "Result dictionary from calculate_billing_deviation",
            "work_item_details": "Optional work item fetch results",
            "format": "Report format: 'html' or 'csv' (default: html)"
        },
        "description": "Generate detailed HTML or CSV billing report with full analysis, area breakdown, and metrics. Saves to outputs/ folder and returns file path.",
        "use_cases": [
            "detailed billing report", "HTML report", "CSV export", "billing analysis",
            "generate report file"
        ],
        "examples": [
            {"description": "HTML report", "args": {"deviation_result": {}, "format": "html"}},
            {"description": "CSV export", "args": {"deviation_result": {}, "format": "csv"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "billing_deviation.tools_breakdown.generate_detailed_billing_report"
    },
    "send_billing_report_email": {
        "pagination": False,
        "required_args": ["recipient_email", "text_summary"],
        "optional_args": {
            "html_report": "str",
            "csv_path": "str",
            "subject": "str"
        },
        "arg_descriptions": {
            "recipient_email": "Email address to send to (validated against config.yaml allowed recipients)",
            "text_summary": "Plain text summary from generate_billing_summary_text",
            "html_report": "Optional HTML report content",
            "csv_path": "Optional CSV file path to attach",
            "subject": "Email subject line (default: 'Billing Deviation Report')"
        },
        "description": "Send billing deviation report via email with HTML and CSV attachments. Validates recipient against config.yaml whitelist. Returns success status and validation result.",
        "use_cases": [
            "send billing report", "email billing deviation", "send report to stakeholder",
            "email validation", "send with attachments"
        ],
        "examples": [
            {"description": "Send text summary", "args": {"recipient_email": "manager@example.com", "text_summary": "..."}},
            {"description": "Send with HTML", "args": {"recipient_email": "manager@example.com", "text_summary": "...", "html_report": "...", "csv_path": "/path/to/report.csv"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "billing_deviation.tools_breakdown.send_billing_report_email"
    },
    
    # ============================================================================
    # OVERLOOKED USER STORIES TOOLS (Tool Breakdown Architecture)
    # ============================================================================
    "parse_overlooked_query": {
        "pagination": False,
        "required_args": [],
        "optional_args": {
            "query": "str"
        },
        "arg_descriptions": {
            "query": "User's natural language query to parse for project, stale days, area paths, and email intent"
        },
        "description": "Extract parameters from user's overlooked stories query: project, thresholds, area paths, email settings. First step in overlooked stories workflow.",
        "use_cases": [
            "parse overlooked query", "extract area paths", "identify stale threshold",
            "understand overlooked request"
        ],
        "examples": [
            {"description": "Parse query with area", "args": {"query": "show overlooked stories for xops"}},
            {"description": "Parse with email", "args": {"query": "overlooked stories send to user@example.com"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "overlooked_user_stories.tool_breakdown_overlooked.parse_overlooked_query"
    },
    "build_overlooked_wiql": {
        "pagination": False,
        "required_args": ["project"],
        "optional_args": {
            "new_threshold_days": "int",
            "active_threshold_days": "int",
            "area_paths": "list[str]"
        },
        "arg_descriptions": {
            "project": "Azure DevOps project name",
            "new_threshold_days": "Days threshold for New items (default 90)",
            "active_threshold_days": "Days threshold for Active items (default 60)",
            "area_paths": "Optional list of area paths to filter"
        },
        "description": "Build WIQL query to find overlooked user stories based on state and staleness thresholds. Returns WIQL string and boundary dates.",
        "use_cases": [
            "build overlooked query", "construct WIQL", "find stale stories",
            "query old user stories"
        ],
        "examples": [
            {"description": "Default thresholds", "args": {"project": "FracPro-OPS"}},
            {"description": "Custom thresholds", "args": {"project": "FracPro-OPS", "new_threshold_days": 60, "active_threshold_days": 30}}
        ],
        "agent": "pm_skill_agent",
        "handler": "overlooked_user_stories.tool_breakdown_overlooked.build_overlooked_wiql"
    },
    "fetch_overlooked_work_items": {
        "pagination": False,
        "required_args": ["wiql", "project", "org_url", "pat"],
        "optional_args": {},
        "arg_descriptions": {
            "wiql": "WIQL query string to execute",
            "project": "Azure DevOps project name",
            "org_url": "ADO organization URL",
            "pat": "Personal Access Token"
        },
        "description": "Execute WIQL query and fetch full work item details from Azure DevOps. Returns list of work items with all fields and relations.",
        "use_cases": [
            "fetch work items", "execute WIQL", "get user stories",
            "retrieve ADO items"
        ],
        "examples": [
            {"description": "Fetch items", "args": {"wiql": "SELECT [System.Id] FROM WorkItems...", "project": "FracPro-OPS", "org_url": "https://dev.azure.com/org", "pat": "***"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "overlooked_user_stories.tool_breakdown_overlooked.fetch_overlooked_work_items"
    },
    "filter_by_iteration": {
        "pagination": False,
        "required_args": ["work_items", "project", "active_threshold_days"],
        "optional_args": {},
        "arg_descriptions": {
            "work_items": "List of work item dictionaries",
            "project": "Project name for iteration path comparison",
            "active_threshold_days": "Threshold for active staleness"
        },
        "description": "Filter out items in active iterations unless they're stale Active items. Returns filtered list and formatted rows for reporting.",
        "use_cases": [
            "filter iterations", "exclude active sprints", "backlog filtering",
            "iteration filtering"
        ],
        "examples": [
            {"description": "Filter items", "args": {"work_items": [], "project": "FracPro-OPS", "active_threshold_days": 60}}
        ],
        "agent": "pm_skill_agent",
        "handler": "overlooked_user_stories.tool_breakdown_overlooked.filter_by_iteration"
    },
    "build_hierarchy": {
        "pagination": False,
        "required_args": ["rows", "work_items", "org_url", "pat"],
        "optional_args": {},
        "arg_descriptions": {
            "rows": "Report row dictionaries",
            "work_items": "Full work item objects",
            "org_url": "ADO organization URL",
            "pat": "Personal Access Token"
        },
        "description": "Build Epic → Feature → Story hierarchy structure by fetching parent work items and relations. Essential for hierarchical reporting.",
        "use_cases": [
            "build hierarchy", "epic feature mapping", "parent child relations",
            "hierarchical structure"
        ],
        "examples": [
            {"description": "Build hierarchy", "args": {"rows": [], "work_items": [], "org_url": "https://dev.azure.com/org", "pat": "***"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "overlooked_user_stories.tool_breakdown_overlooked.build_hierarchy"
    },
    "generate_summary": {
        "pagination": False,
        "required_args": ["hierarchy", "project"],
        "optional_args": {},
        "arg_descriptions": {
            "hierarchy": "Nested dict {epic: {feature: [rows]}}",
            "project": "Project name"
        },
        "description": "Generate categorized summary with text/HTML/UI formats. Returns statistics, epic counts, feature counts, and formatted summaries.",
        "use_cases": [
            "generate summary", "format report", "create overview",
            "summarize overlooked items"
        ],
        "examples": [
            {"description": "Generate summary", "args": {"hierarchy": {}, "project": "FracPro-OPS"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "overlooked_user_stories.tool_breakdown_overlooked.generate_summary"
    },
    "generate_report_files": {
        "pagination": False,
        "required_args": ["enriched_rows", "hierarchy", "summary_text", "summary_html", "project"],
        "optional_args": {},
        "arg_descriptions": {
            "enriched_rows": "Rows with hierarchy information",
            "hierarchy": "Epic → Feature → Story structure",
            "summary_text": "Plain text summary",
            "summary_html": "HTML summary",
            "project": "Project name"
        },
        "description": "Generate CSV and HTML report files with hierarchical structure. Saves files to outputs/ directory with timestamp.",
        "use_cases": [
            "generate reports", "create CSV", "create HTML report",
            "save report files"
        ],
        "examples": [
            {"description": "Generate reports", "args": {"enriched_rows": [], "hierarchy": {}, "summary_text": "...", "summary_html": "...", "project": "FracPro-OPS"}}
        ],
        "agent": "pm_skill_agent",
        "handler": "overlooked_user_stories.tool_breakdown_overlooked.generate_report_files"
    },
    "send_overlooked_email": {
        "pagination": False,
        "required_args": ["recipient_emails", "text_summary"],
        "optional_args": {
            "html_report_path": "str",
            "csv_path": "str",
            "summary_path": "str",
            "project": "str",
            "total_stories": "int",
            "epic_count": "int",
            "feature_count": "int"
        },
        "arg_descriptions": {
            "recipient_emails": "List of email addresses (validated against config)",
            "text_summary": "Plain text summary",
            "html_report_path": "Path to HTML report file (optional)",
            "csv_path": "Path to CSV file (optional)",
            "summary_path": "Path to summary text file (optional)",
            "project": "Project name for subject",
            "total_stories": "Total story count for subject",
            "epic_count": "Epic count for subject",
            "feature_count": "Feature count for subject"
        },
        "description": "Send overlooked stories email with attachments. Validates recipients against config.yaml allowlist for security. Returns success status and validation results.",
        "use_cases": [
            "send email", "email report", "notify recipients",
            "send overlooked report"
        ],
        "examples": [
            {"description": "Send email", "args": {"recipient_emails": ["user@example.com"], "text_summary": "...", "project": "FracPro-OPS", "total_stories": 42}}
        ],
        "agent": "pm_skill_agent",
        "handler": "overlooked_user_stories.tool_breakdown_overlooked.send_overlooked_email"
    },
    "search_developers_by_skill": {
        "name": "search_developers_by_skill",
        "description": """Advanced semantic developer expertise discovery system powered by vector database technology (Milvus) that performs intelligent skill-based search to find team members with specific programming languages, frameworks, and technology stack proficiencies. This tool uses natural language processing and machine learning embeddings to match complex skill queries against a comprehensive knowledge base of developer capabilities derived from actual code contributions, work history, and project involvement.
        
        CORE FUNCTIONALITY:
        - Semantic vector search using state-of-the-art NLP embeddings for intelligent skill matching beyond keyword search
        - Natural language query support enabling queries like "who knows Angular and TypeScript for frontend development"
        - Technology-specific filtering for precise matching on individual technologies (e.g., 'angular', 'java', 'python', 'react', 'spring boot')
        - Ranked results by similarity score showing best matches first based on semantic relevance
        - Evidence-based skill validation with concrete proof from commit history, code contributions, and work items
        - Support for frontend, backend, fullstack, and specialized role discovery
        - Multi-technology query support for complex skill combinations (e.g., "React Redux TypeScript testing")
        - Synonym and related technology understanding (e.g., recognizes JavaScript/TypeScript/ES6 relationships)
        
        DATA SOURCES & EVIDENCE:
        - Milvus vector database containing semantic embeddings of all developer skill profiles
        - Git commit history analysis with programming language statistics and file change patterns
        - Work item associations showing project domains and feature areas developers have worked on
        - Code contribution metrics: total commits, lines of code written, files modified, repositories touched
        - Language usage distribution showing which languages each developer actively uses and their proficiency
        - Framework and library detection from import statements and configuration files
        - Time-series data showing skill evolution and recent vs historical expertise
        
        SEMANTIC MATCHING ALGORITHM:
        - Converts natural language skill queries into high-dimensional vector embeddings using transformer models
        - Performs approximate nearest neighbor (ANN) search in vector space for fast, accurate matching
        - Calculates cosine similarity scores between query vector and developer skill vectors
        - Returns developers ranked by similarity score with configurable top-k results
        - Handles complex queries with multiple technologies by combining semantic representations
        - Understands context and relationships between technologies (e.g., React developers likely know JavaScript)
        
        SKILL EVIDENCE PROVIDED:
        - Commit count: Total number of commits made by developer in relevant technologies
        - Lines of code (LOC): Amount of code written across all repositories and projects
        - Languages used: List of programming languages with usage percentages (e.g., "Java 60%, Python 25%, JavaScript 15%")
        - Frameworks detected: Specific frameworks and libraries used (Spring Boot, React, Django, Angular, etc.)
        - Work items: Associated user stories, tasks, and bugs showing domain expertise
        - Recent activity: Timestamp of last contribution in relevant technology
        - Project associations: Which projects, repositories, or components developer has contributed to
        
        USE CASES:
        - Finding experts for task assignment: "who knows Angular", "who is best at Spring Boot"
        - Team composition analysis: "list all frontend developers", "show me backend specialists"
        - Technology discovery: "find Java developers", "who can work on React", "python automation experts"
        - Skill gap identification: "do we have anyone with Kubernetes experience"
        - Cross-functional team building: "find fullstack developers", "who knows both frontend and backend"
        - Knowledge transfer planning: "who can mentor on Django", "angular subject matter experts"
        - Resource planning: "available React developers", "Java developers for new project"
        - Technology questions: "who knows typescript", "who knows javascript", "who is expert in angular"
        
        QUERY EXAMPLES:
        - Simple technology: "who knows Angular", "who knows Java", "find Python developers"
        - Role-based: "frontend developers", "backend developers", "fullstack developers"
        - Multi-technology: "Angular TypeScript developers", "Java Spring Boot backend", "React Redux frontend"
        - Specific combinations: "Python data analysis", "JavaScript testing automation", "Java microservices"
        - Natural language: "who can work on our Angular frontend", "developers with Spring Boot API experience"
        
        PARAMETERS:
        - skill_query (optional): Natural language description of required skills, can be complex multi-word phrases like "Angular TypeScript frontend development" or "Java Spring Boot microservices REST API". Leave empty for technology-only filtering.
        - technology (optional): Specific technology to filter on (e.g., 'angular', 'java', 'react', 'python', 'spring', 'django'). Case-insensitive single-word filter.
        - top_k (optional): Maximum number of developers to return, defaults to all matching developers ranked by relevance
        - include_evidence (optional): Boolean flag to include detailed evidence (commits, LOC, languages, work items), default True
        
        OUTPUT FORMAT:
        Returns a ranked list of developer objects, each containing:
        - Developer name and contact information
        - Similarity score (0.0 to 1.0) indicating match quality, higher is better
        - Evidence package when include_evidence=True:
          * Total commit count in relevant technologies
          * Programming languages used with percentages
          * Lines of code contributed
          * Associated work items and project areas
          * Recent activity timestamps
          * Specific frameworks and tools detected
        - Sorted by similarity score descending (best matches first)
        
        ADVANCED FEATURES:
        - Handles typos and variations in technology names through semantic understanding
        - Recognizes technology abbreviations and expansions (e.g., "JS" = "JavaScript", "TS" = "TypeScript")
        - Understands technology relationships and ecosystems (React ecosystem includes Redux, React Router, etc.)
        - Supports filtering by seniority level when combined with evidence (commit counts correlate with experience)
        - Can combine with other tools for comprehensive team analysis and skill matrix generation""",
        "required_args": [],
        "optional_args": {
            "skill_query": "str - Natural language description of required skills (e.g., 'Angular TypeScript', 'Java Spring Boot', 'Python data analysis')",
            "technology": "str - Specific technology to search for (e.g., 'angular', 'java', 'react')",
            "top_k": "int - Number of top matches to return (default: all matching developers)",
            "include_evidence": "bool - Include commit counts, languages, and work evidence (default: True)"
        },
        "returns": "List of developers matching the skill query with similarity scores, commit counts, languages used, and evidence of expertise",
        "use_cases": [
            "who knows angular",
            "who knows java",
            "who knows react",
            "who knows python",
            "who knows typescript",
            "find angular developers",
            "find java developers",
            "find react developers",
            "find frontend developers",
            "find backend developers",
            "list all angular developers",
            "list all java developers",
            "list all frontend developers",
            "list all backend developers",
            "developers with angular experience",
            "developers with java experience",
            "team members with angular skills",
            "who can work on angular",
            "who can work on frontend",
            "who is expert in angular",
            "angular expertise",
            "java expertise",
            "frontend expertise",
            "backend expertise",
            "show me developers who know",
            "developer skills search",
            "search developer knowledge base"
        ],
        "examples": [
            {"query": "Who knows Angular?", "params": {"technology": "angular"}},
            {"query": "Find all Java developers", "params": {"skill_query": "Java Spring Boot backend"}},
            {"query": "List all frontend developers", "params": {"skill_query": "React Angular TypeScript frontend"}},
            {"query": "Who can work on Python automation?", "params": {"skill_query": "Python automation testing"}},
            {"query": "Show me backend developers", "params": {"skill_query": "Java Spring Boot API backend microservices"}}
        ],
        "category": "discovery"
    },
    
    "bug_areas_highlight": {
        "name": "bug_areas_highlight",
        "description": """Intelligent bug pattern detection and code quality analysis system that identifies recurring defects, clusters bugs by similarity, and highlights problematic area paths (code modules/components) requiring attention. This tool uses natural language processing and statistical analysis to find systemic quality issues and help teams prioritize refactoring and bug prevention efforts.
        
        CORE FUNCTIONALITY:
        - Automated detection of recurring bugs using advanced text similarity algorithms on bug titles and descriptions
        - Area path clustering to identify which code modules, components, or features have the highest bug concentrations
        - Temporal pattern analysis to detect bugs that keep occurring over time periods
        - Statistical aggregation with configurable recurrence thresholds to focus on genuinely repetitive issues
        - HTML report generation with visual charts, tables, and highlighted problem areas
        - Automated email delivery to stakeholders with actionable insights and recommendations
        - Support for customizable analysis windows and similarity matching precision
        
        DATA SOURCES:
        - Azure DevOps bug work items retrieved via WIQL queries and search APIs
        - Bug titles and descriptions for text similarity analysis
        - Area path hierarchies for code module classification
        - Bug creation timestamps and activity dates for temporal analysis
        - Bug states (Active, Resolved, Closed) and resolution information
        - Related work items and dependency relationships
        
        ANALYTICAL ALGORITHMS:
        - TF-IDF (Term Frequency-Inverse Document Frequency) vectorization of bug text
        - Cosine similarity calculation to measure textual similarity between bug pairs
        - Configurable similarity threshold (default 0.75) for clustering related bugs
        - Area path grouping and bug count aggregation
        - Recurrence threshold filtering (default 3+ bugs) to identify patterns
        - Statistical ranking of problem areas by bug density and similarity clusters
        
        BUG PATTERN DETECTION:
        - Identifies bugs with highly similar titles indicating the same underlying issue
        - Groups bugs by area path to show which components are most defect-prone
        - Detects recurring patterns across different time periods
        - Highlights bugs that are semantically related even if worded differently
        - Provides similarity scores showing how closely bugs are related (0.0 to 1.0)
        - Filters out one-time bugs to focus on systematic issues
        
        USE CASES:
        - Quality retrospectives: "show recurring bugs", "find recurring bugs", "bug areas highlight"
        - Root cause analysis: "analyze bug patterns", "where do bugs keep happening"
        - Refactoring prioritization: "problematic code areas", "bug hotspots", "which areas need attention"
        - Proactive quality management: "detect recurring bugs", "find bug patterns", "prevent bug recurrence"
        - Component analysis: "bugs by area path", "area wise bugs", "module with most defects"
        - Pattern recognition: "repeated bugs", "similar bugs", "same bugs happening again"
        - Management reporting: "bug analysis report", "recurring bug report", "quality trends"
        
        CONFIGURABLE PARAMETERS:
        - lookback_days: Historical time window for bug analysis (default: 60 days), controls how far back to search
        - recurrence_threshold: Minimum number of similar bugs to flag as recurring (default: 3), higher values show only most repeated issues
        - similarity_threshold: Text similarity cutoff for bug clustering (default: 0.75, range 0.0-1.0), higher values require more exact matches
        - recipients: Email distribution list for automated report delivery
        - send_email: Boolean to enable/disable email notifications (default: True)
        - preview_only: Preview report without sending emails (default: False), useful for testing
        - project: Azure DevOps project name (default: from configuration)
        
        OUTPUT FORMAT:
        Generates comprehensive HTML report containing:
        - Executive summary with total bugs analyzed, recurring patterns found, and top problem areas
        - Area path ranking table showing bug counts and recurrence percentages by module/component
        - Bug cluster groups with similar bugs listed together and similarity scores
        - Visual charts showing bug distribution across area paths and time periods
        - Detailed bug lists with IDs, titles, states, and creation dates
        - Actionable recommendations for addressing recurring issues
        - Automatically emails formatted report to configured recipient list
        
        ADVANCED FEATURES:
        - Handles large bug datasets efficiently through optimized similarity calculations
        - Supports filtering by bug state to focus on active vs resolved issues
        - Provides drill-down capability to specific area paths
        - Tracks bug trends over time to show improving or declining quality
        - Generates exportable data for further analysis in BI tools
        - Integrates with other PM Agent features for comprehensive quality management""",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name (default: from config)",
            "lookback_days": "int - Days to look back for bugs (default: 60)",
            "recurrence_threshold": "int - Minimum bugs to consider recurring (default: 3)",
            "similarity_threshold": "float - Title similarity threshold 0-1 (default: 0.75)",
            "recipients": "list[str] - Email recipients",
            "send_email": "bool - Whether to send email (default: True)",
            "preview_only": "bool - Preview without emailing (default: False)"
        },
        "returns": "HTML report with recurring bug areas, email sent to recipients",
        "use_cases": [
            "recurring bugs",
            "bug analysis", 
            "bug patterns",
            "bug areas",
            "highlight bugs",
            "bug report",
            "which areas have most bugs",
            "where do bugs keep happening",
            "problematic code areas",
            "bug hotspots"
        ],
        "examples": [
            {"query": "Show me recurring bugs", "params": {}},
            {"query": "Find bug patterns in the last 30 days", "params": {"lookback_days": 30}},
            {"query": "What areas have the most bugs?", "params": {"send_email": False}}
        ],
        "category": "analysis"
    },
    
    "feedback_to_dev": {
        "name": "feedback_to_dev",
        "description": """Automated developer learning and root cause analysis (RCA) system that detects newly created bugs, uses semantic vector search to find similar historical defects, extracts RCA insights from past resolutions, and delivers personalized feedback notifications to help developers prevent recurring issues and learn from the team's collective experience.
        
        CORE FUNCTIONALITY:
        - Automated detection of new and recently modified bugs within configurable time windows (default 24 hours)
        - Advanced semantic similarity search using vector embeddings (Milvus database) to find related historical bugs
        - Intelligent RCA content extraction from historical bug resolutions, comments, and fix descriptions
        - Personalized feedback report generation for developers assigned to new bugs
        - Automated email delivery with actionable insights and preventive recommendations
        - Support for test mode to preview feedback before production deployment
        - Configurable similarity matching precision for high-quality historical bug matches
        
        DATA SOURCES:
        - Azure DevOps bug work items filtered by creation/modification timestamps
        - Vector database (Milvus) containing semantic embeddings of all historical bug descriptions
        - Bug resolution notes, RCA documentation, and fix descriptions from closed bugs
        - Developer assignment records to target feedback to appropriate team members
        - Bug state history showing creation dates, resolution dates, and modification timelines
        - Related commits and pull requests showing how historical bugs were fixed
        
        SEMANTIC SEARCH & MATCHING:
        - Converts bug descriptions into high-dimensional vector embeddings using transformer-based NLP models
        - Performs approximate nearest neighbor (ANN) search in Milvus vector database for fast similarity matching
        - Calculates cosine similarity scores between new bugs and historical bug embeddings
        - Applies configurable similarity threshold (default 0.82) for high-precision matching, ensuring only truly similar bugs are returned
        - Returns top-k most similar historical bugs ranked by relevance score
        - Understands semantic relationships beyond keyword matching (e.g., "null pointer" similar to "undefined reference")
        
        RCA EXTRACTION:
        - Parses historical bug resolution descriptions for root cause analysis content
        - Identifies fix patterns and common solutions from similar historical bugs
        - Extracts preventive measures and lessons learned from past resolutions
        - Highlights code patterns or practices that led to similar bugs
        - Provides links to related commits showing actual code fixes
        - Aggregates insights from multiple similar historical bugs for comprehensive learning
        
        FEEDBACK GENERATION:
        - Creates personalized feedback reports for each developer assigned to new bugs
        - Includes new bug details: ID, title, description, severity, priority
        - Lists top similar historical bugs with similarity scores and quick links
        - Presents extracted RCA insights and resolution strategies from historical matches
        - Provides preventive recommendations to avoid recurring similar issues
        - Formats feedback for readability with sections, highlights, and actionable items
        
        USE CASES:
        - Developer learning: "feedback to dev", "feedback to developer", "help developer with bug"
        - Bug investigation: "bug feedback", "find similar bugs", "similar historical bugs"
        - RCA assistance: "rca feedback", "root cause analysis", "what caused this bug"
        - Automated notifications: "new bug notification", "developer feedback", "notify developer about bug"
        - Pattern recognition: "bug analysis feedback", "learn from past bugs"
        - Knowledge transfer: "send feedback to developers", "share bug insights", "prevent recurring bugs"
        - Continuous improvement: "help developers learn", "improve code quality", "reduce bug recurrence"
        
        CONFIGURABLE PARAMETERS:
        - lookback_minutes: Time window for detecting new bugs (default: 1440 minutes = 24 hours), determines "recent" bug threshold
        - historical_days: How far back to search historical bugs for similarity matching (default: 30 days)
        - embedding_threshold: Similarity score cutoff for historical bug matching (default: 0.82, range 0.0-1.0), higher values require closer matches
        - recipients: Email addresses for feedback notification distribution (in addition to assigned developers)
        - is_test: Test mode flag (default: False), when True previews feedback without sending emails
        - project: Azure DevOps project name (default: from configuration)
        
        OUTPUT FORMAT:
        Comprehensive feedback report containing:
        - New bug summary: ID, title, description, severity, assigned developer, creation date
        - Similar historical bugs section with:
          * Bug ID, title, and resolution status
          * Similarity score (0.0-1.0) indicating match quality
          * Resolution description and RCA notes
          * Links to related commits and fixes
        - Extracted RCA insights highlighting:
          * Root causes identified in similar historical bugs
          * Effective fix patterns and solutions
          * Code patterns or practices to avoid
        - Preventive recommendations for addressing the new bug
        - Automatically delivered via email to assigned developer(s) and configured recipients
        
        ADVANCED FEATURES:
        - Continuous learning system that improves as more bugs are resolved with RCA content
        - Handles multiple new bugs efficiently through batch processing
        - Supports custom embedding models for domain-specific similarity matching
        - Integrates with CI/CD pipelines for automatic feedback on build-time detected bugs
        - Tracks feedback effectiveness through bug recurrence rate metrics
        - Provides feedback history for developer performance reviews and learning assessments""",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "lookback_minutes": "int - How far back to look for new bugs (default: 1440 = 24h)",
            "historical_days": "int - How far back to look for historical bugs (default: 30)",
            "embedding_threshold": "float - Similarity threshold for embeddings (default: 0.82)",
            "recipients": "list[str] - Email recipients",
            "is_test": "bool - Test run mode (default: False)"
        },
        "returns": "Feedback report with similar bugs and RCA suggestions, sent to developers",
        "use_cases": [
            "feedback to dev",
            "bug feedback",
            "rca feedback",
            "new bug notification",
            "developer feedback",
            "bug analysis feedback",
            "similar bugs",
            "what caused this bug",
            "root cause analysis",
            "help developer with bug"
        ],
        "examples": [
            {"query": "Send feedback to developers about new bugs", "params": {}},
            {"query": "Find similar bugs for RCA", "params": {"send_email": False}}
        ],
        "category": "notification"
    },
    
    "overlooked_stories": {
        "name": "overlooked_stories",
        "description": """Proactive backlog health monitoring and work item staleness detection system that identifies user stories, tasks, and features that have been dormant without recent activity, ensuring no critical work is forgotten and maintaining healthy backlog hygiene for continuous delivery.
        
        CORE FUNCTIONALITY:
        - Automated detection of stale work items based on configurable inactivity thresholds (default 14 days)
        - Analysis of last modification timestamps to calculate days of inactivity
        - Identification of work items stuck in active states (New, Active, Committed) without progress
        - Prioritization of overlooked items by combining staleness with priority/severity ratings
        - Automated reminder generation and email delivery to re-engage teams
        - Support for filtering by area path, iteration, or work item type for targeted monitoring
        - Detailed activity timeline visualization showing when items last changed
        
        DATA SOURCES:
        - Azure DevOps work items of types: User Story, Feature, Task, Bug, Epic
        - Work item revision history and change timestamps via ADO APIs
        - "Changed Date" and "Last Updated" fields for each work item
        - Work item state information (New, Active, Committed, Resolved, Closed, Removed)
        - Assignment records showing current and historical owners
        - Priority (1-4) and Severity (1-Critical, 2-High, 3-Medium, 4-Low) classifications
        - Area path associations for team/component filtering
        - Iteration path assignments showing sprint associations
        
        STALENESS DETECTION CRITERIA:
        - Inactivity period: Days since last modification exceeds configurable staleness threshold
        - State analysis: Work items in non-terminal states (not Closed or Removed) without recent updates
        - Priority escalation: Higher priority given to critical or high-priority stale items
        - Assignment consideration: Unassigned items or items assigned to inactive/departed team members
        - Sprint association: Items committed to past iterations that remain unresolved
        - Activity patterns: Items with single creation event but no subsequent updates
        
        RISK FACTORS IDENTIFIED:
        - Critical priority items dormant for extended periods
        - Committed work items from past sprints still in active state
        - Unassigned high-priority stories with no ownership
        - Features with no child tasks or progress for long durations
        - Stories in "New" state for weeks/months indicating lack of refinement
        
        USE CASES:
        - Backlog grooming: "overlooked stories", "find overlooked stories", "stale stories"
        - Health checks: "forgotten items", "overlooked user stories", "what stories are stale"
        - Proactive management: "find forgotten tasks", "dormant items", "untouched stories"
        - Activity monitoring: "work items with no activity", "stories not updated", "neglected stories"
        - Team accountability: "story reminder", "inactive stories", "stuck work items"
        - Quality assurance: "are there any forgotten work items", "check for stale backlog"
        - Sprint planning: "old stories still open", "what needs attention before planning"
        
        CONFIGURABLE PARAMETERS:
        - stale_days: Days of inactivity threshold to flag items as overlooked (default: 14 days), lower values catch items sooner
        - recipients: Email distribution list for automated reminder notifications
        - send_email: Boolean flag to enable/disable email reminders (default: True)
        - project: Target Azure DevOps project scope (default: from configuration)
        
        OUTPUT FORMAT:
        Comprehensive report listing overlooked work items with:
        - Work item details: ID, title, type (Story/Task/Bug/Feature), state, area path
        - Assignment information: Currently assigned to, or "Unassigned"
        - Priority and severity ratings highlighting critical items
        - Last activity date and calculated days inactive
        - Activity summary showing what last changed (description, state, assignment, etc.)
        - Iteration association if applicable
        - Recommended actions for each item (reassign, close, update, refine, etc.)
        - Summary statistics: total overlooked items, breakdown by type and priority
        - Direct links to work items in Azure DevOps for quick access
        - Automatically sends email reminders to configured recipients with formatted HTML tables
        
        ADVANCED FEATURES:
        - Trend analysis showing if backlog health is improving or declining over time
        - Customizable staleness thresholds per work item type (stories vs tasks vs bugs)
        - Integration with team capacity planning to identify overlooked items for assignment
        - Escalation rules for progressively urgent reminders as staleness increases
        - Dashboard widgets showing real-time stale item counts and aging distribution
        - Automated state transitions for extremely stale items (e.g., auto-close after 90 days)""",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "stale_days": "int - Days of inactivity to consider stale (default: 14)",
            "recipients": "list[str] - Email recipients",
            "send_email": "bool - Whether to send email"
        },
        "returns": "List of overlooked stories with last activity date, email reminder sent",
        "use_cases": [
            "overlooked stories",
            "stale stories",
            "forgotten items",
            "overlooked user stories",
            "story reminder",
            "inactive stories",
            "stuck work items",
            "what stories are stale",
            "find forgotten tasks",
            "work items with no activity"
        ],
        "examples": [
            {"query": "Find overlooked user stories", "params": {}},
            {"query": "What stories haven't been touched in 30 days?", "params": {"stale_days": 30}}
        ],
        "category": "analysis"
    },
    
    "iteration_report": {
        "name": "iteration_report",
        "description": """Comprehensive sprint analytics and iteration health reporting system that generates detailed performance metrics, completion statistics, burndown analysis, and team progress insights for agile development cycles. This tool provides stakeholders with data-driven visibility into sprint execution quality and delivery health across all work item types.
        
        CORE FUNCTIONALITY:
        - Generate complete iteration reports with work item status breakdowns across all states (New, Active, Resolved, Closed, Removed)
        - Calculate sprint completion metrics including total items, completed items, in-progress items, not-started items, and removed scope
        - Provide detailed completion percentage analytics comparing planned vs actual delivery
        - Analyze burndown and burnup trends to assess sprint trajectory and forecast completion
        - Track work item distribution by type (User Story, Bug, Task, Feature, Epic) with individual completion rates
        - Identify at-risk items, blocked work, and impediments requiring immediate attention
        - Monitor team velocity, throughput, and capacity utilization for the iteration
        - Compare current sprint performance against historical baselines and averages
        - Generate visual charts, graphs, and tables for executive dashboards and stakeholder communications
        - Support multi-team filtering via area paths for consolidated or component-specific reports
        - Enable work item type filtering for focused analysis (bugs only, stories only, features only, etc.)
        - Automatically send formatted HTML reports via email to configured stakeholders
        
        DATA SOURCES:
        - Azure DevOps work items filtered by specified iteration path (default: @CurrentIteration)
        - Work item states and complete state transition history (New → Active → Resolved → Closed)
        - Work item type classifications (User Story, Bug, Task, Feature, Epic, Test Case)
        - Area path hierarchies enabling team/component-level filtering and aggregation
        - Iteration configuration including start date, end date, and sprint timeline
        - Effort estimates (Story Points, Original Estimate) and remaining work calculations
        - Completion timestamps showing when items transitioned to Closed state
        - Velocity data from completed work and historical sprint metrics
        
        ANALYTICAL METRICS PROVIDED:
        - Total planned work items at sprint start (baseline scope)
        - Completed work items count and completion percentage
        - In-progress work items with state details (Active, Resolved)
        - Not started items indicating potential scope risk or over-commitment
        - Removed/cut scope showing items pulled from sprint
        - Work distribution charts by type showing Story/Bug/Task/Feature breakdown
        - Work distribution by priority (1-Critical, 2-High, 3-Medium, 4-Low)
        - Burndown rate calculations and projected completion date if not on track
        - Scope changes during iteration (items added or removed mid-sprint)
        - Team velocity in story points or work item count
        - Throughput metrics (items completed per day)
        - Blocked item count and duration
        
        USE CASES:
        - Sprint reviews and retrospectives: "iteration report", "sprint report", "generate sprint report"
        - Daily status updates: "sprint status", "iteration status", "how is the sprint going"
        - Progress tracking: "sprint progress", "sprint summary", "show me sprint metrics"
        - Health checks and red flag identification: "current sprint status", "sprint health", "are we on track"
        - Stakeholder reporting: "iteration summary", "sprint overview", "send sprint report to management"
        - Performance analysis: "sprint completion rate", "how many items finished", "burndown status"
        - Team retrospectives: "what did we accomplish this sprint", "sprint achievements", "sprint velocity"
        
        CONFIGURABLE PARAMETERS:
        - iteration: Iteration path to report on (default: @CurrentIteration for active sprint), supports historical iterations
        - areas: List of area paths to filter by specific teams or components for multi-team environments
        - wi_types: Work item types to include (User Story, Bug, Task, Feature), defaults to all types
        - recipients: Email distribution list for automated report delivery via SMTP
        - send_email: Boolean flag to enable automatic email delivery of generated reports (default: False for on-demand)
        - project: Target Azure DevOps project (default: from configuration)
        
        OUTPUT FORMAT:
        Comprehensive sprint report document containing:
        - Executive summary with key metrics and sprint health indicator (Green/Yellow/Red)
        - Work item counts by state with completion percentages and visual progress bars
        - Work item distribution by type showing Story/Bug/Task/Feature breakdown with individual completion rates
        - Burndown chart data showing planned vs actual progress over sprint timeline
        - At-risk items table highlighting blocked, delayed, or high-priority incomplete work
        - Scope change analysis showing items added or removed during sprint
        - Velocity and throughput calculations comparing to team historical averages
        - Detailed work item lists with IDs, titles, states, assignments, and priorities
        - Actionable insights and recommendations for sprint closure or continuation
        - Visual charts and graphs embedded in HTML format for easy stakeholder consumption
        - Reports can be viewed in-app or automatically emailed with professional formatting""",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "iteration": "str - Iteration path (default: @CurrentIteration)",
            "areas": "list[str] - Area paths to filter",
            "wi_types": "list[str] - Work item types to include",
            "recipients": "list[str] - Email recipients",
            "send_email": "bool - Send email after generation"
        },
        "returns": "Sprint report with work item counts, completion %, burndown status",
        "use_cases": [
            "iteration report",
            "sprint report",
            "sprint status",
            "iteration status",
            "sprint summary",
            "how is the sprint going",
            "sprint progress",
            "generate sprint report",
            "show me sprint metrics"
        ],
        "examples": [
            {"query": "Generate sprint report", "params": {}},
            {"query": "Show me the current iteration status", "params": {"send_email": False}}
        ],
        "category": "reporting"
    },
    
    "get_sprint_status": {
        "name": "get_sprint_status",
        "description": """Real-time sprint health monitoring and tracking status system that provides current sprint state with work item counts, completion percentages, planned vs completed comparisons, and intelligent tracking status classification (on_track/at_risk/off_track) for proactive sprint management.
        
        CORE FUNCTIONALITY:
        - Retrieve current sprint status with up-to-the-minute work item counts and metrics
        - Calculate completion percentages showing progress toward sprint goals
        - Compare planned vs completed work to identify delivery gaps
        - Automatically classify sprint tracking status (on_track, at_risk, off_track) based on intelligent heuristics
        - Identify at-risk work items that may not complete by sprint end
        - Provide actionable insights for course correction during sprint execution
        - Support historical sprint analysis by specifying past iteration paths
        
        DATA SOURCES:
        - Azure DevOps work items filtered by iteration (default: @CurrentIteration)
        - Work item states for completion status determination
        - Sprint timeline (start date, end date, current date) for progress calculation
        - Remaining work estimates and effort tracking
        - Team velocity and historical completion rates for forecasting
        
        TRACKING STATUS CLASSIFICATION:
        - on_track: >80% completion or forecasted to finish on time, all critical items progressing
        - at_risk: 50-80% completion or some delays detected, requires monitoring and potential intervention
        - off_track: <50% completion or significant blockers, requires immediate action and possible scope adjustment
        
        METRICS PROVIDED:
        - Total work items in sprint (planned scope)
        - Completed work items count and percentage
        - In-progress work items count
        - Not started work items count
        - Blocked items count and duration
        - Completion rate trend (improving/declining)
        - Days remaining in sprint
        - Projected completion percentage based on current velocity
        
        USE CASES:
        - Daily standups: "sprint status", "what is the sprint status", "how is the sprint"
        - Quick health checks: "are we on track", "sprint health", "current sprint status"
        - Progress monitoring: "sprint progress", "iteration status", "sprint tracking"
        - Delivery forecasting: "planned vs completed", "will we finish on time", "sprint forecast"
        - Risk identification: "off track tasks", "at risk items", "what's blocking the sprint"
        - Management updates: "sprint summary", "give me sprint overview"
        
        OPTIONAL PARAMETERS:
        - iteration: Iteration path (default: @CurrentIteration), can specify historical sprints
        - project: Azure DevOps project name (default: from configuration)
        
        OUTPUT FORMAT:
        Sprint status object containing:
        - Sprint name and date range (start - end)
        - Days elapsed and days remaining
        - Work item counts: total, completed, in-progress, not started, blocked
        - Completion percentage with visual indicator
        - Planned vs completed comparison metrics
        - Tracking status: on_track / at_risk / off_track with justification
        - At-risk items list if applicable
        - Recommended actions for sprint success""",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "iteration": "str - Iteration path (default: @CurrentIteration)"
        },
        "returns": "Sprint status object with counts, percentages, and tracking status",
        "use_cases": [
            "sprint status",
            "iteration status",
            "sprint progress",
            "how is the sprint",
            "sprint tracking",
            "planned vs completed",
            "off track tasks",
            "sprint summary",
            "are we on track",
            "sprint health",
            "current sprint status"
        ],
        "examples": [
            {"query": "What is the sprint status?", "params": {}},
            {"query": "Are we on track for this sprint?", "params": {}},
            {"query": "Show me planned vs completed", "params": {}}
        ],
        "category": "status"
    },
    
    "backlog_triaging": {
        "name": "backlog_triaging",
        "description": """Comprehensive backlog health analysis and triaging system that evaluates product backlog quality, generates detailed health reports, provides AI-powered effort estimates for unestimated items, and identifies backlogs running thin to ensure continuous delivery velocity and sprint planning readiness.
        
        CORE FUNCTIONALITY:
        - Analyze backlog health by team/area path with automatic data fetching from Azure DevOps
        - Generate executive summary reports with key metrics, velocity analysis, and recommendations
        - Calculate backlog runway in sprints based on team velocity and available work
        - Identify thin backlogs (< 2 sprints of work) requiring immediate attention
        - List all backlog User Stories with Effort 3P values and current states
        - AI-powered effort estimation for items missing Effort 3P using GPT-4o-mini
        - Smart estimation based on work item title, description, and historical patterns
        - Provide actionable recommendations for backlog improvement and grooming
        
        DATA SOURCES:
        - Azure DevOps backlog User Stories in New, Approved, or Proposed states
        - Custom.Effort3P field for work sizing (3-point estimation)
        - Team area paths for filtering (e.g., "Global Management\\WTT Development\\XOPS 25")
        - Historical velocity data from recent sprints (default: last 3 sprints)
        - Work item titles, descriptions, and acceptance criteria for AI estimation
        
        AI EFFORT ESTIMATION:
        When Effort 3P is missing, AI estimates using this sizing guide:
        - Very Small Work: 1-10 points (simple bug fixes, minor UI tweaks, config changes)
        - Small Work: 20-40 points (straightforward features, basic CRUD, small enhancements)
        - Medium Work: 40-60 points (moderate features, integration work, API development)
        - Large Work: 60-100 points (complex features, significant refactoring, multi-component work)
        - Very Large Work: 100+ points (major features, architectural changes, epic-level work)
        
        HEALTH ASSESSMENT:
        - Status: HEALTHY (>= 3 sprints), WARNING (2-3 sprints), THIN (< 2 sprints)
        - Backlog Runway: Total Effort / Team Velocity = Sprints of work remaining
        - Velocity Trend: Stable, Increasing, or Decreasing based on last 3 sprints
        - Total Items: Count of all backlog User Stories
        - Total Effort: Sum of all Effort 3P values (including AI estimates)
        
        USE CASES:
        - Health Reports: "backlog health of XOPS 25", "show backlog health", "backlog status"
        - Team Analysis: "is backlog healthy for team X", "check backlog for area Y"
        - Planning Prep: "do we have enough backlog", "backlog runway", "refined items"
        - Grooming Triggers: "thin backlog warning", "backlog health report"
        - Effort Estimation: "estimate effort for backlog", "AI estimate missing effort"
        
        CONFIGURABLE PARAMETERS:
        - team: Team name extracted from user query (CRITICAL: preserve exact team name from query)
          * Examples: "XOPS 25", "XOPS Bugs Enhancement", "XOPS Bugs", "Global Management"
          * Multi-word teams: Always extract the FULL team name (e.g., "XOPS Bugs Enhancement" not just "XOPS Bugs")
          * Query patterns: "for [team]", "of [team]", "[team] backlog", "backlog health for [team]"
          * If no team mentioned: leave empty to analyze entire project backlog
        - area_path: Full Azure DevOps area path for filtering backlog items
        - project: Azure DevOps project name (default: FracPro-OPS)
        - effort_field: Custom effort field name (default: Custom.Effort3P)
        - velocity: Team velocity override (default: calculated from last 3 sprints)
        
        OUTPUT FORMAT:
        Rich markdown report containing:
        - Executive Summary: Backlog runway status and critical warnings
        - Key Metrics: Total Items, Total Effort, Team Velocity, Backlog Runway in sprints
        - Velocity Analysis: Average velocity, trend direction, data source
        - Recommendations: Immediate actions, grooming session needs, process improvements
        - Detailed Item List: All User Stories with ID, Title, State, Effort 3P (AI-estimated if missing)
        - Visual indicators: Status badges (HEALTHY/WARNING/THIN), priority icons""",
        "required_args": [],
        "optional_args": {
            "team": "str - Team name (e.g., 'XOPS 25', 'XOPS Bugs Enhancement')",
            "area_path": "str - Azure DevOps area path for filtering",
            "project": "str - ADO project name (default: FracPro-OPS)",
            "effort_field": "str - Custom effort field (default: Custom.Effort3P)",
            "velocity": "float - Team velocity override (default: auto-calculated)"
        },
        "returns": "Comprehensive backlog health report with metrics, AI estimates, and recommendations",
        "use_cases": [
            "backlog health",
            "backlog status",
            "thin backlog",
            "is backlog healthy",
            "refined items",
            "backlog check",
            "do we have enough backlog",
            "backlog items count",
            "grooming status",
            "backlog health of XOPS 25",
            "show backlog health for team"
        ],
        "examples": [
            {"query": "Show backlog health of XOPS 25", "params": {"team": "XOPS 25"}},
            {"query": "Is backlog healthy for XOPS Bugs Enhancement?", "params": {"team": "XOPS Bugs Enhancement"}},
            {"query": "is my backlog healthy for XOPS Bugs Enhancement", "params": {"team": "XOPS Bugs Enhancement"}},
            {"query": "how many sprints of work for Global Management", "params": {"team": "Global Management"}},
            {"query": "Backlog health report", "params": {}},
            {"query": "backlog status of XOPS 25", "params": {"team": "XOPS 25"}}
        ],
        "category": "analysis"
    },
    
    # NOTE: get_capacity_forecast is a PM Skill Agent skill, NOT a PM Agent tool
    # Its definition was removed from here to prevent MCP routing conflicts
    # See: agents/pm_skill_agent/skills.py -> SKILL_DEFINITIONS["get_capacity_forecast"]
    
    "send_email": {
        "name": "send_email",
        "description": "Universal email communication and notification delivery system enabling automated distribution of reports, alerts, status updates, and custom messages to team members and stakeholders. Supports both plain text and richly formatted HTML email bodies with inline images, embedded charts, tables, and professional styling. Provides flexible attachment support for including generated reports (CSV, PDF, HTML), analysis documents, charts, screenshots, logs, and any other file types. Features multi-recipient distribution lists, CC/BCC options for broader notification reach, SMTP integration with configurable authentication, delivery confirmation tracking, and error handling for failed sends. Ideal for automated reporting workflows, stakeholder notifications, team alerts, sprint reviews, management updates, and any scenario requiring reliable electronic communication delivery. Integrates seamlessly with other PM Agent skills to automatically deliver generated reports and analysis outputs.",
        "required_args": ["recipients", "subject"],
        "optional_args": {
            "body": "str - Email body (HTML or text)",
            "attachments": "list[str] - File paths to attach"
        },
        "returns": "Email send confirmation",
        "use_cases": [
            "send email",
            "email report",
            "send notification",
            "email to team",
            "notify via email"
        ],
        "examples": [
            {"query": "Send an email to the team", "params": {"recipients": ["team@company.com"], "subject": "Update"}}
        ],
        "category": "notification"
    },
    
    "list_area_paths": {
        "name": "list_area_paths",
        "description": "Comprehensive project structure discovery and organizational hierarchy exploration tool that retrieves, displays, and analyzes all area path configurations within an Azure DevOps project. Presents complete hierarchical area path structures showing multi-level parent-child relationships, team ownership boundaries, component classifications, and functional area divisions. Enables users to understand complex organizational structures across teams, components, products, and business units. Provides area path metadata including creation timestamps, modification history, team associations, and permission scopes. Facilitates informed decision-making for work item organization, bug reporting, feature planning, capacity management, and reporting by exposing the full taxonomy of project structure. Supports filtering and searching within area path hierarchies, identification of unused or deprecated areas, and discovery of naming conventions and organizational patterns. Essential for navigating large enterprise projects with multiple teams, understanding responsibility boundaries, configuring work item queries, setting up automated workflows, and ensuring proper classification of new work items.",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name"
        },
        "returns": "List of area paths in the project",
        "use_cases": [
            "list area",
            "show area",
            "get area",
            "area path",
            "area paths",
            "project areas",
            "what areas exist",
            "show me all areas"
        ],
        "examples": [
            {"query": "List all area paths", "params": {}},
            {"query": "What areas are in the project?", "params": {}}
        ],
        "category": "discovery"
    },
    
    "change_ado_assignee": {
        "name": "change_ado_assignee",
        "description": "Efficient work item ownership transfer and assignment management system that updates the 'Assigned To' field in Azure DevOps with full validation, audit trailing, and notification support. Streamlines reassignment operations for bugs, user stories, tasks, features, and all work item types to different team members. Provides assignee validation against active directory and team membership before updating to prevent assignment to invalid or inactive users. Maintains complete audit history of assignment changes with timestamps, previous assignee records, and change reasons for accountability and tracking. Supports optional comment attachment explaining reassignment rationale (e.g., 'Reassigning due to workload rebalancing', 'Developer on leave', 'Skill match optimization'). Enables bulk reassignment operations for efficient workload redistribution during team changes, developer absences, or capacity rebalancing. Triggers automatic email notifications to both previous and new assignees ensuring transparent ownership transfers. Preserves all other work item fields, state, and history during reassignment process. Essential for dynamic workload management, emergency handoffs during PTO, skill-based task allocation, load balancing, team transitions, and responsive sprint adjustments. Integrates with capacity forecasting for data-driven reassignment recommendations.",
        "required_args": ["work_item_id", "assignee"],
        "optional_args": {
            "comment": "str - Optional comment for the change"
        },
        "returns": "Updated work item with new assignee",
        "use_cases": [
            "change assignee",
            "reassign work item",
            "assign to",
            "update assignee",
            "transfer work item",
            "reassign bug",
            "give task to",
            "assign bug to"
        ],
        "examples": [
            {"query": "Assign bug 12345 to John", "params": {"work_item_id": 12345, "assignee": "john@company.com"}},
            {"query": "Reassign work item 67890", "params": {"work_item_id": 67890, "assignee": "jane@company.com"}}
        ],
        "category": "action"
    },
    
    "detect_recurring_bugs": {
        "name": "detect_recurring_bugs",
        "description": "Specialized bug pattern analysis and recurrence detection system focused on identifying systematic defect patterns, repeated failures, and quality hotspots within the codebase. Uses advanced text similarity algorithms, statistical clustering, and temporal analysis to detect bugs that repeatedly occur in specific code modules, components, or functional areas. Analyzes bug titles, descriptions, reproduction steps, and error messages to identify semantically similar defects even when worded differently. Groups bugs by area path to highlight problematic code sections requiring refactoring, additional testing coverage, or architectural improvements. Calculates similarity scores between bug pairs using natural language processing and provides configurable matching thresholds to control detection sensitivity. Filters results by recurrence threshold to focus only on genuinely repetitive issues (default minimum 3 occurrences). Supports customizable historical lookback windows for trend analysis over different time periods. Generates actionable insights about root causes, affected components, and recommended preventive measures. Particularly effective for quality retrospectives, technical debt planning, refactoring prioritization, test coverage gap identification, and proactive defect prevention strategies. Functions as a focused alias of bug_areas_highlight skill with emphasis on pattern detection and recurrence analysis rather than comprehensive reporting.",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "lookback_days": "int - Days to look back (default: 60)",
            "recurrence_threshold": "int - Min bugs to consider recurring (default: 3)"
        },
        "returns": "List of recurring bug patterns by area",
        "use_cases": [
            "detect recurring bugs",
            "recurring bugs",
            "bug detection",
            "find repeated bugs",
            "bug patterns",
            "same bugs again",
            "repeat issues"
        ],
        "examples": [
            {"query": "Find recurring bugs", "params": {}},
            {"query": "Detect bug patterns", "params": {"lookback_days": 90}}
        ],
        "category": "analysis"
    },
    
    "sprint_plan": {
        "name": "sprint_plan",
        "description": "Advanced AI-powered sprint planning and intelligent task allocation system that generates comprehensive, execution-ready sprint plans with automated task decomposition, skill-based developer assignments leveraging frontend/backend expertise analysis, and capacity-aware workload distribution. Uses large language models (LLMs) to perform intelligent breakdown of user stories and requirements into granular, actionable development tasks with accurate effort estimates. Automatically assigns tasks to optimal developers by analyzing proven frontend (FE) proficiencies (React, Angular, TypeScript, HTML/CSS) and backend (BE) expertise (Java, Python, Spring Boot, Django, REST APIs) from the team's skill matrix and contribution history. Calculates real-time capacity utilization percentages for each developer to prevent overallocation, identify underutilized team members, and ensure balanced workload distribution across the sprint. Generates detailed task hierarchies with parent-child relationships for complex features, maintains dependency tracking, and optimizes task ordering for efficient parallel execution. Produces exportable sprint plan documents in professional CSV format containing complete assignment details, effort breakdowns, skill requirements, and capacity analytics. Features intelligent workload balancing algorithms with manual override options for fine-tuned control. Integrates seamlessly with developer knowledge base for accurate skill matching. Supports what-if scenario planning for scope adjustments. Requires interactive UI form for sprint configuration including dates, scope boundaries, balancing preferences, and LLM enhancement toggles. Essential for data-driven sprint planning, optimal resource utilization, skill-based task matching, and predictable delivery execution.",
        "requires_ui_form": True,
        "required_args": ["sprint", "start_date", "end_date"],
        "optional_args": {
            "balance": "bool - Enable workload balancing (default: True)",
            "use_llm": "bool - Use LLM for intelligent task breakdown (default: True)"
        },
        "returns": "Sprint plan CSV with task assignments, capacity utilization, and breakdown",
        "use_cases": [
            "generate sprint plan",
            "create sprint plan",
            "sprint planning",
            "plan the sprint",
            "assign developers to tasks",
            "task assignment",
            "sprint capacity planning",
            "who should work on what"
        ],
        "examples": [
            {"query": "Generate sprint plan for next week", "params": {"sprint": "Sprint 2025-01-01", "start_date": "2025-01-01", "end_date": "2025-01-14"}},
            {"query": "Plan the upcoming sprint", "params": {}}
        ],
        "category": "planning"
    },
    
    "execute_wiql": {
        "name": "execute_wiql",
        "description": """Direct WIQL (Work Item Query Language) execution against Azure DevOps REST API.
        This is the CANONICAL tool for all WIQL queries. The LLM generates the WIQL string,
        and this skill executes it directly via REST API (POST to _apis/wit/wiql).

        CRITICAL ADO FIELD REFERENCE (use EXACT names):
        ┌─────────────────────────────────────────────────────┬──────────────────────────┐
        │ Field Name                                          │ Use For                  │
        ├─────────────────────────────────────────────────────┼──────────────────────────┤
        │ [System.Id]                                         │ Work item ID             │
        │ [System.Title]                                      │ Title                    │
        │ [System.State]                                      │ State (Active/Closed)    │
        │ [System.AssignedTo]                                 │ Assigned person           │
        │ [System.WorkItemType]                               │ Bug/Task/User Story etc  │
        │ [System.CreatedDate]                                │ Date created             │
        │ [System.ChangedDate]                                │ Date last modified       │
        │ [System.AreaPath]                                   │ Area path                │
        │ [System.IterationPath]                              │ Iteration/Sprint path    │
        │ [System.Tags]                                       │ Tags                     │
        │ [System.TeamProject]                                │ Project name             │
        │ [Microsoft.VSTS.Common.ClosedDate]                  │ Date item was closed     │
        │ [Microsoft.VSTS.Common.ResolvedDate]                │ Date item was resolved   │
        │ [Microsoft.VSTS.Common.Priority]                    │ Priority (1-4)           │
        │ [Microsoft.VSTS.Common.Severity]                    │ Severity                 │
        │ [Microsoft.VSTS.Scheduling.StoryPoints]             │ Story points             │
        │ [Microsoft.VSTS.Scheduling.RemainingWork]           │ Remaining work hours     │
        │ [Microsoft.VSTS.Scheduling.OriginalEstimate]        │ Original estimate hours  │
        │ [Microsoft.VSTS.Scheduling.CompletedWork]           │ Completed work hours     │
        └─────────────────────────────────────────────────────┴──────────────────────────┘

        ⚠️ COMMON MISTAKES TO AVOID:
        - [System.ClosedDate] does NOT exist → use [Microsoft.VSTS.Common.ClosedDate]
        - [System.ResolvedDate] does NOT exist → use [Microsoft.VSTS.Common.ResolvedDate]
        - [System.Priority] does NOT exist → use [Microsoft.VSTS.Common.Priority]

        WIQL SYNTAX:
        - Use @Today macro: @Today - 10 for 10 days ago
        - Multiple states: [System.State] IN ('Closed', 'Resolved')
        - Single quotes for strings: 'FracPro-OPS'
        - UNDER for area/iteration hierarchy: [System.AreaPath] UNDER 'Project\\Team'
        - CONTAINS for text search: [System.Title] CONTAINS 'login'

        EXAMPLE QUERIES:
        - Closed bugs last 10 days:
          SELECT [System.Id], [System.Title], [System.State], [Microsoft.VSTS.Common.ClosedDate]
          FROM WorkItems
          WHERE [System.TeamProject] = 'FracPro-OPS'
          AND [System.WorkItemType] = 'Bug'
          AND [System.State] IN ('Closed', 'Resolved')
          AND [Microsoft.VSTS.Common.ClosedDate] >= @Today - 10
          ORDER BY [Microsoft.VSTS.Common.ClosedDate] DESC

        - High priority active items:
          SELECT [System.Id], [System.Title], [Microsoft.VSTS.Common.Priority]
          FROM WorkItems
          WHERE [System.TeamProject] = 'FracPro-OPS'
          AND [Microsoft.VSTS.Common.Priority] <= 2
          AND [System.State] = 'Active'
          ORDER BY [Microsoft.VSTS.Common.Priority] ASC""",
        "required_args": ["project", "wiql"],
        "optional_args": {
            "top": "int - Maximum number of results (default: 1000)"
        },
        "arg_descriptions": {
            "project": "Azure DevOps project name (e.g., 'FracPro-OPS')",
            "wiql": "WIQL query string with correct ADO field names",
            "top": "Max results to return (default: 1000)"
        },
        "returns": "Dict with 'success', 'count', 'items' (full work item details), and 'query'",
        "use_cases": [
            "bugs closed in last X days",
            "items created recently",
            "work items by priority",
            "date range queries",
            "complex WIQL queries",
            "items by area path",
            "sprint work items",
            "high priority bugs",
            "items changed this week",
            "closed items count"
        ],
        "examples": [
            {
                "query": "bugs closed in last 10 days",
                "params": {
                    "project": "FracPro-OPS",
                    "wiql": "SELECT [System.Id], [System.Title], [System.State], [Microsoft.VSTS.Common.ClosedDate] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [System.WorkItemType] = 'Bug' AND [System.State] IN ('Closed', 'Resolved') AND [Microsoft.VSTS.Common.ClosedDate] >= @Today - 10 ORDER BY [Microsoft.VSTS.Common.ClosedDate] DESC"
                }
            },
            {
                "query": "high priority active items",
                "params": {
                    "project": "FracPro-OPS",
                    "wiql": "SELECT [System.Id], [System.Title], [Microsoft.VSTS.Common.Priority] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [Microsoft.VSTS.Common.Priority] <= 2 AND [System.State] = 'Active' ORDER BY [Microsoft.VSTS.Common.Priority] ASC"
                }
            }
        ],
        "category": "query",
        "agent": "pm_agent",
        "handler": "agents.pm_agent.pm_skills.wiql_skill.execute_wiql"
    }
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_skill_metadata() -> Dict[str, Dict[str, Any]]:
    """Get all PM skills with their metadata for LLM planning.
    
    Returns:
        Dictionary of skill name -> metadata
    """
    return PM_SKILL_REGISTRY.copy()


def get_skill_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Get a specific skill's metadata by name.
    
    Args:
        name: Skill name
        
    Returns:
        Skill metadata dict or None if not found
    """
    return PM_SKILL_REGISTRY.get(name)


def get_skills_by_category(category: str) -> Dict[str, Dict[str, Any]]:
    """Get all skills in a specific category.
    
    Args:
        category: One of 'analysis', 'notification', 'reporting', 'status', 'capacity', 'action', 'discovery', 'planning'
        
    Returns:
        Dictionary of skills in that category
    """
    return {
        name: meta 
        for name, meta in PM_SKILL_REGISTRY.items() 
        if meta.get("category") == category
    }


def match_query_to_skill(query: str) -> Optional[str]:
    """Match a user query to the most appropriate skill.
    
    Uses use_cases and description matching to find the best skill.
    
    Args:
        query: User's natural language query
        
    Returns:
        Skill name if matched, None otherwise
    """
    query_lower = query.lower().strip()
    
    # Score each skill based on use case matches
    scores: Dict[str, int] = {}
    
    for skill_name, metadata in PM_SKILL_REGISTRY.items():
        score = 0
        
        # Check use cases (highest weight)
        for use_case in metadata.get("use_cases", []):
            if use_case.lower() in query_lower:
                score += 10
            # Partial word match
            elif any(word in query_lower for word in use_case.lower().split()):
                score += 3
        
        # Check skill name
        if skill_name.replace("_", " ") in query_lower:
            score += 8
        
        # Check description keywords
        desc_words = metadata.get("description", "").lower().split()
        matching_desc_words = sum(1 for word in desc_words if len(word) > 4 and word in query_lower)
        score += matching_desc_words
        
        if score > 0:
            scores[skill_name] = score
    
    if not scores:
        return None
    
    # Return the skill with highest score
    best_skill = max(scores, key=lambda k: scores[k])
    
    # Require minimum score threshold
    if scores[best_skill] < 5:
        logger.debug(f"No confident skill match for query: {query}")
        return None
    
    logger.info(f"Matched query '{query[:50]}...' to skill '{best_skill}' (score: {scores[best_skill]})")
    return best_skill


def get_skill_prompt_context() -> str:
    """Generate a formatted context string for LLM prompts.
    
    This creates a concise summary of available skills that can be
    included in LLM system prompts for tool selection.
    
    Returns:
        Formatted string describing available skills
    """
    lines = ["Available PM Agent Skills:"]
    
    for name, meta in PM_SKILL_REGISTRY.items():
        desc = meta.get("description", "")[:100]
        required = meta.get("required_args", [])
        
        if required:
            lines.append(f"- {name}: {desc} (requires: {', '.join(required)})")
        else:
            lines.append(f"- {name}: {desc}")
    
    return "\n".join(lines)


def validate_skill_params(skill_name: str, params: Dict[str, Any]) -> tuple[bool, List[str]]:
    """Validate parameters for a skill.
    
    Args:
        skill_name: Name of the skill
        params: Parameters to validate
        
    Returns:
        Tuple of (is_valid, list of missing required params)
    """
    skill = PM_SKILL_REGISTRY.get(skill_name)
    if not skill:
        return False, [f"Unknown skill: {skill_name}"]
    
    required = skill.get("required_args", [])
    missing = [arg for arg in required if arg not in params]
    
    return len(missing) == 0, missing


def get_priority_tools_for_query(query: str) -> List[str]:
    """Get priority tools for a query based on SKILL_TO_TOOLS_MAP.
    
    Uses semantic matching to determine which skill the query maps to,
    then returns the primary and supporting tools for that skill.
    
    Args:
        query: User's natural language query
        
    Returns:
        List of tool names to prioritize
    """
    query_lower = query.lower().strip()
    best_skill = None
    best_score = 0
    
    for skill_id, mapping in SKILL_TO_TOOLS_MAP.items():
        score = 0
        
        # Check canonical queries
        for canonical in mapping.get("canonical_queries", []):
            canonical_lower = canonical.lower()
            if canonical_lower in query_lower or query_lower in canonical_lower:
                score += 10
            else:
                # Word overlap
                canonical_words = set(canonical_lower.split())
                query_words = set(query_lower.split())
                overlap = len(canonical_words & query_words)
                if overlap >= 2:
                    score += overlap * 2
        
        # Check description keywords
        desc_lower = mapping.get("description", "").lower()
        desc_words = set(w for w in desc_lower.split() if len(w) > 3)
        query_words = set(w for w in query_lower.split() if len(w) > 3)
        desc_overlap = len(desc_words & query_words)
        score += desc_overlap
        
        if score > best_score:
            best_score = score
            best_skill = skill_id
    
    if best_skill and best_score >= 3:
        mapping = SKILL_TO_TOOLS_MAP[best_skill]
        tools = mapping.get("primary_tools", []) + mapping.get("supporting_tools", [])
        logger.debug(f"Matched query to skill '{best_skill}', priority tools: {tools}")
        return tools
    
    return []


# Export for easy importing
__all__ = [
    "PM_SKILL_REGISTRY",
    "SKILL_TO_TOOLS_MAP",
    "get_skill_metadata",
    "get_skill_by_name",
    "get_skills_by_category",
    "match_query_to_skill",
    "get_skill_prompt_context",
    "validate_skill_params",
    "get_skill_tools",
    "get_priority_tools_for_query",
]
