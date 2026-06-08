"""
Team Data Enrichment Module for Dynamic Work Item Assignments

This module enriches work item data with team capacity and developer skills
to enable intelligent assignment recommendations.
"""
import json
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

# Path to developer skills data
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def load_developer_skills() -> List[Dict[str, Any]]:
    """Load developer skills from data/developer_skills.json."""
    skills_file = DATA_DIR / "developer_skills.json"
    if not skills_file.exists():
        logger.warning("developer_skills.json not found at %s", skills_file)
        return []
    try:
        with open(skills_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load developer_skills.json: %s", e)
        return []


def get_developer_name_from_email(email: str) -> str:
    """Extract display name from email address."""
    if not email:
        return "Unknown"
    # Convert email like "pratham.vij@walkingtree.tech" to "Pratham Vij"
    if "@" in email:
        local_part = email.split("@")[0]
        parts = local_part.replace(".", " ").replace("_", " ").split()
        return " ".join(p.capitalize() for p in parts)
    return email


def find_relevant_developers(
    work_item: Dict[str, Any],
    all_developers: List[Dict[str, Any]],
    capacity_data: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Find developers relevant to a work item based on skills, area path, and tags.
    
    Args:
        work_item: Work item dict with fields
        all_developers: List of developer skill profiles
        capacity_data: Optional capacity data with team member availability
        
    Returns:
        List of matching developers with relevance scores
    """
    if not all_developers:
        return []
    
    fields = work_item.get('fields', {})
    if not fields and isinstance(work_item, dict):
        fields = work_item  # Sometimes fields are at top level
    
    # Extract work item characteristics
    wi_type = fields.get('System.WorkItemType', '')
    title = fields.get('System.Title', '')
    area_path = fields.get('System.AreaPath', '')
    tags = fields.get('System.Tags', '')
    description = fields.get('System.Description', '')
    
    # Combine text for skill matching
    text_to_match = f"{title} {description} {tags} {area_path}".lower()
    
    # Keywords to look for in work item text
    java_keywords = ['java', 'spring', 'hibernate', 'maven', 'gradle', 'backend', 'api', 'service']
    python_keywords = ['python', 'django', 'flask', 'fastapi', 'ml', 'machine learning']
    frontend_keywords = ['angular', 'react', 'vue', 'javascript', 'typescript', 'frontend', 'ui', 'css']
    dotnet_keywords = ['.net', 'c#', 'csharp', 'asp.net', 'dotnet', 'azure']
    devops_keywords = ['devops', 'jenkins', 'docker', 'kubernetes', 'ci/cd', 'pipeline', 'deployment']
    qa_keywords = ['test', 'qa', 'testing', 'automation', 'selenium', 'quality']
    ba_keywords = ['business', 'analysis', 'requirement', 'story', 'ba ', 'functional']
    
    # Mapping from skill categories to language/technology names in developer profiles
    # This ensures "Frontend" skill matches developers with "React", "TypeScript", etc.
    SKILL_TO_LANGUAGES = {
        'Java': ['java', 'spring', 'kotlin', 'gradle', 'maven'],
        'Python': ['python', 'django', 'flask', 'fastapi'],
        'Frontend': ['angular', 'react', 'vue', 'javascript', 'typescript', 'html', 'css', 'scss', 'sass'],
        '.NET': ['.net', 'c#', 'csharp', 'asp', 'dotnet', 'f#'],
        'DevOps': ['docker', 'kubernetes', 'jenkins', 'terraform', 'ansible', 'yaml', 'bash', 'powershell'],
        'QA': ['selenium', 'cypress', 'playwright', 'pytest', 'junit', 'testng'],
        'BA': []  # BA typically doesn't have programming language matches
    }
    
    # Detect what skills are needed
    needed_skills = []
    if any(kw in text_to_match for kw in java_keywords):
        needed_skills.append('Java')
    if any(kw in text_to_match for kw in python_keywords):
        needed_skills.append('Python')
    if any(kw in text_to_match for kw in frontend_keywords):
        needed_skills.append('Frontend')
    if any(kw in text_to_match for kw in dotnet_keywords):
        needed_skills.append('.NET')
    if any(kw in text_to_match for kw in devops_keywords):
        needed_skills.append('DevOps')
    if any(kw in text_to_match for kw in qa_keywords):
        needed_skills.append('QA')
    if any(kw in text_to_match for kw in ba_keywords):
        needed_skills.append('BA')
    
    # Score each developer
    scored_developers = []
    for dev in all_developers:
        email = dev.get('developer', '')
        name = get_developer_name_from_email(email)
        languages = dev.get('languages', [])
        all_langs = dev.get('all_languages', {})
        commits = dev.get('commits', 0)
        loc_added = dev.get('loc_added', 0)
        wi_count = dev.get('wi_count', 0)
        
        # Calculate relevance score
        score = 0
        matched_skills = []
        
        # Skill matching - PRIMARY scoring factor
        # Use SKILL_TO_LANGUAGES mapping for better matching
        for skill in needed_skills:
            # Get the language keywords that match this skill
            skill_languages = SKILL_TO_LANGUAGES.get(skill, [skill.lower()])
            
            # Check if any of the developer's languages match the skill's language keywords
            for lang in languages:
                lang_lower = lang.lower()
                if any(skill_lang in lang_lower for skill_lang in skill_languages):
                    score += 30  # Increased weight for skill match
                    matched_skills.append(skill)
                    break
        
        # CRITICAL: Only add activity-based scoring if there's at least one skill match
        # This prevents high-activity developers from being suggested for unrelated work
        if matched_skills:
            # Activity-based scoring (secondary, only if skills match)
            if commits > 10:
                score += 5  # Reduced from 10
            if wi_count > 5:
                score += 3  # Reduced from 5
            if loc_added > 1000:
                score += 2  # Reduced from 5
        
        # Check capacity if available
        available_hours = None
        if capacity_data:
            team_members = capacity_data.get('teamMembers', [])
            for member in team_members:
                member_name = member.get('teamMember', {}).get('displayName', '')
                member_email = member.get('teamMember', {}).get('uniqueName', '')
                if email.lower() == member_email.lower() or name.lower() in member_name.lower():
                    # Calculate available hours
                    activities = member.get('activities', [])
                    total_capacity = sum(a.get('capacityPerDay', 0) for a in activities)
                    days_off = len(member.get('daysOff', []))
                    # Assume 5-day sprint remaining
                    available_hours = max(0, (5 - days_off) * total_capacity)
                    if available_hours > 0 and matched_skills:
                        score += 10  # Reduced from 15, only if skills match
                    break
        
        # CRITICAL: Only include developers with actual skill matches
        # This prevents random suggestions when no relevant skills are found
        if score > 0 and matched_skills:
            scored_developers.append({
                'name': name,
                'email': email,
                'score': score,
                'matched_skills': matched_skills,
                'languages': languages[:5],  # Top 5 languages
                'commits': commits,
                'available_hours': available_hours
            })
    
    # Sort by score descending
    scored_developers.sort(key=lambda x: x['score'], reverse=True)
    
    # Log the matching for debugging
    if scored_developers:
        logger.info(f"[TeamEnrichment] Found {len(scored_developers)} matching developers for needed_skills={needed_skills}")
    else:
        logger.info(f"[TeamEnrichment] No skill-matched developers found for needed_skills={needed_skills}")
    
    return scored_developers[:3]  # Return top 3 matches


def estimate_completion_time(work_item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Estimate completion time for a work item based on type and complexity.
    
    Returns EOD estimate with reasoning.
    """
    fields = work_item.get('fields', {})
    if not fields and isinstance(work_item, dict):
        fields = work_item
    
    wi_type = fields.get('System.WorkItemType', 'Task')
    title = str(fields.get('System.Title', '')).lower()
    state = fields.get('System.State', 'New')
    remaining_work = fields.get('Microsoft.VSTS.Scheduling.RemainingWork', 0) or 0
    original_estimate = fields.get('Microsoft.VSTS.Scheduling.OriginalEstimate', 0) or 0
    
    # Base hours by type
    base_hours = {
        'Bug': 4,
        'Task': 2,
        'User Story': 8,
        'Feature': 16,
        'Epic': 40
    }.get(wi_type, 4)
    
    # Adjust based on complexity indicators in title
    complexity_multiplier = 1.0
    if any(kw in title for kw in ['complex', 'refactor', 'migration', 'redesign']):
        complexity_multiplier = 2.0
    elif any(kw in title for kw in ['simple', 'fix', 'minor', 'small']):
        complexity_multiplier = 0.5
    elif any(kw in title for kw in ['api', 'integration', 'service']):
        complexity_multiplier = 1.5
    
    # Use remaining work if available
    if remaining_work > 0:
        estimated_hours = remaining_work
    elif original_estimate > 0:
        estimated_hours = original_estimate
    else:
        estimated_hours = base_hours * complexity_multiplier
    
    # Calculate EOD based on current date and estimated hours
    today = datetime.now()
    work_hours_per_day = 6  # Effective work hours per day
    
    days_needed = max(1, int(estimated_hours / work_hours_per_day + 0.5))
    
    # Calculate target date (skip weekends)
    target_date = today
    days_added = 0
    while days_added < days_needed:
        target_date = datetime(target_date.year, target_date.month, target_date.day)
        days_added += 1
        # Skip to next day
        from datetime import timedelta
        target_date = target_date + timedelta(days=1)
        # Skip weekends
        while target_date.weekday() >= 5:  # Saturday = 5, Sunday = 6
            target_date = target_date + timedelta(days=1)
    
    return {
        'estimated_hours': round(estimated_hours, 1),
        'target_date': target_date.strftime('%Y-%m-%d'),
        'target_date_display': target_date.strftime('%b %d, %Y'),
        'confidence': 'high' if remaining_work > 0 else 'medium',
        'basis': 'remaining_work' if remaining_work > 0 else ('original_estimate' if original_estimate > 0 else 'type_heuristic')
    }


async def enrich_work_items_with_team_data(
    work_items: List[Dict[str, Any]],
    mcp_connector: Any,
    project: str = None,
    iteration_id: str = None,
    team: str = None
) -> Dict[str, Any]:
    """
    Enrich work items with team capacity and developer assignments.
    
    Args:
        work_items: List of work item dicts
        mcp_connector: MCP connector for fetching capacity
        project: Project name/ID
        iteration_id: Iteration ID
        team: Team name
        
    Returns:
        Enrichment data including team capacity and suggested assignments
    """
    enrichment = {
        'team_capacity': None,
        'developers': [],
        'suggested_assignments': []
    }
    
    # Load developer skills
    all_developers = load_developer_skills()
    if all_developers:
        enrichment['developers'] = [
            {
                'name': get_developer_name_from_email(d.get('developer', '')),
                'email': d.get('developer', ''),
                'skills': d.get('languages', [])[:5],
                'commits': d.get('commits', 0)
            }
            for d in all_developers[:15]  # Top 15 developers
        ]
        logger.info(f"[TeamEnrichment] Loaded {len(enrichment['developers'])} developers")
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # ARCHITECTURE COMPLIANCE: MCP calls during synthesis are DISABLED
    # ═══════════════════════════════════════════════════════════════════════════════
    # MCP tools should ONLY be called during agent execution, NOT during synthesis.
    # Team capacity fetching was happening AFTER main tool execution, causing:
    # 1. MCP tool spans appearing in Langfuse trace during synthesis phase
    # 2. Violation of the orchestrator flow architecture
    # 3. Confusion about when Deep LLM vs tool execution happens
    #
    # For now, we skip dynamic capacity fetching and only use static developer_skills.json
    # TODO: Move team capacity fetching to orchestrator planning phase or agent execution
    # ═══════════════════════════════════════════════════════════════════════════════
    capacity_data = None
    
    # # Fetch team capacity if MCP connector available (DISABLED)
    # if mcp_connector and project:
    #     try:
    #         # Resolve iteration if needed
    #         iter_id = iteration_id or '@CurrentIteration'
    #         if iter_id in ('@CurrentIteration', '@PreviousIteration'):
    #             iter_response = await mcp_connector.call_tool('work_list_team_iterations', {
    #                 'project': project,
    #                 'team': team or project,
    #                 'timeframe': 'current'
    #             })
    #             if iter_response and iter_response != 'null':
    #                 iter_data = json.loads(iter_response) if isinstance(iter_response, str) else iter_response
    #                 if isinstance(iter_data, list) and len(iter_data) > 0:
    #                     iter_id = str(iter_data[0].get('id') or iter_data[0].get('identifier'))
    #         
    #         # Fetch capacity
    #         capacity_args = {
    #             'project': project,
    #             'iterationId': iter_id,
    #             'team': team
    #         }
    #         capacity_response = await mcp_connector.call_tool('work_get_iteration_capacity', capacity_args)
    #         
    #         if capacity_response and capacity_response != 'null' and capacity_response.strip():
    #             try:
    #                 capacity_data = json.loads(capacity_response) if isinstance(capacity_response, str) else capacity_response
    #             except json.JSONDecodeError as json_err:
    #                 logger.warning(f"[TeamEnrichment] Failed to parse capacity response as JSON: {json_err}")
    #                 capacity_data = None
    #             
    #             if capacity_data:
    #                 # Parse team members
    #                 team_members = capacity_data.get('teamMembers', [])
    #                 capacity_summary = []
    #                 for member in team_members:
    #                     member_info = member.get('teamMember', {})
    #                     name = member_info.get('displayName', 'Unknown')
    #                     activities = member.get('activities', [])
    #                     total_capacity = sum(a.get('capacityPerDay', 0) for a in activities)
    #                     days_off = len(member.get('daysOff', []))
    #                     
    #                     capacity_summary.append({
    #                         'name': name,
    #                         'capacity_per_day': total_capacity,
    #                         'days_off': days_off,
    #                         'available': total_capacity > 0
    #                     })
    #                 
    #                 enrichment['team_capacity'] = {
    #                     'iteration': iter_id,
    #                     'total_members': len(team_members),
    #                     'members': capacity_summary[:10]  # Top 10 for context
    #                 }
    #                 logger.info(f"[TeamEnrichment] Loaded capacity for {len(capacity_summary)} team members")
    #     except Exception as e:
    #         logger.warning(f"[TeamEnrichment] Failed to fetch capacity: {e}")
    
    
    # Generate suggested assignments for each work item
    for wi in work_items[:10]:  # Limit to first 10 for performance
        fields = wi.get('fields', {})
        wi_id = wi.get('id') or fields.get('System.Id')
        title = fields.get('System.Title', '')
        
        # Find relevant developers
        relevant_devs = find_relevant_developers(wi, all_developers, capacity_data)
        
        # Estimate completion
        eod_estimate = estimate_completion_time(wi)
        
        if relevant_devs:
            top_dev = relevant_devs[0]
            suggestion = {
                'work_item_id': wi_id,
                'title': title[:60] + '...' if len(title) > 60 else title,
                'suggested_assignee': top_dev['name'],
                'assignee_email': top_dev['email'],
                'reason': f"Matched skills: {', '.join(top_dev['matched_skills'][:3]) or 'General expertise'}",
                'available_hours': top_dev.get('available_hours'),
                'eod_estimate': eod_estimate['target_date_display'],
                'estimated_hours': eod_estimate['estimated_hours'],
                'alternatives': [d['name'] for d in relevant_devs[1:3]]
            }
        else:
            suggestion = {
                'work_item_id': wi_id,
                'title': title[:60] + '...' if len(title) > 60 else title,
                'suggested_assignee': None,
                'reason': 'No skill match found - requires manual assignment',
                'eod_estimate': eod_estimate['target_date_display'],
                'estimated_hours': eod_estimate['estimated_hours']
            }
        
        enrichment['suggested_assignments'].append(suggestion)
    
    return enrichment


def format_enrichment_for_prompt(enrichment: Dict[str, Any]) -> str:
    """
    Format enrichment data for inclusion in synthesizer prompt.
    """
    lines = []
    
    # Team capacity section
    if enrichment.get('team_capacity'):
        cap = enrichment['team_capacity']
        lines.append("## 👥 TEAM CAPACITY DATA")
        lines.append(f"**Iteration:** {cap.get('iteration', 'Current')}")
        lines.append(f"**Team Size:** {cap.get('total_members', 0)} members")
        lines.append("")
        
        members = cap.get('members', [])
        if members:
            lines.append("| Team Member | Capacity/Day | Days Off | Available |")
            lines.append("|-------------|--------------|----------|-----------|")
            for m in members[:8]:
                avail = "✅ Yes" if m.get('available') else "❌ No"
                lines.append(f"| {m['name']} | {m.get('capacity_per_day', 0)}h | {m.get('days_off', 0)} | {avail} |")
        lines.append("")
    
    # Developer skills section
    if enrichment.get('developers'):
        lines.append("## 🛠️ DEVELOPER SKILLS SUMMARY")
        lines.append("| Developer | Top Skills | Recent Commits |")
        lines.append("|-----------|------------|----------------|")
        for dev in enrichment['developers'][:8]:
            skills = ', '.join(dev.get('skills', [])[:3]) or 'General'
            lines.append(f"| {dev['name']} | {skills} | {dev.get('commits', 0)} |")
        lines.append("")
    
    # Suggested assignments section
    if enrichment.get('suggested_assignments'):
        lines.append("## 📋 AI-SUGGESTED ASSIGNMENTS")
        lines.append("*Use these suggestions when recommending actions for work items:*")
        lines.append("")
        for sug in enrichment['suggested_assignments']:
            wi_id = sug.get('work_item_id', '?')
            title = sug.get('title', 'Unknown')
            assignee = sug.get('suggested_assignee', 'Unassigned')
            reason = sug.get('reason', '')
            eod = sug.get('eod_estimate', 'TBD')
            hours = sug.get('estimated_hours', '?')
            
            lines.append(f"- **#{wi_id}**: {title}")
            if assignee:
                lines.append(f"  - **Assign to:** {assignee}")
                lines.append(f"  - **Reason:** {reason}")
            lines.append(f"  - **Target EOD:** {eod} (~{hours}h effort)")
            
            alternatives = sug.get('alternatives', [])
            if alternatives:
                lines.append(f"  - **Alternatives:** {', '.join(alternatives)}")
            lines.append("")
    
    return "\n".join(lines)
