"""
Developer Knowledge Base Handler - Executes developer skill searches via Milvus vector DB.

This module provides the handler function for the search_developers_by_skill tool.
It uses the developer knowledge base stored in Milvus to find developers with matching skills.

Features:
- Semantic search using OpenAI embeddings
- Evidence-based results with commit counts and languages
- Deduplication of developer results
- Langfuse tracing integration
"""

import json
import logging
from typing import Any, Dict, List, Optional

from utilities.langfuse_client import create_span, finalize_span, get_current_trace

logger = logging.getLogger(__name__)


def _normalize_developer_name(email: str) -> str:
    """Extract readable name from email address."""
    if not email:
        return "Unknown"
    
    # Handle GitHub-style emails like "86947981+bhanuvinay-WT@users.noreply.github.com"
    if "+" in email and "@users.noreply.github.com" in email:
        parts = email.split("+")
        if len(parts) > 1:
            name_part = parts[1].split("@")[0]
            # Convert hyphen-separated to title case
            return name_part.replace("-", " ").replace("WT", "").strip().title()
    
    # Standard email: extract name from local part
    local_part = email.split("@")[0]
    # Replace dots and underscores with spaces, title case
    name = local_part.replace(".", " ").replace("_", " ").title()
    return name


def _build_evidence(dev_data: Dict[str, Any]) -> Dict[str, Any]:
    """Build evidence dictionary from raw developer data."""
    raw_data = dev_data.get("raw_data", {})
    
    # Extract languages with counts
    all_languages = raw_data.get("all_languages", {})
    if not all_languages and dev_data.get("languages"):
        all_languages = {lang: 1 for lang in dev_data["languages"] if lang}
    
    # Sort languages by count
    sorted_langs = sorted(all_languages.items(), key=lambda x: x[1], reverse=True)
    
    # Extract top files/areas worked on
    top_files = raw_data.get("top_files", [])
    if isinstance(top_files, list) and top_files:
        # Extract meaningful folder names
        areas = []
        for item in top_files[:5]:
            if isinstance(item, (list, tuple)) and len(item) >= 1:
                path = item[0]
                # Extract meaningful folder name
                parts = path.strip("/").split("/")
                for part in parts:
                    if part and not part.startswith(".") and len(part) > 2:
                        areas.append(part)
                        break
        areas = list(dict.fromkeys(areas))[:3]  # Deduplicate, take top 3
    else:
        areas = []
    
    return {
        "commits": raw_data.get("commits", dev_data.get("commits", 0)),
        "loc_added": raw_data.get("loc_added", 0),
        "wi_count": raw_data.get("wi_count", 0),
        "languages": sorted_langs[:5],  # Top 5 languages with counts
        "areas_worked_on": areas,
    }


def _determine_role(dev_data: Dict[str, Any]) -> str:
    """Determine developer role based on languages and file patterns."""
    languages = [lang.lower() for lang in dev_data.get("languages", [])]
    profile_text = dev_data.get("profile_text", "").lower()
    
    fe_indicators = ["angular", "react", "vue", "typescript", "javascript", "html", "css", "frontend", "ui"]
    be_indicators = ["java", "spring", "python", "c#", "golang", "backend", "api", "service", "controller"]
    
    fe_score = sum(1 for ind in fe_indicators if any(ind in lang for lang in languages) or ind in profile_text)
    be_score = sum(1 for ind in be_indicators if any(ind in lang for lang in languages) or ind in profile_text)
    
    if fe_score > be_score * 1.5:
        return "Frontend Developer"
    elif be_score > fe_score * 1.5:
        return "Backend Developer"
    elif fe_score > 0 and be_score > 0:
        return "Fullstack Developer"
    else:
        return "Developer"


