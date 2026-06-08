# -*- coding: utf-8 -*-
"""Skill Registry for Intent Classification and Tool Discovery.

This module provides a unified registry of all skills that the chatbot can use:
1. Semantic matcher uses these for intent classification
2. LLM planner uses these for tool selection  
3. Chat UI uses these for dynamic skill discovery

Each skill includes:
- Canonical prompts (variations of how users ask)
- Required/optional arguments
- Handler function for execution
- Use cases for LLM understanding

IMPORTANT: This integrates both:
- Chat UI skills (sprint tracking, developer skills, etc.)
- PM Agent fixed skills (bug areas, overlooked stories, etc.)

Note: This is separate from `skills_registry.py` which manages team member
skill matrices. This module is for intent classification/routing.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = DATA_DIR / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# CORE CHAT UI SKILLS
# =============================================================================

CORE_SKILLS: List[Dict[str, Any]] = [
    # DEPRECATED: sprint_tracking skill
    # Sprint queries should go through PM Agent's LLM planner + ADO tools (wit_get_work_items_for_iteration)
    # This ensures proper Langfuse tracing and real-time ADO data fetching
    # Keeping the skill definition for backward compatibility but marked as deprecated
    {
        "id": "sprint_tracking",
        "display_name": "Sprint Tracking & Progress (DEPRECATED - use PM Agent)",
        "description": "DEPRECATED: Use PM Agent with wit_get_work_items_for_iteration tool instead",
        "priority": 999,  # Very low priority - should never match
        "category": "deprecated",
        "deprecated": True,  # Mark as deprecated
        "required_args": [],
        "optional_args": {},
        "canonical_prompts": [],  # Empty - should never match semantically
        "handler": "utilities.sprint_responder.handle_sprint_query",
    },
    {
        "id": "developer_skills",
        "display_name": "Developer Skills & Tech Stack",
        "description": """Comprehensive developer expertise discovery and technology proficiency analysis system. This skill provides deep insights into team capabilities by analyzing multiple data sources including code contributions, work item history, and knowledge base records. 
        
        KEY CAPABILITIES:
        - Search and discover developers based on specific technology skills (e.g., Angular, React, Java, Python, TypeScript, Spring Boot, Django, .NET)
        - Analyze developer expertise levels and proficiency across frontend, backend, and fullstack domains
        - View detailed tech stack breakdown showing which developers have experience with specific frameworks, libraries, and tools
        - Examine code contribution metrics including commit history, lines of code, and language usage patterns
        - Explore developer knowledge base with evidence-based skill validation from actual work performed
        - Identify subject matter experts for specific technologies or project areas
        - Understand team composition and capability gaps across the technology landscape
        
        DATA SOURCES:
        - Developer knowledge base (Milvus vector database) containing semantic skill embeddings
        - Commit history and code contribution analytics from version control systems
        - Work item associations showing project involvement and domain expertise
        - Language usage statistics and framework/library utilization patterns
        - Historical performance data and technology-specific accomplishments
        
        USE CASES:
        - Finding developers with specific technology expertise for task assignment ("who knows Angular?", "find Java experts")
        - Understanding team capabilities before planning new features or projects
        - Identifying knowledge gaps and training opportunities
        - Matching developers to tasks based on their proven expertise and experience
        - Building cross-functional teams with complementary skill sets
        - Discovering hidden expertise and underutilized skills within the team
        - Creating skill matrices and competency maps for resource planning
        - Answering questions about team composition like "who can work on frontend?", "which developers have backend experience?"
        
        OUTPUT:
        Returns detailed developer profiles with skill evidence, contribution metrics, technology proficiency levels, and work history relevant to the search criteria.""",
        "priority": 2,
        "category": "discovery",
        "required_args": [],
        "optional_args": {
            "developer": "str - Developer name to filter",
            "technology": "str - Technology to search for",
        },
        "canonical_prompts": [
            "show developer skills",
            "show me the tech stack",
            "list developer expertise",
            "show skill matrix",
            "developer knowledge base",
            "show code contributions",
            "who contributed the most",
            # NEW: General team capability queries without specific technology
            "display capabilities of our engineers",
            "what technologies does our team have",
            "team technical capabilities",
            "overall team skills",
            "skill matrix",
        ],
        "handler": "utilities.developer_skills_responder.handle_developer_query",
    },
    {
        "id": "upcoming_tasks",
        "display_name": "Upcoming Tasks & Planning",
        "description": """Advanced sprint and backlog planning system that provides comprehensive visibility into upcoming work, task prioritization, and resource allocation. This skill enables proactive project management through intelligent workload distribution and capacity-aware planning.
        
        KEY CAPABILITIES:
        - View upcoming tasks and work items scheduled for future sprints and iterations
        - Analyze backlog items ready for sprint planning and refinement
        - Track task assignments and developer workload across upcoming iterations
        - Perform capacity planning by matching available developer hours with planned work
        - Identify unassigned tasks and orphaned work items requiring ownership
        - Distribute backlog items intelligently based on developer availability and skills
        - Monitor workload balance to prevent overallocation or underutilization
        - Generate sprint planning recommendations based on team velocity and capacity
        - Visualize upcoming deadlines, milestones, and critical deliverables
        
        DATA SOURCES:
        - Azure DevOps work item queries (WIQL) for backlog and sprint data
        - Team capacity information from iteration settings
        - Work item assignment history and status tracking
        - Developer availability and utilization metrics
        - Sprint velocity and historical completion rates
        - Task dependency relationships and blocking issues
        
        USE CASES:
        - Sprint planning meetings: "show upcoming tasks", "what tasks are coming up", "ready for planning"
        - Backlog grooming: "show the backlog", "what items need assignment", "unassigned tasks"
        - Capacity management: "who is available", "capacity planning", "workload distribution"
        - Resource allocation: "assign backlog items", "distribute work", "balance workload"
        - Task ownership: "tasks without owner", "unassigned work items", "orphaned tasks"
        - Planning visibility: "show task assignments", "upcoming deadlines", "next sprint work"
        - Workload balancing: "who can take more work", "overloaded developers", "redistribute tasks"
        
        OUTPUT:
        Returns structured lists of upcoming tasks with assignment details, capacity utilization analysis, workload distribution metrics, and actionable planning recommendations for balanced sprint execution.""",
        "priority": 3,
        "category": "planning",
        "required_args": [],
        "optional_args": {
            "developer": "str - Filter by developer",
            "sprint": "str - Sprint/iteration name",
        },
        "canonical_prompts": [
            "show upcoming tasks",
            "what tasks are coming up",
            "sprint planning",
            "ready for planning",
            "show the backlog",
            "assign backlog items",
            "capacity planning",
            "who is available",
            "workload distribution",
            "show task assignments",
            "unassigned tasks",
            "tasks without owner",
        ],
        "handler": "utilities.task_responder.handle_task_query",
    },
    {
        "id": "work_item_details",
        "display_name": "Work Item Details",
        "description": """Comprehensive work item information retrieval and analysis system for Azure DevOps. This skill provides detailed insights into bugs, user stories, tasks, and other work item types through both direct ID lookup and intelligent text-based search.
        
        KEY CAPABILITIES:
        - Retrieve complete details for specific work items by ID including all fields and metadata
        - Search for work items using natural language queries and text matching
        - View work item status, state transitions, and lifecycle history
        - Access work item relationships including parent-child hierarchies, dependencies, and related items
        - Examine detailed fields: title, description, acceptance criteria, assigned to, priority, severity, tags
        - Track work item progress with state changes, comments, and activity history
        - Analyze bug details including reproduction steps, root cause analysis, and resolution notes
        - Review user story requirements, acceptance criteria, and business value
        - Inspect task breakdowns, effort estimates, and completion status
        - View attachment history, linked commits, and pull request associations
        
        DATA SOURCES:
        - Azure DevOps Work Item Tracking system via MCP tools (wit_get_work_items, search_workitem)
        - Work item query language (WIQL) for advanced filtering
        - Work item revision history and audit trails
        - Related artifacts including commits, pull requests, and builds
        - Comments, discussions, and collaboration history
        
        USE CASES:
        - Direct lookup: "show work item 12345", "get details of bug 67890", "what is item 54321"
        - Status checks: "what is the status of work item 12345", "is bug 67890 closed"
        - Bug investigation: "tell me about bug 12345", "show bug details", "get bug information"
        - Story analysis: "show user story 45678", "get story details", "what does story 45678 do"
        - Task tracking: "get task details", "show task 98765", "what is the task about"
        - Text search: "find work item about login", "search for items mentioning authentication"
        - Discovery: "find items related to performance", "search for bugs in payment module"
        - Investigation: "show me details about the crash", "find work items about database errors"
        
        OUTPUT:
        Returns comprehensive work item information including ID, type, title, description, state, assigned to, priority, tags, relationships, comments, and complete field values with formatting preserved.""",
        "priority": 4,
        "category": "discovery",
        "required_args": [],
        "optional_args": {
            "work_item_id": "int - Specific work item ID",
            "search_text": "str - Text to search for",
        },
        "canonical_prompts": [
            "show work item details",
            "what is the status of item",
            "tell me about bug",
            "show user story",
            "get task details",
            "what is work item",
            "find work item",
            "search for item",
            "get details of item 12345",
            "show me bug 67890",
        ],
        "handler": "utilities.work_item_responder.handle_work_item_query",
    },
]

# =============================================================================
# PM AGENT FIXED SKILLS (Integrated from agents/pm_agent/tool_registry.py)
# These are deterministic skills that don't need LLM planning
# =============================================================================

PM_AGENT_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "bug_areas_highlight",
        "display_name": "Bug Areas Highlight",
        "description": """Advanced bug pattern detection and recurring issue analysis system that identifies problematic code areas by analyzing bug distribution, similarity patterns, and area path concentrations. This skill helps teams proactively address systemic quality issues and focus refactoring efforts.
        
        KEY CAPABILITIES:
        - Detect recurring bugs by analyzing title similarity and description patterns using NLP
        - Identify area paths (code modules/components) with the highest bug concentration
        - Analyze bug recurrence patterns to find repeated issues across time periods
        - Calculate similarity scores between bugs to cluster related defects
        - Generate comprehensive HTML reports highlighting problematic areas with visual charts
        - Send automated email notifications to stakeholders with actionable insights
        - Track bug trends over configurable lookback periods (default 60 days)
        - Filter by recurrence threshold to focus on genuinely repetitive issues (default 3+ occurrences)
        - Apply customizable similarity thresholds for accurate pattern matching (default 0.75)
        
        DATA SOURCES:
        - Azure DevOps bug work items via WIQL queries and search APIs
        - Bug title and description text for semantic similarity analysis
        - Area path classifications for module/component grouping
        - Bug creation dates and activity timestamps for temporal analysis
        - Bug state history and resolution information
        - Related work items and dependency relationships
        
        ANALYTICAL METHODS:
        - Text similarity algorithms (TF-IDF, cosine similarity) for bug title/description matching
        - Area path clustering to identify hot zones in the codebase
        - Temporal pattern analysis to detect recurring issues over time
        - Statistical aggregation for bug count thresholds and recurrence metrics
        
        USE CASES:
        - Quality analysis: "show recurring bugs", "find recurring bugs", "bug areas highlight"
        - Problem identification: "which areas have most bugs", "where do bugs keep happening"
        - Root cause investigation: "problematic code areas", "bug hotspots", "analyze bug patterns"
        - Proactive quality management: "detect recurring bugs", "find bug patterns", "show bug trends"
        - Area-based analysis: "bugs by area path", "area wise bugs", "module with most bugs"
        - Pattern detection: "repeated bugs", "similar bugs", "same bugs happening again"
        - Reporting: "bug analysis report", "recurring bug report", "generate bug pattern report"
        
        CONFIGURABLE PARAMETERS:
        - lookback_days: Historical window for bug analysis (default 60)
        - recurrence_threshold: Minimum bugs to flag as recurring (default 3)
        - similarity_threshold: Text similarity cutoff for clustering (default 0.75)
        - recipients: Email distribution list for reports
        - send_email: Enable/disable email notifications (default True)
        - preview_only: Generate report without sending (default False)
        
        OUTPUT:
        HTML-formatted analytical report with bug clustering by area path, recurrence statistics, similarity groups, trend visualizations, and actionable recommendations for addressing systemic quality issues. Automatically emails report to configured recipients.""",
        "priority": 1,
        "category": "analysis",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name (default: from config)",
            "lookback_days": "int - Days to look back for bugs (default: 60)",
            "recurrence_threshold": "int - Minimum bugs to consider recurring (default: 3)",
            "similarity_threshold": "float - Title similarity threshold 0-1 (default: 0.75)",
            "recipients": "list[str] - Email recipients",
            "send_email": "bool - Whether to send email (default: True)",
            "preview_only": "bool - Preview without emailing (default: False)",
        },
        "returns": "HTML report with recurring bug areas, email sent to recipients",
        "canonical_prompts": [
            "show recurring bugs",
            "find recurring bugs",
            "bug areas highlight",
            "highlight bug areas",
            "bug analysis report",
            "recurring bug report",
            "which areas have most bugs",
            "where do bugs keep happening",
            "problematic code areas",
            "bug hotspots",
            "analyze bug patterns",
            "detect recurring bugs",
            "find bug patterns",
            "show bug trends",
            "bugs by area path",
            "area wise bugs",
            "repeated bugs",
            "similar bugs",
            "same bugs again",
        ],
        "handler": "features.bug_area_highlight.handler.handle_bug_areas",
    },
    {
        "id": "feedback_to_dev",
        "display_name": "Feedback to Developer",
        "description": """Intelligent developer feedback and root cause analysis (RCA) system that automatically detects new bugs, finds similar historical issues using semantic search, and delivers actionable insights to help developers learn from past mistakes and prevent recurring defects.
        
        KEY CAPABILITIES:
        - Automatically detect newly created or recently modified bugs within configurable time windows
        - Find similar historical bugs using advanced embedding-based semantic similarity search
        - Extract root cause analysis (RCA) content from related historical bugs and resolutions
        - Generate personalized feedback notifications for developers assigned to new bugs
        - Provide contextual learning by linking current bugs to past similar issues and their solutions
        - Analyze bug descriptions and titles using vector embeddings for accurate similarity matching
        - Deliver insights about common failure patterns, effective fixes, and preventive measures
        - Support test mode for preview and validation before production deployment
        - Enable continuous learning through automated feedback loops
        
        DATA SOURCES:
        - Azure DevOps bug work items via time-filtered WIQL queries
        - Vector database (Milvus) containing bug description embeddings for semantic search
        - Historical bug repository with RCA notes, resolution descriptions, and fix details
        - Bug assignment information and developer ownership records
        - Bug state history showing creation, modification, and resolution timelines
        
        SEMANTIC SEARCH & MATCHING:
        - Uses state-of-the-art text embedding models to vectorize bug descriptions
        - Performs cosine similarity comparison against historical bug embeddings
        - Applies configurable similarity threshold (default 0.82) for high-precision matching
        - Returns top-k most similar historical bugs with relevance scores
        - Extracts RCA content from matched historical bugs for learning transfer
        
        USE CASES:
        - Developer learning: "feedback to dev", "feedback to developer", "help developer with bug"
        - Bug investigation: "bug feedback", "find similar bugs", "what caused this bug"
        - RCA assistance: "rca feedback", "root cause analysis", "similar bugs for rca"
        - Automated notifications: "new bug notification", "developer feedback", "notify developer about bug"
        - Pattern recognition: "bug analysis feedback", "find similar historical bugs"
        - Knowledge transfer: "send feedback to developers", "share bug insights"
        
        CONFIGURABLE PARAMETERS:
        - lookback_minutes: Time window for detecting new bugs (default 1440 minutes = 24 hours)
        - historical_days: How far back to search for similar historical bugs (default 30 days)
        - embedding_threshold: Similarity score cutoff for matching (default 0.82, range 0-1)
        - recipients: Email recipients for feedback notifications
        - is_test: Test mode for validation without sending actual notifications
        
        OUTPUT:
        Comprehensive feedback report containing new bug details, top similar historical bugs with similarity scores, extracted RCA content and resolution notes, recommended preventive actions, and developer-specific insights. Automatically delivered via email to assigned developers and configured recipients.""",
        "priority": 2,
        "category": "notification",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "lookback_minutes": "int - How far back to look for new bugs (default: 1440 = 24h)",
            "historical_days": "int - How far back to look for historical bugs (default: 30)",
            "embedding_threshold": "float - Similarity threshold for embeddings (default: 0.82)",
            "recipients": "list[str] - Email recipients",
            "is_test": "bool - Test run mode (default: False)",
        },
        "returns": "Feedback report with similar bugs and RCA suggestions, sent to developers",
        "canonical_prompts": [
            "feedback to dev",
            "feedback to developer",
            "bug feedback",
            "rca feedback",
            "new bug notification",
            "developer feedback",
            "bug analysis feedback",
            "find similar bugs",
            "what caused this bug",
            "root cause analysis",
            "help developer with bug",
            "send feedback to developers",
            "notify developer about bug",
            "similar bugs for rca",
        ],
        "handler": "features.feedback_to_dev.handler.handle_feedback_to_dev",
    },
    {
        "id": "overlooked_stories",
        "display_name": "Overlooked Stories",
        "description": """Proactive work item health monitoring system that identifies stale, dormant, and potentially forgotten user stories, tasks, and features by analyzing activity patterns and timestamps. This skill ensures no critical work slips through the cracks and maintains backlog hygiene.
        
        KEY CAPABILITIES:
        - Detect user stories with no recent activity or updates within configurable staleness windows
        - Identify work items stuck in non-terminal states without progress (e.g., Active, Committed, New)
        - Track last modification dates and calculate days of inactivity for prioritization
        - Flag high-priority or critical items that have been dormant despite their importance
        - Generate automated reminder notifications to re-engage teams on forgotten work
        - Provide detailed activity timelines showing when work items last changed
        - Filter by area path, iteration, or assignment to focus on specific team or project scopes
        - Support customizable staleness thresholds to match team workflows (default 14 days)
        - Send email reminders with actionable lists of overlooked items to stakeholders
        
        DATA SOURCES:
        - Azure DevOps work items filtered by type (User Story, Feature, Task, etc.)
        - Work item change history and revision timestamps via ADO APIs
        - Last updated date/time fields for each work item
        - Work item state and status information
        - Assignment and ownership records
        - Priority and severity classifications
        
        DETECTION CRITERIA:
        - Inactivity period: Days since last modification exceeds staleness threshold
        - State analysis: Work items in active/committed states without recent changes
        - Priority consideration: Higher urgency for critical or high-priority items
        - Assignment status: Unassigned items or items with inactive assignees
        - Sprint association: Items outside current iteration that remain unresolved
        
        USE CASES:
        - Backlog grooming: "overlooked stories", "find overlooked stories", "stale stories"
        - Health checks: "forgotten items", "overlooked user stories", "what stories are stale"
        - Proactive management: "find forgotten tasks", "dormant items", "untouched stories"
        - Activity monitoring: "work items with no activity", "stories not updated", "neglected stories"
        - Team reminders: "story reminder", "inactive stories", "stuck work items"
        - Quality assurance: "are there any forgotten work items", "check for stale backlog"
        
        CONFIGURABLE PARAMETERS:
        - stale_days: Days of inactivity to flag as overlooked (default 14)
        - recipients: Email distribution list for reminder notifications
        - send_email: Enable/disable automated reminder emails
        - project: Target Azure DevOps project scope
        
        OUTPUT:
        Detailed list of overlooked work items with ID, title, type, state, assigned to, priority, last activity date, days inactive, and recommended actions. Automatically sends email reminders to configured recipients with summary statistics and direct links to neglected items for quick action.""",
        "priority": 3,
        "category": "analysis",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "stale_days": "int - Days of inactivity to consider stale (default: 14)",
            "recipients": "list[str] - Email recipients",
            "send_email": "bool - Whether to send email",
        },
        "returns": "List of overlooked stories with last activity date, email reminder sent",
        "canonical_prompts": [
            "overlooked stories",
            "find overlooked stories",
            "stale stories",
            "forgotten items",
            "overlooked user stories",
            "story reminder",
            "inactive stories",
            "stuck work items",
            "what stories are stale",
            "find forgotten tasks",
            "work items with no activity",
            "neglected stories",
            "dormant items",
            "untouched stories",
            "stories not updated",
        ],
        "handler": "agents.pm_skill_agent.skills.handle_overlooked_stories",
    },
    {
        "id": "iteration_report",
        "display_name": "Iteration/Sprint Report",
        "description": """Comprehensive sprint analytics and iteration health reporting system that generates detailed performance metrics, completion statistics, and team progress insights for agile development cycles. This skill provides stakeholders with data-driven visibility into sprint execution and delivery health.
        
        KEY CAPABILITIES:
        - Generate complete iteration reports with work item status breakdowns and completion percentages
        - Calculate sprint metrics including total items, completed items, in-progress items, and not-started items
        - Provide completion percentage analytics comparing planned vs actual delivery
        - Analyze burndown and burnup trends to assess sprint trajectory
        - Track work item distribution by type (User Story, Bug, Task, Feature) and state
        - Identify at-risk items and blocked work requiring attention
        - Monitor team velocity and throughput for the iteration
        - Compare current sprint performance against historical baselines
        - Generate visual charts and graphs for sprint health dashboards
        - Support filtering by area paths for multi-team or component-specific reports
        - Enable work item type filtering for focused analysis (bugs only, stories only, etc.)
        - Automatically send formatted reports via email to stakeholders
        
        DATA SOURCES:
        - Azure DevOps work items within the specified iteration path
        - Work item states and state transitions (New, Active, Resolved, Closed, etc.)
        - Work item type classifications (User Story, Bug, Task, Feature, Epic)
        - Area path hierarchies for team/component filtering
        - Iteration dates and sprint timelines
        - Effort estimates and remaining work calculations
        - Completion timestamps and velocity metrics
        
        ANALYTICAL METRICS:
        - Total planned work items at sprint start
        - Completed work items and completion percentage
        - In-progress work items and their status
        - Not started items indicating scope risk
        - Work distribution by type and priority
        - Burndown rate and projected completion date
        - Scope changes during iteration (added/removed items)
        - Team velocity and throughput trends
        
        USE CASES:
        - Sprint reviews: "iteration report", "sprint report", "generate sprint report"
        - Status updates: "sprint status", "iteration status", "how is the sprint going"
        - Progress tracking: "sprint progress", "sprint summary", "show me sprint metrics"
        - Health checks: "current sprint status", "sprint health", "are we on track"
        - Stakeholder reporting: "iteration summary", "sprint overview", "send sprint report"
        - Performance analysis: "sprint completion rate", "how many items finished"
        - Team retrospectives: "what did we accomplish this sprint", "sprint achievements"
        
        CONFIGURABLE PARAMETERS:
        - iteration: Iteration path to report on (default: @CurrentIteration)
        - areas: List of area paths to filter by team or component
        - wi_types: Work item types to include (User Story, Bug, Task, etc.)
        - recipients: Email distribution list for automated reports
        - send_email: Enable email delivery of generated reports
        - project: Target Azure DevOps project
        
        OUTPUT:
        Comprehensive sprint report document with work item counts by state and type, completion percentages, burndown status, at-risk items highlighted, velocity metrics, scope change analysis, and actionable insights. Reports can be viewed in-app or automatically emailed as HTML with embedded charts and tables.""",
        "priority": 4,
        "category": "reporting",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "iteration": "str - Iteration path (default: @CurrentIteration)",
            "areas": "list[str] - Area paths to filter",
            "wi_types": "list[str] - Work item types to include",
            "recipients": "list[str] - Email recipients",
            "send_email": "bool - Send email after generation",
        },
        "returns": "Sprint report with work item counts, completion %, burndown status",
        "canonical_prompts": [
            "iteration report",
            "sprint report",
            "sprint status",
            "iteration status",
            "sprint summary",
            "how is the sprint going",
            "sprint progress",
            "generate sprint report",
            "show me sprint metrics",
            "current sprint status",
            "sprint health",
            "iteration summary",
            "sprint overview",
        ],
        "handler": "agents.pm_skill_agent.skills.handle_iteration_report",
    },
    {
        "id": "get_sprint_status",
        "display_name": "Sprint Status Check (DEPRECATED - use PM Agent)",
        "description": "DEPRECATED: Use PM Agent with wit_get_work_items_for_iteration tool instead",
        "priority": 999,  # Very low priority
        "category": "deprecated",
        "deprecated": True,
        "required_args": [],
        "optional_args": {},
        "returns": "DEPRECATED",
        "canonical_prompts": [],  # Empty - should never match
        "handler": "agents.pm_agent.handlers.handle_sprint_status",
    },
    {
        "id": "get_capacity_forecast",
        "display_name": "Capacity Forecast",
         "description": """Advanced team capacity planning and workload analysis system that forecasts available bandwidth, identifies resource constraints, and detects utilization imbalances to prevent developer burnout and maximize team efficiency.
         
         KEY CAPABILITIES:
         - Calculate total available hours for the team across current and upcoming iterations
         - Analyze individual developer capacity including planned hours, completed hours, and remaining hours
         - Compute team-wide utilization percentages to assess overall workload health
         - Identify overloaded developers at risk of burnout (utilization >100% or >90%)
         - Detect underutilized developers with available bandwidth for additional work (utilization <60%)
         - Generate capacity warnings and alerts for resource planning risks
         - Forecast capacity constraints that may impact sprint commitments
         - Compare committed work effort against available team capacity
         - Track capacity trends over time for resource planning
         - Provide developer-level capacity breakdowns with hours allocation details
         - Support multi-team capacity aggregation for portfolio-level planning
         - Enable what-if scenario analysis for sprint planning decisions
         
         DATA SOURCES:
         - Azure DevOps iteration and team capacity settings via work_list_team_iterations tool
         - Developer capacity configurations (hours per day, working days per iteration)
         - Work item effort estimates and remaining work fields via WIQL queries
         - Assignment information showing work distribution across team members
         - Iteration dates and sprint timelines
         - Historical utilization data for trend analysis
         - Developer availability calendars and time-off records
         
         CAPACITY CALCULATIONS:
         - Total capacity = Sum of all developer available hours in iteration
         - Planned capacity = Sum of estimated effort for all assigned work items
         - Utilization % = (Planned capacity / Total capacity) × 100
         - Remaining capacity = Total capacity - Planned capacity
         - At-risk threshold = Utilization > 90% (configurable)
         - Underutilized threshold = Utilization < 60% (configurable)
         
         USE CASES:
         - Capacity planning: "capacity forecast", "capacity status", "team capacity"
         - Availability queries: "available hours", "who has bandwidth", "team availability"
         - Resource analysis: "workload distribution", "check capacity", "developer capacity"
         - Risk identification: "capacity warning", "who is overloaded", "at-risk developers"
         - Balance optimization: "underutilized developers", "who can take more work"
         - Sprint planning: "show capacity forecast", "do we have capacity for more work"
         - Utilization tracking: "utilization", "team utilization status", "capacity utilization"
         - Bottleneck detection: "overloaded team members", "resource constraints"
         
         CONFIGURABLE PARAMETERS:
         - iteration: Specific iteration path to analyze (default: current iteration)
         - team: Team identifier for capacity scope
         - project: Azure DevOps project context
         
         OUTPUT:
         Detailed capacity forecast report containing total available hours, planned hours, remaining hours, utilization percentages by developer and team aggregate, list of at-risk overloaded developers with warning indicators, list of underutilized developers with available capacity, capacity warnings for planning risks, and recommendations for workload rebalancing to optimize team efficiency.""",
        "priority": 7,
        "category": "capacity",
        "required_args": [],
         "optional_args": {
            "project": "str - ADO project name",
            "team": "str - Team name",
            "iteration": "str - Iteration path",
        },
        "returns": "Capacity forecast with available hours, utilization %, risk indicators",
        "canonical_prompts": [
            "capacity forecast",
            "capacity status",
            "available hours",
            "team capacity",
            "utilization",
            "capacity warning",
            "who is overloaded",
            "underutilized developers",
            "team availability",
            "workload distribution",
            "who has bandwidth",
            "developer capacity",
            "check capacity",
            # NEW: Workload and bandwidth specific phrases
            "how much work can the team handle",
            "are developers stretched too thin",
            "who has spare cycles",
            "are we over-committed",
            "team bandwidth situation",
            "overloaded team members",
            "capacity constraints",
        ],
        "handler": "agents.pm_skill_agent.skills._run_get_capacity_forecast",
    },
    {
        "id": "change_ado_assignee",
        "display_name": "Change Work Item Assignee",
        "description": """Work item ownership management system that enables reassignment of bugs, user stories, tasks, and other work items to different team members with full audit trail and notification support. This skill facilitates workload balancing, handoffs, and ownership transfers.
        
        KEY CAPABILITIES:
        - Reassign any work item type (Bug, User Story, Task, Feature, etc.) to a different developer
        - Update the 'Assigned To' field in Azure DevOps with full validation
        - Add optional comments explaining the reason for reassignment
        - Maintain complete audit history of assignment changes with timestamps
        - Support bulk reassignment operations for multiple work items
        - Validate assignee existence and permissions before updating
        - Trigger automatic notifications to both old and new assignees
        - Preserve work item history and state during reassignment
        - Enable emergency reassignments during developer absences or transitions
        - Support reassignment based on workload balancing recommendations
        
        DATA SOURCES:
        - Azure DevOps Work Item Tracking API via wit_update_work_item MCP tool
        - Work item assignee field and assignment history
        - Team member directory for assignee validation
        - Work item audit trail and revision history
        
        ASSIGNMENT OPERATIONS:
        - Single work item reassignment by ID
        - Assignee validation against active team members
        - Assignment history tracking with before/after states
        - Comment attachment for reassignment rationale
        - Notification triggers for stakeholder awareness
        
        USE CASES:
        - Workload balancing: "change assignee", "reassign work item", "transfer work item"
        - Developer assignment: "assign to [developer]", "give task to [developer]"
        - Bug reassignment: "reassign bug", "assign bug to [developer]", "transfer bug"
        - Ownership transfer: "change owner", "move item to [developer]", "reassign story"
        - Team transitions: "update assignee", "assign work item to someone else"
        - Emergency handoffs: "reassign all items from [developer A] to [developer B]"
        - Availability changes: "move tasks to available developers"
        
        REQUIRED PARAMETERS:
        - work_item_id: The unique identifier of the work item to reassign (integer)
        - assignee: Email address or display name of the new assignee
        
        OPTIONAL PARAMETERS:
        - comment: Explanation for the reassignment (e.g., "Reassigning due to workload balance")
        
        OUTPUT:
        Updated work item record confirming successful reassignment with new assignee details, timestamp of change, comment if provided, and complete assignment history. Automatically triggers notifications to relevant stakeholders about the ownership transfer.""",
        "priority": 8,
        "category": "action",
        "required_args": ["work_item_id", "assignee"],
        "optional_args": {
            "comment": "str - Optional comment for the change",
        },
        "returns": "Updated work item with new assignee",
        "canonical_prompts": [
            "change assignee",
            "reassign work item",
            "assign to",
            "update assignee",
            "transfer work item",
            "reassign bug",
            "give task to",
            "assign bug to",
            "move item to",
            "reassign story",
            "change owner",
            # NEW: Additional assignment change phrases
            "transfer task to developer",
            "move ownership of bug",
            "hand off work item",
            "switch assignee for",
            "reassign to different developer",
        ],
        "handler": "agents.pm_agent.handlers.handle_change_assignee",
    },
    {
        "id": "detect_recurring_bugs",
        "display_name": "Detect Recurring Bugs",
        "description": "Detect recurring bugs by area path. Analyze bug patterns to find repeated issues.",
        "priority": 9,
        "category": "analysis",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "lookback_days": "int - Days to look back (default: 60)",
            "recurrence_threshold": "int - Min bugs to consider recurring (default: 3)",
        },
        "returns": "List of recurring bug patterns by area",
        "canonical_prompts": [
            "detect recurring bugs",
            "recurring bugs",
            "bug detection",
            "find repeated bugs",
            "bug patterns",
            "same bugs again",
            "repeat issues",
            "find recurring issues",
            "analyze bug recurrence",
        ],
        "handler": "agents.pm_agent.handlers.handle_detect_recurring",
    },
    {
        "id": "sprint_plan",
        "display_name": "Generate Sprint Plan",
        "description": """Intelligent sprint planning and task allocation system that generates comprehensive sprint plans with automated task breakdown, skill-based developer assignments (frontend/backend expertise), and capacity-aware workload distribution. This advanced skill uses LLM-powered analysis to create optimal sprint execution plans.
        
        KEY CAPABILITIES:
        - Generate complete sprint plans from user stories and requirements using AI-powered task decomposition
        - Automatically break down user stories into actionable development tasks with effort estimates
        - Assign tasks to developers based on their proven frontend (FE) and backend (BE) skill proficiencies
        - Match task requirements to developer expertise using semantic skill analysis from knowledge base
        - Balance workload distribution across team members based on capacity and utilization
        - Calculate capacity utilization to prevent overallocation and identify underutilized developers
        - Generate task hierarchies with parent-child relationships for complex features
        - Produce exportable sprint plan documents in CSV format with complete assignment details
        - Support LLM-enhanced intelligent task breakdown for better granularity and accuracy
        - Enable manual workload balancing toggles for fine-tuned control
        - Integrate with developer skill matrix to ensure proper skill-task alignment
        
        REQUIRES UI FORM:
        This skill requires interactive UI form input for sprint configuration including dates, scope, and preferences. Users must provide sprint details through the dedicated planning interface.
        
        DATA SOURCES:
        - User stories and requirements from Azure DevOps backlog
        - Developer skill matrix with FE/BE proficiency ratings from knowledge base
        - Team capacity data from iteration settings
        - Historical velocity and completion metrics
        - Developer availability and working hours
        - Technology stack and framework requirements from work items
        
        INTELLIGENT TASK BREAKDOWN:
        - LLM-powered analysis of user story descriptions and acceptance criteria
        - Automatic identification of frontend vs backend work requirements
        - Technology-specific task creation (UI components, API endpoints, database changes, etc.)
        - Effort estimation based on historical data and complexity analysis
        - Dependency detection and task ordering for optimal execution flow
        
        SKILL-BASED ASSIGNMENT:
        - Matches frontend tasks to developers with React, Angular, TypeScript, HTML/CSS expertise
        - Assigns backend tasks to developers with Java, Python, Spring Boot, API development skills
        - Considers developer proficiency levels and experience for optimal assignments
        - Balances workload to prevent overloading specialists while utilizing generalists
        - Respects developer preferences and past performance on similar technologies
        
        USE CASES:
        - Sprint planning meetings: "generate sprint plan", "create sprint plan", "plan the sprint"
        - Resource allocation: "assign developers to tasks", "who should work on what"
        - Capacity planning: "sprint capacity planning", "plan next sprint"
        - Task breakdown: "create task breakdown", "break down user stories"
        - Automated planning: "intelligent sprint planning", "AI-assisted task assignment"
        
        REQUIRED PARAMETERS:
        - sprint: Sprint name or identifier (e.g., "Sprint 2026-01-20")
        - start_date: Sprint start date (ISO format: YYYY-MM-DD)
        - end_date: Sprint end date (ISO format: YYYY-MM-DD)
        
        OPTIONAL PARAMETERS:
        - balance: Enable intelligent workload balancing (default: True)
        - use_llm: Use LLM for advanced task breakdown and analysis (default: True)
        
        OUTPUT:
        Comprehensive sprint plan CSV file containing task breakdown with descriptions, effort estimates, assigned developers, skill requirements (FE/BE), capacity utilization percentages per developer, workload balance analysis, and recommended adjustments. Includes visual capacity charts and utilization warnings for overallocated or underutilized team members.""",
        "priority": 10,
        "category": "planning",
        "requires_ui_form": True,  # This skill requires UI form input
        "required_args": ["sprint", "start_date", "end_date"],
        "optional_args": {
            "balance": "bool - Enable workload balancing (default: True)",
            "use_llm": "bool - Use LLM for intelligent task breakdown (default: True)",
        },
        "returns": "Sprint plan CSV with task assignments, capacity utilization, and breakdown",
        "canonical_prompts": [
            "generate sprint plan",
            "create sprint plan",
            "sprint planning",
            "plan the sprint",
            "assign developers to tasks",
            "task assignment",
            "sprint capacity planning",
            "who should work on what",
            "plan next sprint",
            "create task breakdown",
            # NEW: Planning and organization phrases
            "help organize the sprint",
            "create roadmap for iteration",
            "build sprint schedule with assignments",
            "generate sprint work allocation",
        ],
        "handler": "agents.pm_skill_agent.skills._run_sprint_plan",
    },
    {
        "id": "backlog_triaging",
        "display_name": "Backlog Health Analysis",
        "description": """Comprehensive backlog health assessment and triaging system that analyzes product backlog quality, generates detailed health reports with AI-powered effort estimates, identifies thin backlogs, and provides actionable recommendations to ensure continuous delivery velocity and sprint planning readiness.
        
        KEY CAPABILITIES:
        - Automatic backlog health analysis by team/area path without requiring UI interaction
        - Generate executive summary reports with backlog runway, velocity trends, and status indicators
        - Calculate backlog runway in sprints based on team velocity and available story points
        - Identify thin backlogs (< 2 sprints worth) requiring immediate grooming attention
        - List all User Stories in backlog with Effort 3P values, states, and assignments
        - AI-powered effort estimation using GPT-4o-mini for items missing Effort 3P field
        - Smart estimation based on title, description analysis, and work complexity
        - Provide health status (HEALTHY/WARNING/THIN) with visual indicators
        - Generate actionable recommendations for backlog improvement and grooming sessions
        - Track velocity trends (Stable/Increasing/Decreasing) from last 3 sprints
        - Support team-specific or area path-specific backlog analysis
        
        INTELLIGENT QUERY HANDLING:
        - When user asks for backlog health report: Execute analysis automatically without showing UI
        - Extract team name from query (e.g., "XOPS 25", "XOPS Bugs Enhancement")
        - Fetch backlog User Stories filtered by team area path
        - Calculate metrics and generate formatted report immediately
        - Only show UI form if user requests assignment planning or manual configuration
        
        DATA SOURCES:
        - Azure DevOps User Stories in New, Approved, or Proposed states (backlog items)
        - Custom.Effort3P field for 3-point effort estimation
        - Team area paths for scoped filtering (e.g., "Global Management\\WTT Development\\XOPS 25")
        - Historical sprint velocity from completed iterations (default: last 3 sprints)
        - Work item titles, descriptions, acceptance criteria for AI estimation context
        - Team capacity and iteration data for runway calculations
        
        AI EFFORT ESTIMATION RULES:
        For items missing Custom.Effort3P, AI estimates using these size categories:
        - Very Small (1-10 points): Simple bug fixes, minor UI tweaks, small config changes, documentation updates
        - Small (20-40 points): Straightforward features, basic CRUD operations, simple enhancements, standard form additions
        - Medium (40-60 points): Moderate complexity features, integration work, API development, multi-screen workflows
        - Large (60-100 points): Complex features requiring multiple components, significant refactoring, cross-team coordination
        - Very Large (100+ points): Major features or epics, architectural changes, multi-iteration work, major system redesigns
        
        HEALTH ASSESSMENT CRITERIA:
        - HEALTHY: >= 3 sprints of work (Total Effort / Velocity >= 3.0)
        - WARNING: 2-3 sprints of work (good but approaching thin threshold)
        - THIN: < 2 sprints of work (critical - immediate grooming needed)
        - Backlog Runway = Total Effort ÷ Team Velocity (in sprints)
        - Velocity Trend = Compare last 3 sprints' completed effort
        
        USE CASES:
        - Health reports: "show backlog health of XOPS 25", "backlog health report"
        - Team analysis: "is backlog healthy for XOPS Bugs", "check backlog status"
        - Grooming triggers: "thin backlog warning", "do we have enough backlog"
        - Planning prep: "backlog runway", "refined items count", "backlog items"
        - Effort estimation: "estimate missing effort", "AI estimate backlog"
        
        QUERY DETECTION LOGIC:
        - If query contains team/area identifier (e.g., "XOPS 25"): Auto-execute with team filter
        - If query is simple health check: Auto-execute for default team
        - If query requests assignment/planning: Show UI form for configuration
        - Extract team from patterns: "backlog health of X", "backlog for team Y", "X backlog status"
        
        OUTPUT FORMAT:
        Rich markdown report containing:
        - **Status Badge**: HEALTHY (green), WARNING (yellow), or THIN (red) with emoji indicators
        - **Executive Summary**: Backlog runway status, critical warnings, immediate action needs
        - **Key Metrics**: Total Items count, Total Effort (story points), Team Velocity, Backlog Runway (sprints)
        - **Velocity Analysis**: Average velocity over last 3 sprints, trend direction, data source details
        - **Recommendations**: Immediate brainstorming sessions, grooming needs, refinement targets, process improvements
        - **Detailed Item List**: Table with Work Item ID, Title, State, Effort 3P (with AI-estimated indicator)
        - **Visual Indicators**: Priority icons, AI estimation markers, status badges for quick scanning""",
        "priority": 8,
        "category": "analysis",
        "requires_ui_form": False,  # Can execute directly for health queries
        "required_args": [],
        "optional_args": {
            "team": "str - Team name (e.g., 'XOPS 25', 'XOPS Bugs Enhancement')",
            "area_path": "str - Azure DevOps area path for filtering",
            "project": "str - ADO project name (default: FracPro-OPS)",
            "effort_field": "str - Custom effort field (default: Custom.Effort3P)",
            "velocity": "float - Team velocity override (default: auto-calculated from last 3 sprints)",
        },
        "returns": "Comprehensive backlog health report with metrics, AI estimates, and actionable recommendations",
        "canonical_prompts": [
            "backlog health",
            "show backlog health",
            "backlog status",
            "is backlog healthy",
            "check backlog",
            "backlog health report",
            "thin backlog",
            "backlog runway",
            "refined items",
            "do we have enough backlog",
            "backlog items",
            "list backlog items",
            "backlog health of XOPS 25",
            "backlog health of XOPS Bugs",
            "show backlog for team",
            "backlog health for area",
            # NEW: Team-specific and context-aware phrases
            "XOPS 25 backlog",
            "XOPS Bugs backlog health",
            "check XOPS backlog status",
            "is backlog thin",
            "grooming status",
        ],
        "handler": "agents.pm_skill_agent.skills._run_backlog_triaging",
    },
    {
        "id": "list_area_paths",
        "display_name": "List Area Paths",
        "description": """Project structure discovery tool that retrieves and displays all area paths within an Azure DevOps project, with optional team-based filtering, enabling users to understand organizational hierarchies, team boundaries, and component classifications for effective filtering and reporting.
        
        KEY CAPABILITIES:
        - Retrieve complete list of all area paths configured in an Azure DevOps project
        - Filter area paths by specific team (new capability)
        - Display hierarchical area path structures showing parent-child relationships
        - Identify team ownership and responsibility boundaries through area path assignments
        - Enable informed filtering for work item queries, reports, and analytics
        - Support multi-level area path hierarchies (e.g., ProjectName\\TeamA\\Component1\\SubComponent)
        - Provide area path metadata including creation dates and ownership information
        - Help users navigate complex project structures with multiple teams and components
        - Facilitate accurate area path selection for bug reporting, feature planning, and capacity management
        
        DATA SOURCES:
        - Azure DevOps Classification Nodes API for area path hierarchies
        - Project configuration and team settings
        - Area path assignments and team associations
        
        TEAM FILTERING:
        - Get paths for specific team: "area paths for team XOPS"
        - Navigate team structure: "show paths for Global FP"
        - Component discovery: "list paths for team Backend"
        - Returns only paths under the specified team's area
        
        USE CASES:
        - Discovery: "list area paths", "show area paths", "get area paths"
        - Team filtering: "area paths for team XOPS", "paths for Global FP", "show team areas"
        - Navigation: "what areas exist", "show me all areas", "project areas"
        - Structure understanding: "list areas", "area paths", "team areas"
        - Configuration assistance: "available area paths", "which areas can I use"
        - Planning support: "what components are in the project", "team structure"
        
        OPTIONAL PARAMETERS:
        - project: Azure DevOps project name (uses default project if not specified)
        - team: Optional team name to filter area paths (returns only paths under this team)
        
        OUTPUT:
        Complete hierarchical list of area paths in the project, formatted as full path strings (e.g., "ProjectName\\Team\\Component"), with indentation or tree structure to show parent-child relationships, enabling easy understanding of project organization and team boundaries. When team parameter provided, returns filtered list of paths under that team only.""",
        "priority": 11,
        "category": "discovery",
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "team": "str - Optional team name to filter area paths",
        },
        "returns": "List of area paths in the project",
        "canonical_prompts": [
            "list area paths",
            "show area paths",
            "get area paths",
            "area paths",
            "project areas",
            "what areas exist",
            "show me all areas",
            "list areas",
        ],
        "handler": "agents.pm_agent.handlers.handle_list_area_paths",
    },
    {
        "id": "billing_deviation",
        "display_name": "Billing Deviation Report",
        "description": """Advanced billing and effort tracking system that compares actual logged hours against target budgets to identify effort variances, cost overruns, and billing discrepancies by area path and team member. This skill provides financial visibility and budget management for project execution.
        
        KEY CAPABILITIES:
        - Track and compare actual logged hours versus target/budgeted hours for sprints and iterations
        - Calculate billing deviations as percentage variance from target (over/under budget)
        - Analyze effort distribution by area path to identify which components consume most resources
        - Break down hours by individual user/developer for granular cost allocation
        - Identify projects or features that are over-budget or under-budget with deviation thresholds
        - Generate comprehensive billing reports with actual vs target comparisons and variance analysis
        - Support monthly, sprint-based, or custom date range billing periods
        - Highlight deviations exceeding configurable threshold percentages (default 20%)
        - Send automated billing deviation alerts via email to finance and project stakeholders
        - Filter billing data by area paths for department or component-specific analysis
        - Provide trend analysis showing billing patterns over time
        
        TOOL BREAKDOWN AVAILABLE:
        This skill now supports granular tool breakdown for LLM orchestration. The following 8 focused tools enable step-by-step workflow with user interaction:
        1. parse_billing_query - Extract area path and month from query
        2. prompt_for_target_hours - Determine if user input needed or use default 4000
        3. fetch_work_items_by_billing_date - Get closed items by Estimated Billing Date
        4. get_area_paths_for_month - List available areas for validation
        5. calculate_billing_deviation - Compute deviation (Target - Actual)
        6. generate_billing_summary_text - Format chat display summary
        7. generate_detailed_billing_report - Create HTML/CSV reports
        8. send_billing_report_email - Email with attachments and validation
        
        The LLM can orchestrate these tools to ask clarifying questions, show intermediate results, and handle errors granularly instead of running one monolithic function.
        
        REQUIRES UI FORM:
        For streamlit UI flow, this skill requires interactive form input for billing configuration including iteration scope, area paths, target hours, and threshold settings. The tool breakdown approach allows the LLM to prompt for these inputs conversationally.
        
        DATA SOURCES:
        - Azure DevOps completed work hours from work item tracking
        - Time log entries and effort actuals recorded by developers
        - Work item completion data with associated hour estimates
        - Area path classifications for cost center allocation
        - Iteration and sprint boundaries for time-based filtering
        - Budget targets and planned effort configurations
        
        DEVIATION CALCULATIONS:
        - Actual hours = Sum of all completed work logged in time tracking
        - Target hours = Budgeted or planned hours for the scope/period
        - Deviation % = ((Actual - Target) / Target) × 100
        - Over budget = Positive deviation percentage
        - Under budget = Negative deviation percentage
        - Deviation threshold = Configurable alert level (default 20%)
        
        USE CASES:
        - Budget tracking: "billing deviation", "billing deviation report", "show billing deviation"
        - Effort analysis: "effort deviation", "effort variance", "actual vs target hours"
        - Financial monitoring: "billing status", "billing off-track", "over budget", "under budget"
        - Cost control: "hours deviation", "billing analysis", "effort tracking"
        - Area reporting: "show billing by area", "billing summary", "billing for component"
        - Sprint billing: "billing report for sprint", "billing for current month"
        - Utilization review: "completed hours", "work hours logged", "who logged most hours"
        - Comparison analysis: "compare actual and target", "budget variance"
        
        OPTIONAL PARAMETERS:
        - iteration: Iteration path for sprint-scoped billing (default: @CurrentIteration)
        - area_paths: List of area paths to filter billing analysis
        - target_hours: Target/budgeted hours for comparison baseline
        - threshold: Deviation percentage threshold to highlight (default: 20%)
        - recipients: Email distribution list for billing reports
        - send_email: Enable automated email delivery of reports
        - filter_current_month: Filter to current calendar month only
        - project: Azure DevOps project scope
        
        OUTPUT:
        Detailed billing deviation report with tables showing actual hours logged, target hours budgeted, deviation amounts and percentages by area path and developer, highlighting of variances exceeding threshold, trend charts for billing over time, and actionable recommendations for budget adjustments. Reports delivered as HTML with embedded visualizations and automatically emailed to stakeholders.""",
        "priority": 12,
        "category": "reporting",
        "requires_ui_form": False,  # CHANGED: Dynamic calculation, no UI form needed
        "required_args": [],
        "optional_args": {
            "project": "str - ADO project name",
            "iteration": "str - Iteration path (default: @CurrentIteration)",
            "area_paths": "list[str] - Area paths to filter",
            "target_hours": "float - Target hours for comparison",
            "threshold": "float - Deviation % threshold to highlight (default: 20)",
            "recipients": "list[str] - Email recipients",
            "send_email": "bool - Whether to send email report",
            "filter_current_month": "bool - Filter to current month only",
        },
        "returns": "Billing deviation report with actual vs target comparison, deviation analysis",
        "canonical_prompts": [
            "billing deviation",
            "billing deviation report",
            "show billing deviation",
            "billing status",
            "effort deviation",
            "effort variance",
            "actual vs target hours",
            "billing hours",
            "billing off-track",
            "over budget",
            "under budget",
            "hours deviation",
            "billing analysis",
            "effort tracking",
            "show billing by area",
            "billing summary",
            "completed hours",
            "work hours logged",
            "billing report for sprint",
            "billing for current month",
            "who logged most hours",
            "compare actual and target",
            # NEW: Budget and cost-focused phrases
            "compare hours to budget",
            "spending more time than planned",
            "cost overruns in sprint",
            "variance between planned and actual effort",
            "billing budget tracking",
        ],
        "handler": "agents.pm_skill_agent.skills._run_billing_deviation",
    },
    {
        "id": "send_email",
        "display_name": "Send Email",
        "description": """General-purpose email communication system for sending formatted notifications, reports, and messages to team members and stakeholders with support for HTML content and file attachments.
        
        KEY CAPABILITIES:
        - Send emails with custom subject lines and body content
        - Support both plain text and HTML-formatted email bodies
        - Attach multiple files to emails (reports, CSV exports, images, PDFs)
        - Send to multiple recipients simultaneously via email list
        - Preserve formatting, tables, and charts in HTML email bodies
        - Integrate with other skills to automatically deliver generated reports
        - Support CC and BCC for broader notification distribution
        
        DATA SOURCES:
        - Email configuration from application settings (SMTP server, credentials)
        - User-provided content, recipients, and attachments
        - Generated reports and documents from other skills
        
        USE CASES:
        - Manual notifications: "send email", "email report", "send notification"
        - Team communication: "email to team", "notify via email"
        - Report delivery: "send report email", "email this to stakeholders"
        - Automated alerts: "send summary to team", "notify management"
        
        REQUIRED PARAMETERS:
        - recipients: List of email addresses or single email address
        - subject: Email subject line
        
        OPTIONAL PARAMETERS:
        - body: Email body content (plain text or HTML)
        - attachments: List of file paths to attach
        
        OUTPUT:
        Email send confirmation with delivery status, timestamp, and recipient list.""",
        "priority": 12,
        "category": "notification",
        "required_args": ["recipients", "subject"],
        "optional_args": {
            "body": "str - Email body (HTML or text)",
            "attachments": "list[str] - File paths to attach",
        },
        "returns": "Email send confirmation",
        "canonical_prompts": [
            "send email",
            "email report",
            "send notification",
            "email to team",
            "notify via email",
            "send report email",
            "email this to",
        ],
        "handler": "utilities.emailer.send_email",
    },
    {
        "id": "search_developers_by_skill",
        "display_name": "Search Developers by Skill",
        "description": """Advanced semantic developer search system using vector database technology to find team members with specific technical skills, programming language expertise, and framework experience based on proven code contributions and work history.
        
        KEY CAPABILITIES:
        - Semantic search for developers using natural language skill descriptions (e.g., "Angular TypeScript frontend developers")
        - Technology-specific filtering for precise skill matching (e.g., technology="angular", technology="java")
        - Return developers ranked by skill similarity and relevance scores
        - Provide evidence-based skill validation including commit counts, lines of code, and languages used
        - Show work item associations demonstrating domain expertise and project involvement
        - Support frontend, backend, and fullstack developer discovery
        - Match developers to complex multi-technology requirements
        - Identify subject matter experts for specific frameworks and tools
        
        DATA SOURCES:
        - Milvus vector database containing semantic embeddings of developer skills
        - Commit history with language statistics and framework usage
        - Work item associations showing project involvement
        - Code contribution metrics (commits, LOC, file changes)
        - Technology proficiency evidence from actual development work
        
        SEMANTIC MATCHING:
        - Converts skill queries to vector embeddings using advanced NLP models
        - Performs cosine similarity search against developer skill vectors
        - Returns top-k most relevant developers with similarity scores
        - Supports natural language queries like "who knows React and Redux"
        - Understands technology synonyms and related skills
        
        USE CASES:
        - Technology discovery: "who knows angular", "who knows react", "who knows java", "who knows python"
        - Developer search: "find angular developers", "find react developers", "list all java developers"
        - Role-based queries: "find frontend developers", "find backend developers", "list fullstack developers"
        - Expertise queries: "who can work on angular", "who is experienced in spring boot", "developer with java experience"
        - Team composition: "team expertise", "skill matrix", "developer knowledge base"
        - Assignment planning: "who worked on this feature", "who is familiar with this technology"
        
        OPTIONAL PARAMETERS:
        - skill_query: Natural language skill description (e.g., "React Redux TypeScript frontend")
        - technology: Specific technology filter (e.g., "angular", "java", "python")
        - top_k: Maximum number of developers to return
        - include_evidence: Include detailed commit counts, LOC, and work items (default: True)
        
        OUTPUT:
        Ranked list of developers with similarity scores, technology expertise evidence (commits, languages, LOC), work item associations, and detailed skill profiles demonstrating proven capabilities in requested technologies.""",
        "priority": 1,  # High priority for developer skill queries
        "category": "discovery",
        "required_args": [],
        "optional_args": {
            "skill_query": "str - Natural language description of required skills",
            "technology": "str - Specific technology to filter (e.g., 'angular', 'java')",
            "top_k": "int - Maximum number of developers to return",
            "include_evidence": "bool - Include commit counts, LOC, work items",
        },
        "returns": "List of developers with matching skills and evidence of expertise",
        "canonical_prompts": [
            # Who knows patterns
            "who knows angular",
            "who knows react",
            "who knows java",
            "who knows python",
            "who knows typescript",
            "who knows javascript",
            "who knows spring",
            "who knows django",
            "who knows dotnet",
            # Find/list developer patterns
            "find angular developers",
            "find react developers",
            "find java developers",
            "find python developers",
            "find frontend developers",
            "find backend developers",
            "find fullstack developers",
            "list angular developers",
            "list all angular developers",
            "list all java developers",
            "list all python developers",
            "list frontend developers",
            "list backend developers",
            "show angular developers",
            "show java developers",
            # Technology + "developers" patterns
            "angular developers",
            "react developers",
            "java developers",
            "python developers",
            "typescript developers",
            "frontend developers",
            "backend developers",
            "fullstack developers",
            # Expertise patterns
            "who can work on react",
            "who can work on angular",
            "who is familiar with java",
            "who is experienced in python",
            "who has experience with spring",
            "developer with angular experience",
            "developer with java experience",
            # NEW: Alternative phrasings from tests
            "which developers have experience with angular",
            "who is proficient in java",
            "team members who worked with react",
            "which engineers are skilled in python",
            "who on the team knows",
            # Team/skill queries
            "team expertise",
            "developer expertise",
            "who worked on this feature",
        ],
        "handler": "agents.pm_agent.developer_kb_handler.search_developers_by_skill",
    },
]

# =============================================================================
# COMBINED SKILL REGISTRY
# =============================================================================

# Merge all skills into a single list
SKILLS: List[Dict[str, Any]] = CORE_SKILLS + PM_AGENT_SKILLS

# In-memory cache for skill vectors
_skill_vectors_cache: Dict[str, List[float]] = {}
_cache_loaded = False
_fixed_skills_registered = False


def load_skills() -> List[Dict[str, Any]]:
    """Return a copy of all registered skills."""
    return SKILLS.copy()


def get_skill_by_id(skill_id: str) -> Optional[Dict[str, Any]]:
    """Get a skill by its ID."""
    for s in SKILLS:
        if s.get("id") == skill_id:
            return s.copy()
    return None


def get_skills_by_category(category: str) -> List[Dict[str, Any]]:
    """Get all skills in a specific category.
    
    Categories: analysis, notification, reporting, status, capacity, action, discovery, planning
    """
    return [s.copy() for s in SKILLS if s.get("category") == category]


def get_fixed_skills() -> List[Dict[str, Any]]:
    """Get only PM Agent fixed skills (deterministic, no LLM planning needed)."""
    return PM_AGENT_SKILLS.copy()


def get_skill_text_for_embedding(skill: Dict[str, Any]) -> str:
    """Combine skill fields into text for embedding."""
    parts: List[str] = [
        skill.get("display_name", ""),
        skill.get("description", ""),
    ]
    parts.extend(skill.get("canonical_prompts", []) or [])
    return " \n ".join([p for p in parts if p])


def match_skill_by_query(query: str, threshold: float = 0.5) -> Optional[Dict[str, Any]]:
    """Match a query to the most appropriate skill using keyword matching.
    
    This is a fast, deterministic matcher for common queries.
    For semantic matching, use the semantic_matcher module.
    
    Args:
        query: User's natural language query
        threshold: Minimum score (0-1) to consider a match
        
    Returns:
        Skill dict if matched, None otherwise
    """
    query_lower = query.lower().strip()
    query_words = set(query_lower.split())

    # Normalized word set to improve matching for plurals
    def _normalize_word(w: str) -> str:
        w = w.strip()
        if w.endswith('s') and len(w) > 3:
            return w[:-1]
        return w

    query_words_norm = {_normalize_word(w) for w in query_words}
    
    best_skill = None
    best_score = 0.0
    
    for skill in SKILLS:
        score = 0.0
        
        # Check canonical prompts
        for prompt in skill.get("canonical_prompts", []):
            prompt_lower = prompt.lower()
            
            # Exact match
            if prompt_lower == query_lower:
                score = 1.0
                break
            
            # Substring match
            if prompt_lower in query_lower or query_lower in prompt_lower:
                score = max(score, 0.8)
            
            # Word overlap
            prompt_words = set(prompt_lower.split())
            overlap = len(prompt_words & query_words)
            if overlap >= 2:
                word_score = overlap / max(len(prompt_words), len(query_words))
                score = max(score, word_score * 0.7)
        
        # Check skill name in query
        skill_name = skill.get("display_name", "").lower()
        if skill_name and skill_name in query_lower:
            score = max(score, 0.9)
        
        # Check skill name words overlap (include normalized/plural-insensitive matching)
        if skill_name:
            name_words = set(skill_name.split())
            name_words_norm = {_normalize_word(w) for w in name_words}
            # Direct overlap
            overlap = len(name_words & query_words)
            # Normalized overlap to catch plurals and minor variants
            norm_overlap = len(name_words_norm & query_words_norm)
            total_overlap = max(overlap, norm_overlap)
            if total_overlap >= 1:
                word_score = total_overlap / max(len(name_words), 1)
                score = max(score, word_score * 0.6)
        
        # Check skill id in query (e.g., "bug_areas_highlight")
        skill_id = skill.get("id", "").replace("_", " ").lower()
        if skill_id and skill_id in query_lower:
            score = max(score, 0.85)
        
        # Check description keywords
        description = skill.get("description", "").lower()
        desc_words = set(description.split())
        # Only match on significant words (length >= 4)
        significant_query = {w for w in query_words if len(w) >= 4}
        significant_desc = {w for w in desc_words if len(w) >= 4}
        desc_overlap = len(significant_query & significant_desc)
        if desc_overlap >= 2:
            desc_score = desc_overlap / max(len(significant_query), 1)
            score = max(score, desc_score * 0.5)
        
        # Check use_cases if available
        use_cases = skill.get("use_cases", [])
        for use_case in use_cases:
            use_case_lower = use_case.lower()
            if query_lower in use_case_lower or use_case_lower in query_lower:
                score = max(score, 0.75)
                break
            # Word overlap with use case
            use_case_words = set(use_case_lower.split())
            overlap = len(query_words & use_case_words)
            if overlap >= 2:
                score = max(score, 0.55)
        
        if score > best_score:
            best_score = score
            best_skill = skill
    
    if best_score >= threshold:
        return best_skill.copy() if best_skill else None
    
    return None


def semantic_match_query_to_skill(
    query: str, 
    confident_threshold: float = 0.65,
    tentative_threshold: float = 0.45
) -> Tuple[Optional[str], float, str, bool]:
    """
    Match a query to the most appropriate skill using semantic similarity.
    
    This combines embedding-based matching with heuristic boosts for
    keyword matches and use case matching.
    
    Args:
        query: User's natural language query
        confident_threshold: Score above which we're confident (0.65)
        tentative_threshold: Score above which we have a tentative match (0.45)
        
    Returns:
        Tuple of (skill_id, confidence_score, confidence_level, requires_ui_form)
        confidence_level is one of: "high", "medium", "low", "none"
        requires_ui_form is True if skill needs a UI form for input
    """
    if not query or len(query.strip()) < 3:
        return None, 0.0, "none"
    
    query_lower = query.lower().strip()
    query_words = set(query_lower.split())
    
    # Try to use embeddings first
    try:
        from utilities.semantic_matcher import get_embedding_fn, cosine_similarity
        
        embed_fn, embed_type = get_embedding_fn()
        skill_vectors = get_skill_vectors()
        
        # Embed the query
        query_vec = embed_fn([query])[0]
        
        best_skill_id = None
        best_score = 0.0
        
        for skill in SKILLS:
            skill_id = skill.get("id")
            if not skill_id:
                continue
            
            # Skip deprecated skills
            if skill.get("deprecated", False):
                continue
            
            # Get or compute skill embedding
            skill_vec = skill_vectors.get(skill_id)
            if not skill_vec:
                text = get_skill_text_for_embedding(skill)
                try:
                    skill_vec = embed_fn([text])[0]
                    set_skill_vector(skill_id, skill_vec)
                except Exception:
                    continue
            
            # Compute cosine similarity
            similarity = cosine_similarity(query_vec, skill_vec)
            
            # Apply heuristic boosts
            boost = 0.0
            
            # Boost for canonical prompt matches
            for prompt in skill.get("canonical_prompts", []):
                prompt_lower = prompt.lower()
                if prompt_lower in query_lower or query_lower in prompt_lower:
                    boost = max(boost, 0.15)
                    break
                # Significant word overlap
                prompt_words = set(prompt_lower.split())
                overlap = len(prompt_words & query_words)
                if overlap >= 2:
                    overlap_boost = min(0.10, overlap * 0.03)
                    boost = max(boost, overlap_boost)
            
            # Boost for skill name in query
            skill_name = skill.get("display_name", "").lower()
            if skill_name and skill_name in query_lower:
                boost = max(boost, 0.12)
            
            # Boost for use_cases match (from PM Agent tool registry)
            use_cases = skill.get("use_cases", [])
            for use_case in use_cases:
                if use_case.lower() in query_lower:
                    boost = max(boost, 0.10)
                    break
            
            final_score = min(1.0, similarity + boost)
            
            if final_score > best_score:
                best_score = final_score
                best_skill_id = skill_id
        
        # Determine confidence level
        if best_score >= confident_threshold:
            # Check if skill requires UI form
            best_skill = next((s for s in SKILLS if s.get("id") == best_skill_id), None)
            requires_ui = best_skill.get("requires_ui_form", False) if best_skill else False
            return best_skill_id, best_score, "high", requires_ui
        elif best_score >= tentative_threshold:
            best_skill = next((s for s in SKILLS if s.get("id") == best_skill_id), None)
            requires_ui = best_skill.get("requires_ui_form", False) if best_skill else False
            return best_skill_id, best_score, "medium", requires_ui
        else:
            return None, best_score, "low", False
            
    except Exception as e:
        logger.warning(f"Semantic matching failed, falling back to keyword matching: {e}")
        # Fallback to keyword matching
        skill = match_skill_by_query(query, threshold=0.5)
        if skill:
            requires_ui = skill.get("requires_ui_form", False)
            return skill.get("id"), 0.6, "medium", requires_ui
        return None, 0.0, "none", False


def get_skill_with_tools(skill_id: str) -> Optional[Dict[str, Any]]:
    """Get a skill along with its tool mappings from SKILL_TO_TOOLS_MAP.
    
    Args:
        skill_id: The skill identifier
        
    Returns:
        Dict with skill metadata and tool mappings, or None if not found
    """
    skill = get_skill_by_id(skill_id)
    if not skill:
        return None
    
    # Try to get tool mappings
    try:
        from agents.pm_agent.tool_registry import SKILL_TO_TOOLS_MAP
        tool_mapping = SKILL_TO_TOOLS_MAP.get(skill_id, {})
        skill["primary_tools"] = tool_mapping.get("primary_tools", [])
        skill["supporting_tools"] = tool_mapping.get("supporting_tools", [])
        skill["execution_script"] = tool_mapping.get("execution_script")
    except ImportError:
        pass
    
    return skill


def get_skill_prompt_context() -> str:
    """Generate a formatted context string for LLM prompts.
    
    This creates a concise summary of available skills that can be
    included in LLM system prompts for tool selection.
    
    Returns:
        Formatted string describing available skills
    """
    lines = ["Available PM Agent Skills:"]
    
    for skill in SKILLS:
        name = skill.get("display_name", skill.get("id", "Unknown"))
        desc = skill.get("description", "")[:100]
        required = skill.get("required_args", [])
        
        if required:
            lines.append(f"- {name}: {desc} (requires: {', '.join(required)})")
        else:
            lines.append(f"- {name}: {desc}")
    
    return "\n".join(lines)


def get_skill_discovery_info() -> Dict[str, Dict[str, Any]]:
    """Get all skills formatted for tool discovery by the LLM planner.
    
    Returns a dict keyed by skill_id with metadata for each skill.
    This is the primary interface for chatbot tool discovery.
    """
    discovery = {}
    
    for skill in SKILLS:
        skill_id = skill.get("id")
        if not skill_id:
            continue
        
        discovery[skill_id] = {
            "name": skill.get("display_name", skill_id),
            "description": skill.get("description", ""),
            "category": skill.get("category", "general"),
            "required_args": skill.get("required_args", []),
            "optional_args": skill.get("optional_args", {}),
            "returns": skill.get("returns", ""),
            "use_cases": skill.get("canonical_prompts", [])[:10],  # Top 10 use cases
            "handler": skill.get("handler", ""),
        }
    
    return discovery


def validate_skill_params(skill_id: str, params: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate parameters for a skill.
    
    Args:
        skill_id: ID of the skill
        params: Parameters to validate
        
    Returns:
        Tuple of (is_valid, list of missing required params)
    """
    skill = get_skill_by_id(skill_id)
    if not skill:
        return False, [f"Unknown skill: {skill_id}"]
    
    required = skill.get("required_args", [])
    missing = [arg for arg in required if arg not in params or params[arg] is None]
    
    return len(missing) == 0, missing


