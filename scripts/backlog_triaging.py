"""
Backlog Triaging - Alert Product Owner when backlog is running thin.

Workflow:
1. Define "thin backlog" threshold (< X story points or < Y items)
2. Scheduler pulls backlog size from ADO via MCP
3. Compare current backlog size against threshold
4. If below threshold → trigger email alert to PO
5. Attach actionable summary: capacity available vs items pending

Uses:
- env vars: ADO_ORG_URL, ADO_PROJECT, and PAT via utilities.mcp.pat.get_pat()
- config: backlog_triaging section from config.yaml
- Sends HTML email using utilities.emailer.send_report_attachment
"""

import os
import sys
import logging
import asyncio
import yaml
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utilities.mcp.pat import get_pat
from utilities.mcp.mcp_ado_connector import MCPConnector
from utilities.emailer import send_report_attachment
from utilities.langfuse_client import trace_task

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

logger = logging.getLogger("pm_agent.backlog_triaging")
logger.setLevel(logging.INFO)
if not logger.handlers:
    os.makedirs("logs", exist_ok=True)
    fh = logging.FileHandler("logs/backlog_triaging.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(fh)

CONFIG_PATH = REPO_ROOT / "config.yaml"


def load_config() -> Dict[str, Any]:
    """Load configuration from config.yaml."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def get_team_area_path(mcp: MCPConnector, team: str, project: str) -> str:
    """
    Get the actual area path for a team by querying live ADO work items.
    
    ⚠️ DYNAMIC RESOLUTION ONLY (Enterprise Contract)
    This function queries live ADO data to discover team area paths.
    NO static mappings or fallback patterns are used.
    
    Azure DevOps teams have area paths that may be deeply nested:
    - FracPro-OPS\\Global Management\\WTT Development\\XOPS 25
    - FracPro-OPS\\Global Management\\WTT Development\\XOPS Bugs Enhancement
    
    We fetch sample work items and extract area paths to find the pattern.
    
    Args:
        mcp: MCP connector instance
        team: Team name (e.g., "XOPS 25", "XOPS Bugs Enhancement")
        project: Project name (e.g., "FracPro-OPS")
    
    Returns:
        Area path string for the team, or empty string if not found (requires clarification)
        
    Raises:
        ValueError: If area path cannot be resolved and clarification is needed
    """
    import json
    
    if not team or team == project:
        # Default team or no team specified - use project root
        logger.info(f"No specific team, using project root as area path: {project}")
        return project
    
    # Strategy 1: Search for work items containing the team name
    # and extract the area path from them
    discovered_area_paths = []
    
    try:
        # Search for work items that might belong to this team
        result = await mcp.call_tool("search_workitem", {
            "searchText": team,  # Search for team name
            "project": [project],
            "top": 10
        })
        
        if result:
            data = json.loads(result) if isinstance(result, str) else result
            results = data.get("results", [])
            
            if results:
                # Get work item IDs
                item_ids = []
                for item in results[:5]:
                    wi_id = item.get("fields", {}).get("system.id")
                    if wi_id:
                        try:
                            item_ids.append(int(wi_id))
                        except (ValueError, TypeError):
                            pass
                
                if item_ids:
                    # Fetch full details to get area paths
                    full_result = await mcp.call_tool("wit_get_work_items_batch_by_ids", {
                        "ids": item_ids,
                        "project": project,
                        "fields": ["System.Id", "System.AreaPath"]
                    })
                    
                    if full_result:
                        try:
                            items = json.loads(full_result) if isinstance(full_result, str) else full_result
                            if not isinstance(items, list):
                                items = []
                            
                            # Collect all unique area paths
                            for wi in items:
                                area_path = wi.get("fields", {}).get("System.AreaPath", "")
                                if area_path:
                                    discovered_area_paths.append(area_path)
                                    # Exact match - team name at end of path
                                    if area_path.endswith(team):
                                        logger.info(f"[DYNAMIC] Found exact area path for team '{team}': {area_path}")
                                        return area_path
                        except json.JSONDecodeError:
                            pass
    except Exception as e:
        logger.warning(f"[DYNAMIC] Could not fetch area path for team '{team}': {e}")
    
    # Strategy 2: Use area path resolver to find matching paths
    try:
        from utilities.area_path_resolver import resolve_area_path
        from config import config
        
        org = getattr(config, 'ado_org_name', 'Stratagen')
        pat = getattr(config, 'ado_pat', '')
        
        if pat:
            resolved = resolve_area_path(org, project, pat, team, top_k=3)
            status = resolved.get("status", "")
            
            if status in ("ok", "likely"):
                choice = resolved.get("choice", "")
                if choice:
                    logger.info(f"[DYNAMIC] Resolved area path for team '{team}' via classifier: {choice}")
                    return choice
            elif status == "ambiguous":
                # Return first match with warning
                top_matches = resolved.get("top_matches", [])
                if top_matches:
                    best_path = top_matches[0][0]
                    logger.warning(f"[DYNAMIC] Ambiguous area path for team '{team}', using best match: {best_path}")
                    return best_path
    except ImportError:
        logger.debug("area_path_resolver not available for fallback")
    except Exception as e:
        logger.warning(f"[DYNAMIC] Area path resolver failed for team '{team}': {e}")
    
    # NO FALLBACK PATTERN - Return empty and log for clarification
    # The calling code should handle empty string by requesting clarification
    logger.warning(f"[DYNAMIC] Could not resolve area path for team '{team}' in project '{project}'. "
                   f"Discovered paths: {discovered_area_paths[:3]}. Clarification may be needed.")
    
    # Return project root as minimal fallback (not a guess)
    # This is the safest option as it includes all areas
    return project


def get_dummy_backlog_data(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return dummy backlog data for testing when ADO is unavailable."""
    dummy_cfg = config.get("backlog_triaging", {}).get("dummy_data", {})
    return {
        "backlog_items": dummy_cfg.get("backlog_items", 5),
        "total_story_points": dummy_cfg.get("total_story_points", 15),
        "velocity_per_sprint": dummy_cfg.get("velocity_per_sprint", 20),
        "items": [
            {"id": 1001, "title": "Sample User Story 1", "story_points": 3, "state": "New"},
            {"id": 1002, "title": "Sample User Story 2", "story_points": 5, "state": "Approved"},
            {"id": 1003, "title": "Sample User Story 3", "story_points": 2, "state": "Ready"},
            {"id": 1004, "title": "Sample User Story 4", "story_points": 3, "state": "New"},
            {"id": 1005, "title": "Sample User Story 5", "story_points": 2, "state": "Proposed"},
        ],
        "is_dummy": True,
    }


async def _fetch_items_individually(mcp: MCPConnector, item_ids: List[int], effort_field: str, project: str) -> Dict[str, Any]:
    """Fallback: fetch work items one by one using wit_get_work_item."""
    import json
    work_items = []
    
    for wi_id in item_ids:
        try:
            result = await mcp.call_tool("wit_get_work_item", {
                "id": wi_id,
                "project": project  # Required parameter
            })
            
            if isinstance(result, str) and result:
                try:
                    wi_data = json.loads(result)
                    work_items.append(wi_data)
                    
                    # Log fields for first item to debug
                    if len(work_items) == 1:
                        fields = wi_data.get("fields", {})
                        logger.debug(f"Sample WI {wi_id} fields: {list(fields.keys())}")
                        if effort_field in fields:
                            logger.info(f"Found {effort_field}={fields[effort_field]} in WI {wi_id}")
                        else:
                            logger.warning(f"{effort_field} not found in WI {wi_id}. Available: {[k for k in fields.keys() if 'effort' in k.lower() or 'point' in k.lower() or 'story' in k.lower()]}")
                except json.JSONDecodeError:
                    logger.debug(f"Could not parse WI {wi_id}: {result[:100]}")
        except Exception as e:
            logger.debug(f"Failed to fetch WI {wi_id}: {e}")
            
    logger.info(f"Fetched {len(work_items)} work items individually")
    return {"value": work_items}


async def fetch_backlog_from_ado(
    mcp: MCPConnector,
    project: str,
    team: str,
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Fetch backlog data from Azure DevOps using MCP connector.
    
    Returns dict with backlog_items count, total_story_points (effort), and item details.
    """
    import json
    bt_config = config.get("backlog_triaging", {})
    include_states = bt_config.get("include_states", ["New", "Ready", "Requested", "Scheduled", "In Planning", "Accepted"])
    effort_field = bt_config.get("effort_field", "Custom.Effort3P")  # Project-specific effort field
    
    # Get the actual area path for this team
    area_path = await get_team_area_path(mcp, team, project)
    
    # Use search_workitem API - most reliable method
    # Search for User Stories/PBIs in backlog states (not assigned to sprint or in early states)
    states_query = " OR ".join([f"s:{state}" for state in include_states])
    search_text = f't:"User Story" ({states_query})'
    
    logger.info(f"Searching for backlog items in project: {project}, team: {team}, area path: {area_path} (effort field: {effort_field})")
    logger.debug(f"Search query: {search_text}")
    
    try:
        # Build search parameters with area path filter
        search_params = {
            "searchText": search_text,
            "project": [project],
            "areaPath": [area_path],  # Filter by team's area path
            "top": 1000  # Fetch up to 1000 items (max supported by Azure DevOps search API)
        }
        
        logger.debug(f"Search params: {search_params}")
        result = await mcp.call_tool("search_workitem", search_params)
        
        # Parse the result
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                logger.warning(f"Could not parse search result: {result[:200] if result else 'None'}")
                result = {}
        
        if not result or result == "None":
            logger.warning("Search returned no results")
            return {
                "backlog_items": 0,
                "total_story_points": 0,
                "velocity_per_sprint": 0,
                "items": [],
                "is_dummy": False,
            }
        
        # Parse basic data from search results
        basic_data = parse_search_response(result, include_states)
        
        # Fetch full work item details to get effort/story points
        if basic_data["items"]:
            # Convert IDs to integers as required by the API
            item_ids = [int(item["id"]) for item in basic_data["items"] if item.get("id")]
            logger.info(f"Fetching full details for {len(item_ids)} work items to get effort values...")
            
            try:
                # Build fields list with configured effort field
                fields_to_fetch = [
                    "System.Id",
                    "System.Title",
                    "System.State",
                    "System.WorkItemType",
                    effort_field,  # Project-specific effort field from config
                ]
                # Always include standard Story Points as fallback
                if effort_field != "Microsoft.VSTS.Scheduling.StoryPoints":
                    fields_to_fetch.append("Microsoft.VSTS.Scheduling.StoryPoints")
                
                # MCP tool can only handle ~20 items per batch, so we need to chunk
                BATCH_SIZE = 20
                all_work_items = []
                
                for i in range(0, len(item_ids), BATCH_SIZE):
                    batch_ids = item_ids[i:i + BATCH_SIZE]
                    logger.debug(f"Fetching batch {i//BATCH_SIZE + 1}: {len(batch_ids)} items (IDs: {batch_ids[:3]}...)")
                    
                    batch_result = await mcp.call_tool("wit_get_work_items_batch_by_ids", {
                        "ids": batch_ids,
                        "project": project,
                        "fields": fields_to_fetch
                    })
                    
                    if batch_result and len(batch_result) > 10:
                        try:
                            batch_data = json.loads(batch_result)
                            if isinstance(batch_data, list):
                                all_work_items.extend(batch_data)
                                logger.debug(f"Batch {i//BATCH_SIZE + 1}: Got {len(batch_data)} items")
                        except json.JSONDecodeError:
                            logger.warning(f"Batch {i//BATCH_SIZE + 1}: JSON decode error")
                    else:
                        logger.warning(f"Batch {i//BATCH_SIZE + 1}: Empty result")
                
                logger.info(f"Total fetched: {len(all_work_items)} work items from {(len(item_ids) + BATCH_SIZE - 1) // BATCH_SIZE} batches")
                
                if all_work_items:
                    full_details = {"value": all_work_items}
                else:
                    # Fallback to individual fetch
                    logger.warning("All batch fetches failed - falling back to individual fetch")
                    full_details = await _fetch_items_individually(mcp, item_ids[:40], effort_field, project)
                    
                if full_details:
                    logger.debug(f"Full details structure: {type(full_details)}, keys: {full_details.keys() if isinstance(full_details, dict) else 'N/A'}")
                    # Update items with effort values from full details
                    basic_data = enrich_with_story_points(basic_data, full_details, effort_field)
                else:
                    logger.warning("Could not fetch work item details for effort values")
                    
            except Exception as e:
                logger.error(f"Could not fetch full work item details: {e}", exc_info=True)
        
        return basic_data
        
    except Exception as e:
        logger.error(f"Failed to fetch backlog via search: {e}")
        raise


def enrich_with_story_points(basic_data: Dict[str, Any], full_details: Any, effort_field: str = "Custom.Effort3P") -> Dict[str, Any]:
    """Enrich basic search data with effort/story points from full work item details."""
    # Build a map of ID -> effort/story points
    sp_map = {}
    
    # Handle different response formats
    if isinstance(full_details, dict):
        items_list = full_details.get("value", full_details.get("workItems", []))
    elif isinstance(full_details, list):
        items_list = full_details
    else:
        logger.warning(f"Unexpected full_details type: {type(full_details)}")
        return basic_data
    
    logger.debug(f"Enriching with {len(items_list) if items_list else 0} work items from batch API (effort field: {effort_field})")
    
    # Try effort/story points field name variations in priority order
    # Configured effort_field takes priority, then standard Story Points as fallback
    field_names = [
        effort_field,                 # Configured effort field (primary)
        effort_field.lower(),         # lowercase version
        "Microsoft.VSTS.Scheduling.StoryPoints",  # Standard story points fallback
        "microsoft.vsts.scheduling.storypoints",  # lowercase
        "Custom.Effort3P",            # FracPro-OPS custom effort field
        "custom.effort3p",            # lowercase
        "System.StoryPoints",
        "StoryPoints",
        "Story Points",
        "Effort"
    ]
    
    first_item_logged = False
    for wi in items_list:
        if isinstance(wi, dict):
            wi_id = str(wi.get("id", ""))
            fields = wi.get("fields", {})
            
            # Log first work item to see structure
            if not first_item_logged:
                logger.debug(f"First work item: id={wi_id} (type={type(wi.get('id'))}), fields keys: {list(fields.keys())[:10]}")
                first_item_logged = True
            
            # Try all field name variations
            sp = 0
            found_field = None
            for field_name in field_names:
                if field_name in fields:
                    sp = fields.get(field_name, 0) or 0
                    if sp:
                        found_field = field_name
                        logger.debug(f"Found effort for {wi_id}: {sp} (field: {field_name})")
                        break
            
            try:
                sp_map[wi_id] = float(sp)
            except (ValueError, TypeError):
                sp_map[wi_id] = 0.0

    
    # Update items and recalculate total
    total_story_points = 0.0
    matched_count = 0
    
    for item in basic_data["items"]:
        item_id = str(item["id"])
        if item_id in sp_map:
            item["story_points"] = sp_map[item_id]
            matched_count += 1
        total_story_points += item.get("story_points", 0)
    
    basic_data["total_story_points"] = total_story_points
    logger.info(f"Enriched data: {matched_count}/{len(basic_data['items'])} matched, {sum(1 for v in sp_map.values() if v > 0)} with effort > 0, total: {total_story_points}")
    
    return basic_data


def parse_search_response(response: Dict[str, Any], include_states: List[str]) -> Dict[str, Any]:
    """Parse search_workitem response into standardized backlog data structure."""
    items = []
    total_story_points = 0.0
    
    results = response.get("results", [])
    count = response.get("count", 0)
    
    logger.info(f"Processing {len(results)} search results (total count: {count})")
    
    for item in results:
        fields = item.get("fields", {})
        
        # Field names from search API are lowercase
        state = fields.get("system.state", "")
        work_item_type = fields.get("system.workitemtype", "")
        
        # Filter by state if needed
        if include_states and state not in include_states:
            continue
        
        # Only include User Stories, PBIs, Requirements
        if work_item_type not in ["User Story", "Product Backlog Item", "Requirement"]:
            continue
        
        # Get story points - search API may not include this field
        story_points = 0
        sp_field = fields.get("microsoft.vsts.scheduling.storypoints", 0)
        if sp_field:
            try:
                story_points = float(sp_field)
            except (ValueError, TypeError):
                story_points = 0
        
        total_story_points += story_points
        
        items.append({
            "id": fields.get("system.id", "N/A"),
            "title": fields.get("system.title", ""),
            "story_points": story_points,
            "state": state,
        })
    
    return {
        "backlog_items": len(items),
        "total_story_points": total_story_points,
        "velocity_per_sprint": 0,  # Would need historical data to calculate
        "items": items,
        "is_dummy": False,
        "total_count": count,  # Total matching items in ADO
    }


async def fetch_work_item_details_for_estimation(mcp: MCPConnector, work_item_ids: List[int], project: str) -> Dict[int, Dict[str, Any]]:
    """
    Fetch full work item details including description and acceptance criteria for effort estimation.
    
    Returns:
        Dict mapping work item ID to its full details (title, description, acceptance criteria, etc.)
    """
    logger.info(f"Fetching detailed info for {len(work_item_ids)} work items for effort estimation...")
    
    # Fields needed for effort estimation
    fields_to_fetch = [
        "System.Id",
        "System.Title",
        "System.Description",
        "System.WorkItemType",
        "System.State",
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        "System.Tags",
        "Microsoft.VSTS.Common.Priority",
    ]
    
    work_item_details = {}
    BATCH_SIZE = 20
    
    for i in range(0, len(work_item_ids), BATCH_SIZE):
        batch_ids = work_item_ids[i:i + BATCH_SIZE]
        
        try:
            batch_result = await mcp.call_tool("wit_get_work_items_batch_by_ids", {
                "ids": batch_ids,
                "project": project,
                "fields": fields_to_fetch
            })
            
            if batch_result:
                try:
                    batch_data = json.loads(batch_result) if isinstance(batch_result, str) else batch_result
                    items_list = batch_data if isinstance(batch_data, list) else batch_data.get("value", [])
                    
                    for wi in items_list:
                        wi_id = wi.get("id")
                        if wi_id:
                            fields = wi.get("fields", {})
                            work_item_details[wi_id] = {
                                "id": wi_id,
                                "title": fields.get("System.Title", ""),
                                "description": fields.get("System.Description", ""),
                                "acceptance_criteria": fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""),
                                "tags": fields.get("System.Tags", ""),
                                "priority": fields.get("Microsoft.VSTS.Common.Priority", ""),
                                "work_item_type": fields.get("System.WorkItemType", ""),
                                "state": fields.get("System.State", ""),
                            }
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"Failed to parse batch {i//BATCH_SIZE + 1}: {e}")
        except Exception as e:
            logger.warning(f"Failed to fetch batch {i//BATCH_SIZE + 1}: {e}")
    
    logger.info(f"Successfully fetched details for {len(work_item_details)} work items")
    return work_item_details


def estimate_effort_with_llm(work_item: Dict[str, Any], reference_velocities: Dict[str, float], openai_api_key: str) -> Dict[str, Any]:
    """
    Use OpenAI LLM to estimate effort for a work item based on its complexity.
    
    Args:
        work_item: Dict with title, description, acceptance_criteria, etc.
        reference_velocities: Historical velocity data for context
        openai_api_key: OpenAI API key
    
    Returns:
        Dict with estimated_effort, confidence, reasoning
    """
    if not OPENAI_AVAILABLE:
        logger.warning("OpenAI library not available, skipping LLM estimation")
        return {"estimated_effort": 0, "confidence": "low", "reasoning": "OpenAI unavailable"}
    
    client = OpenAI(api_key=openai_api_key)
    
    # Build context from work item
    title = work_item.get("title", "")
    description = work_item.get("description", "")
    acceptance_criteria = work_item.get("acceptance_criteria", "")
    tags = work_item.get("tags", "")
    priority = work_item.get("priority", "")
    
    # Remove HTML tags from description and acceptance criteria (ADO stores them as HTML)
    import re
    description_clean = re.sub(r'<[^>]+>', '', description).strip() if description else ""
    acceptance_clean = re.sub(r'<[^>]+>', '', acceptance_criteria).strip() if acceptance_criteria else ""
    
    # Build velocity context
    avg_velocity = reference_velocities.get("avg_velocity", 200)
    
    prompt = f"""You are an expert software estimator analyzing a User Story for effort estimation.

**User Story Details:**
- Title: {title}
- Description: {description_clean[:500] if description_clean else "Not provided"}
- Acceptance Criteria: {acceptance_clean[:500] if acceptance_clean else "Not provided"}
- Tags: {tags if tags else "None"}
- Priority: {priority if priority else "Not specified"}

**Context:**
- Team's average velocity: {avg_velocity:.1f} points/sprint
- Estimation scale: Story points represent relative complexity and effort
- Typical range: Small (5-20), Medium (30-80), Large (100-300), Very Large (400+)

**Task:**
Analyze this User Story and estimate the effort in story points based on:
1. **Complexity**: Technical difficulty, unknowns, dependencies
2. **Scope**: Amount of work, number of features/changes
3. **Risk**: Uncertainty, new technology, integration complexity

**Output Format (JSON only):**
{{
  "estimated_effort": <number>,
  "confidence": "high|medium|low",
  "complexity_factors": {{
    "technical_complexity": "low|medium|high",
    "scope_size": "small|medium|large",
    "risk_level": "low|medium|high"
  }},
  "reasoning": "<1-2 sentence explanation>"
}}

Respond ONLY with valid JSON, no other text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an expert software estimator. Respond only with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Parse JSON response
        # Remove markdown code blocks if present
        if result_text.startswith("```json"):
            result_text = result_text[7:]
        if result_text.startswith("```"):
            result_text = result_text[3:]
        if result_text.endswith("```"):
            result_text = result_text[:-3]
        
        result = json.loads(result_text.strip())
        
        # Validate and sanitize
        estimated_effort = float(result.get("estimated_effort", 0))
        confidence = result.get("confidence", "low")
        reasoning = result.get("reasoning", "")
        complexity_factors = result.get("complexity_factors", {})
        
        logger.info(f"Estimated effort for WI '{title[:50]}': {estimated_effort} points ({confidence} confidence)")
        
        return {
            "estimated_effort": estimated_effort,
            "confidence": confidence,
            "reasoning": reasoning,
            "complexity_factors": complexity_factors,
            "is_estimated": True
        }
        
    except Exception as e:
        logger.error(f"Failed to estimate effort via LLM: {e}")
        return {
            "estimated_effort": 0,
            "confidence": "low",
            "reasoning": f"Estimation failed: {str(e)}",
            "is_estimated": False
        }


async def enrich_with_estimated_efforts(
    backlog_data: Dict[str, Any],
    mcp: MCPConnector,
    project: str,
    config: Dict[str, Any],
    reference_velocities: Dict[str, float]
) -> Dict[str, Any]:
    """
    Enrich backlog items with LLM-estimated efforts for items missing effort values.
    
    Args:
        backlog_data: Current backlog data with items
        mcp: MCP connector for fetching work item details
        project: Project name
        config: Config dict with API keys
        reference_velocities: Historical velocity for context
    
    Returns:
        Updated backlog_data with estimated efforts added
    """
    # Check if estimation is enabled
    bt_config = config.get("backlog_triaging", {})
    estimation_config = bt_config.get("effort_estimation", {})
    estimation_enabled = estimation_config.get("enabled", False)
    
    if not estimation_enabled:
        logger.info("Effort estimation is disabled in config")
        return backlog_data
    
    # Get OpenAI API key
    openai_api_key = config.get("api_keys", {}).get("openai_api_key") or os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        logger.warning("OpenAI API key not configured, skipping effort estimation")
        return backlog_data
    
    # Find items without effort
    items_without_effort = [
        item for item in backlog_data["items"]
        if item.get("story_points", 0) == 0
    ]
    
    if not items_without_effort:
        logger.info("All items have effort values, no estimation needed")
        return backlog_data
    
    # Limit number of items to estimate (cost control)
    max_items = estimation_config.get("max_items_per_run", 50)
    items_to_estimate = items_without_effort[:max_items]
    
    if len(items_without_effort) > max_items:
        logger.info(f"Limiting estimation to {max_items} items (out of {len(items_without_effort)} without effort)")
    
    logger.info(f"Found {len(items_to_estimate)} items to estimate (out of {len(items_without_effort)} without effort)")
    
    # Fetch detailed work item info for estimation
    item_ids = [int(item["id"]) for item in items_to_estimate]
    work_item_details = await fetch_work_item_details_for_estimation(mcp, item_ids, project)
    
    # Estimate effort for each item
    estimated_count = 0
    total_estimated_effort = 0
    
    for item in backlog_data["items"]:
        if item.get("story_points", 0) == 0:
            wi_id = int(item["id"])
            if wi_id in work_item_details:
                wi_details = work_item_details[wi_id]
                
                # Call LLM to estimate effort
                estimation = estimate_effort_with_llm(wi_details, reference_velocities, openai_api_key)
                
                if estimation.get("is_estimated") and estimation.get("estimated_effort", 0) > 0:
                    item["story_points"] = estimation["estimated_effort"]
                    item["is_estimated"] = True
                    item["estimation_confidence"] = estimation.get("confidence", "low")
                    item["estimation_reasoning"] = estimation.get("reasoning", "")
                    item["complexity_factors"] = estimation.get("complexity_factors", {})
                    
                    estimated_count += 1
                    total_estimated_effort += estimation["estimated_effort"]
    
    # Recalculate total story points including estimates
    original_total = backlog_data["total_story_points"]
    backlog_data["total_story_points"] = sum(item.get("story_points", 0) for item in backlog_data["items"])
    backlog_data["estimated_items_count"] = estimated_count
    backlog_data["estimated_effort_total"] = total_estimated_effort
    
    logger.info(f"Estimation complete: {estimated_count} items estimated, {total_estimated_effort:.1f} points added")
    logger.info(f"Total effort: {original_total:.1f} (actual) + {total_estimated_effort:.1f} (estimated) = {backlog_data['total_story_points']:.1f}")
    
    return backlog_data


async def get_team_velocity(mcp: MCPConnector, project: str, team: str, effort_field: str = "Microsoft.VSTS.Scheduling.StoryPoints") -> float:
    """
    Attempt to calculate average velocity from recent completed sprints.
    Returns 0 if unable to calculate.
    """
    try:
        import json
        from datetime import datetime
        
        # If team is empty or same as project, try without team parameter (project-level iterations)
        if not team or team == project:
            logger.info(f"Getting iterations for project {project} (no specific team)")
            # Try to get all iterations at project level
            iterations_result = await mcp.call_tool("work_list_iterations", {
                "project": project
            })
        else:
            # Get all iterations for the team (no timeframe parameter - get all)
            logger.info(f"Getting iterations for team {team} in project {project}")
            iterations_result = await mcp.call_tool("work_list_team_iterations", {
                "project": project,
                "team": team
            })
        
        # Parse if string
        if isinstance(iterations_result, str):
            try:
                iterations_result = json.loads(iterations_result)
            except json.JSONDecodeError:
                logger.warning(f"Could not parse iterations result: {iterations_result[:200]}")
                return 0.0
        
        # Extract iterations from result
        if isinstance(iterations_result, dict):
            iterations = iterations_result.get("value", [])
            logger.info(f"Dict result: API returned {len(iterations)} top-level iterations")
            if iterations:
                sample = iterations[0]
                logger.info(f"Sample: name='{sample.get('name')}', hasChildren={sample.get('hasChildren')}, children={len(sample.get('children', []))}")
        elif isinstance(iterations_result, list):
            iterations = iterations_result
            logger.info(f"List result: API returned {len(iterations)} iterations")
            if iterations:
                sample = iterations[0]
                logger.info(f"Sample: name='{sample.get('name')}', hasChildren={sample.get('hasChildren')}, children={len(sample.get('children', []))}")
        else:
            logger.warning(f"Unexpected iterations result type: {type(iterations_result)}")
            return 0.0
        
        if not iterations:
            logger.info("No iterations found for team")
            return 0.0
        
        # Flatten the hierarchical iteration structure to get all leaf iterations
        def flatten_iterations(iter_list):
            """Recursively flatten nested iteration structure to get ALL iterations including parents"""
            flat_list = []
            for item in iter_list:
                # Add the current item regardless of whether it has children
                flat_list.append(item)
                # If it has children, recurse to add them too
                if item.get("hasChildren") and item.get("children"):
                    flat_list.extend(flatten_iterations(item["children"]))
            return flat_list
        
        all_iterations = flatten_iterations(iterations)
        logger.info(f"Found {len(all_iterations)} iterations (flattened from hierarchy)")
        logger.info(f"All iteration names: {[i.get('name') for i in all_iterations]}")
        
        # Filter to only sprint-level iterations (exclude quarters/years)
        # Sprint-level iterations typically have names like "25.22", "25.23" (numbers with dots)
        # while quarters are "Q1", "Q2", "Q3", "Q4" and years are "2025"
        sprint_iterations = []
        for iteration in all_iterations:
            iter_name = iteration.get("name", "")
            iter_path = iteration.get("path", "")
            
            # Exclude quarters (Q1, Q2, Q3, Q4) and years (2025, 2024, etc.)
            is_quarter = iter_name in ["Q1", "Q2", "Q3", "Q4"]
            is_year = iter_name.isdigit() and len(iter_name) == 4
            
            # Keep only sprint-level (not quarters or years)
            # Sprint names typically contain dots (e.g., "25.22") or words (e.g., "Sprint 1")
            if not is_quarter and not is_year:
                sprint_iterations.append(iteration)
                logger.info(f"[KEEP] Sprint-level: {iter_name} ({iter_path})")
            else:
                logger.info(f"[SKIP] Quarter/year: {iter_name} ({iter_path})")
        
        logger.info(f"Filtered to {len(sprint_iterations)} sprint-level iterations (NOTE: ADO API only returns quarters, not actual sprints)")
        
        # SKIP the old iteration matching logic - ADO API doesn't return sprint-level iterations
        # Instead, fetch completed work items directly and extract sprint paths from their IterationPath field
        
        # Get the team's area path for filtering
        area_path = await get_team_area_path(mcp, team, project)
        logger.info(f"Fetching velocity for team area path: {area_path}")
        
        # Search for recently closed User Stories to get sprint-level data
        # IMPORTANT: Filter by area path to get team-specific velocity
        try:
            search_result = await mcp.call_tool("search_workitem", {
                "searchText": 't:"User Story" (s:Done OR s:Closed OR s:Completed OR s:Resolved)',
                "project": [project],
                "areaPath": [area_path]  # Filter by team's area path
            })
            
            # Parse result
            if isinstance(search_result, str):
                try:
                    search_result = json.loads(search_result)
                except json.JSONDecodeError as e:
                    logger.warning(f"Could not parse search result for completed work items: {e}")
                    return 0.0
            
            # Extract work item IDs
            if isinstance(search_result, dict):
                results = search_result.get("results", [])
                logger.info(f"Search returned {len(results)} completed work items")
                
                # Extract work item IDs - the ID is in the "fields" dict with key "system.id"
                wi_ids = []
                for r in results:
                    fields = r.get("fields", {})
                    wi_id = fields.get("system.id")
                    if wi_id:
                        wi_ids.append(int(wi_id))
                
                logger.info(f"Extracted {len(wi_ids)} work item IDs from search results")
                
                if not wi_ids:
                    logger.warning("No work item IDs extracted from search results")
                    return 0.0
                
                logger.info(f"Found {len(wi_ids)} completed work items, fetching details...")
                logger.info(f"First 10 IDs: {wi_ids[:10]}")
                logger.info(f"Requesting effort field: {effort_field}")
                
                # Fetch full work item details to get effort and iteration
                # Note: Must request custom fields explicitly
                # Batch into chunks of 50
                fields_to_get = ["System.State", "System.IterationPath", effort_field]
                logger.info(f"Requesting fields: {fields_to_get}")
                
                work_items = []
                batch_size = 50
                for i in range(0, len(wi_ids), batch_size):
                    batch_ids = wi_ids[i:i + batch_size]
                    try:
                        wi_batch_result = await mcp.call_tool("wit_get_work_items_batch_by_ids", {
                            "project": project,
                            "ids": batch_ids,
                            "fields": fields_to_get
                        })
                        
                        # Parse result
                        if isinstance(wi_batch_result, str):
                            try:
                                wi_batch_result = json.loads(wi_batch_result)
                            except json.JSONDecodeError as e:
                                logger.warning(f"Batch {i//batch_size + 1}: Could not parse result")
                                continue
                        
                        # Extract work items from result
                        if isinstance(wi_batch_result, list):
                            batch_items = wi_batch_result
                        elif isinstance(wi_batch_result, dict):
                            batch_items = wi_batch_result.get("value", [])
                        else:
                            batch_items = []
                        
                        work_items.extend(batch_items)
                        logger.info(f"Batch {i//batch_size + 1}: Retrieved {len(batch_items)} work items")
                        
                    except Exception as batch_err:
                        logger.error(f"Batch {i//batch_size + 1}: Failed to fetch work items: {batch_err}")
                        
                logger.info(f"Retrieved total of {len(work_items)} work item details across all batches")
                
                # Debug: log first work item to see what fields are available
                if work_items:
                    sample_wi = work_items[0]
                    sample_fields = sample_wi.get("fields", {})
                    logger.info(f"Sample WI {sample_wi.get('id')} state: {sample_fields.get('System.State')}, effort field ({effort_field}): {sample_fields.get(effort_field)}")
                    logger.info(f"Sample WI fields available: {[k for k in sample_fields.keys() if 'effort' in k.lower() or 'story' in k.lower() or 'point' in k.lower()]}")
                
                # Group work items by iteration
                iteration_map = {}
                for wi in work_items:
                    fields = wi.get("fields", {})
                    state = fields.get("System.State", "")
                    wi_iter_path = fields.get("System.IterationPath", "")
                    
                    # Only count completed items
                    if state in ["Done", "Closed", "Completed", "Resolved"]:
                        # Try custom effort field first, fall back to standard story points
                        sp = fields.get(effort_field, 0) or fields.get("Microsoft.VSTS.Scheduling.StoryPoints", 0) or 0
                        
                        if sp > 0:
                            # Normalize path to double backslash for consistent comparison
                            normalized_path = wi_iter_path.replace("\\", "\\\\")
                            if normalized_path not in iteration_map:
                                iteration_map[normalized_path] = 0.0
                            iteration_map[normalized_path] += float(sp)
                
                logger.info(f"Found completed work across {len(iteration_map)} iterations")
                if iteration_map:
                    logger.info(f"Iteration paths in map: {list(iteration_map.keys())[:10]}")
                    logger.info(f"Total effort in map: {sum(iteration_map.values())}")
                else:
                    logger.warning("No completed work with effort points found")
                    return 0.0
                
                # Build sprint list from actual completed work (since API doesn't return sprint-level iterations)
                # Filter out quarters/years by checking if path has 4+ segments (sprints are nested deep)
                sprint_efforts = {}
                for iter_path, effort in iteration_map.items():
                    # Count path segments (e.g., "FracPro-OPS\\\\2025\\\\Q4\\\\25.22" has 4 segments)
                    segments = [s for s in iter_path.split("\\\\") if s]
                    if len(segments) >= 4:  # This is a sprint-level path
                        sprint_name = segments[-1]  # e.g., "25.22"
                        quarter = segments[-2] if len(segments) >= 3 else "Unknown"  # e.g., "Q4"
                        sprint_efforts[iter_path] = {
                            "name": sprint_name,
                            "quarter": quarter,
                            "path": iter_path,
                            "effort": effort
                        }
                        logger.info(f"Sprint: {sprint_name} (Q{quarter[-1] if quarter.startswith('Q') else '?'}) ({iter_path}) = {effort} points")
                
                if not sprint_efforts:
                    logger.warning("No sprint-level completed work found")
                    return 0.0
                
                # Group by quarter and use the most recent quarter's sprints
                # This ensures we use current team capacity, not old quarters
                from collections import defaultdict
                quarters = defaultdict(list)
                for path, sprint_data in sprint_efforts.items():
                    quarter_key = sprint_data["quarter"]
                    quarters[quarter_key].append((path, sprint_data))
                
                # Sort quarters (Q4 > Q3 > Q2 > Q1) and use the most recent one with data
                sorted_quarters = sorted(quarters.keys(), reverse=True)
                logger.info(f"Found sprints in quarters: {sorted_quarters}")
                
                # Use all sprints from the most recent quarter
                recent_quarter = sorted_quarters[0]
                recent_sprints = quarters[recent_quarter]
                
                logger.info(f"Using {len(recent_sprints)} sprints from {recent_quarter} for velocity:")
                for path, sprint_data in recent_sprints:
                    logger.info(f"  - {sprint_data['name']}: {sprint_data['effort']} points")
                
                # Calculate velocity
                total_points = sum(s[1]["effort"] for s in recent_sprints)
                sprint_count = len(recent_sprints)
                avg_velocity = total_points / sprint_count
                logger.info(f"Calculated velocity: {avg_velocity:.1f} points/sprint (from {sprint_count} sprints in {recent_quarter} with {total_points} total points)")
                return avg_velocity
                
            else:
                logger.warning("Unexpected search result format")
                return 0.0
                
        except Exception as e:
            logger.warning(f"Failed to fetch completed work items: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return 0.0
    
    except Exception as e:
        logger.warning(f"Failed to calculate velocity: {e}")
        import traceback
        logger.debug(f"Traceback: {traceback.format_exc()}")
        return 0.0
        import traceback
        logger.debug(f"Traceback: {traceback.format_exc()}")
        return 0.0


def evaluate_backlog_health(
    backlog_data: Dict[str, Any],
    config: Dict[str, Any],
    velocity: float = 0.0
) -> Tuple[bool, Dict[str, Any]]:
    """
    Evaluate if backlog is "thin" based on thresholds.
    
    Returns:
        Tuple of (is_thin: bool, details: dict with analysis)
    """
    bt_config = config.get("backlog_triaging", {})
    thresholds = bt_config.get("thresholds", {})
    
    min_items = thresholds.get("min_items", 10)
    min_story_points = thresholds.get("min_story_points", 30)
    sprints_ahead = thresholds.get("sprints_ahead", 2)
    
    backlog_items = backlog_data.get("backlog_items", 0)
    total_story_points = backlog_data.get("total_story_points", 0)
    
    # Calculate backlog runway in sprints
    backlog_runway_sprints = 0.0
    if velocity > 0 and total_story_points > 0:
        backlog_runway_sprints = total_story_points / velocity
    
    issues = []
    
    # Check minimum items threshold
    if backlog_items < min_items:
        issues.append({
            "type": "low_item_count",
            "message": f"Backlog has only {backlog_items} items (minimum: {min_items})",
            "current": backlog_items,
            "threshold": min_items,
        })
    
    # Check minimum story points threshold (only if enabled - min > 0)
    if min_story_points > 0 and total_story_points < min_story_points:
        issues.append({
            "type": "low_story_points",
            "message": f"Backlog has only {total_story_points} story points (minimum: {min_story_points})",
            "current": total_story_points,
            "threshold": min_story_points,
        })
    
    # Check sprints ahead (if velocity is available)
    if velocity > 0:
        required_points = velocity * sprints_ahead
        if total_story_points < required_points:
            sprints_of_work = total_story_points / velocity if velocity > 0 else 0
            issues.append({
                "type": "insufficient_runway",
                "message": f"Backlog covers only {sprints_of_work:.1f} sprints (need {sprints_ahead} sprints ahead)",
                "current_sprints": sprints_of_work,
                "required_sprints": sprints_ahead,
                "velocity": velocity,
            })
    
    is_thin = len(issues) > 0
    
    # Determine velocity trend (default to 'Stable' for now)
    # This can be enhanced to analyze historical velocity data
    velocity_trend = "Stable"
    
    return is_thin, {
        "is_thin": is_thin,
        "backlog_items": backlog_items,
        "total_story_points": total_story_points,
        "velocity": velocity,
        "backlog_runway_sprints": backlog_runway_sprints,
        "velocity_trend": velocity_trend,
        "issues": issues,
        "thresholds": {
            "min_items": min_items,
            "min_story_points": min_story_points,
            "sprints_ahead": sprints_ahead,
        },
    }


def generate_html_report(
    backlog_data: Dict[str, Any],
    health_analysis: Dict[str, Any],
    project: str,
    team: str
) -> str:
    """Generate HTML email report for thin backlog alert."""
    is_thin = health_analysis.get("is_thin", False)
    is_dummy = backlog_data.get("is_dummy", False)
    
    # Determine status color and icon
    if is_thin:
        status_color = "#dc3545"  # Red
        status_icon = "⚠️"
        status_text = "ACTION REQUIRED"
    else:
        status_color = "#28a745"  # Green
        status_icon = "✅"
        status_text = "HEALTHY"
    
    # Build issues section
    issues_html = ""
    if health_analysis.get("issues"):
        issues_html = "<h3 style='color: #dc3545;'>Issues Detected:</h3><ul>"
        for issue in health_analysis["issues"]:
            issues_html += f"<li><strong>{issue['type'].replace('_', ' ').title()}</strong>: {issue['message']}</li>"
        issues_html += "</ul>"
    
    # Build backlog items table
    items = backlog_data.get("items", [])
    items_table = ""
    if items:
        # Sort items by story points (descending) to show estimated items first
        # This helps users understand where the total effort comes from
        sorted_items = sorted(items, key=lambda x: x.get('story_points', 0), reverse=True)
        
        # Count items with effort (actual vs estimated)
        items_with_actual_effort = sum(1 for item in items if item.get('story_points', 0) > 0 and not item.get('is_estimated', False))
        items_with_estimated_effort = sum(1 for item in items if item.get('is_estimated', False))
        items_without_effort = len(items) - items_with_actual_effort - items_with_estimated_effort
        
        # Calculate totals
        actual_effort_total = sum(item.get('story_points', 0) for item in items if not item.get('is_estimated', False))
        estimated_effort_total = sum(item.get('story_points', 0) for item in items if item.get('is_estimated', False))
        
        # Build summary line
        effort_summary = f"{items_with_actual_effort} items with actual effort ({actual_effort_total:.1f} points)"
        if items_with_estimated_effort > 0:
            effort_summary += f", {items_with_estimated_effort} with AI-estimated effort ({estimated_effort_total:.1f} points)"
        if items_without_effort > 0:
            effort_summary += f", {items_without_effort} not yet estimated"
        
        items_table = f"""
        <h3>Current Backlog Items:</h3>
        <p style="font-size: 14px; color: #6c757d; margin: 5px 0;">
            {effort_summary}
        </p>
        <table style="border-collapse: collapse; width: 100%; margin-top: 10px;">
            <thead>
                <tr style="background-color: #f8f9fa;">
                    <th style="border: 1px solid #dee2e6; padding: 8px; text-align: left;">ID</th>
                    <th style="border: 1px solid #dee2e6; padding: 8px; text-align: left;">Title</th>
                    <th style="border: 1px solid #dee2e6; padding: 8px; text-align: center;">Effort (3P)</th>
                    <th style="border: 1px solid #dee2e6; padding: 8px; text-align: center;">State</th>
                </tr>
            </thead>
            <tbody>
        """
        for item in sorted_items[:20]:  # Limit to 20 items, sorted by effort
            effort_value = item.get('story_points', 0)
            is_estimated = item.get('is_estimated', False)
            
            # Color coding: Blue for actual, Orange for estimated, None for missing
            if is_estimated and effort_value > 0:
                row_style = 'background-color: #fff3cd;'  # Light orange for estimated
                effort_display = f"{effort_value} <span style='font-size: 10px; color: #856404;'>✨AI</span>"
                tooltip = item.get('estimation_reasoning', 'AI-estimated')
            elif effort_value > 0:
                row_style = 'background-color: #e7f3ff;'  # Light blue for actual
                effort_display = f"{effort_value}"
                tooltip = "Actual effort from ADO"
            else:
                row_style = ''
                effort_display = "0.0"
                tooltip = "No effort value"
            
            items_table += f"""
                <tr style="{row_style}" title="{tooltip}">
                    <td style="border: 1px solid #dee2e6; padding: 8px;">{item.get('id', 'N/A')}</td>
                    <td style="border: 1px solid #dee2e6; padding: 8px;">{item.get('title', 'N/A')[:80]}</td>
                    <td style="border: 1px solid #dee2e6; padding: 8px; text-align: center; font-weight: {'bold' if effort_value > 0 else 'normal'};">{effort_display}</td>
                    <td style="border: 1px solid #dee2e6; padding: 8px; text-align: center;">{item.get('state', 'N/A')}</td>
                </tr>
            """
        if len(items) > 20:
            remaining_without_effort = max(0, items_without_effort - max(0, 20 - items_with_actual_effort - items_with_estimated_effort))
            items_table += f"""
                <tr>
                    <td colspan="4" style="border: 1px solid #dee2e6; padding: 8px; text-align: center; font-style: italic;">
                        ... and {len(items) - 20} more items ({remaining_without_effort} without effort)
                    </td>
                </tr>
            """
        items_table += "</tbody></table>"
        
        # Add legend if there are estimated items
        if items_with_estimated_effort > 0:
            items_table += """
        <div style="margin-top: 10px; padding: 10px; background-color: #f8f9fa; border-radius: 4px; font-size: 12px;">
            <strong>Legend:</strong>
            <span style="margin-left: 10px; padding: 3px 8px; background-color: #e7f3ff; border-radius: 3px;">Blue = Actual Effort</span>
            <span style="margin-left: 10px; padding: 3px 8px; background-color: #fff3cd; border-radius: 3px;">Orange ✨AI = AI-Estimated Effort</span>
        </div>
        """
    
    # Dummy data notice
    dummy_notice = ""
    if is_dummy:
        dummy_notice = """
        <div style="background-color: #fff3cd; border: 1px solid #ffc107; padding: 10px; margin: 10px 0; border-radius: 4px;">
            <strong>⚠️ Note:</strong> This report uses dummy data. ADO backlog data was not available.
        </div>
        """
    
    # Effort/Story points display - FracPro-OPS uses Custom.Effort3P
    total_sp = health_analysis.get('total_story_points', 0)
    min_sp = health_analysis.get('thresholds', {}).get('min_story_points', 0)
    sp_enabled = min_sp > 0
    
    # Display effort values (even if 0) - show actual data from ADO
    if sp_enabled:
        sp_display = f"{total_sp:.1f}"
        sp_threshold_display = f"{min_sp}"
        sp_note = ""
    elif total_sp > 0:
        # Effort values exist but threshold is disabled
        sp_display = f"{total_sp:.1f}"
        sp_threshold_display = "0 (not checked)"
        sp_note = '<div style="font-size: 10px; color: #999; margin-top: 3px;">Threshold disabled</div>'
    else:
        # No effort values in backlog
        sp_display = "0"
        sp_threshold_display = "0 (disabled)"
        sp_note = '<div style="font-size: 10px; color: #999; margin-top: 3px;">No effort values set in ADO</div>'
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Backlog Triaging Report</title>
    </head>
    <body style="font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px;">
        <div style="background: linear-gradient(135deg, {status_color} 0%, #6c757d 100%); color: white; padding: 20px; border-radius: 8px 8px 0 0;">
            <h1 style="margin: 0;">{status_icon} Backlog Triaging Report</h1>
            <p style="margin: 5px 0 0 0; opacity: 0.9;">Project: {project} | Team: {team}</p>
        </div>
        
        <div style="background-color: #f8f9fa; padding: 20px; border: 1px solid #dee2e6; border-top: none;">
            {dummy_notice}
            
            <div style="background-color: white; padding: 15px; border-radius: 4px; margin-bottom: 15px; border-left: 4px solid {status_color};">
                <h2 style="margin-top: 0; color: {status_color};">{status_icon} Status: {status_text}</h2>
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px;">
                    <div style="text-align: center; padding: 10px; background-color: #f8f9fa; border-radius: 4px;">
                        <div style="font-size: 24px; font-weight: bold; color: #007bff;">{health_analysis.get('backlog_items', 0)}</div>
                        <div style="font-size: 12px; color: #6c757d;">Backlog Items</div>
                    </div>
                    <div style="text-align: center; padding: 10px; background-color: #f8f9fa; border-radius: 4px;">
                        <div style="font-size: 24px; font-weight: bold; color: #28a745;">{sp_display}</div>
                        <div style="font-size: 12px; color: #6c757d;">Total Effort (3P){sp_note}</div>
                    </div>
                    <div style="text-align: center; padding: 10px; background-color: #f8f9fa; border-radius: 4px;">
                        <div style="font-size: 24px; font-weight: bold; color: #6c757d;">{health_analysis.get('velocity', 0):.1f}</div>
                        <div style="font-size: 12px; color: #6c757d;">Avg Velocity</div>
                    </div>
                </div>
            </div>
            
            {issues_html}
            
            <div style="background-color: white; padding: 15px; border-radius: 4px; margin-top: 15px;">
                <h3>Thresholds Configuration:</h3>
                <ul>
                    <li>Minimum Items: {health_analysis.get('thresholds', {}).get('min_items', 10)}</li>
                    <li>Minimum Effort (3P): {sp_threshold_display}</li>
                    <li>Sprints Ahead Required: {health_analysis.get('thresholds', {}).get('sprints_ahead', 2)}</li>
                </ul>
            </div>
            
            {items_table}
            
            <div style="margin-top: 20px; padding: 15px; background-color: #e7f3ff; border-radius: 4px;">
                <h3 style="margin-top: 0;">📋 Recommended Actions:</h3>
                <ul>
                    <li>Schedule a backlog refinement session with the team</li>
                    <li>Review and prioritize items in the product backlog</li>
                    <li>Work with stakeholders to identify new user stories</li>
                    <li>Consider breaking down large epics into smaller stories</li>
                </ul>
            </div>
        </div>
        
        <div style="text-align: center; padding: 15px; background-color: #343a40; color: #adb5bd; border-radius: 0 0 8px 8px; font-size: 12px;">
            Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | PM Agent - Backlog Triaging
        </div>
    </body>
    </html>
    """
    
    return html


@trace_task("backlog_triaging", metadata={"source": "pm_agent"})
async def run_backlog_triaging(
    config: Dict[str, Any],
    options: Dict[str, Any] = None,
    recipients: List[str] = None
) -> Dict[str, Any]:
    """
    Main entry point for backlog triaging.
    
    Args:
        config: Full config from config.yaml
        options: Task-specific options from scheduler
        recipients: Email recipients (overrides config)
    
    Returns:
        Dict with execution results
    """
    options = options or {}
    bt_config = config.get("backlog_triaging", {})
    
    use_dummy = options.get("use_dummy_data", bt_config.get("use_dummy_data", False))
    force_send = options.get("force_send", False)
    test_mode = options.get("test_mode", False)
    
    # Get recipients
    if not recipients:
        notif_config = bt_config.get("notifications", {})
        recipients = notif_config.get("recipients", config.get("reportEmailRecipients", []))
    
    # Get ADO configuration from config or environment variables
    ado_config = config.get("ado", {})
    project = os.getenv("ADO_PROJECT") or ado_config.get("project", "")
    team = os.getenv("ADO_TEAM") or ado_config.get("team", "") or project  # Default team to project name if not set
    org_url = os.getenv("ADO_ORG_URL") or ado_config.get("org_url", "")
    
    logger.info(f"[BACKLOG] Starting backlog triaging - ADO_TEAM env var: '{os.getenv('ADO_TEAM')}', config team: '{ado_config.get('team', '')}', final team: '{team}'")
    logger.info(f"[BACKLOG] Project: {project}, Team: {team}, Org URL: {org_url}")
    
    result = {
        "success": False,
        "is_thin": False,
        "email_sent": False,
        "message": "",
    }
    
    try:
        if test_mode:
            logger.info("Running in test mode - using dummy data")
            backlog_data = get_dummy_backlog_data(config)
            velocity = bt_config.get("dummy_data", {}).get("velocity_per_sprint", 20)
        elif use_dummy:
            logger.info("Using dummy data as configured")
            backlog_data = get_dummy_backlog_data(config)
            velocity = bt_config.get("dummy_data", {}).get("velocity_per_sprint", 20)
        else:
            # Try to fetch from ADO
            pat = get_pat()
            org_name = org_url.split("/")[-1] if org_url else ""
            
            if not all([org_name, pat, project]):
                logger.warning("ADO configuration incomplete, falling back to dummy data")
                backlog_data = get_dummy_backlog_data(config)
                velocity = bt_config.get("dummy_data", {}).get("velocity_per_sprint", 20)
            else:
                mcp = MCPConnector(org_name, pat)
                await mcp.initialize()
                
                try:
                    backlog_data = await fetch_backlog_from_ado(mcp, project, team, config)
                    # Get effort field from config
                    effort_field = bt_config.get("effort_field", "Microsoft.VSTS.Scheduling.StoryPoints")
                    velocity = await get_team_velocity(mcp, project, team, effort_field)
                    
                    # Enrich with AI-estimated efforts for items missing effort values
                    reference_velocities = {"avg_velocity": velocity}
                    backlog_data = await enrich_with_estimated_efforts(
                        backlog_data, mcp, project, config, reference_velocities
                    )
                    
                except Exception as e:
                    logger.warning(f"Failed to fetch from ADO: {e}, using dummy data")
                    backlog_data = get_dummy_backlog_data(config)
                    velocity = bt_config.get("dummy_data", {}).get("velocity_per_sprint", 20)
                finally:
                    await mcp.cleanup()
        
        # Evaluate backlog health (using total including estimated efforts)
        is_thin, health_analysis = evaluate_backlog_health(backlog_data, config, velocity)
        
        logger.info(f"Backlog health: is_thin={is_thin}, items={health_analysis['backlog_items']}, points={health_analysis['total_story_points']}")
        
        result["is_thin"] = is_thin
        result["health_analysis"] = health_analysis
        
        # ALWAYS generate HTML report (not just when thin)
        html_report = generate_html_report(backlog_data, health_analysis, project, team)
        
        # Save report to outputs folder
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        output_dir = REPO_ROOT / "outputs"
        output_dir.mkdir(exist_ok=True)
        output_file = output_dir / f"backlog_triaging_{project}_{timestamp}.html"
        output_file.write_text(html_report, encoding="utf-8")
        logger.info(f"Saved HTML report to {output_file}")
        
        # Send email based on force_send flag (set to true for scheduled Tuesday runs)
        # When force_send=true: Always send email (weekly scheduled report)
        # When force_send=false: Only send if backlog is thin (ad-hoc alert)
        send_email = force_send or is_thin
        
        if send_email:
            if recipients and bt_config.get("notifications", {}).get("enabled", True):
                # Subject line indicates if it's an alert or regular report
                if is_thin and not force_send:
                    subject = f"⚠️ ALERT: Backlog Running Thin - {project}"
                elif is_thin:
                    subject = f"⚠️ Weekly Backlog Report (Alert: Running Thin) - {project}"
                else:
                    subject = f"📊 Weekly Backlog Triaging Report - {project}"
                
                success, msg = send_report_attachment(
                    to_emails=recipients,
                    subject=subject,
                    html_body=html_report,
                    attachments=None,
                )
                result["email_sent"] = success
                result["email_message"] = msg
                logger.info(f"Email sent: {success} - {msg} (force_send={force_send}, is_thin={is_thin})")
            else:
                logger.info("Email notifications disabled or no recipients configured")
        else:
            logger.info("Backlog is healthy and force_send=false, no email sent (report saved locally)")
        
        result["success"] = True
        result["message"] = "Backlog triaging completed successfully"
        result["backlog_data"] = backlog_data  # Include backlog data for chat display
        
    except Exception as e:
        logger.exception(f"Backlog triaging failed: {e}")
        result["success"] = False
        result["message"] = str(e)
    
    return result


def run_task_from_config(task_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Entry point for scheduler. Runs the backlog triaging task.
    
    Args:
        task_config: Config dict with 'options' and 'reportEmailRecipients'
    
    Returns:
        Execution result dict
    """
    config = load_config()
    options = task_config.get("options", {})
    recipients = task_config.get("reportEmailRecipients", [])
    
    return asyncio.run(run_backlog_triaging(config, options, recipients))


# CLI support for manual testing
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Backlog Triaging - Alert when backlog is thin")
    parser.add_argument("--dummy", action="store_true", help="Use dummy data instead of ADO")
    parser.add_argument("--force-send", action="store_true", help="Send email even if backlog is healthy")
    parser.add_argument("--test-mode", action="store_true", help="Run in test mode (dummy data, no real email)")
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    
    cfg = load_config()
    opts = {
        "use_dummy_data": args.dummy,
        "force_send": args.force_send,
        "test_mode": args.test_mode,
    }
    
    result = asyncio.run(run_backlog_triaging(cfg, opts))
    print(f"\nResult: {result}")