def _extract_technology_from_query(query: str) -> Optional[str]:
    """
    Extract technology/skill keyword from a natural language query.
    
    Examples:
        "who knows angular" -> "angular"
        "list all java developers" -> "java"
        "find react developers" -> "react"
    """
    if not query:
        return None
    
    q = query.lower().strip()
    
    # Technology keywords to look for
    technologies = [
        # Frontend
        'angular', 'react', 'vue', 'typescript', 'javascript', 'html', 'css', 'scss',
        'frontend', 'front-end', 'front end', 'ui', 'ux',
        # Backend
        'java', 'python', 'c#', 'csharp', 'dotnet', '.net', 'golang', 'go lang', 'rust',
        'kotlin', 'swift', 'php', 'ruby', 'scala', 'perl',
        'spring', 'springboot', 'spring boot', 'django', 'flask', 'fastapi', 'express',
        'nodejs', 'node.js', 'node js', 'nest', 'nestjs',
        'backend', 'back-end', 'back end', 'api', 'microservices',
        # Database
        'sql', 'mysql', 'postgresql', 'postgres', 'mongodb', 'redis', 'elasticsearch',
        'oracle', 'sqlserver', 'dynamodb', 'cassandra',
        # Cloud/DevOps
        'aws', 'azure', 'gcp', 'docker', 'kubernetes', 'k8s', 'terraform', 'ansible',
        'jenkins', 'ci/cd', 'cicd', 'devops',
        # Mobile
        'android', 'ios', 'flutter', 'react native', 'xamarin',
        # Data/ML
        'machine learning', 'ml', 'ai', 'data science', 'tensorflow', 'pytorch',
        'pandas', 'numpy', 'spark',
        # General roles
        'fullstack', 'full-stack', 'full stack',
    ]
    
    # Sort by length (longer matches first) to avoid partial matches
    technologies = sorted(technologies, key=len, reverse=True)
    
    for tech in technologies:
        if tech in q:
            # Return normalized version
            return tech.replace('-', '').replace(' ', '').replace('.', '')
    
    return None


