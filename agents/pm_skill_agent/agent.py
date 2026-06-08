"""
PM Skills Agent - Main agent class for PM business logic.

This agent handles domain-specific PM skills using LangGraph workflows:
- Bug Areas Highlight (recurring bugs analysis)
- Overlooked Stories Reminder
- Iteration Reports
- Email notifications

Architecture:
- Orchestrator routes requests here based on skill patterns
- This agent uses LangGraph for workflow orchestration
- Data fetching is delegated to PM Agent (ADO Data Agent) when needed
"""

import os
import json
import logging
from typing import Dict, Any, Optional, AsyncIterable
from collections.abc import AsyncIterable as AsyncIterableABC
from dataclasses import dataclass
from datetime import datetime

from .skills import SkillRegistry, SkillResult, execute_skill
from .langgraph_workflow import execute_skill_via_langgraph, get_workflow

logger = logging.getLogger("pm_skill_agent")
logger.setLevel(logging.INFO)


@dataclass
class AgentResponse:
    """Response from PM Skills Agent."""
    success: bool
    skill_name: str
    result: Any
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class PMSkillAgent:
    """
    PM Skills Agent - executes PM business logic skills using LangGraph.
    
    Responsibilities:
    1. Execute PM fixed skills via LangGraph workflows
    2. Apply domain rules and SOPs
    3. Return structured results for synthesis
    
    NOT responsibilities:
    - LLM planning (handled by Orchestrator's planner)
    - ADO data fetching (delegated to PM Agent / ADO Data Agent)
    """
    
    AGENT_NAME = "pm_skill_agent"
    AGENT_VERSION = "2.0.0"  # Updated for LangGraph integration
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize PM Skills Agent.
        
        Args:
            config: Optional configuration dict with keys:
                - project: ADO project name (default from config)
                - team: ADO team name (default from config)
                - log_level: Logging level
                - use_langgraph: Whether to use LangGraph (default True)
        """
        from config import config as app_config
        self.config = config or {}
        self.project = self.config.get("project") or app_config.ado_project
        self.team = self.config.get("team") or app_config.ado_team
        self.skill_registry = SkillRegistry()
        self.use_langgraph = self.config.get("use_langgraph", True)
        
        # Set up logging
        log_level = self.config.get("log_level", "INFO")
        logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        
        # Initialize LangGraph workflow if enabled
        if self.use_langgraph:
            logger.info("Initializing LangGraph workflow...")
            self.workflow = get_workflow()
            logger.info(f"PM Skills Agent initialized with LangGraph (project={self.project}, team={self.team})")
        else:
            self.workflow = None
            logger.info(f"PM Skills Agent initialized without LangGraph (project={self.project}, team={self.team})")
    
    async def invoke(self, request: Dict[str, Any]) -> AsyncIterable[Dict[str, Any]]:
        """
        Execute a skill request and yield results.
        
        Routes through LangGraph if enabled, otherwise uses direct execution.
        
        Args:
            request: Request dict with keys:
                - skill: Skill name to execute (optional if query provided)
                - query: User query for skill matching (optional if skill provided)
                - params: Skill parameters dict
                - session_id: Optional session identifier
                
        Yields:
            {"is_task_complete": bool, "content": str/dict}
        """
        skill_name = request.get("skill")
        query = request.get("query")
        params = request.get("params", {})
        session_id = request.get("session_id", "default")
        parent_trace = request.get("parent_trace")  # Get parent trace from orchestrator
        
        # Trace propagation enforcement: Warn if parent_trace is missing
        if not parent_trace:
            logger.warning("PMSkillAgent invoked without parent_trace - this may indicate a bypass of canonical flow (UI → Controller → Orchestrator → Agent)")
        
        # Merge project/team context
        full_params = {
            "project": self.project,
            "team": self.team,
            **params
        }
        
        if not skill_name and not query:
            yield {
                "is_task_complete": True,
                "status": "FAILED",
                "content": {"success": False, "error": "No skill or query specified"},
                "error": "No skill or query specified"
            }
            return
        
        logger.info(f"[{session_id}] Processing request (skill={skill_name}, query={query})")
        
        try:
            # Route through LangGraph if enabled
            if self.use_langgraph and (query or skill_name):
                logger.info(f"[{session_id}] Using LangGraph workflow (skill={skill_name})")
                result = await execute_skill_via_langgraph(
                    query=query or f"execute {skill_name}",
                    params=full_params,
                    session_id=session_id,
                    skill_name=skill_name,  # Pass explicit skill name for direct routing
                    parent_trace=parent_trace  # Pass parent trace from orchestrator
                )
                
                # Determine status from result
                status = "SUCCESS"
                error = None
                if isinstance(result, dict):
                    if result.get("success") is False or result.get("error"):
                        status = "FAILED"
                        error = result.get("error")
                    elif result.get("needs_deep_analysis"):
                        status = "NEEDS_DEEP_ANALYSIS"
                
                yield {
                    "is_task_complete": True,
                    "status": status,
                    "content": result,
                    "error": error,
                    "deep_analysis_context": result.get("deep_analysis_context") if status == "NEEDS_DEEP_ANALYSIS" else None
                }
                return
            
            # Direct execution if skill name provided or LangGraph disabled
            if not skill_name:
                # Need to match skill from query
                from agents.pm_agent.skills import should_use_fixed_skill
                match = should_use_fixed_skill(query)
                if match:
                    skill_name = match.skill_name
                    # Merge extracted params
                    full_params = {**full_params, **match.extracted_params}
                else:
                    yield {
                        "is_task_complete": True,
                        "status": "NEEDS_DEEP_ANALYSIS",
                        "content": {
                            "success": False,
                            "requires_llm": True,
                            "message": "No fixed skill matched, needs LLM routing"
                        },
                        "deep_analysis_context": {
                            "reason": "no_skill_match",
                            "query": query,
                            "available_skills": list(self.get_available_skills().keys())
                        }
                    }
                    return
            
            logger.info(f"[{session_id}] Executing skill directly: {skill_name}")
            result = await self.execute_skill(skill_name, full_params)
            
            if result.success:
                logger.info(f"[{session_id}] Skill {skill_name} completed successfully")
                yield {
                    "is_task_complete": True,
                    "status": "SUCCESS",
                    "content": {
                        "success": True,
                        "skill": skill_name,
                        "result": result.result,
                        "metadata": result.metadata
                    }
                }
            else:
                logger.warning(f"[{session_id}] Skill {skill_name} failed: {result.error}")
                yield {
                    "is_task_complete": True,
                    "status": "FAILED",
                    "content": {
                        "success": False,
                        "skill": skill_name,
                        "error": result.error
                    },
                    "error": result.error
                }
                
        except Exception as e:
            logger.exception(f"[{session_id}] Error processing request: {e}")
            yield {
                "is_task_complete": True,
                "status": "FAILED",
                "content": {
                    "success": False,
                    "error": str(e)
                },
                "error": str(e)
            }
    
    async def execute_skill(self, skill_name: str, params: Dict[str, Any]) -> SkillResult:
        """
        Execute a specific skill by name.
        
        Args:
            skill_name: Name of the skill to execute
            params: Parameters for the skill
            
        Returns:
            SkillResult with success status and result/error
        """
        # Merge default context
        full_params = {
            "project": self.project,
            "team": self.team,
            **params
        }
        
        return await execute_skill(skill_name, full_params)
    
    def get_available_skills(self) -> Dict[str, Dict[str, Any]]:
        """Get list of available skills with their metadata."""
        return self.skill_registry.get_all_skills()
    
    def get_skill_info(self, skill_name: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific skill."""
        return self.skill_registry.get_skill(skill_name)
    
    async def health_check(self) -> Dict[str, Any]:
        """Return agent health status."""
        return {
            "agent": self.AGENT_NAME,
            "version": self.AGENT_VERSION,
            "status": "healthy",
            "project": self.project,
            "team": self.team,
            "skills_count": len(self.get_available_skills()),
            "timestamp": datetime.utcnow().isoformat()
        }


# Singleton instance
_agent_instance: Optional[PMSkillAgent] = None


def get_pm_skill_agent(config: Optional[Dict[str, Any]] = None) -> PMSkillAgent:
    """Get or create the PM Skills Agent singleton."""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = PMSkillAgent(config)
    return _agent_instance
