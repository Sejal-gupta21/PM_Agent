"""
Backlog Tool Handlers - Implementations for backlog triaging operations.

Each handler is a concrete implementation of a tool defined in backlog_tools.py.
Handlers orchestrate MCP calls, LLM operations, and business logic.
"""

import logging
import json
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("pm_skill_agent.backlog_handlers")


# ============================================================================
# HANDLER IMPLEMENTATIONS
# ============================================================================

async def handle_get_team_area_path(
    project: str,
    team: str,
    pm_agent: Any
) -> Dict[str, Any]:
    """
    Get the actual area path for a team by querying work items.
    
    Args:
        project: ADO project name
        team: Team name
        pm_agent: PM Agent instance for MCP calls
    
    Returns:
        {
            "area_path": str,
            "confidence": "high" | "medium" | "low",
            "discovery_method": str
        }
    """
    try:
        # Import here to access the existing implementation
        import sys
        from pathlib import Path
        REPO_ROOT = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(REPO_ROOT))
        
        from scripts.backlog_triaging import get_team_area_path
        from utilities.mcp.mcp_ado_connector import MCPConnector
        from utilities.mcp.pat import get_pat
        
        # Create MCP connector (reuse PM Agent's if available)
        if hasattr(pm_agent, 'mcp_connector'):
            mcp = pm_agent.mcp_connector
        else:
            pat = get_pat()
            mcp = MCPConnector(pat=pat)
        
        # Call existing implementation
        area_path = await get_team_area_path(mcp, team, project)
        
        # Determine confidence based on result
        if area_path and area_path != project:
            confidence = "high"
            discovery_method = "work_item_query"
        elif area_path == project:
            confidence = "medium"
            discovery_method = "project_default"
        else:
            confidence = "low"
            discovery_method = "fallback"
            area_path = project
        
        logger.info(f"Discovered area path for {team}: {area_path} (confidence: {confidence})")
        
        return {
            "area_path": area_path,
            "confidence": confidence,
            "discovery_method": discovery_method
        }
        
    except Exception as e:
        logger.error(f"Error getting team area path: {e}", exc_info=True)
        # Fallback to project name
        return {
            "area_path": project,
            "confidence": "low",
            "discovery_method": "error_fallback",
            "error": str(e)
        }


async def handle_get_backlog_items(
    project: str,
    team: str,
    pm_agent: Any,
    area_path: Optional[str] = None,
    include_states: Optional[List[str]] = None,
    effort_field: str = "Custom.Effort3P",
    max_items: int = 1000
) -> Dict[str, Any]:
    """
    Fetch backlog items from Azure DevOps.
    
    Args:
        project: ADO project name
        team: Team name
        pm_agent: PM Agent instance
        area_path: Team area path (auto-detected if None)
        include_states: States to include (default: ["New", "Ready", "Requested", "Scheduled", "In Planning", "Accepted"])
        effort_field: Effort field name
        max_items: Maximum items to fetch
    
    Returns:
        {
            "backlog_items": int,
            "total_story_points": float,
            "items": List[Dict],
            "unestimated_count": int,
            "area_path": str
        }
    """
    try:
        import sys
        from pathlib import Path
        REPO_ROOT = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(REPO_ROOT))
        
        from scripts.backlog_triaging import fetch_backlog_from_ado, load_config
        from utilities.mcp.mcp_ado_connector import MCPConnector
        from utilities.mcp.pat import get_pat
        
        # Load config for defaults
        config = load_config()
        bt_config = config.get("backlog_triaging", {})
        
        # Override config with provided parameters
        if include_states:
            bt_config["include_states"] = include_states
        bt_config["effort_field"] = effort_field
        
        # Create MCP connector
        if hasattr(pm_agent, 'mcp_connector'):
            mcp = pm_agent.mcp_connector
        else:
            pat = get_pat()
            mcp = MCPConnector(pat=pat)
        
        # Fetch backlog data
        logger.info(f"Fetching backlog for {team} in {project} (effort field: {effort_field})")
        backlog_data = await fetch_backlog_from_ado(mcp, project, team, config)
        
        # Add metadata
        backlog_data["area_path"] = area_path or backlog_data.get("area_path", project)
        backlog_data["unestimated_count"] = sum(
            1 for item in backlog_data.get("items", [])
            if not item.get("story_points") or item.get("story_points", 0) == 0
        )
        
        logger.info(
            f"Fetched {backlog_data['backlog_items']} items, "
            f"{backlog_data['total_story_points']:.1f} total points, "
            f"{backlog_data['unestimated_count']} unestimated"
        )
        
        return backlog_data
        
    except Exception as e:
        logger.error(f"Error fetching backlog items: {e}", exc_info=True)
        return {
            "backlog_items": 0,
            "total_story_points": 0.0,
            "items": [],
            "unestimated_count": 0,
            "area_path": area_path or project,
            "error": str(e)
        }


