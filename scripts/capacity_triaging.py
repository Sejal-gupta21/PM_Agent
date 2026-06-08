#!/usr/bin/env python3
"""
Capacity Triaging - Advanced Sprint Offtrack Warning System

This script implements capacity-based sprint monitoring:
1. Captures planned sprint capacity vs current available capacity (leaves, unplanned events)
2. Defines deviation thresholds (e.g., >20% drop)
3. Detects early indicators of sprint slippage (too many tasks in To-Do after 30% sprint)
4. Generates automated warning email to PM with detailed risk summary
5. Suggests corrective actions (re-prioritize, descoping candidates)

Usage:
    ADO_PROJECT="FracPro-OPS" ADO_TEAM="XOPS 25" python scripts/capacity_triaging.py
    
Environment Variables:
    ADO_ORG_URL - Azure DevOps organization URL
    ADO_PROJECT - Project name (default: FracPro-OPS)
    ADO_TEAM - Team name (optional, checks all teams if not set)
    ADO_PAT - Personal Access Token
    CAPACITY_DEVIATION_THRESHOLD - Capacity drop threshold (default: 20%)
    SPRINT_PROGRESS_THRESHOLD - Sprint elapsed % to check work distribution (default: 30%)
"""

import os
import sys
import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
import logging

# Add project root to path
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utilities.mcp.mcp_ado_connector import MCPConnector
from utilities.emailer import send_report_attachment
from utilities.logging_config import setup_logging
from utilities.capacity_data_sources import create_capacity_source
from utilities.langfuse_client import trace_task

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Configuration
ADO_ORG_NAME = os.getenv("ADO_ORG_NAME", "Stratagen")
ADO_PROJECT = os.getenv("ADO_PROJECT", "FracPro-OPS")
ADO_TEAM = os.getenv("ADO_TEAM")  # Optional - will check all teams if not set

# Thresholds
CAPACITY_DEVIATION_THRESHOLD = float(os.getenv("CAPACITY_DEVIATION_THRESHOLD", "20"))  # 20% drop
SPRINT_PROGRESS_THRESHOLD = float(os.getenv("SPRINT_PROGRESS_THRESHOLD", "30"))  # 30% elapsed

# Capacity Data Source Configuration
CAPACITY_SOURCE_TYPE = os.getenv("CAPACITY_SOURCE_TYPE", "ado")  # ado, google-sheets, csv
CAPACITY_SOURCE_URL = os.getenv("CAPACITY_SOURCE_URL")  # For Google Sheets or file path
CAPACITY_GOOGLE_CREDS = os.getenv("CAPACITY_GOOGLE_CREDS_PATH", "credentials/google_sheets_creds.json")

# Test recipients for development
DEV_EMAIL_RECIPIENTS = [
    "ankur.kumar@walkingtree.tech",
    "sejal.gupta@walkingtree.tech",
    "yati.gautam@walkingtree.tech",
    "sarthak.singh@walkingtree.tech"
]

logger = logging.getLogger(__name__)