async def search_developers_by_skill(
    skill_query: str = None,
    technology: str = None,
    top_k: int = None,
    include_evidence: bool = True,
    parent_trace: Any = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Search for developers with specific skills using the Milvus vector database.
    
    This function performs a semantic search against the developer knowledge base
    to find developers whose skills match the query. Results include evidence of
    expertise based on commit history, languages used, and work item associations.
    
    Args:
        skill_query: Natural language description of required skills
        technology: Specific technology to search for (e.g., 'angular', 'java')
        top_k: Maximum number of results (None = return all matches)
        include_evidence: Include detailed evidence in results
        parent_trace: Langfuse trace for observability
        
    Returns:
        Dict with:
            - success: bool
            - developers: List of matching developers with evidence
            - total_found: Number of developers found
            - query: The search query used
    """
    # Extract technology from skill_query if not explicitly provided
    if not technology and skill_query:
        technology = _extract_technology_from_query(skill_query)
        if technology:
            logger.info(f"[DEV_KB] Extracted technology '{technology}' from query")
    
    # Start Langfuse span for tracing
    span = None
    if parent_trace:
        try:
            span = parent_trace.start_observation(
                as_type="SPAN",
                name="search_developers_by_skill",
                input={
                    "skill_query": skill_query,
                    "technology": technology,
                    "top_k": top_k
                },
                metadata={"tool": "search_developers_by_skill"}
            )
        except Exception as e:
            logger.debug(f"Failed to create span: {e}")
    
    try:
        # Build the search query
        if technology:
            # Technology-specific search
            query = f"{technology} developer experience expertise"
        elif skill_query:
            query = skill_query
        else:
            # Default to showing all developers
            query = "developer software engineer programmer"
        
        logger.info(f"[DEV_KB] Searching for developers with query: {query}")
        
        # Import here to avoid circular imports
        from utilities.developer_knowledge_base import (
            find_similar_developers, 
            load_developer_skills_from_file,
            get_kb_stats
        )
        
        # Get KB stats for context
        kb_stats = get_kb_stats()
        logger.info(f"[DEV_KB] Knowledge base stats: {kb_stats}")
        
        # Perform semantic search
        # Use a high top_k to get all relevant matches, then filter
        search_top_k = top_k if top_k else 50  # Get up to 50 matches
        raw_results = find_similar_developers(query, top_k=search_top_k)
        
        if not raw_results:
            # Fallback: Load all developers from file
            logger.info("[DEV_KB] No vector search results, loading all developers from file")
            all_developers = load_developer_skills_from_file()
            
            # Filter by technology if specified
            if technology:
                tech_lower = technology.lower()
                filtered = []
                for dev in all_developers:
                    languages = [lang.lower() for lang in dev.get("languages", [])]
                    if any(tech_lower in lang for lang in languages):
                        filtered.append({
                            "developer": dev.get("developer", ""),
                            "similarity": 0.7,  # Exact technology match
                            "languages": dev.get("languages", []),
                            "commits": dev.get("commits", 0),
                            "raw_data": dev,
                        })
                raw_results = filtered
            else:
                # Return all developers
                raw_results = [
                    {
                        "developer": dev.get("developer", ""),
                        "similarity": 0.5,
                        "languages": dev.get("languages", []),
                        "commits": dev.get("commits", 0),
                        "raw_data": dev,
                    }
                    for dev in all_developers
                ]
        
        # Post-filter by technology if specified (for vector search results)
        if technology and raw_results:
            tech_lower = technology.lower()
            filtered_results = []
            for dev in raw_results:
                languages = [lang.lower() for lang in dev.get("languages", [])]
                profile_text = dev.get("profile_text", "").lower()
                raw_data = dev.get("raw_data", {})
                all_langs = raw_data.get("all_languages", {})
                all_langs_lower = [k.lower() for k in all_langs.keys()]
                
                # Check if technology appears in languages or profile
                if (any(tech_lower in lang for lang in languages) or
                    any(tech_lower in lang for lang in all_langs_lower) or
                    tech_lower in profile_text):
                    filtered_results.append(dev)
            
            raw_results = filtered_results
        
        # Deduplicate by email (case-insensitive)
        seen_emails = set()
        deduplicated = []
        for dev in raw_results:
            email = dev.get("developer", "").lower()
            if email and email not in seen_emails:
                seen_emails.add(email)
                deduplicated.append(dev)
        
        raw_results = deduplicated
        
        # Sort by commits (evidence of expertise) then similarity
        raw_results.sort(key=lambda x: (x.get("commits", 0), x.get("similarity", 0)), reverse=True)
        
        # Apply top_k limit if specified
        if top_k and top_k > 0:
            raw_results = raw_results[:top_k]
        
        # Build formatted results with evidence
        developers = []
        for dev in raw_results:
            email = dev.get("developer", "")
            name = _normalize_developer_name(email)
            
            dev_entry = {
                "name": name,
                "email": email,
                "similarity_score": round(dev.get("similarity", 0), 3),
                "role": _determine_role(dev),
                "languages": dev.get("languages", [])[:5],
            }
            
            if include_evidence:
                evidence = _build_evidence(dev)
                dev_entry["evidence"] = {
                    "commits": evidence["commits"],
                    "lines_of_code_added": evidence["loc_added"],
                    "work_items_completed": evidence["wi_count"],
                    "top_languages": [
                        {"language": lang, "lines": count}
                        for lang, count in evidence["languages"]
                    ],
                    "areas_worked_on": evidence["areas_worked_on"],
                }
            
            developers.append(dev_entry)
        
        result = {
            "success": True,
            "developers": developers,
            "total_found": len(developers),
            "total_in_kb": kb_stats.get("developer_count", len(developers)),
            "query": query,
            "technology_filter": technology,
            "vector_db_type": kb_stats.get("vector_store_type", "Unknown"),
        }
        
        logger.info(f"[DEV_KB] Found {len(developers)} developers matching query")
        
        # Finalize span
        if span:
            try:
                span.update(
                    output=result,
                    status_message="success"
                )
                span.end()
            except Exception as e:
                logger.debug(f"Failed to finalize span: {e}")
        
        return result
        
    except Exception as e:
        logger.error(f"[DEV_KB] Error searching developers: {e}", exc_info=True)
        
        # Finalize span with error
        if span:
            try:
                span.update(
                    output={"error": str(e)},
                    status_message="error",
                    level="ERROR"
                )
                span.end()
            except Exception:
                pass
        
        return {
            "success": False,
            "error": str(e),
            "developers": [],
            "total_found": 0,
        }


def format_developer_search_results(result: Dict[str, Any], query: str = None) -> str:
    """
    Format developer search results into a human-readable response.
    
    Args:
        result: Result from search_developers_by_skill
        query: Original user query for context
        
    Returns:
        Formatted string response
    """
    if not result.get("success"):
        return f"❌ Error searching developer knowledge base: {result.get('error', 'Unknown error')}"
    
    developers = result.get("developers", [])
    total_found = result.get("total_found", 0)
    total_in_kb = result.get("total_in_kb", 0)
    technology = result.get("technology_filter")
    
    if not developers:
        if technology:
            return f"No developers found with **{technology}** expertise in the knowledge base.\n\n" \
                   f"ℹ️ The knowledge base contains {total_in_kb} developers. Try a different technology or broader search."
        return f"No developers found matching the search criteria.\n\nℹ️ The knowledge base contains {total_in_kb} developers."
    
    # Build response
    lines = []
    
    # Header
    if technology:
        lines.append(f"## 👥 Developers with **{technology.title()}** Expertise")
    else:
        lines.append("## 👥 Developer Search Results")
    
    lines.append(f"\nFound **{total_found}** matching developers (out of {total_in_kb} in knowledge base):\n")
    
    # Developer list
    for i, dev in enumerate(developers, 1):
        name = dev.get("name", "Unknown")
        email = dev.get("email", "")
        role = dev.get("role", "Developer")
        languages = dev.get("languages", [])
        evidence = dev.get("evidence", {})
        
        # Developer header with ranking
        lines.append(f"### {i}. **{name}**")
        lines.append(f"- 📧 Email: `{email}`")
        lines.append(f"- 💼 Role: {role}")
        
        # Languages
        if languages:
            lang_str = ", ".join(languages[:5])
            lines.append(f"- 💻 Languages: {lang_str}")
        
        # Evidence of expertise
        if evidence:
            commits = evidence.get("commits", 0)
            loc = evidence.get("lines_of_code_added", 0)
            wi_count = evidence.get("work_items_completed", 0)
            
            if commits > 0 or loc > 0 or wi_count > 0:
                lines.append(f"- 📊 **Evidence of Expertise:**")
                if commits > 0:
                    lines.append(f"  - {commits} commits")
                if loc > 0:
                    lines.append(f"  - {loc:,} lines of code added")
                if wi_count > 0:
                    lines.append(f"  - {wi_count} work items completed")
            
            # Top languages with counts
            top_langs = evidence.get("top_languages", [])
            if top_langs:
                lang_details = ", ".join([
                    f"{l['language']} ({l['lines']} lines)" 
                    for l in top_langs[:3]
                ])
                lines.append(f"  - Top contributions: {lang_details}")
            
            # Areas worked on
            areas = evidence.get("areas_worked_on", [])
            if areas:
                lines.append(f"  - Areas: {', '.join(areas)}")
        
        lines.append("")  # Blank line between developers
    
    # Footer
    lines.append("---")
    lines.append(f"*Data source: Developer Knowledge Base ({result.get('vector_db_type', 'Milvus')})*")
    
    return "\n".join(lines)


# Export for use in agent
__all__ = [
    "search_developers_by_skill",
    "format_developer_search_results",
]
