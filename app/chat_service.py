# -*- coding: utf-8 -*-
"""
Chat Service Module for PM Agent.

This module handles ALL business logic for the PM Agent chat interface:
- Chat routing and prompt handling
- MCP/PM-agent fallback logic
- Data loading helpers (skills, tasks, capacity, etc.)
- Subprocess execution for scripts
- Skill-based intent recognition
- UI state management

The Streamlit `st` object is passed in where needed to avoid import-time issues.
"""
import os
import re
import sys
import json
import asyncio
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from glob import glob
from typing import Optional, Dict, List, Any, Tuple
from uuid import uuid4

# Import httpx lazily in functions to avoid import-time dependency errors
import httpx
import requests
import pandas as pd
from a2a.client import A2ACardResolver

from utilities.a2a import agent_connector, agent_discovery
from utilities.emailer import send_wi_report

logger = logging.getLogger("pm_agent.chat_service")

# Project root
ROOT = Path(__file__).resolve().parents[1]

# Lazy langfuse client (optional). Initialize if available to avoid NameError
try:
    from utilities.langfuse_client import (
        get_langfuse_client, 
        create_trace,
        create_parent_trace,
            get_current_trace_id,
        set_current_trace, 
        create_span,
        finalize_span,
        finalize_trace
    )
    try:
        _langfuse_client = get_langfuse_client()
    except Exception:
        _langfuse_client = None
except Exception:
    _langfuse_client = None

# Import sprint responder handler
try:
    from utilities.sprint_responder import handle_sprint_query
except ImportError:
    handle_sprint_query = None

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _handle_pm_agent_skill(skill_id: str, prompt: str, intent: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle PM Agent fixed skills by routing to orchestrator.
    
    This routes fixed PM skills to the Orchestrator so they execute properly
    (and get traced under the controller/orchestrator trace). Uses asyncio.run
    to synchronously call the orchestrator in Streamlit / sync contexts.
    """
    try:
        # Create a small Langfuse span for the PM-skill invocation (child of controller trace)
        try:
            trace_parent = None
            if 'create_span' in globals():
                from utilities.langfuse_client import get_current_trace
                trace_parent = get_current_trace()
                span = create_span(
                    name="pm_skill_invoke",
                    input_data={"skill": skill_id, "prompt": prompt[:500]},
                    metadata={"skill": skill_id},
                    parent_trace=trace_parent
                )
            else:
                span = None
        except Exception:
            span = None

        # Route through controller (CANONICAL ENTRY POINT)
        from controller.chatbot_controller import get_controller
        controller = get_controller()
        
        # Generate session ID for skill invocation
        import uuid
        session_id = f"skill-session-{uuid.uuid4().hex[:12]}"
        
        try:
            result = controller.process_request(
                user_query=prompt,
                session_id=session_id,
                user_id="skill_invocation",
                project_id=os.getenv("ADO_PROJECT", "FracPro-OPS"),
                organization=os.getenv("ADO_ORG_NAME", "Stratagen"),
                turn_number=0
            )
            # Extract content from controller response
            result_content = result.get("content", str(result))
        except Exception as e:
            # If controller raised, finalize span with error and return a helpful message
            if span:
                try:
                    finalize_span(span, output={"error": str(e)}, status="error", level="ERROR")
                except Exception:
                    pass
            logger.exception("Error invoking controller for skill %s: %s", skill_id, e)
            return {
                "response": f"⚠️ Failed to run skill '{skill_id}': {e}",
                "skip_final_response": True,
                "download_path": None,
                "evidence_paths": []
            }

        # Finalize the invocation span with the result
        if span:
            try:
                finalize_span(span, output=result, status="success")
            except Exception:
                pass

        # Map orchestrator result into chat service response shape
        response_text = None
        if isinstance(result, dict):
            # prefer 'content' or 'response' keys used by orchestrator
            response_text = result.get("content") or result.get("response") or result.get("message")
            download_path = result.get("download_path")
            evidence_paths = result.get("evidence_paths", [])
        else:
            response_text = str(result)
            download_path = None
            evidence_paths = []

        return {
            "response": response_text or f"Executed {skill_id}",
            "skip_final_response": True,
            "download_path": download_path,
            "evidence_paths": evidence_paths
        }
    except Exception as outer_e:
        logger.exception("Unhandled error in _handle_pm_agent_skill: %s", outer_e)
        return {
            "response": f"⚠️ Error handling skill '{skill_id}': {outer_e}",
            "skip_final_response": True,
            "download_path": None,
            "evidence_paths": []
        }


# =============================================================================
# DATA LOADING HELPERS
# =============================================================================

def load_developer_skills() -> Optional[List[Dict]]:
    """Load developer skills from the knowledge base."""
    skills_file = ROOT / "data" / "developer_skills.json"
    if not skills_file.exists():
        return None
    try:
        return json.loads(skills_file.read_text(encoding='utf-8'))
    except Exception:
        return None


def load_functionality_docs() -> Optional[Dict]:
    """Load functionality documents from the knowledge base."""
    func_file = ROOT / "data" / "functionality_docs.json"
    if not func_file.exists():
        return None
    try:
        return json.loads(func_file.read_text(encoding='utf-8'))
    except Exception:
        return None


def load_upcoming_tasks() -> List[Dict]:
    """Load profiled upcoming tasks from wi_tags.json."""
    tags_file = ROOT / "data" / "wi_tags.json"
    if not tags_file.exists():
        return []
    try:
        data = json.loads(tags_file.read_text(encoding='utf-8'))
        return data.get("items", [])
    except Exception:
        return []


def save_complexity_override(wi_id: int, complexity: str) -> bool:
    """Save complexity override for a work item."""
    try:
        from utilities.task_profiler import update_complexity_override
        return update_complexity_override(wi_id, complexity, user="streamlit_user")
    except Exception as e:
        logger.error(f"Failed to save complexity override: {e}")
        return False


def load_assignment_suggestions() -> List[Dict]:
    """Load assignment suggestions from data/assignment_suggestions.json."""
    suggestions_file = ROOT / "data" / "assignment_suggestions.json"
    if not suggestions_file.exists():
        return []
    try:
        data = json.loads(suggestions_file.read_text(encoding='utf-8'))
        return data.get("suggestions", [])
    except Exception:
        return []


def load_sprint_plan_overrides() -> Dict:
    """Load latest sprint plan from data/sprint_plan_overrides.json."""
    overrides_file = ROOT / "data" / "sprint_plan_overrides.json"
    if overrides_file.exists():
        try:
            return json.loads(overrides_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def load_latest_sprint_plan_csv() -> Optional[Path]:
    """Find the latest sprint_plan CSV file."""
    pattern = str(ROOT / "outputs" / "sprint_plan_*.csv")
    files = sorted(glob(pattern), reverse=True)
    if files:
        return Path(files[0])
    return None


def load_latest_backlog_assignments_csv() -> Optional[Path]:
    """Find the latest backlog_assignments CSV file."""
    pattern = str(ROOT / "outputs" / "backlog_assignments_*.csv")
    files = sorted(glob(pattern), reverse=True)
    if files:
        return Path(files[0])
    return None


def load_latest_capacity_report() -> Optional[Dict]:
    """Find and load the latest capacity report JSON file."""
    import logging
    logger = logging.getLogger(__name__)
    
    pattern = str(ROOT / "outputs" / "capacity_report_*.json")
    files = sorted(glob(pattern), reverse=True)
    if files:
        try:
            file_path = Path(files[0])
            content = file_path.read_text(encoding='utf-8').strip()
            
            # Check if file is empty
            if not content:
                logger.warning(f"Capacity report file is empty: {file_path.name}")
                return None
            
            # Try to parse JSON
            data = json.loads(content)
            logger.info(f"Loaded capacity report: {file_path.name}")
            return data
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in capacity report {files[0]}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error loading capacity report {files[0]}: {e}")
            return None
    return None


def load_developer_availability() -> Dict:
    """Load developer availability factors from config file."""
    avail_file = ROOT / "data" / "developer_availability.json"
    if not avail_file.exists():
        return {}
    try:
        return json.loads(avail_file.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_developer_availability(availability: dict) -> None:
    """Save developer availability factors to config file."""
    avail_file = ROOT / "data" / "developer_availability.json"
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    with avail_file.open("w", encoding="utf-8") as f:
        json.dump(availability, f, indent=2)


def save_sprint_plan_overrides(overrides: Dict) -> None:
    """Save sprint plan overrides to file."""
    overrides_file = ROOT / "data" / "sprint_plan_overrides.json"
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    with overrides_file.open("w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2)


# =============================================================================
# SUBPROCESS HANDLERS
# =============================================================================

def run_knowledge_base_refresh(days: int = 30, max_wi: int = 50) -> Tuple[bool, str]:
    """Run ADO commit analysis and build knowledge base.
    
    Args:
        days: Number of days of history to analyze
        max_wi: Maximum work items to analyze (lower = faster, default 50 for ~2-3 min)
    """
    try:
        result = subprocess.run(
            [sys.executable, "scripts/build_knowledge_base.py", "--days", str(days), "--max-wi", str(max_wi)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=300  # 5 minutes should be enough for 50 work items
        )
        if result.returncode == 0:
            output_lines = result.stdout.split('\n')
            summary_lines = [l for l in output_lines if any(x in l.lower() for x in 
                ['loaded', 'processed', 'saved', 'developer', 'commit', 'total'])]
            summary = "\n".join(summary_lines[-10:]) if summary_lines else result.stdout[-1000:]
            return True, summary
        else:
            return False, result.stderr or result.stdout[-1000:]
    except Exception as e:
        return False, str(e)


def run_commit_analysis(days: int = 30, max_wi: int = 100) -> Tuple[bool, str]:
    """Analyze commits from ADO."""
    try:
        result = subprocess.run(
            [sys.executable, "scripts/analyze_commits.py", "--days", str(days), "--max-wi", str(max_wi)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=600
        )
        if result.returncode == 0:
            output_lines = result.stdout.split('\n')
            summary_lines = [l for l in output_lines if any(x in l.lower() for x in 
                ['analyzing', 'processed', 'developer', 'commit', 'saved', 'total'])]
            summary = "\n".join(summary_lines[-15:]) if summary_lines else result.stdout[-2000:]
            return True, summary
        else:
            return False, result.stderr or result.stdout[-1000:]
    except Exception as e:
        return False, str(e)


def run_profile_upcoming_tasks(area_paths: List[str], max_wis: int = 200, use_llm: bool = False) -> Tuple[bool, int, str]:
    """Profile upcoming tasks from ADO."""
    try:
        from utilities.task_profiler import profile_upcoming_tasks
        profiled = profile_upcoming_tasks(
            area_paths=area_paths,
            max_wis=max_wis,
            use_llm=use_llm,
        )
        if profiled:
            return True, len(profiled), f"Profiled {len(profiled)} tasks"
        else:
            return False, 0, "No upcoming tasks found matching criteria"
    except Exception as e:
        return False, 0, str(e)


def run_generate_suggestions(top_k: int = 3) -> Tuple[bool, int, str]:
    """Generate assignment suggestions."""
    try:
        from utilities.assignment import run_assignment_pipeline
        suggestions = run_assignment_pipeline(top_k=top_k)
        if suggestions:
            return True, len(suggestions), f"Generated suggestions for {len(suggestions)} work items"
        else:
            return False, 0, "No suggestions generated"
    except Exception as e:
        return False, 0, str(e)


def run_generate_sprint_plan(sprint_name: str, start_date: str, end_date: str) -> Tuple[bool, str]:
    """Generate sprint plan."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "generate_sprint_plan.py"),
                "--sprint", sprint_name,
                "--start", start_date,
                "--end", end_date,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT),
        )
        if result.returncode == 0:
            output_lines = result.stdout.split('\n')
            summary_lines = [l for l in output_lines if any(x in l for x in 
                ['Total tasks', 'Frontend', 'Backend', 'Profiled', 'Backlog', 
                 'Excluded', 'Sprint plan saved', 'Task breakdown'])]
            summary = "\n".join(summary_lines[-12:]) if summary_lines else "Sprint plan generated"
            return True, summary
        else:
            return False, result.stderr or result.stdout[-1500:]
    except Exception as e:
        return False, str(e)