async def handle_calculate_team_velocity(
    project: str,
    team: str,
    pm_agent: Any,
    sprint_count: int = 3,
    effort_field: str = "Custom.Effort3P",
    fallback_capacity: float = 8.0
) -> Dict[str, Any]:
    """
    Calculate team velocity from recent completed sprints.
    
    Args:
        project: ADO project name
        team: Team name
        pm_agent: PM Agent instance
        sprint_count: Number of recent sprints to analyze
        effort_field: Effort field name
        fallback_capacity: Fallback hrs/day if no historical data
    
    Returns:
        {
            "velocity": float,
            "sprints_analyzed": int,
            "velocity_trend": "increasing" | "stable" | "decreasing",
            "confidence": "high" | "medium" | "low",
            "fallback_used": bool
        }
    """
    try:
        import sys
        from pathlib import Path
        REPO_ROOT = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(REPO_ROOT))
        
        from scripts.backlog_triaging import get_team_velocity
        from utilities.mcp.mcp_ado_connector import MCPConnector
        from utilities.mcp.pat import get_pat
        
        # Create MCP connector
        if hasattr(pm_agent, 'mcp_connector'):
            mcp = pm_agent.mcp_connector
        else:
            pat = get_pat()
            mcp = MCPConnector(pat=pat)
        
        # Calculate velocity
        logger.info(f"Calculating velocity for {team} in {project}")
        velocity = await get_team_velocity(mcp, project, team, effort_field)
        
        # Determine confidence and fallback status
        if velocity > 0:
            confidence = "high"
            fallback_used = False
            sprints_analyzed = sprint_count  # Approximation
        else:
            # Fallback: estimate based on team capacity
            logger.warning(f"No historical velocity data for {team}, using capacity-based estimate")
            try:
                # Try to get team capacity
                capacity_result = await mcp.call_tool("work_get_team_capacity", {
                    "project": project,
                    "team": team
                })
                
                if isinstance(capacity_result, str):
                    capacity_result = json.loads(capacity_result)
                
                team_members = capacity_result.get("value", []) if isinstance(capacity_result, dict) else []
                team_size = len(team_members)
                
                if team_size > 0:
                    # Assume 10 working days per sprint, fallback_capacity hrs/day
                    velocity = team_size * fallback_capacity * 10  # Rough estimate
                    logger.info(f"Estimated velocity from capacity: {velocity:.1f} (team size: {team_size})")
                else:
                    # Ultimate fallback
                    velocity = 100.0  # Reasonable default
                    logger.warning(f"Using default velocity estimate: {velocity}")
                    
            except Exception as e:
                logger.error(f"Error getting team capacity: {e}")
                velocity = 100.0
            
            confidence = "low"
            fallback_used = True
            sprints_analyzed = 0
        
        # Simple trend analysis (would need more data for accurate trend)
        velocity_trend = "stable"  # Default without historical comparison
        
        logger.info(f"Velocity: {velocity:.1f}, confidence: {confidence}, fallback: {fallback_used}")
        
        return {
            "velocity": velocity,
            "sprints_analyzed": sprints_analyzed,
            "velocity_trend": velocity_trend,
            "confidence": confidence,
            "fallback_used": fallback_used
        }
        
    except Exception as e:
        logger.error(f"Error calculating team velocity: {e}", exc_info=True)
        return {
            "velocity": 100.0,  # Safe default
            "sprints_analyzed": 0,
            "velocity_trend": "unknown",
            "confidence": "low",
            "fallback_used": True,
            "error": str(e)
        }