# =============================================================================
# SKILL VECTOR CACHING (for semantic matching)
# =============================================================================

def _compute_skills_hash() -> str:
    """Compute hash of skills definitions for cache invalidation."""
    content = json.dumps(SKILLS, sort_keys=True)
    return hashlib.md5(content.encode()).hexdigest()


def _compute_skill_vectors() -> Dict[str, List[float]]:
    """Compute embeddings for all skills using available embedding fn."""
    vectors: Dict[str, List[float]] = {}
    try:
        from utilities.semantic_matcher import get_embedding_fn
        embed_fn, _ = get_embedding_fn()
        texts = []
        ids = []
        for skill in SKILLS:
            sid = skill.get("id")
            if not sid:
                continue
            txt = get_skill_text_for_embedding(skill)
            texts.append(txt)
            ids.append(sid)

        if texts:
            emb_list = embed_fn(texts)
            for sid, emb in zip(ids, emb_list):
                vectors[sid] = emb
    except Exception as e:
        logger.warning("Failed to compute skill embeddings: %s", e)
    return vectors


def get_skill_vectors() -> Dict[str, List[float]]:
    """Return cached skill vectors; compute and cache to disk if missing."""
    global _skill_vectors_cache, _cache_loaded

    cache_file = CACHE_DIR / "skill_vectors.json"
    skills_hash = _compute_skills_hash()

    if _cache_loaded and _skill_vectors_cache:
        return _skill_vectors_cache

    if cache_file.exists():
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("hash") == skills_hash:
                _skill_vectors_cache = cached.get("vectors", {})
                _cache_loaded = True
                return _skill_vectors_cache
        except Exception:
            logger.debug("Skill vectors cache load failed, will recompute")

    # compute
    _skill_vectors_cache = _compute_skill_vectors()
    _cache_loaded = True

    try:
        with cache_file.open("w", encoding="utf-8") as f:
            json.dump({"hash": skills_hash, "vectors": _skill_vectors_cache}, f)
    except Exception:
        logger.debug("Failed to write skill vectors cache")

    return _skill_vectors_cache