def run_role_assignments(top_k: int = 3) -> Tuple[bool, int, str]:
    """Generate role-based (FE/BE) assignments."""
    try:
        from utilities.assignment import run_role_based_assignment_pipeline
        suggestions = run_role_based_assignment_pipeline(top_k=top_k)
        if suggestions:
            fe_count = sum(1 for s in suggestions if s.get('frontend_suggestions'))
            be_count = sum(1 for s in suggestions if s.get('backend_suggestions'))
            summary = f"Work items with FE suggestions: {fe_count}\nWork items with BE suggestions: {be_count}"
            return True, len(suggestions), summary
        else:
            return False, 0, "No suggestions generated"
    except Exception as e:
        return False, 0, str(e)


def run_backlog_assignments(sprint_name: str, sprint_start: str, sprint_end: str, sprint_days: int = 10) -> Tuple[bool, str]:
    """Assign backlog items to underutilized developers.
    
    Args:
        sprint_name: Name of the sprint
        sprint_start: Sprint start date (YYYY-MM-DD)
        sprint_end: Sprint end date (YYYY-MM-DD)
        sprint_days: Duration in days (default 10 for 80 hours total capacity)
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "assign_backlog_to_underutilized.py"),
                "--sprint", sprint_name,
                "--start", sprint_start,
                "--end", sprint_end,
                "--days", str(sprint_days),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT),
        )
        if result.returncode == 0:
            output_lines = result.stdout.split('\n')
            summary_lines = [l for l in output_lines if any(x in l for x in 
                ['Total backlog', 'Assigned:', 'Skipped', 'Cross-role', 
                 'Excluded', 'Available developers', 'frontend', 'backend', 'saved'])]
            summary = "\n".join(summary_lines[-15:]) if summary_lines else "Assignments generated"
            return True, summary
        else:
            return False, result.stderr or result.stdout[-1500:]
    except Exception as e:
        return False, str(e)


# =============================================================================
# SKILL-BASED INTENT HANDLING
# =============================================================================

def handle_skill_based_intent(prompt: str, session_state) -> Dict[str, Any]:
    """
    Handle skill-based intent recognition for chat messages.
    
    Returns a dict with:
        - handled: bool - whether the intent was handled
        - response: str or None - the response text if handled
        - skip_final_response: bool - whether to skip the final response rendering
        - download_path: str or None - path to downloadable file
        - evidence_paths: list - paths to evidence files
        - requires_confirmation: bool - whether action needs confirmation
        - pending_action: dict or None - action pending confirmation
    """
    result = {
        "handled": False,
        "response": None,
        "skip_final_response": False,
        "download_path": None,
        "evidence_paths": [],
        "requires_confirmation": False,
        "pending_action": None,
    }
    
    try:
        from utilities.semantic_matcher import classify_intent
        
        # CRITICAL: Check if this is an ADO data query that should bypass skill matching
        # These queries should go directly to PM Agent's LLM planner for proper tool execution
        prompt_lower = prompt.lower()
        
        # Patterns that indicate ADO data queries (NOT skill operations)
        # These should go to PM Agent for search_workitem / wit_get_work_items calls
        ado_data_patterns = [
            # Assigned to queries
            "assigned to" in prompt_lower,
            "assigned for" in prompt_lower,
            "assign to" in prompt_lower,
            # Work item listing with various verbs (list, show, get, find, give, fetch, pull, display)
            re.search(r"(list|show|get|give|find|fetch|pull|display|retrieve)\s+(me\s+)?(all\s+)?(\w+\s+)?(bugs?|stories?|tasks?|items?|work\s*items?)", prompt_lower) is not None,
            # Queries with state modifiers (active, open, closed, resolved, new, etc.)
            re.search(r"(active|open|closed|resolved|new|in\s*progress|done)\s+(bugs?|stories?|tasks?|items?)", prompt_lower) is not None,
            # Reverse pattern: bugs/items with state modifier
            re.search(r"(bugs?|stories?|tasks?|items?)\s+(that\s+are\s+)?(active|open|closed|resolved|new)", prompt_lower) is not None,
            # Sprint work item queries
            re.search(r"(in|for)\s+(the\s+)?(current|my|this)\s+sprint", prompt_lower) is not None,
            re.search(r"items?\s+(slowing|blocking|derailing)", prompt_lower) is not None,
            re.search(r"what.*(slowing|blocking|impeding)", prompt_lower) is not None,
            # Person-specific queries (assigned to someone or work for someone)
            re.search(r"(bugs?|items?|stories?|tasks?)\s+(for|of|assigned\s+to)\s+\w+", prompt_lower) is not None,
            re.search(r"(for|of|to|by)\s+\w+\s+\w*\s*(bugs?|items?|stories?|tasks?)?", prompt_lower) and any(x in prompt_lower for x in ["bug", "item", "story", "task", "work"]),
            # Work item by ID
            re.search(r"(work\s*item|bug|story|task)\s*#?\d{4,6}", prompt_lower) is not None,
            # General "all X" patterns without specific skill keywords
            re.search(r"(all|every)\s+(the\s+)?(bugs?|stories?|tasks?|items?|work\s*items?)", prompt_lower) is not None,
            # "What are" patterns for work items
            re.search(r"what\s+(are|is)\s+(the\s+)?(bugs?|stories?|tasks?|items?)", prompt_lower) is not None,
            # Count/how many queries
            re.search(r"(how\s+many|count|total)\s+(bugs?|stories?|tasks?|items?)", prompt_lower) is not None,
        ]
        
        if any(ado_data_patterns):
            logger.info(f"ADO data query detected, bypassing skill matching: '{prompt[:50]}...'")
            result["handled"] = False
            return result

        intent = classify_intent(prompt, confident_threshold=0.65, tentative_threshold=0.4)

        if intent.get("matched") and intent.get("confidence") in ("high", "medium"):
            skill_id = intent.get("skill_id")

            if skill_id == "sprint_tracking":
                result["handled"] = True
                
                # Create Langfuse trace for sprint_tracking skill
                session_id = session_state.get("session_id", f"sprint_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
                trace = create_trace(
                    name="sprint_tracking_skill",
                    input_data={"query": prompt, "skill": "sprint_tracking"},
                    metadata={
                        "source": "streamlit_chat",
                        "skill": "sprint_tracking",
                        "intent": intent.get("skill_id"),
                        "confidence": intent.get("score", 0)
                    },
                    session_id=session_id
                )
                
                if trace:
                    set_current_trace(trace)
                    logger.info(f"Created trace for sprint_tracking (session_id={session_id})")
                
                try:
                    # Create span for the handler execution
                    handler_span = create_span(
                        name="sprint_responder_handler",
                        input_data={"query": prompt, "intent": intent.get("skill_id")},
                        metadata={"handler": "handle_sprint_query"},
                        parent_trace=trace,
                        session_id=session_id
                    )
                    
                    sprint_response = handle_sprint_query(prompt, intent)
                    
                    if handler_span:
                        finalize_span(handler_span, output={"success": True, "response_length": len(str(sprint_response))}, status="success")
                    
                    response_parts = [sprint_response.get("summary_text", "")]

                    if intent.get("confidence") == "medium":
                        alternatives = intent.get("alternatives", [])
                        if alternatives:
                            alt_names = [a.get("skill", {}).get("display_name", a.get("skill_id", "")) for a in alternatives[:2]]
                            response_parts.append(f"\n\n_Did you mean Sprint Tracking? Other options: {', '.join(alt_names)}_")

                    result["response"] = "\n".join(response_parts)
                    result["skip_final_response"] = True
                    result["download_path"] = sprint_response.get("data", {}).get("download_path")
                    result["evidence_paths"] = sprint_response.get("evidence_paths", [])
                    result["requires_confirmation"] = sprint_response.get("requires_confirmation", False)
                    result["pending_action"] = sprint_response.get("pending_action")
                    
                    # Finalize trace
                    if trace:
                        finalize_trace(trace, output={"response_length": len(result["response"])}, status="success")
                        # Flush to Langfuse
                        client = get_langfuse_client()
                        if client:
                            client.flush()
                            logger.info("Flushed sprint_tracking trace to Langfuse")
                        
                except Exception as e:
                    logger.exception("Error in sprint_responder handler")
                    if handler_span:
                        finalize_span(handler_span, output={"error": str(e)}, status="error", level="ERROR")
                    if trace:
                        finalize_trace(trace, output={"error": str(e)}, status="error")
                    raise

                result["response"] = "\n".join(response_parts)
                result["skip_final_response"] = True
                result["download_path"] = sprint_response.get("data", {}).get("download_path")
                result["evidence_paths"] = sprint_response.get("evidence_paths", [])
                result["requires_confirmation"] = sprint_response.get("requires_confirmation", False)
                result["pending_action"] = sprint_response.get("pending_action")

                return result
            
            # Handle overlooked stories dynamically (similar to sprint_tracking)
            elif skill_id == "overlooked_stories":
                result["handled"] = True
                try:
                    from utilities.overlooked_responder import handle_overlooked_query
                    overlooked_response = handle_overlooked_query(prompt, intent)
                    
                    response_parts = [overlooked_response.get("summary_text", "")]
                    
                    if intent.get("confidence") == "medium":
                        alternatives = intent.get("alternatives", [])
                        if alternatives:
                            alt_names = [a.get("skill", {}).get("display_name", a.get("skill_id", "")) for a in alternatives[:2]]
                            response_parts.append(f"\n\n_Did you mean Overlooked Stories? Other options: {', '.join(alt_names)}_")
                    
                    result["response"] = "\n".join(response_parts)
                    result["skip_final_response"] = True
                    result["download_path"] = overlooked_response.get("data", {}).get("download_path")
                    result["evidence_paths"] = overlooked_response.get("evidence_paths", [])
                    
                    return result
                except ImportError as e:
                    import traceback
                    traceback.print_exc()
                    result["response"] = f"⚠️ Overlooked stories module not available: {e}"
                    result["skip_final_response"] = True
                    return result
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    result["response"] = f"⚠️ Error processing overlooked stories query: {e}"
                    result["skip_final_response"] = True
                    return result
            
            # Handle billing deviation dynamically - ROUTED TO PM SKILL AGENT
            elif skill_id == "billing_deviation":
                # NEW: Route to PM Skill Agent for dynamic parameter extraction
                # This skill now handles area path and target hours intelligently
                # No form needed - will ask for missing params conversationally
                result["handled"] = False  # Let controller handle via PM skill
                # Don't intercept - let it go through normal skill routing
                pass
            
            # Handle PM Agent fixed skills dynamically
            elif skill_id in ["bug_areas_highlight", "iteration_report",
                              "feedback_to_dev", "get_sprint_status", "get_backlog_health",
                              "get_capacity_forecast", "detect_recurring_bugs", "developer_skills"]:
                result["handled"] = True
                # Route to PM Skill Agent handler
                skill_response = _handle_pm_agent_skill(skill_id, prompt, intent)
                result["response"] = skill_response.get("response", f"Executing {skill_id}...")
                result["skip_final_response"] = skill_response.get("skip_final_response", True)
                result["download_path"] = skill_response.get("download_path")
                result["evidence_paths"] = skill_response.get("evidence_paths", [])
                return result

        # Handle confirmation for pending actions (for skills that need confirmation)
        if prompt.lower().strip() in ("confirm", "yes", "proceed"):
            pending = session_state.get("pending_skill_action")
            if pending:
                result["handled"] = True
                # Import execute_action only when needed for confirmation
                from utilities.sprint_responder import execute_action
                action_result = execute_action(pending, confirmed=True)

                if action_result.get("success"):
                    result["response"] = f"[OK] {action_result.get('message', 'Action completed')}"
                else:
                    result["response"] = f"[ERROR] {action_result.get('message', 'Action failed')}"

                result["skip_final_response"] = True
                session_state["pending_skill_action"] = None
                return result

        if prompt.lower().strip() in ("cancel", "no", "abort"):
            pending = session_state.get("pending_skill_action")
            if pending:
                result["handled"] = True
                result["response"] = "[CANCELLED] Action cancelled."
                result["skip_final_response"] = True
                session_state["pending_skill_action"] = None
                return result

    except ImportError:
        pass
    except Exception:
        import traceback
        traceback.print_exc()

    return result


# =============================================================================
# DATA PREPARATION FOR UI
# =============================================================================

def get_developer_skills_df() -> Optional[pd.DataFrame]:
    """Get developer skills as a DataFrame for display."""
    skills = load_developer_skills()
    if not skills:
        return None
    
    dev_data = []
    for dev in skills:
        dev_data.append({
            "Developer": dev.get("developer", "Unknown"),
            "Primary Languages": ", ".join(dev.get("languages", [])[:5]),
            "Commits": dev.get("commits", 0),
            "LOC Added": dev.get("loc_added", 0),
            "WIs Touched": dev.get("wi_count", 0),
        })
    
    if dev_data:
        df = pd.DataFrame(dev_data)
        return df.sort_values("Commits", ascending=False)
    return None


def get_upcoming_tasks_df(tasks: List[Dict], complexity_filter: List[str], 
                          skill_filter: List[str], search_text: str) -> Tuple[pd.DataFrame, int]:
    """Filter and prepare upcoming tasks for display."""
    filtered = tasks
    
    if complexity_filter:
        filtered = [t for t in filtered if t.get("complexity") in complexity_filter]
    if skill_filter:
        filtered = [t for t in filtered if any(
            s.get("skill") in skill_filter for s in t.get("inferred_skills", [])
        )]
    if search_text:
        filtered = [t for t in filtered if search_text.lower() in t.get("title", "").lower()]
    
    display_data = []
    for t in filtered:
        skills_str = ", ".join(s.get("skill", "") for s in t.get("inferred_skills", [])[:3])
        display_data.append({
            "ID": t.get("id"),
            "Title": t.get("title", "")[:60] + ("..." if len(t.get("title", "")) > 60 else ""),
            "Area": t.get("area_info", {}).get("module", t.get("area_path", "")[-30:]),
            "Skills": skills_str,
            "Complexity": t.get("complexity", "Medium"),
            "Confidence": f"{t.get('complexity_confidence', 0.5):.0%}",
            "Assigned To": t.get("assigned_to", "")[:20],
        })
    
    return pd.DataFrame(display_data), len(tasks)


def get_capacity_summary(capacity_report: Dict) -> Dict[str, Any]:
    """Extract capacity summary data for UI display."""
    if not capacity_report:
        return {}
    
    developers = capacity_report.get("developers", {})
    summary = capacity_report.get("summary", {})
    
    # Get sprint days from report, default to 10 (80 hours total)
    sprint_days = capacity_report.get("sprint_days", 10)
    hours_per_day = capacity_report.get("hours_per_day", 8)
    default_capacity = sprint_days * hours_per_day  # 10 days * 8 hours = 80 hours
    
    total_devs = len([d for d in developers.values() if d.get("assigned_hours", 0) > 0])
    total_assigned = sum(d.get("assigned_hours", 0) for d in developers.values())
    total_capacity = sum(d.get("total_hours", default_capacity) for d in developers.values() if d.get("assigned_hours", 0) > 0)
    overall_util = (total_assigned / total_capacity * 100) if total_capacity > 0 else 0
    
    dev_data = []
    for email, info in sorted(developers.items(), key=lambda x: -x[1].get("utilization", 0)):
        util = info.get("utilization", 0)
        status = info.get("status", "unknown")
        
        status_emoji = {"overloaded": "[!!]", "warning": "[!]", "optimal": "[+]"}.get(status, "[-]")
        
        dev_data.append({
            "Status": status_emoji,
            "Developer": info.get("name", email.split("@")[0]),
            "Assigned Hours": f"{info.get('assigned_hours', 0):.0f}h",
            "Total Capacity": f"{info.get('total_hours', default_capacity):.0f}h",
            "Utilization": f"{util * 100:.0f}%",
            "Tasks": info.get("task_count", 0),
        })
    
    return {
        "total_devs": total_devs,
        "total_assigned": total_assigned,
        "total_capacity": total_capacity,
        "overall_util": overall_util,
        "overloaded_count": summary.get("overloaded_count", 0),
        "warning_count": summary.get("warning_count", 0),
        "optimal_count": summary.get("optimal_count", 0),
        "underutilized_count": summary.get("underutilized_count", 0),
        "dev_data": dev_data,
        "overloaded_developers": summary.get("overloaded_developers", []),
        "redistribution_needed": capacity_report.get("redistribution_needed", False),
        "developers": developers,
    }


def get_backlog_summary(df: pd.DataFrame) -> Dict[str, Any]:
    """Extract backlog assignments summary for UI display."""
    total = len(df)
    
    # Count unique parent work items
    if "Parent WI ID" in df.columns:
        parent_items = df["Parent WI ID"].nunique()
    elif "Feature / User Story" in df.columns:
        parent_items = df["Feature / User Story"].nunique()
    else:
        parent_items = total
    
    # Handle both old and new column names for compatibility
    fe_col = "Responsible - Frontend" if "Responsible - Frontend" in df.columns else "Assigned Frontend"
    be_col = "Responsible - Backend" if "Responsible - Backend" in df.columns else "Assigned Backend"
    
    fe_assigned = len(df[df[fe_col].notna() & (df[fe_col] != "") & (df[fe_col] != "None")]) if fe_col in df.columns else 0
    be_assigned = len(df[df[be_col].notna() & (df[be_col] != "") & (df[be_col] != "None")]) if be_col in df.columns else 0
    cross_role = len(df[df["Task Type"].str.contains("cross-role", na=False)]) if "Task Type" in df.columns else 0
    
    priority_counts = {}
    if "Priority" in df.columns:
        for p in [1, 2, 3, 4]:
            priority_counts[p] = len(df[df["Priority"] == p])
    
    return {
        "total": total,
        "parent_items": parent_items,
        "fe_assigned": fe_assigned,
        "be_assigned": be_assigned,
        "cross_role": cross_role,
        "priority_counts": priority_counts,
    }


async def retrieve_host_card(host_agent_url: str):
    """Async helper to fetch a Host AgentCard from a host agent URL."""
    host_agent_card = None
    async with httpx.AsyncClient(timeout=300) as httpx_client:
        try:
            resolver = A2ACardResolver(base_url=host_agent_url, httpx_client=httpx_client)
            host_agent_card = await resolver.get_agent_card()
            if host_agent_card:
                logger.info("Discovered agent: %s at %s", host_agent_card.name, host_agent_url)
            else:
                logger.warning("No AgentCard found at %s", host_agent_url)
        except Exception as e:
            logger.debug("Error retrieving AgentCard from %s: %s", host_agent_url, e)
    return host_agent_card


async def retrieve_all_tools():
    """Discover MCP servers and local agents (async). Returns (mcp_servers, agents)."""
    mcp_servers = []
    agents = []
    mcps = {}
    # Note: mcp_discovery module not available, skipping MCP server discovery

    for server_name, server_info in mcps.items():
        mcp_servers.append({"name": server_name, "url": server_info['args'][0], "status": "✅ Running"})

    try:
        agent_discover = agent_discovery.AgentDiscovery()
        agent_cards = await agent_discover.list_agent_cards()
        for card in agent_cards:
            agents.append({"name": card.name, "description": card.description, "status": "🟢 Connected"})
    except Exception:
        pass

    return mcp_servers, agents


def ensure_mcp(timeout: int = 30):
    """Ensure an MCP connector is available and cached in `st.session_state`.

    This function expects the Streamlit `st` module to be accessible via
    import inside the calling context; callers (like `chat_ai.py`) typically
    call `ensure_mcp()` while `st` is available in their scope. The function
    accesses `st.session_state` directly.
    """
    # Import streamlit lazily to avoid import-time side-effects in tests.
    import streamlit as st

    # Return cached connector if available
    cached_conn = st.session_state.get('mcp_connector')
    if cached_conn is not None:
        logger.debug("Returning cached MCP connector from session state")
        return cached_conn

    # Mark attempt
    st.session_state.mcp_init_attempted = True

    ado_pat = os.getenv('ADO_PAT')
    ado_org = os.getenv('ADO_ORG_URL')

    logger.debug("ensure_mcp() called: ADO_PAT=%s ADO_ORG_URL=%s", bool(ado_pat), bool(ado_org))

    if not ado_pat:
        logger.error("ADO_PAT environment variable not set")
        st.session_state.mcp_connector = None
        st.session_state.mcp_init_error = "ADO_PAT not configured"
        return None

    try:
        from utilities.mcp.mcp_ado_connector import get_mcp_connector
    except Exception as e:
        logger.exception("Failed to import MCP connector: %s", e)
        st.session_state.mcp_connector = None
        st.session_state.mcp_init_error = str(e)
        return None

    try:
        logger.debug("Initializing MCP connector with asyncio.run()...")
        conn = asyncio.run(get_mcp_connector())
        logger.info("MCP connector initialized: %s", type(conn).__name__)
        
        # Auto-generate tool registry from live MCP tools
        try:
            from utilities.mcp.tool_registry import initialize_registry
            registry = initialize_registry(conn.tools_cache)
            logger.info("Tool registry auto-generated with %d tools from live MCP", len(registry))
            
            # Refresh unified registry to pick up the newly generated metadata
            try:
                from agents.pm_agent.unified_tool_registry import UNIFIED_REGISTRY
                UNIFIED_REGISTRY.refresh()
                logger.info("Unified registry refreshed with auto-generated tool metadata")
            except Exception as e:
                logger.warning("Could not refresh unified registry: %s", e)
        except Exception as e:
            logger.warning("Could not auto-generate tool registry: %s. Using fallback.", e)
        
        st.session_state.mcp_connector = conn
        st.session_state.mcp_initialized = True
        return conn
    except Exception as e:
        logger.exception("MCP initialization failed: %s", e)
        st.session_state.mcp_connector = None
        st.session_state.mcp_init_error = str(e)
        return None


def handle_chat_prompt(prompt: str, st, config):
    """Main entry to handle a user prompt from Streamlit.

    - Mutates `st.session_state.messages` to append assistant/user messages.
    - Routes to PM Agent (HTTP) first; falls back to MCP connector via `ensure_mcp()`.
    - Renders tables / attachments by mutating `st.session_state` and using Streamlit UI.
    """
    response = None
    skip_final_response = False
    trace_id = None
    request_start_time = datetime.now()

    # Create parent trace for request lifecycle (Langfuse observability)
    if _langfuse_client and create_parent_trace:
        try:
            user_email = st.session_state.get("user_email", "anonymous")
            session_id = st.session_state.get("session_id", f"session-{uuid4().hex[:12]}")
            trace_id = create_parent_trace(
                name="chat_request",  # Trace name for identification
                input_data={"query": prompt[:500]},  # Pass query in input_data
                metadata={
                    "user_email": user_email,
                    "session_id": session_id,
                    "entry_point": "handle_chat_prompt",
                    "timestamp": request_start_time.isoformat()
                }
            )
            logger.debug(f"[Langfuse] Created parent trace: {trace_id}")
        except Exception as e:
            logger.warning(f"[Langfuse] Failed to create parent trace: {e}")

    qlow = prompt.lower()

    # NOTE: All quick-handlers removed - everything goes through controller → orchestrator → router
    # The router will detect these patterns and route to appropriate skills
    # This ensures consistent trace/observability and follows canonical architecture

    # Route ALL requests through controller (CANONICAL ENTRY POINT)
    # Controller → Orchestrator → Router → Agent (follows architecture diagram)
    logger.info("Routing query through controller: %s", prompt[:100])
    with st.spinner("🔍 Processing your request..."):
        try:
            # Get controller singleton (CANONICAL ENTRY POINT)
            from controller.chatbot_controller import get_controller
            controller = get_controller()
            
            # Get or create session ID
            session_id = st.session_state.get("session_id")
            if not session_id:
                session_id = f"session-{uuid4().hex[:12]}"
                st.session_state["session_id"] = session_id
            
            user_email = st.session_state.get("user_email", None)
            turn_number = st.session_state.get("turn_number", 0)
            st.session_state["turn_number"] = turn_number + 1

            # Call controller - it creates unified trace and forwards to orchestrator
            controller_result = controller.process_request(
                user_query=prompt,
                session_id=session_id,
                user_id=user_email,
                project_id=os.getenv("ADO_PROJECT", "FracPro-OPS"),
                organization=os.getenv("ADO_ORG_NAME", "Stratagen"),
                turn_number=turn_number
            )

            response = controller_result.get("content")
            logger.debug(f"[UI] Controller response type: {type(response)}, is_dict: {isinstance(response, dict)}")
            if isinstance(response, dict):
                logger.debug(f"[UI] Controller response keys: {list(response.keys())[:10]}")
            
            # [DEBUG] Log the actual response for synthesis debugging
            logger.info(f"[UI_RESPONSE_CHAIN] Controller returned response:")
            logger.info(f"[UI_RESPONSE_CHAIN]   Type: {type(response).__name__}")
            if isinstance(response, str):
                logger.info(f"[UI_RESPONSE_CHAIN]   Length: {len(response)} chars")
                logger.info(f"[UI_RESPONSE_CHAIN]   First 300 chars: {response[:300]}")
                logger.info(f"[UI_RESPONSE_CHAIN]   Last 200 chars: {response[-200:]}")
            else:
                logger.info(f"[UI_RESPONSE_CHAIN]   Value: {str(response)[:300]}")
            
            # Check for skills that require UI form interaction
            # These are routed through controller/orchestrator for proper tracing,
            # but the response triggers a UI form in Streamlit
            routing_info = controller_result.get("_routing", {})
            controller_metadata = controller_result.get("metadata", {})
            
            # Skill name can be in routing, metadata, or metadata may have an alias
            # E.g., get_backlog_health returns skill="backlog_triaging" (the actual form)
            # and alias="get_backlog_health" (the original skill name)
            skill_name = (
                controller_metadata.get("skill") or  # Actual skill/form to render
                routing_info.get("skill") or          # Routing skill name
                controller_metadata.get("alias")      # Fallback to alias if needed
            )
            
            # Check multiple sources for UI form flags (orchestrator may lift them to different levels)
            requires_ui_form = (
                routing_info.get("requires_ui_form", False) or
                controller_metadata.get("requires_ui_form", False) or
                controller_result.get("requires_ui_form", False)
            )
            
            auto_grant_access = (
                routing_info.get("auto_grant_access", False) or
                controller_metadata.get("auto_grant_access", False) or
                controller_result.get("auto_grant_access", False)
            )
            
            open_ui_directly = (
                routing_info.get("open_ui_directly", False) or
                controller_metadata.get("open_ui_directly", False) or
                controller_result.get("open_ui_directly", False)
            )
            
            # UI Form Skills - handled by rendering interactive forms
            UI_FORM_SKILLS = {
                # REMOVED billing_deviation - now handled conversationally by PM Skills Agent
                "sprint_plan": "📅 Please use the Sprint Plan Generator form below to configure your sprint plan.",
                "backlog_triaging": "📋 Please use the Backlog Triaging form below to run the analysis.",
                "capacity_triaging": "📊 Please use the Capacity Triaging form below to analyze capacity.",
                "backlog_assignments": "📋 Please use the Backlog Assignments form below to assign work items.",
                "developer_skills": "📚 Developer Knowledge Base opened below.",
                "get_capacity_forecast": "📊 Opening Capacity Manager to run capacity checks...",
            }
            
            # Check if skill already executed successfully with a report
            # The synthesized response is in the 'response' variable (controller_result.get("content"))
            # If it contains actual report data, don't show the UI form
            has_skill_result = False
            if response and isinstance(response, str):
                # Check if the response contains actual report content (not just a form prompt)
                response_lower = response.lower()
                has_report_content = (
                    "backlog health report" in response_lower or
                    "capacity" in response_lower or
                    "## " in response or  # Markdown headers indicate a report
                    "### " in response or
                    "sprint" in response_lower and ("points" in response_lower or "velocity" in response_lower)
                )
                # Also check it's not just a prompt to use the form
                is_form_prompt = "please use the" in response_lower and "form below" in response_lower
                has_skill_result = has_report_content and not is_form_prompt
                logger.debug(f"[UI] Response check: has_report_content={has_report_content}, is_form_prompt={is_form_prompt}, has_skill_result={has_skill_result}")
            
            logger.debug(f"[UI] Final has_skill_result={has_skill_result}, skill_name={skill_name}, requires_ui_form={requires_ui_form}")
            
            # CRITICAL: Only show UI form if skill hasn't executed yet
            # If skill already returned results, show those instead of the form
            should_open_ui = (
                not has_skill_result and (
                    skill_name in UI_FORM_SKILLS or 
                    requires_ui_form or 
                    (auto_grant_access and open_ui_directly)
                )
            )
            
            # SPECIAL CASE: billing_deviation should NEVER open a UI form
            # It's handled conversationally by the PM Skills Agent
            if skill_name == "billing_deviation":
                logger.info(f"[UI] BILLING_DEVIATION detected - forcing should_open_ui=False")
                logger.info(f"[UI] Response type: {type(response)}, is dict: {isinstance(response, dict)}")
                if response and isinstance(response, dict):
                    logger.info(f"[UI] Response keys: {list(response.keys())}")
                should_open_ui = False
                # Extract the result/error/message from the response if it's a dict
                if response and isinstance(response, dict):
                    # Try to extract in priority order: message > result > error
                    if 'message' in response:
                        logger.info(f"[UI] Extracting 'message' from billing_deviation response")
                        response = response.get('message', str(response))
                    elif 'result' in response:
                        logger.info(f"[UI] Extracting 'result' from billing_deviation response")
                        response = response.get('result', str(response))
                    elif 'error' in response:
                        logger.info(f"[UI] Extracting 'error' from billing_deviation response")
                        response = response.get('error', str(response))
                    else:
                        logger.warning(f"[UI] billing_deviation response has no message/result/error field")
                        response = str(response)
                    logger.info(f"[UI] Final billing_deviation response (length: {len(response) if isinstance(response, str) else 'not string'})")
            
            if should_open_ui:
                # Skill requires UI form input
                logger.info(f"[UI] Skill '{skill_name}' requires UI form - triggering form")

                # Persist the current controller/orchestrator trace id so downstream
                # form submissions can re-join the same trace for unified observability.
                try:
                    pending_tid = get_current_trace_id()
                    if pending_tid:
                        st.session_state["_pending_trace_id"] = pending_tid
                        logger.debug(f"[UI] Saved pending trace id for UI form: {pending_tid}")
                except Exception:
                    logger.exception("Failed to persist pending trace id for UI form")
                
                # REMOVED: billing_deviation is now handled conversationally by PM Skills Agent
                # No form is shown - the agent will ask for target hours in the chat
                
                if skill_name in ("sprint_plan", "backlog_triaging", "capacity_triaging", "backlog_assignments", "developer_skills", "get_capacity_forecast"):
                    # For these skills, show a message and render the form inline
                    from app.chat_extensions import SECTION_RENDERERS, render_section_if_requested
                    
                    # Map skill name to section key
                    skill_to_section = {
                        "sprint_plan": "sprint_plan",
                        "backlog_triaging": "backlog_triaging",
                        "capacity_triaging": "capacity_triaging",
                        "backlog_assignments": "backlog_assignments",
                        "developer_skills": "developer_knowledge_base",  # Maps to the actual section key
                        "get_capacity_forecast": "capacity_check",  # Maps to capacity_check renderer
                    }
                    section_key = skill_to_section.get(skill_name)
                    
                    if section_key and section_key in SECTION_RENDERERS:
                        # When auto_grant_access is True, skip permission text and directly open UI
                        if auto_grant_access and open_ui_directly:
                            response = UI_FORM_SKILLS.get(skill_name, f"📋 Opening {skill_name.replace('_', ' ').title()}...")
                        else:
                            response = UI_FORM_SKILLS.get(skill_name, f"📋 Please use the form below for {skill_name}.")
                        # Store section to render after response
                        st.session_state["_pending_section"] = section_key
                        logger.info(f"[UI] Will render section '{section_key}' for skill '{skill_name}' (auto_grant={auto_grant_access})")
                    else:
                        response = UI_FORM_SKILLS.get(skill_name, f"📋 {skill_name.replace('_', ' ').title()} requires form input.")
            
            # Handle dict responses from controller (work item details, etc.)
            elif response and isinstance(response, dict):
                # Check if it's a PM Skills Agent response (success, skill, result/message/error)
                if 'success' in response and 'skill' in response:
                    skill_from_response = response.get('skill', '')
                    logger.info(f"[UI] PM Skills Agent response for skill '{skill_from_response}'")
                    
                    # For billing_deviation, extract in priority order: message > result > error
                    if skill_from_response == 'billing_deviation':
                        if 'message' in response:
                            logger.info("[UI] Extracting 'message' from billing_deviation response")
                            response = response.get('message')
                        elif 'result' in response:
                            logger.info("[UI] Extracting 'result' from billing_deviation response")
                            response = response.get('result')
                        elif 'error' in response:
                            logger.info("[UI] Extracting 'error' from billing_deviation response")
                            response = response.get('error')
                        else:
                            logger.warning("[UI] billing_deviation response has no message/result/error field")
                            response = str(response)
                    # For other skills, extract result
                    elif 'result' in response:
                        logger.info(f"[UI] Extracting 'result' from {skill_from_response} response")
                        response = response.get('result', str(response))
                    else:
                        logger.warning(f"[UI] {skill_from_response} response has no 'result' field")
                        response = str(response)
                # Check if it's a valid data response (has 'result' field)
                elif 'result' in response:
                    logger.info("[UI] Controller returned dict with 'result' field - extracting")
                    response = response.get('result', str(response))
                # Check for planner/routing metadata patterns
                elif 'action' in response and response.get('action') in ['call_tool', 'no_tool', 'ask_clarification']:
                    action = response.get('action', '')
                    if action == 'ask_clarification':
                        logger.warning("[UI] Detected ask_clarification dict, extracting message")
                        response = response.get('message', "I need more information to process this request. Could you please rephrase?")
                    else:
                        logger.error("[UI] CRITICAL: Raw planner dict leaked to UI!")
                        logger.error(f"[UI] Plan dict: {json.dumps(response, indent=2)[:500]}")
                        response = response.get('message', "⚠️ I encountered an issue processing your request. The query was understood but couldn't be completed. Please try rephrasing your question.")
                # Check for tool/args/confidence pattern WITHOUT valid data fields
                elif ('tool' in response and 'args' in response and 'confidence' in response) and not any(k in response for k in ['result', 'work_item_id', 'id', 'title', 'state']):
                    logger.error("[UI] CRITICAL: Raw tool plan dict leaked to UI!")
                    logger.error(f"[UI] Tool plan dict: {json.dumps(response, indent=2)[:500]}")
                    response = "⚠️ I encountered an issue processing your request. The query was understood but couldn't be completed. Please try rephrasing your question."
                # Check for _routing metadata
                elif '_routing' in response and not any(k in response for k in ['result', 'work_item_id', 'id', 'title', 'state']):
                    logger.warning("[UI] Detected routing metadata dict, extracting content")
                    response = response.get('content', "Response processing completed.")
                # Otherwise convert dict to string representation
                else:
                    logger.debug("[UI] Controller returned dict - converting to string")
                    response = str(response)
            
            # Safety check for string responses: ensure we never display raw routing metadata or JSON plans
            # BUT allow valid data responses (work item details, etc.)
            if response and isinstance(response, str):
                # Check if response looks like raw JSON (starts with { or [)
                response_stripped = response.strip()
                if response_stripped.startswith(('{', '[')):
                    try:
                        # Try parsing - if it's routing metadata, extract content
                        parsed = json.loads(response_stripped)
                        if isinstance(parsed, dict):
                            # ONLY filter out routing/planner metadata, not actual data responses
                            # Check for action pattern (raw planner output or ask_clarification)
                            if 'action' in parsed and parsed.get('action') in ['call_tool', 'no_tool', 'ask_clarification']:
                                action = parsed.get('action', '')
                                if action == 'ask_clarification':
                                    logger.warning("[UI] Detected ask_clarification JSON, extracting message")
                                    response = parsed.get('message', "I need more information to process this request. Could you please rephrase?")
                                else:
                                    logger.error("[UI] CRITICAL: Raw planner JSON leaked to UI!")
                                    logger.error(f"[UI] Plan JSON: {json.dumps(parsed, indent=2)[:500]}")
                                    # Extract message or return generic error
                                    message = parsed.get('message', '')
                                    if message:
                                        response = message
                                    else:
                                        response = "⚠️ I encountered an issue processing your request. The query was understood but couldn't be completed. Please try rephrasing your question."
                            # Check for tool/args/confidence pattern WITHOUT valid data fields
                            elif ('tool' in parsed and 'args' in parsed and 'confidence' in parsed) and not any(k in parsed for k in ['result', 'work_item_id', 'id', 'title', 'state']):
                                logger.error("[UI] CRITICAL: Raw tool plan JSON leaked to UI!")
                                logger.error(f"[UI] Tool plan JSON: {json.dumps(parsed, indent=2)[:500]}")
                                response = "⚠️ I encountered an issue processing your request. The query was understood but couldn't be completed. Please try rephrasing your question."
                            # Check for _routing metadata leakage
                            elif '_routing' in parsed and not any(k in parsed for k in ['result', 'work_item_id', 'id', 'title', 'state']):
                                logger.warning("[UI] Detected routing metadata leakage, extracting content")
                                response = parsed.get('content', "Response processing completed.")
                            # Check for PM Skills Agent response (billing_deviation, etc.)
                            elif 'success' in parsed and 'skill' in parsed:
                                skill_from_response = parsed.get('skill', '')
                                logger.info(f"[UI] PM Skills Agent JSON response for skill '{skill_from_response}'")
                                
                                # For billing_deviation, extract in priority order: message > result > error
                                if skill_from_response == 'billing_deviation':
                                    if 'message' in parsed:
                                        logger.info("[UI] Extracting 'message' from billing_deviation response")
                                        response = parsed.get('message')
                                    elif 'result' in parsed:
                                        logger.info("[UI] Extracting 'result' from billing_deviation response")
                                        response = parsed.get('result')
                                    elif 'error' in parsed:
                                        logger.info("[UI] Extracting 'error' from billing_deviation response")
                                        response = parsed.get('error')
                                    else:
                                        logger.warning("[UI] billing_deviation response has no message/result/error field")
                                        response = str(parsed)
                                # For other skills, extract result
                                elif 'result' in parsed:
                                    logger.info(f"[UI] Extracting 'result' from {skill_from_response} response")
                                    response = parsed.get('result', str(parsed))
                                else:
                                    logger.warning(f"[UI] {skill_from_response} response has no 'result' field")
                                    response = str(parsed)
                            # Otherwise, it's a valid data response (work item, etc.) - leave as-is
                            else:
                                logger.debug("[UI] Response contains JSON data (work item details, etc.) - passing through")
                    except json.JSONDecodeError:
                        pass  # Not JSON, use as-is
            
            pm_agent_success = True
            logger.info("Controller success: %s", str(response)[:200])

            # Note: Langfuse event removed - controller already creates unified trace
            # The controller_request trace captures the entire lifecycle including success/failure
        except Exception as e:
            logger.exception("Controller error: %s", e)
            response = f"[ERROR] Request processing failed: {e}"
            pm_agent_success = True

    # No MCP fallback - controller is the ONLY entry point
    # Architecture enforced: UI → Controller → Orchestrator → Router → Agent
    if False and not pm_agent_success and not skip_final_response:
        logger.info("Falling back to direct MCP: %s", prompt[:100])
        with st.spinner("🔍 Querying Azure DevOps directly..."):
            mcp_conn = ensure_mcp()
            if mcp_conn is None:
                error_msg = st.session_state.get('mcp_init_error', 'Unknown error')
                response = f"[ERROR] Failed to initialize MCP connector: {error_msg}"
                logger.debug("MCP connector is None. Error: %s", error_msg)
            else:
                logger.debug("MCP connector obtained successfully")

            # Only attempt structured queries if MCP is available and response not already set
            if response is None and mcp_conn is not None:
                # Structured work item handling (time logs / deployments)
                structured = None
                wi_match = re.search(r"([A-Z]+-\d+)", prompt, re.IGNORECASE)
                if wi_match:
                    work_item_id = wi_match.group(1)
                    try:
                        structured = asyncio.run(mcp_conn.timelog_get_time_and_comments(work_item_id, includeTimeLogExtension=True))
                    except Exception:
                        structured = None
                else:
                    wi_match = re.search(r"\b(\d{4,6})\b", prompt)
                    if wi_match:
                        work_item_id = wi_match.group(1)
                        try:
                            structured = asyncio.run(mcp_conn.timelog_get_time_and_comments(work_item_id, includeTimeLogExtension=True))
                        except Exception:
                            structured = None

                if isinstance(structured, str):
                    try:
                        structured = json.loads(structured)
                    except Exception:
                        pass

                qlow_local = prompt.lower()
                time_log_phrases = [
                    "time log", "time spent", "hours spent", "time tracking",
                    "work log", "how much time", "extension entries", "time entry",
                    "time entries", "logged time", "tracked time"
                ]
                deployment_phrases = ["deployment", "deployment schedule", "deployment dates", "scheduled", "qa", "uat", "pre-prod", "prod", "deploy"]
                contains_time = any(p in qlow_local for p in time_log_phrases)
                contains_deploy = any(p in qlow_local for p in deployment_phrases)

                if contains_deploy and not contains_time and isinstance(structured, dict) and structured.get('deployments'):
                    deps = structured.get('deployments')
                    rows = []
                    if isinstance(deps, dict):
                        for env, date in deps.items():
                            rows.append({'Environment': str(env), 'ScheduledUTC': date})
                    else:
                        for item in deps:
                            if isinstance(item, dict):
                                env = item.get('Environment') or item.get('environment') or item.get('env')
                                date = item.get('ScheduledUTC') or item.get('scheduled') or item.get('date')
                                rows.append({'Environment': env, 'ScheduledUTC': date})

                    summary_lines = [f"📅 Deployment schedule for Work Item {work_item_id}", ""]
                    for r in rows:
                        summary_lines.append(f"{r['Environment']}: {r['ScheduledUTC']}")
                    response = "\n".join(summary_lines)
                    # Attach pending section if present so UI can render inline forms
                    section_key = st.session_state.get("_pending_section")
                    msg = {"role": "assistant", "content": response}
                    if section_key:
                        msg["_section"] = section_key
                    st.session_state.messages.append(msg)
                    with st.chat_message('assistant'):
                        st.write('**Deployment Schedule**')
                        st.table(rows)
                        with st.expander('Email this report'):
                            default_pm = os.getenv('DEFAULT_PM_EMAIL', '')
                            pm = st.text_input('Project manager email', value=default_pm, key=f'pm_deploy_{work_item_id}')
                            if st.button('Send deployment report', key=f'send_deploy_{work_item_id}'):
                                try:
                                    send_wi_report(work_item_id, structured if isinstance(structured, dict) else {}, pm or None)
                                    st.success('✅ Report emailed')
                                except Exception as e:
                                    st.error(f'❌ Error sending email: {e}')
                    response = None

                elif isinstance(structured, dict) and structured.get('timeLogs'):
                    tls = structured.get('timeLogs')
                    trows = []
                    for t in tls:
                        date = t.get('date') or t.get('loggedDate') or t.get('when') or t.get('timestamp') or t.get('createdAt')
                        user = t.get('user') or t.get('author') or t.get('displayName') or (t.get('person') and t['person'].get('displayName'))
                        hours = t.get('hours') or t.get('time') or t.get('spent') or t.get('completedWork') or t.get('delta')
                        comment = t.get('comment') or t.get('notes') or t.get('description') or t.get('message')
                        trows.append({'Date': date, 'User': user, 'Hours': hours, 'Comment': comment})

                    section_key = st.session_state.get("_pending_section")
                    msg = {'role': 'assistant', 'content': f"🕒 Time logs for Work Item {work_item_id} ({len(trows)} entries)"}
                    if section_key:
                        msg["_section"] = section_key
                    st.session_state.messages.append(msg)
                    with st.chat_message('assistant'):
                        st.write('**Time Log Entries**')
                        st.table(trows)
                        with st.expander('Email this report'):
                            default_pm = os.getenv('DEFAULT_PM_EMAIL', '')
                            pm = st.text_input('Project manager email', value=default_pm, key=f'pm_timelogs_{work_item_id}')
                            if st.button('Send time-log report', key=f'send_timelogs_{work_item_id}'):
                                try:
                                    send_wi_report(work_item_id, structured if isinstance(structured, dict) else {}, pm or None)
                                    st.success('✅ Report emailed')
                                except Exception as e:
                                    st.error(f'❌ Error sending email: {e}')
                    response = None

                else:
                    # fallback: general MCP execution
                    try:
                        response = asyncio.run(mcp_conn.execute_query(prompt))
                    except Exception as e:
                        logger.exception("MCP execute_query failed: %s", e)
                        response = f"[ERROR] MCP query failed: {e}"
            elif response is None:
                # response was not set, MCP init must have failed
                response = "[ERROR] Failed to process query with MCP. Please check logs for details."

    # Append/render final response
    # Only append/render the assistant message if `response` contains text.
    # When `response` is None it means we already rendered a table (deployments/timelogs)
    if response is not None and not skip_final_response:
        logger.info(f"[UI_FINAL_RESPONSE] About to render response:")
        logger.info(f"[UI_FINAL_RESPONSE]   Type: {type(response).__name__}")
        if isinstance(response, str):
            logger.info(f"[UI_FINAL_RESPONSE]   Length: {len(response)} chars")
            logger.info(f"[UI_FINAL_RESPONSE]   First 200 chars: {response[:200]}")
            logger.info(f"[UI_FINAL_RESPONSE]   Last 150 chars: {response[-150:]}")
        
        # Render MCP JSON outputs as structured tables where possible
        def render_mcp_output(resp):
            import pandas as pd
            parsed = None
            if isinstance(resp, (dict, list)):
                parsed = resp
            else:
                if isinstance(resp, str):
                    text = resp.strip()
                    if not text or text.lower() == 'null':
                        st.write(text or 'No results')
                        return
                    try:
                        parsed = json.loads(text)
                    except Exception:
                        try:
                            st.markdown(text)
                        except Exception:
                            st.write(text)
                        return

            try:
                # Handle WIQL query results format
                if isinstance(parsed, dict) and parsed.get('count') is not None and isinstance(parsed.get('results'), list):
                    results = parsed.get('results', [])
                    rows = []
                    for item in results:
                        fields = item.get('fields') if isinstance(item, dict) else None
                        if fields and isinstance(fields, dict):
                            r = {
                                'id': fields.get('system.id') or fields.get('System.Id') or item.get('id') or item.get('workItemId'),
                                'title': fields.get('system.title') or fields.get('System.Title') or fields.get('title') or '',
                                'assignedTo': fields.get('system.assignedto') or fields.get('System.AssignedTo') or fields.get('assignedTo') or '',
                                'state': fields.get('system.state') or fields.get('System.State') or '',
                                'url': item.get('url') or item.get('link') or ''
                            }
                        else:
                            r = {}
                            if isinstance(item, dict):
                                for k, v in item.items():
                                    if isinstance(v, (str, int, float)):
                                        r[k] = v
                        rows.append(r)

                    if rows:
                        df = pd.DataFrame(rows)
                        st.write(f"**{len(rows)} results**")
                        st.table(df.head(200))
                    else:
                        st.write("No results returned.")
                    with st.expander('Raw JSON'):
                        st.code(json.dumps(parsed, indent=2))
                    return

                # Handle list responses
                if isinstance(parsed, list):
                    df = pd.json_normalize(parsed)
                    if not df.empty:
                        st.table(df.head(200))
                    else:
                        st.write(parsed)
                    with st.expander('Raw JSON'):
                        st.code(json.dumps(parsed, indent=2))
                    return

                # Handle dict responses
                if isinstance(parsed, dict):
                    if 'result' in parsed and isinstance(parsed['result'], dict):
                        inner = parsed['result']
                        st.write('Result:')
                        try:
                            st.json(inner)
                        except Exception:
                            st.write(inner)
                        with st.expander('Raw JSON'):
                            st.code(json.dumps(parsed, indent=2))
                        return

                    with st.expander('Raw JSON'):
                        st.code(json.dumps(parsed, indent=2))
                    return
            except Exception as e:
                st.write(f"[ERROR] Failed to render MCP response: {e}")
                try:
                    st.markdown(str(resp))
                except Exception:
                    st.write(str(resp))

        try:
            parsed_test = None
            if isinstance(response, str):
                s = response.strip()
                if s and s.lower() != 'null':
                    try:
                        parsed_test = json.loads(s)
                    except Exception:
                        parsed_test = None
            else:
                parsed_test = response

            if parsed_test is not None:
                summary = ''
                if isinstance(parsed_test, dict) and parsed_test.get('count') is not None:
                    summary = f"Returned {parsed_test.get('count')} items from MCP."
                elif isinstance(parsed_test, list):
                    summary = f"Returned {len(parsed_test)} items from MCP."
                else:
                    summary = 'Returned structured MCP response.'
                section_key = st.session_state.get("_pending_section")
                msg = {"role": "assistant", "content": summary}
                if section_key:
                    msg["_section"] = section_key
                st.session_state.messages.append(msg)
                with st.chat_message("assistant"):
                    st.write(summary)
                    render_mcp_output(parsed_test)
            else:
                section_key = st.session_state.get("_pending_section")
                msg = {"role": "assistant", "content": response}
                if section_key:
                    msg["_section"] = section_key
                st.session_state.messages.append(msg)
                with st.chat_message("assistant"):
                    st.markdown(response)
        except Exception:
            section_key = st.session_state.get("_pending_section")
            msg = {"role": "assistant", "content": response}
            if section_key:
                msg["_section"] = section_key
            st.session_state.messages.append(msg)
            with st.chat_message("assistant"):
                st.markdown(response)