async def handle_estimate_backlog_items(
    items: List[Dict[str, Any]],
    velocity: float,
    pm_agent: Any,
    model: str = "gpt-4o",
    max_items_to_estimate: int = 50,
    context_items: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Use LLM to estimate unestimated backlog items.
    
    Args:
        items: List of backlog items
        velocity: Team velocity for context
        pm_agent: PM Agent instance
        model: LLM model to use
        max_items_to_estimate: Maximum items to estimate
        context_items: Previously estimated items for reference
    
    Returns:
        {
            "estimated_items": List[Dict],
            "total_estimated_effort": float,
            "average_confidence": float,
            "estimation_metadata": Dict
        }
    """
    try:
        import sys
        from pathlib import Path
        REPO_ROOT = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(REPO_ROOT))
        
        from scripts.backlog_triaging import estimate_unestimated_items
        
        # Filter to unestimated items only
        unestimated = [
            item for item in items
            if not item.get("story_points") or item.get("story_points", 0) == 0
        ]
        
        if not unestimated:
            logger.info("No unestimated items to process")
            return {
                "estimated_items": [],
                "total_estimated_effort": 0.0,
                "average_confidence": 1.0,
                "estimation_metadata": {
                    "items_processed": 0,
                    "model": model,
                    "skipped_reason": "no_unestimated_items"
                }
            }
        
        # Limit items to estimate
        items_to_estimate = unestimated[:max_items_to_estimate]
        
        logger.info(f"Estimating {len(items_to_estimate)} unestimated items using {model}")
        
        # Create a backlog_data structure for the existing function
        backlog_data = {
            "items": items_to_estimate,
            "velocity_per_sprint": velocity
        }
        
        # Call existing estimation function
        estimated_data = await estimate_unestimated_items(backlog_data, model=model)
        
        # Extract results
        estimated_items = [
            item for item in estimated_data.get("items", [])
            if item.get("is_estimated", False)
        ]
        
        total_estimated_effort = sum(item.get("story_points", 0) for item in estimated_items)
        
        # Calculate average confidence
        confidences = [
            item.get("estimation_confidence", 0.5)
            for item in estimated_items
        ]
        average_confidence = sum(confidences) / len(confidences) if confidences else 0.0
        
        # Map confidence strings to numeric values for averaging
        confidence_map = {"high": 0.9, "medium": 0.6, "low": 0.3}
        numeric_confidences = [
            confidence_map.get(c, 0.5) if isinstance(c, str) else c
            for c in confidences
        ]
        average_confidence = sum(numeric_confidences) / len(numeric_confidences) if numeric_confidences else 0.0
        
        logger.info(
            f"Estimated {len(estimated_items)} items, "
            f"{total_estimated_effort:.1f} total effort, "
            f"avg confidence: {average_confidence:.2f}"
        )
        
        return {
            "estimated_items": estimated_items,
            "total_estimated_effort": total_estimated_effort,
            "average_confidence": average_confidence,
            "estimation_metadata": {
                "items_processed": len(estimated_items),
                "model": model,
                "total_items": len(items_to_estimate)
            }
        }
        
    except Exception as e:
        logger.error(f"Error estimating backlog items: {e}", exc_info=True)
        return {
            "estimated_items": [],
            "total_estimated_effort": 0.0,
            "average_confidence": 0.0,
            "estimation_metadata": {
                "items_processed": 0,
                "model": model,
                "error": str(e)
            }
        }


async def handle_calculate_backlog_health(
    backlog_items: int,
    total_story_points: float,
    velocity: float,
    pm_agent: Any,
    thin_threshold: float = 1.5,
    healthy_threshold: float = 3.0
) -> Dict[str, Any]:
    """
    Calculate backlog health metrics and risk status.
    
    Args:
        backlog_items: Count of backlog items
        total_story_points: Total effort in backlog
        velocity: Team velocity
        pm_agent: PM Agent instance
        thin_threshold: Sprints threshold for THIN status
        healthy_threshold: Sprints threshold for HEALTHY status
    
    Returns:
        {
            "status": "THIN" | "HEALTHY" | "OVERSTOCKED",
            "backlog_depth": float,
            "risk_level": "critical" | "warning" | "ok",
            "metrics": Dict,
            "alerts": List[str]
        }
    """
    try:
        # Calculate backlog depth (sprints of work)
        if velocity > 0:
            backlog_depth = total_story_points / velocity
        else:
            backlog_depth = 0.0
        
        # Determine status
        if backlog_depth < thin_threshold:
            status = "THIN"
            risk_level = "critical" if backlog_depth < 1.0 else "warning"
        elif backlog_depth <= healthy_threshold:
            status = "HEALTHY"
            risk_level = "ok"
        else:
            status = "OVERSTOCKED"
            risk_level = "warning"  # Too much backlog can also be a problem
        
        # Generate alerts
        alerts = []
        if status == "THIN":
            alerts.append(f"Backlog is THIN: only {backlog_depth:.1f} sprints of refined work")
            if backlog_depth < 1.0:
                alerts.append("CRITICAL: Less than 1 sprint of refined work available")
            alerts.append("Action needed: Schedule refinement session urgently")
        elif status == "OVERSTOCKED":
            alerts.append(f"Backlog is OVERSTOCKED: {backlog_depth:.1f} sprints of work")
            alerts.append("Consider: Focus on near-term items, defer long-term refinement")
        else:
            alerts.append(f"Backlog is HEALTHY: {backlog_depth:.1f} sprints of refined work")
        
        metrics = {
            "total_items": backlog_items,
            "total_story_points": total_story_points,
            "velocity": velocity,
            "backlog_depth": backlog_depth,
            "thin_threshold": thin_threshold,
            "healthy_threshold": healthy_threshold
        }
        
        logger.info(f"Backlog health: {status}, depth: {backlog_depth:.1f} sprints, risk: {risk_level}")
        
        return {
            "status": status,
            "backlog_depth": backlog_depth,
            "risk_level": risk_level,
            "metrics": metrics,
            "alerts": alerts
        }
        
    except Exception as e:
        logger.error(f"Error calculating backlog health: {e}", exc_info=True)
        return {
            "status": "UNKNOWN",
            "backlog_depth": 0.0,
            "risk_level": "critical",
            "metrics": {},
            "alerts": [f"Error calculating health: {str(e)}"],
            "error": str(e)
        }


async def handle_generate_backlog_recommendations(
    health_metrics: Dict[str, Any],
    backlog_items: List[Dict[str, Any]],
    velocity: float,
    pm_agent: Any,
    team_context: Optional[Dict[str, Any]] = None,
    historical_trends: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Generate actionable recommendations based on backlog health.
    
    Uses LLM to create contextual recommendations.
    
    Args:
        health_metrics: Output from calculate_backlog_health
        backlog_items: List of backlog items
        velocity: Team velocity
        pm_agent: PM Agent instance
        team_context: Additional team-specific context
        historical_trends: Previous backlog health data
    
    Returns:
        {
            "recommendations": List[Dict],
            "urgency": str,
            "estimated_impact": str,
            "next_refinement_date": str
        }
    """
    try:
        from openai import OpenAI
        import os
        from datetime import datetime, timedelta
        
        status = health_metrics.get("status", "UNKNOWN")
        backlog_depth = health_metrics.get("backlog_depth", 0)
        risk_level = health_metrics.get("risk_level", "unknown")
        
        # Create OpenAI client
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        # Build context for LLM
        context_text = f"""
Backlog Health Analysis:
- Status: {status}
- Backlog Depth: {backlog_depth:.1f} sprints
- Risk Level: {risk_level}
- Total Items: {len(backlog_items)}
- Team Velocity: {velocity:.1f} points/sprint

Team Context: {json.dumps(team_context or {}, indent=2)}

Historical Trends: {json.dumps(historical_trends or [], indent=2)}
"""
        
        prompt = f"""You are a product management advisor. Based on the backlog health analysis below, provide specific, actionable recommendations.

{context_text}

Provide 3-5 prioritized recommendations with:
1. Action to take
2. Timeline (immediate/this sprint/next sprint)
3. Expected impact
4. Who should be involved

Format as JSON:
{{
  "recommendations": [
    {{
      "action": "...",
      "timeline": "...",
      "impact": "...",
      "stakeholders": ["..."]
    }}
  ],
  "urgency": "immediate|this_sprint|next_sprint|none",
  "estimated_impact": "...",
  "next_refinement_date": "YYYY-MM-DD"
}}
"""
        
        logger.info(f"Generating recommendations for {status} backlog (depth: {backlog_depth:.1f} sprints)")
        
        # Call LLM
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a product management expert providing actionable backlog management advice."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.3
        )
        
        result = json.loads(response.choices[0].message.content)
        
        logger.info(f"Generated {len(result.get('recommendations', []))} recommendations, urgency: {result.get('urgency')}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error generating recommendations: {e}", exc_info=True)
        
        # Fallback: rule-based recommendations
        from datetime import datetime, timedelta
        
        status = health_metrics.get("status", "UNKNOWN")
        backlog_depth = health_metrics.get("backlog_depth", 0)
        
        recommendations = []
        urgency = "none"
        
        if status == "THIN":
            urgency = "immediate" if backlog_depth < 1.0 else "this_sprint"
            recommendations = [
                {
                    "action": "Schedule emergency refinement session",
                    "timeline": "immediate",
                    "impact": "Prevent sprint planning delays",
                    "stakeholders": ["Product Owner", "Team Lead"]
                },
                {
                    "action": "Review epic backlog for ready-to-refine items",
                    "timeline": "this sprint",
                    "impact": "Build 2-3 sprint runway",
                    "stakeholders": ["Product Owner"]
                },
                {
                    "action": "Consider splitting large items for quicker refinement",
                    "timeline": "this sprint",
                    "impact": "Increase refined item count faster",
                    "stakeholders": ["Team", "Product Owner"]
                }
            ]
        elif status == "HEALTHY":
            urgency = "next_sprint"
            recommendations = [
                {
                    "action": "Continue regular refinement cadence",
                    "timeline": "next sprint",
                    "impact": "Maintain healthy backlog depth",
                    "stakeholders": ["Team"]
                }
            ]
        else:  # OVERSTOCKED
            urgency = "next_sprint"
            recommendations = [
                {
                    "action": "Focus on near-term items only",
                    "timeline": "this sprint",
                    "impact": "Reduce refinement overhead",
                    "stakeholders": ["Product Owner"]
                },
                {
                    "action": "Review and archive low-priority items",
                    "timeline": "next sprint",
                    "impact": "Reduce backlog noise",
                    "stakeholders": ["Product Owner"]
                }
            ]
        
        next_refinement = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        
        return {
            "recommendations": recommendations,
            "urgency": urgency,
            "estimated_impact": f"Improve backlog health from {status} to HEALTHY",
            "next_refinement_date": next_refinement,
            "fallback_used": True,
            "error": str(e)
        }