class CapacityTriaging:
    """Capacity-based sprint monitoring and risk detection."""
    
    def __init__(self, mcp_connector: MCPConnector, capacity_source=None):
        self.mcp = mcp_connector
        self.capacity_source = capacity_source
        
    async def get_current_iteration(self, project: str, team: str) -> Optional[Dict[str, Any]]:
        """Get the current active iteration for a team."""
        try:
            result_str = await self.mcp.call_tool("work_list_team_iterations", {
                "project": project,
                "team": team,
                "timeframe": "current"
            })
            
            # Parse JSON response
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
            iterations = result if isinstance(result, list) else []
            
            logger.debug(f"Fetched {len(iterations)} iterations for team {team}")
            if len(iterations) > 0:
                logger.debug(f"First iteration: {iterations[0]}")
                # Return the first current iteration
                # timeFrame: 0=past, 1=current, 2=future
                for iteration in iterations:
                    if isinstance(iteration, dict):
                        timeframe = iteration.get("attributes", {}).get("timeFrame")
                        logger.debug(f"Iteration {iteration.get('name')}: timeFrame={timeframe}")
                        if timeframe == 1 or timeframe == "current":
                            logger.info(f"Found current iteration: {iteration.get('name')}")
                            return iteration
            
            logger.warning(f"No current iteration found for team {team}")
            return None
            
        except Exception as e:
            logger.error(f"Error fetching current iteration: {e}")
            return None
    
    async def get_team_capacity(self, project: str, team: str, iteration_id: str) -> Dict[str, Any]:
        """Fetch team capacity for a specific iteration."""
        try:
            # Use external capacity source if configured
            if self.capacity_source:
                logger.info(f"Fetching capacity from external source: {type(self.capacity_source).__name__}")
                result = await self.capacity_source.get_team_capacity(project, team, iteration_id)
                if result and result.get("teamMembers"):
                    return result
                logger.warning("External capacity source returned empty data")
            
            # Try ADO Sprint Capacity first
            logger.info("Fetching capacity from Azure DevOps Sprint Capacity")
            result_str = await self.mcp.call_tool("work_get_team_capacity", {
                "project": project,
                "team": team,
                "iterationId": iteration_id
            })
            
            logger.debug(f"Team capacity raw response type: {type(result_str)}")
            logger.debug(f"Team capacity raw response: {result_str[:200] if isinstance(result_str, str) else result_str}")
            
            # Handle plain text error messages
            result = {}
            if isinstance(result_str, str):
                if "no team capacity" in result_str.lower() or "not found" in result_str.lower():
                    logger.warning(f"Team {team} has no capacity data in ADO Sprint Capacity")
                else:
                    try:
                        result = json.loads(result_str)
                    except json.JSONDecodeError:
                        logger.warning(f"Cannot parse capacity response as JSON")
            else:
                result = result_str or {}
            
            # Check if ADO Sprint Capacity has actual data
            if result and isinstance(result, dict) and result.get("teamMembers"):
                logger.info(f"ADO Sprint Capacity returned {len(result.get('teamMembers', []))} team members")
                return result
            
            # FALLBACK: Extract capacity from work item completed work
            logger.info("ADO Sprint Capacity empty - falling back to work item completed work")
            fallback_capacity = await self._get_capacity_from_work_items(project, team, iteration_id)
            if fallback_capacity and fallback_capacity.get("teamMembers"):
                logger.info(f"Fallback capacity source found {len(fallback_capacity.get('teamMembers', []))} team members from work items")
                return fallback_capacity
            
            logger.warning("No capacity data available from any source")
            return {}
            
        except Exception as e:
            logger.error(f"Error fetching team capacity: {e}")
            return {}
    
    async def _get_capacity_from_work_items(self, project: str, team: str, iteration_id: str) -> Dict[str, Any]:
        """Extract capacity data from work item completed work (fallback when ADO Sprint Capacity is empty)."""
        try:
            from collections import defaultdict
            
            # Get all work items for the iteration
            work_items = await self.get_iteration_work_items(project, team, iteration_id)
            if not work_items:
                return {}
            
            # Aggregate completed work by user
            user_work = defaultdict(lambda: {"completed": 0, "original": 0, "remaining": 0, "work_items": 0})
            
            for wi in work_items:
                if not isinstance(wi, dict):
                    continue
                
                fields = wi.get("fields", {})
                assigned_to = fields.get("System.AssignedTo")
                if not assigned_to:
                    continue
                
                # Handle different formats of AssignedTo field
                if isinstance(assigned_to, dict):
                    user_name = assigned_to.get("displayName") or assigned_to.get("name", "Unknown")
                elif isinstance(assigned_to, str):
                    user_name = assigned_to.split("<")[0].strip() if "<" in assigned_to else assigned_to
                else:
                    continue
                
                # Get work metrics
                completed = fields.get("Microsoft.VSTS.Scheduling.CompletedWork", 0) or 0
                original = fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate", 0) or 0
                remaining = fields.get("Microsoft.VSTS.Scheduling.RemainingWork", 0) or 0
                
                try:
                    completed = float(completed)
                    original = float(original)
                    remaining = float(remaining)
                except (ValueError, TypeError):
                    continue
                
                if completed > 0 or original > 0:
                    user_work[user_name]["completed"] += completed
                    user_work[user_name]["original"] += original
                    user_work[user_name]["remaining"] += remaining
                    user_work[user_name]["work_items"] += 1
            
            if not user_work:
                return {}
            
            # Build team members list
            team_members = []
            for user, metrics in user_work.items():
                total_completed = metrics["completed"]
                
                # Estimate 8 hours per day capacity (company policy)
                estimated_days = total_completed / 8.0 if total_completed > 0 else 0
                avg_capacity = 8.0
                
                team_members.append({
                    "teamMember": {"displayName": user},
                    "activities": [{"name": "Development", "capacityPerDay": avg_capacity}],
                    "daysOff": [],
                    "actualData": {
                        "totalCompletedHours": round(total_completed, 2),
                        "totalOriginalEstimate": round(metrics["original"], 2),
                        "totalRemainingWork": round(metrics["remaining"], 2),
                        "workItemsCount": metrics["work_items"],
                        "estimatedWorkingDays": round(estimated_days, 1)
                    }
                })
            
            return {
                "teamMembers": team_members,
                "source": "work-item-completed-work",
                "note": "Capacity derived from work item CompletedWork field (ADO Sprint Capacity was empty)"
            }
            
        except Exception as e:
            logger.error(f"Error extracting capacity from work items: {e}")
            return {}
    
    async def get_iteration_work_items(self, project: str, team: str, iteration_id: str) -> List[Dict[str, Any]]:
        """Fetch all work items for a specific iteration."""
        try:
            result_str = await self.mcp.call_tool("wit_get_work_items_for_iteration", {
                "project": project,
                "team": team,
                "iterationId": iteration_id
            })
            
            logger.debug(f"Work items raw response type: {type(result_str)}")
            if isinstance(result_str, str):
                logger.debug(f"Work items raw response length: {len(result_str)}")
                logger.debug(f"Work items raw response preview: {result_str[:300]}")
            
            # Parse JSON response
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
            
            # Handle different response formats
            if isinstance(result, list):
                work_items = result
            elif isinstance(result, dict):
                # Try to extract from value or workItems or workItemRelations property
                raw_items = result.get("value", result.get("workItems", result.get("workItemRelations", [])))
                
                # If we have workItemRelations, extract the actual work items
                if len(raw_items) > 0 and isinstance(raw_items[0], dict) and 'target' in raw_items[0]:
                    logger.debug("Extracting work items from workItemRelations")
                    work_items = [item['target'] for item in raw_items if 'target' in item]
                else:
                    work_items = raw_items
            else:
                work_items = []
            
            logger.info(f"Fetched {len(work_items)} work item IDs for iteration {iteration_id}")
            if len(work_items) > 0:
                logger.debug(f"First work item keys: {list(work_items[0].keys())}")
            
            # If work items only have IDs, fetch full details
            if len(work_items) > 0 and 'fields' not in work_items[0]:
                logger.info("Work items missing field data, fetching full details...")
                work_item_ids = [wi.get('id') for wi in work_items if 'id' in wi]
                if work_item_ids:
                    full_work_items = await self.get_work_items_by_ids(project, work_item_ids)
                    return full_work_items
            
            return work_items
            
        except Exception as e:
            logger.error(f"Error fetching iteration work items: {e}")
            logger.error(f"Raw response: {result_str if 'result_str' in locals() else 'not available'}")
            return []
    
    async def _fetch_single_work_item(self, project: str, work_item_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single work item by ID."""
        try:
            result_str = await self.mcp.call_tool("wit_get_work_item", {
                "project": project,
                "id": work_item_id
            })
            
            # Check for MCP errors
            if isinstance(result_str, str) and ("MCP error" in result_str or "not found" in result_str.lower()):
                return None
            
            # Parse JSON response - be resilient to malformed JSON
            try:
                work_item = json.loads(result_str) if isinstance(result_str, str) else result_str
            except json.JSONDecodeError:
                # Try to extract JSON from wrapped response
                if isinstance(result_str, str) and "{" in result_str:
                    # Find first { and try to parse from there
                    json_start = result_str.find("{")
                    try:
                        work_item = json.loads(result_str[json_start:])
                    except:
                        return None
                else:
                    return None
            
            if work_item and isinstance(work_item, dict):
                return work_item
            return None
        except Exception:
            return None
    
    async def get_work_items_by_ids(self, project: str, work_item_ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch full work item details by IDs sequentially to avoid overwhelming MCP server."""
        try:
            logger.info(f"Fetching full details for {len(work_item_ids)} work items (sequential)...")
            
            all_work_items = []
            failed_count = 0
            
            # Fetch items one at a time to avoid timeouts and JSON parsing issues
            for i, work_item_id in enumerate(work_item_ids):
                if (i + 1) % 10 == 0:
                    logger.info(f"Progress: {i + 1}/{len(work_item_ids)} work items fetched")
                
                work_item = await self._fetch_single_work_item(project, work_item_id)
                if work_item:
                    all_work_items.append(work_item)
                else:
                    failed_count += 1
                
                # Small delay to avoid overwhelming the MCP server
                await asyncio.sleep(0.1)
            
            logger.info(f"Successfully fetched {len(all_work_items)}/{len(work_item_ids)} work items")
            if failed_count > 0:
                logger.warning(f"{failed_count} work items failed to fetch (possibly deleted or inaccessible)")
            if len(all_work_items) > 0:
                logger.debug(f"First full work item keys: {list(all_work_items[0].keys())}")
            
            return all_work_items
            
        except Exception as e:
            logger.error(f"Error fetching work items by IDs: {e}")
            return []
    
    def calculate_capacity_metrics(self, capacity_data: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate capacity metrics from team capacity data."""
        logger.debug(f"calculate_capacity_metrics called with capacity_data: {capacity_data}")
        logger.debug(f"capacity_data type: {type(capacity_data)}, keys: {capacity_data.keys() if isinstance(capacity_data, dict) else 'N/A'}")
        
        if not capacity_data:
            logger.warning("capacity_data is empty/None - returning zero metrics")
            return {
                "totalCapacityPerDay": 0,
                "totalDaysOff": 0,
                "membersCount": 0,
                "memberDetails": [],
                "averageCapacityPerMember": 0,
                "hasCapacityData": False
            }
        
        team_members = capacity_data.get("teamMembers", [])
        logger.info(f"Found {len(team_members)} team members in capacity data")
        
        total_capacity_per_day = 0
        total_days_off = 0
        members_count = 0
        member_details = []
        
        for member in team_members:
            if not isinstance(member, dict):
                continue
                
            members_count += 1
            member_name = member.get("teamMember", {}).get("displayName", "Unknown")
            
            # Get activities (capacity per day)
            activities = member.get("activities", [])
            member_capacity = sum(act.get("capacityPerDay", 0) for act in activities if isinstance(act, dict))
            total_capacity_per_day += member_capacity
            
            # Get days off
            days_off = member.get("daysOff", [])
            days_off_count = len(days_off) if isinstance(days_off, list) else 0
            total_days_off += days_off_count
            
            member_details.append({
                "name": member_name,
                "capacityPerDay": member_capacity,
                "daysOff": days_off_count
            })
        
        result = {
            "totalCapacityPerDay": total_capacity_per_day,
            "totalDaysOff": total_days_off,
            "membersCount": members_count,
            "memberDetails": member_details,
            "averageCapacityPerMember": total_capacity_per_day / members_count if members_count > 0 else 0,
            "hasCapacityData": True
        }
        logger.info(f"Calculated capacity metrics: {members_count} members, {total_capacity_per_day} hrs/day total, {total_days_off} days off")
        return result
    
    def analyze_sprint_progress(
        self, 
        iteration: Dict[str, Any], 
        work_items: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Analyze sprint progress and detect early warning signs."""
        # Calculate sprint timeline
        start_date_str = iteration.get("attributes", {}).get("startDate")
        finish_date_str = iteration.get("attributes", {}).get("finishDate")
        
        if not start_date_str or not finish_date_str:
            return {"error": "Missing iteration dates"}
        
        start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        finish_date = datetime.fromisoformat(finish_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        
        total_duration = (finish_date - start_date).total_seconds()
        elapsed = (now - start_date).total_seconds()
        sprint_elapsed_pct = (elapsed / total_duration * 100) if total_duration > 0 else 0
        
        # Analyze work items by state
        state_distribution = {
            "To Do": 0,
            "In Progress": 0,
            "Done": 0,
            "Removed": 0,
            "Other": 0
        }
        
        total_original_estimate = 0
        total_remaining_work = 0
        total_completed_work = 0
        
        for wi in work_items:
            if not isinstance(wi, dict):
                continue
            
            fields = wi.get("fields", {})
            state = fields.get("System.State", "Other")
            
            # Normalize state
            if state in ["To Do", "New", "Proposed"]:
                state_distribution["To Do"] += 1
            elif state in ["Active", "In Progress", "Committed"]:
                state_distribution["In Progress"] += 1
            elif state in ["Done", "Closed", "Completed"]:
                state_distribution["Done"] += 1
            elif state in ["Removed", "Cut"]:
                state_distribution["Removed"] += 1
            else:
                state_distribution["Other"] += 1
            
            # Calculate work metrics
            original_estimate = fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate", 0)
            remaining_work = fields.get("Microsoft.VSTS.Scheduling.RemainingWork", 0)
            completed_work = fields.get("Microsoft.VSTS.Scheduling.CompletedWork", 0)
            
            total_original_estimate += original_estimate or 0
            total_remaining_work += remaining_work or 0
            total_completed_work += completed_work or 0
        
        total_items = len(work_items)
        todo_pct = (state_distribution["To Do"] / total_items * 100) if total_items > 0 else 0
        done_pct = (state_distribution["Done"] / total_items * 100) if total_items > 0 else 0
        
        # Calculate ideal vs actual progress
        ideal_completion_pct = sprint_elapsed_pct
        actual_completion_pct = done_pct
        deviation_from_ideal = actual_completion_pct - ideal_completion_pct
        
        return {
            "sprintElapsedPct": round(sprint_elapsed_pct, 1),
            "stateDistribution": state_distribution,
            "totalItems": total_items,
            "todoPct": round(todo_pct, 1),
            "inProgressPct": round((state_distribution["In Progress"] / total_items * 100) if total_items > 0 else 0, 1),
            "donePct": round(done_pct, 1),
            "idealCompletionPct": round(ideal_completion_pct, 1),
            "actualCompletionPct": round(actual_completion_pct, 1),
            "deviationFromIdeal": round(deviation_from_ideal, 1),
            "totalOriginalEstimate": total_original_estimate,
            "totalRemainingWork": total_remaining_work,
            "totalCompletedWork": total_completed_work,
            "startDate": start_date.isoformat(),
            "finishDate": finish_date.isoformat()
        }
    
    def detect_risks(
        self, 
        capacity_metrics: Dict[str, Any],
        sprint_analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Detect capacity and sprint risks."""
        risks = []
        severity = "LOW"
        
        # Risk 1: High number of days off (capacity thinning)
        avg_days_off = capacity_metrics["totalDaysOff"] / capacity_metrics["membersCount"] if capacity_metrics["membersCount"] > 0 else 0
        if avg_days_off > 2:  # More than 2 days off per person on average
            risks.append({
                "type": "CAPACITY_THINNING",
                "severity": "HIGH" if avg_days_off > 3 else "MEDIUM",
                "message": f"High team absence: Average {avg_days_off:.1f} days off per team member",
                "recommendation": "Consider descoping lower priority items or extending sprint timeline"
            })
            severity = "HIGH" if avg_days_off > 3 else "MEDIUM"
        
        # Risk 2: Too many items still in To-Do after threshold
        if sprint_analysis["sprintElapsedPct"] > SPRINT_PROGRESS_THRESHOLD:
            if sprint_analysis["todoPct"] > 50:
                risks.append({
                    "type": "SPRINT_SLIPPAGE",
                    "severity": "HIGH",
                    "message": f"{sprint_analysis['todoPct']:.0f}% of items still in To-Do after {sprint_analysis['sprintElapsedPct']:.0f}% sprint elapsed",
                    "recommendation": "Urgently review and re-prioritize backlog, move critical items to In Progress"
                })
                severity = "HIGH"
            elif sprint_analysis["todoPct"] > 30:
                risks.append({
                    "type": "SPRINT_SLIPPAGE",
                    "severity": "MEDIUM",
                    "message": f"{sprint_analysis['todoPct']:.0f}% of items still in To-Do after {sprint_analysis['sprintElapsedPct']:.0f}% sprint elapsed",
                    "recommendation": "Review sprint progress and accelerate work item pickup"
                })
                if severity == "LOW":
                    severity = "MEDIUM"
        
        # Risk 3: Deviation from ideal burndown
        if sprint_analysis["deviationFromIdeal"] < -15:  # More than 15% behind ideal
            risks.append({
                "type": "BURNDOWN_DEVIATION",
                "severity": "HIGH" if sprint_analysis["deviationFromIdeal"] < -25 else "MEDIUM",
                "message": f"Sprint is {abs(sprint_analysis['deviationFromIdeal']):.1f}% behind ideal burndown",
                "recommendation": "Identify blockers, increase team velocity, or descope non-critical items"
            })
            if sprint_analysis["deviationFromIdeal"] < -25:
                severity = "HIGH"
            elif severity == "LOW":
                severity = "MEDIUM"
        
        # Risk 4: Low capacity per member
        if capacity_metrics["averageCapacityPerMember"] < 6:  # Less than 6 hours/day average
            risks.append({
                "type": "LOW_CAPACITY",
                "severity": "MEDIUM",
                "message": f"Low average capacity: {capacity_metrics['averageCapacityPerMember']:.1f} hours/day per member",
                "recommendation": "Review team allocation and address capacity constraints"
            })
            if severity == "LOW":
                severity = "MEDIUM"
        
        return {
            "risks": risks,
            "overallSeverity": severity,
            "riskCount": len(risks)
        }
    
    def suggest_corrective_actions(
        self, 
        risks: Dict[str, Any],
        work_items: List[Dict[str, Any]]
    ) -> List[str]:
        """Generate specific corrective action suggestions."""
        actions = []
        
        # Analyze work items for descoping candidates
        low_priority_items = []
        blocked_items = []
        
        for wi in work_items:
            if not isinstance(wi, dict):
                continue
            
            fields = wi.get("fields", {})
            state = fields.get("System.State", "")
            priority = fields.get("Microsoft.VSTS.Common.Priority", 999)
            
            if state in ["To Do", "New", "Proposed"] and priority >= 3:
                low_priority_items.append({
                    "id": wi.get("id"),
                    "title": fields.get("System.Title", ""),
                    "priority": priority
                })
            
            if "blocked" in fields.get("System.Tags", "").lower():
                blocked_items.append({
                    "id": wi.get("id"),
                    "title": fields.get("System.Title", "")
                })
        
        # Generate actions based on risk types
        for risk in risks["risks"]:
            if risk["type"] == "CAPACITY_THINNING":
                actions.append("[REFRESH] **Re-evaluate sprint commitment** considering reduced team capacity")
                if low_priority_items:
                    actions.append(f"[LIST] **Descoping candidates**: {len(low_priority_items)} low-priority items in To-Do (Priority 3+)")
            
            elif risk["type"] == "SPRINT_SLIPPAGE":
                actions.append("[!] **Accelerate work pickup**: Ensure all team members have active work items")
                actions.append("[>] **Daily standup focus**: Address why items remain in To-Do")
                if blocked_items:
                    actions.append(f"[X] **Unblock items**: {len(blocked_items)} items marked as blocked")
            
            elif risk["type"] == "BURNDOWN_DEVIATION":
                actions.append("[CHART] **Burndown review**: Schedule mid-sprint retrospective")
                actions.append("[SEARCH] **Identify blockers**: Review impediments preventing progress")
        
        # Add descoping recommendations if high severity
        if risks["overallSeverity"] == "HIGH" and low_priority_items:
            descope_candidates = sorted(low_priority_items, key=lambda x: x["priority"], reverse=True)[:5]
            actions.append(f"\n**Recommended descoping candidates** (lowest priority first):")
            for item in descope_candidates:
                actions.append(f"  - Work Item #{item['id']}: {item['title'][:60]}")
        
        return actions if actions else ["[OK] No immediate actions required - continue monitoring"]
    
    async def analyze_team_capacity(self, project: str, team: str) -> Optional[Dict[str, Any]]:
        """Perform complete capacity analysis for a team."""
        logger.info(f"Analyzing capacity for team: {team}")
        
        # Get current iteration
        iteration = await self.get_current_iteration(project, team)
        if not iteration:
            logger.warning(f"No current iteration for team {team}")
            return None
        
        iteration_id = iteration.get("id")
        iteration_name = iteration.get("name", "Unknown")
        logger.info(f"Current iteration: {iteration_name} (ID: {iteration_id})")
        
        # Fetch capacity and work items
        capacity_data = await self.get_team_capacity(project, team, iteration_id)
        logger.debug(f"capacity_data from get_team_capacity: {capacity_data}")
        logger.debug(f"capacity_data teamMembers count: {len(capacity_data.get('teamMembers', [])) if isinstance(capacity_data, dict) else 'N/A'}")
        work_items = await self.get_iteration_work_items(project, team, iteration_id)
        
        if not work_items:
            logger.warning(f"No work items found for analysis (capacity: {bool(capacity_data)}, work items: {len(work_items)})")
            return None
        
        # Capacity is optional - we can still analyze sprint progress
        if not capacity_data:
            logger.info("No capacity data available, will analyze sprint progress only")
        else:
            logger.info(f"Capacity data received: {len(capacity_data.get('teamMembers', []))} team members")
        
        # Calculate metrics
        capacity_metrics = self.calculate_capacity_metrics(capacity_data)
        logger.info(f"Capacity metrics calculated: membersCount={capacity_metrics.get('membersCount')}, totalCapacityPerDay={capacity_metrics.get('totalCapacityPerDay')}")
        sprint_analysis = self.analyze_sprint_progress(iteration, work_items)
        
        if "error" in sprint_analysis:
            logger.error(f"Sprint analysis error: {sprint_analysis['error']}")
            return None
        
        # Detect risks
        risk_analysis = self.detect_risks(capacity_metrics, sprint_analysis)
        
        # Generate corrective actions
        corrective_actions = self.suggest_corrective_actions(risk_analysis, work_items)
        
        return {
            "team": team,
            "iteration": iteration_name,
            "iterationId": iteration_id,
            "capacityMetrics": capacity_metrics,
            "sprintAnalysis": sprint_analysis,
            "riskAnalysis": risk_analysis,
            "correctiveActions": corrective_actions,
            "workItemCount": len(work_items),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    
    def generate_email_html(self, analysis_results: List[Dict[str, Any]]) -> str:
        """Generate HTML email report for capacity triaging."""
        high_risk_teams = [r for r in analysis_results if r and r.get("riskAnalysis", {}).get("overallSeverity") == "HIGH"]
        medium_risk_teams = [r for r in analysis_results if r and r.get("riskAnalysis", {}).get("overallSeverity") == "MEDIUM"]
        
        severity_color = "red" if high_risk_teams else ("orange" if medium_risk_teams else "green")
        severity_label = "HIGH RISK" if high_risk_teams else ("MEDIUM RISK" if medium_risk_teams else "ON TRACK")
        
        html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .header {{ background-color: {severity_color}; color: white; padding: 20px; text-align: center; }}
                .summary {{ background-color: #f4f4f4; padding: 15px; margin: 20px 0; border-left: 4px solid {severity_color}; }}
                .team-section {{ margin: 20px 0; padding: 15px; border: 1px solid #ddd; }}
                .risk-high {{ color: red; font-weight: bold; }}
                .risk-medium {{ color: orange; font-weight: bold; }}
                .risk-low {{ color: green; }}
                .metrics {{ display: flex; flex-wrap: wrap; gap: 10px; }}
                .metric-card {{ background: #f9f9f9; padding: 10px; border-radius: 5px; flex: 1; min-width: 200px; }}
                .action-list {{ background: #fffacd; padding: 15px; margin: 10px 0; border-left: 4px solid #ffa500; }}
                table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #4CAF50; color: white; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>[ALERT] Sprint Capacity Triaging Alert - {severity_label}</h1>
                <p>Automated Sprint Health Check - {datetime.now().strftime('%B %d, %Y %H:%M UTC')}</p>
            </div>
            
            <div class="summary">
                <h2>Executive Summary</h2>
                <p><strong>Teams Analyzed:</strong> {len(analysis_results)}</p>
                <p><strong>High Risk Teams:</strong> {len(high_risk_teams)}</p>
                <p><strong>Medium Risk Teams:</strong> {len(medium_risk_teams)}</p>
                <p><strong>Low Risk Teams:</strong> {len(analysis_results) - len(high_risk_teams) - len(medium_risk_teams)}</p>
            </div>
        """
        
        # Add team details
        for result in analysis_results:
            if not result:
                continue
            
            team = result["team"]
            iteration = result["iteration"]
            risk = result["riskAnalysis"]
            capacity = result["capacityMetrics"]
            sprint = result["sprintAnalysis"]
            actions = result["correctiveActions"]
            
            risk_class = f"risk-{risk['overallSeverity'].lower()}"
            
            html += f"""
            <div class="team-section">
                <h2>{team} - {iteration}</h2>
                <p class="{risk_class}">Overall Risk: {risk['overallSeverity']} ({risk['riskCount']} risk(s) detected)</p>
                
                <div class="metrics">
                    <div class="metric-card">
                        <strong>Sprint Progress</strong><br>
                        {sprint['sprintElapsedPct']}% Elapsed<br>
                        {sprint['donePct']}% Completed<br>
                        <span class="{risk_class}">{sprint['deviationFromIdeal']:+.1f}% vs Ideal</span>
                    </div>
                    <div class="metric-card">
                        <strong>Work Distribution</strong><br>
                        To Do: {sprint['todoPct']}%<br>
                        In Progress: {sprint['inProgressPct']}%<br>
                        Done: {sprint['donePct']}%
                    </div>
                    <div class="metric-card">
                        <strong>Team Capacity</strong><br>
                        {capacity['membersCount']} team members<br>
                        {capacity['totalCapacityPerDay']:.1f} hrs/day total<br>
                        {capacity['totalDaysOff']} total days off
                    </div>
                    <div class="metric-card">
                        <strong>Work Items</strong><br>
                        Total: {result['workItemCount']}<br>
                        To Do: {sprint['stateDistribution']['To Do']}<br>
                        Done: {sprint['stateDistribution']['Done']}
                    </div>
                </div>
                
                <h3>[!] Identified Risks</h3>
                <ul>
            """
            
            for risk_item in risk["risks"]:
                html += f"""<li class="risk-{risk_item['severity'].lower()}">
                    <strong>[{risk_item['severity']}] {risk_item['type']}:</strong> {risk_item['message']}<br>
                    <em>Recommendation: {risk_item['recommendation']}</em>
                </li>"""
            
            html += """</ul>"""
            
            if actions:
                html += f"""
                <div class="action-list">
                    <h3>[TIP] Suggested Corrective Actions</h3>
                    <ul>
                """
                for action in actions:
                    html += f"<li>{action}</li>"
                html += """
                    </ul>
                </div>
                """
            
            # Add member capacity details
            if capacity['memberDetails']:
                html += """
                <h3>Team Member Capacity Details</h3>
                <table>
                    <tr>
                        <th>Team Member</th>
                        <th>Capacity (hrs/day)</th>
                        <th>Days Off</th>
                    </tr>
                """
                for member in capacity['memberDetails']:
                    html += f"""
                    <tr>
                        <td>{member['name']}</td>
                        <td>{member['capacityPerDay']}</td>
                        <td>{member['daysOff']}</td>
                    </tr>
                    """
                html += "</table>"
            
            html += "</div>"
        
        html += """
            <div class="summary">
                <h3>📧 Next Steps</h3>
                <p>Please review the risks and corrective actions above. Schedule a capacity review meeting if HIGH risk teams are identified.</p>
                <p><em>This is an automated report generated by the PM Agent Capacity Triaging System.</em></p>
            </div>
        </body>
        </html>
        """
        
        return html
    
    @trace_task("capacity_triaging", metadata={"source": "pm_agent"})
    async def run(self, project: str, teams: Optional[List[str]] = None) -> Dict[str, Any]:
        """Run capacity triaging for project/teams."""
        logger.info(f"Starting capacity triaging for project: {project}")
        
        # If no teams specified, get all teams
        if not teams:
            try:
                result_str = await self.mcp.call_tool("core_list_project_teams", {"project": project})
                result = json.loads(result_str) if isinstance(result_str, str) else result_str
                team_list = result if isinstance(result, list) else []
                teams = [t.get("name") for t in team_list if isinstance(t, dict) and t.get("name")]
                logger.info(f"Found {len(teams)} teams in project")
            except Exception as e:
                logger.error(f"Error listing teams: {e}")
                return {"error": "Failed to list teams"}
        
        # Analyze each team
        analysis_results = []
        for team in teams:
            try:
                result = await self.analyze_team_capacity(project, team)
                if result:
                    analysis_results.append(result)
            except Exception as e:
                logger.error(f"Error analyzing team {team}: {e}")
        
        if not analysis_results:
            logger.warning("No analysis results generated")
            return {"error": "No teams analyzed"}
        
        # Generate email
        html_content = self.generate_email_html(analysis_results)
        
        # Save HTML report to outputs folder
        from pathlib import Path
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outputs_dir = Path(__file__).parent.parent / "outputs"
        outputs_dir.mkdir(exist_ok=True)
        html_file = outputs_dir / f"capacity_triaging_{project}_{timestamp}.html"
        html_file.write_text(html_content, encoding='utf-8')
        logger.info(f"Saved HTML report to {html_file}")
        
        # Send email to dev recipients
        try:
            email_sent = send_report_attachment(
                to_emails=DEV_EMAIL_RECIPIENTS,
                subject=f"[ALERT] Sprint Capacity Triaging Alert - {project}",
                html_body=html_content,
                attachments=None
            )
            
            logger.info(f"Email sent: {email_sent}")
        except Exception as e:
            logger.error(f"Error sending email: {e}")
        
        return {
            "success": True,
            "teamsAnalyzed": len(analysis_results),
            "results": analysis_results,
            "htmlFile": str(html_file)
        }


async def main():
    """Main entry point."""
    setup_logging()
    logger.info("=== Capacity Triaging Started ===")
    
    # Get PAT token
    from utilities.mcp.pat import get_pat
    pat = get_pat()
    
    # Initialize MCP connector
    mcp = MCPConnector(org_name=ADO_ORG_NAME, pat_token=pat)
    await mcp.initialize()
    
    try:
        # Create capacity data source
        capacity_source = None
        if CAPACITY_SOURCE_TYPE and CAPACITY_SOURCE_TYPE.lower() != "ado":
            logger.info(f"Initializing capacity source: {CAPACITY_SOURCE_TYPE}")
            source_config = {
                "sheet_url": CAPACITY_SOURCE_URL,
                "credentials_path": CAPACITY_GOOGLE_CREDS,
                "file_path": CAPACITY_SOURCE_URL
            }
            capacity_source = create_capacity_source(CAPACITY_SOURCE_TYPE, source_config, mcp)
            if capacity_source:
                logger.info(f"Using external capacity source: {type(capacity_source).__name__}")
            else:
                logger.warning("Failed to create external capacity source, falling back to ADO")
        
        # Run capacity triaging
        triaging = CapacityTriaging(mcp, capacity_source)
        
        teams = [ADO_TEAM] if ADO_TEAM else None
        result = await triaging.run(ADO_PROJECT, teams)
        
        if result.get("success"):
            logger.info(f"[OK] Capacity triaging completed: {result['teamsAnalyzed']} teams analyzed")
        else:
            logger.error(f"[ERROR] Capacity triaging failed: {result.get('error')}")
    
    finally:
        pass  # MCP connector will cleanup on garbage collection
    
    logger.info("=== Capacity Triaging Finished ===")


if __name__ == "__main__":
    asyncio.run(main())