def set_skill_vector(skill_id: str, vector: List[float]) -> None:
    """Set/update a cached skill vector in memory and persist cache."""
    global _skill_vectors_cache, _cache_loaded
    _skill_vectors_cache[skill_id] = vector
    _cache_loaded = True
    try:
        cache_file = CACHE_DIR / "skill_vectors.json"
        skills_hash = _compute_skills_hash()
        with cache_file.open("w", encoding="utf-8") as f:
            json.dump({"hash": skills_hash, "vectors": _skill_vectors_cache}, f)
    except Exception:
        logger.debug("Failed to persist updated skill vector")


def clear_cache() -> None:
    """Clear in-memory and on-disk skill vectors cache."""
    global _skill_vectors_cache, _cache_loaded
    _skill_vectors_cache = {}
    _cache_loaded = False
    cache_file = CACHE_DIR / "skill_vectors.json"
    try:
        if cache_file.exists():
            cache_file.unlink()
    except Exception:
        logger.debug("Failed to remove skill vectors cache file")


def register_skill(skill: Dict[str, Any]) -> None:
    """Register or replace a skill at runtime and clear cache."""
    required = ["id", "display_name", "description"]
    for r in required:
        if r not in skill:
            raise ValueError(f"Skill missing required field: {r}")
    
    # Ensure canonical_prompts exists
    if "canonical_prompts" not in skill:
        skill["canonical_prompts"] = []
    
    # Replace if exists
    for i, s in enumerate(SKILLS):
        if s.get("id") == skill["id"]:
            SKILLS[i] = skill
            clear_cache()
            logger.info("Replaced skill: %s", skill["id"])
            return
    
    SKILLS.append(skill)
    clear_cache()
    logger.info("Registered skill: %s", skill["id"])


def sync_pm_agent_skills() -> None:
    """Sync skills from PM Agent tool registry.
    
    This ensures the skill registry stays in sync with the PM Agent's
    fixed skill definitions, merging any updates.
    """
    global _fixed_skills_registered
    
    if _fixed_skills_registered:
        return
    
    try:
        from agents.pm_agent.tool_registry import PM_SKILL_REGISTRY
        
        for skill_name, meta in PM_SKILL_REGISTRY.items():
            # Check if already in our PM_AGENT_SKILLS
            existing = get_skill_by_id(skill_name)
            if existing:
                continue
            
            # Convert PM_SKILL_REGISTRY format to our format
            skill = {
                "id": skill_name,
                "display_name": meta.get("name", skill_name.replace("_", " ").title()),
                "description": meta.get("description", ""),
                "priority": 20,  # Lower priority than built-in skills
                "category": meta.get("category", "general"),
                "required_args": meta.get("required_args", []),
                "optional_args": meta.get("optional_args", {}),
                "returns": meta.get("returns", ""),
                "canonical_prompts": meta.get("use_cases", []),
                "handler": f"agents.pm_agent.handlers.handle_{skill_name}",
            }
            
            # Add examples as additional prompts
            for example in meta.get("examples", []):
                query = example.get("query", "")
                if query and query not in skill["canonical_prompts"]:
                    skill["canonical_prompts"].append(query)
            
            register_skill(skill)
            logger.debug("Synced PM Agent skill: %s", skill_name)
        
        _fixed_skills_registered = True
        logger.info("PM Agent skills synced to skill registry")
        
    except ImportError:
        logger.debug("PM Agent tool registry not available, skipping sync")
    except Exception as e:
        logger.warning("Failed to sync PM Agent skills: %s", e)


# Auto-sync on module load
try:
    sync_pm_agent_skills()
except Exception:
    pass


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "SKILLS",
    "CORE_SKILLS",
    "PM_AGENT_SKILLS",
    "load_skills",
    "get_skill_by_id",
    "get_skills_by_category",
    "get_fixed_skills",
    "get_skill_text_for_embedding",
    "match_skill_by_query",
    "semantic_match_query_to_skill",
    "get_skill_with_tools",
    "get_skill_prompt_context",
    "get_skill_discovery_info",
    "validate_skill_params",
    "get_skill_vectors",
    "set_skill_vector",
    "clear_cache",
    "register_skill",
    "sync_pm_agent_skills",
]
