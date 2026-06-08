"""
Developer Knowledge Base - Vector DB Integration for Developer Skills

This module manages the developer knowledge base stored in Milvus (or fallback vector stores).
It provides:
- Developer profile embedding generation
- Skill-based developer matching using vector similarity
- Scheduled nightly updates to keep the knowledge base current

Best Practices Implemented:
- Batch processing for efficiency
- Graceful fallback if Milvus unavailable
- Comprehensive logging
- No email notifications (silent background updates)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import config as app_config

logger = logging.getLogger("pm_agent.developer_kb")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

# Collection name for developer skills in vector DB
DEVELOPER_SKILLS_COLLECTION = "developer_skills"


def build_developer_profile_text(dev_data: Dict[str, Any]) -> str:
    """
    Build a rich text representation of a developer's profile for embedding.
    
    This combines multiple data sources into a single text that captures:
    - Developer identity (email, name)
    - Programming languages and expertise levels
    - File paths touched (indicating domain knowledge)
    - Commit activity (experience level)
    - Frontend/Backend classification evidence
    
    Args:
        dev_data: Developer data dict from developer_skills.json
        
    Returns:
        Combined text suitable for embedding
    """
    parts = []
    
    # Developer identity
    email = dev_data.get("developer", "")
    if email:
        # Extract name from email
        name_part = email.split("@")[0]
        name = name_part.replace(".", " ").replace("_", " ").title()
        parts.append(f"Developer: {name}")
        parts.append(f"Email: {email}")
    
    # Programming languages with expertise levels
    languages = dev_data.get("languages", [])
    all_languages = dev_data.get("all_languages", {})
    if languages:
        lang_parts = []
        for lang in languages:
            count = all_languages.get(lang, 1)
            if count > 10:
                level = "expert"
            elif count > 5:
                level = "proficient"
            else:
                level = "familiar"
            lang_parts.append(f"{lang} ({level})")
        parts.append(f"Languages: {', '.join(lang_parts)}")
    
    # Determine FE/BE classification from file paths and languages
    fe_indicators = ["typescript", "javascript", "html", "css", "angular", "react", "vue", "scss"]
    be_indicators = ["java", "python", "c#", "sql", "api", "service", "server", "database"]
    
    fe_score = sum(1 for lang in languages if lang.lower() in fe_indicators)
    be_score = sum(1 for lang in languages if lang.lower() in be_indicators)
    
    # File paths - extract domain knowledge
    top_files = dev_data.get("top_files", [])
    if top_files:
        paths = []
        fe_paths = []
        be_paths = []
        for file_info in top_files[:10]:  # Top 10 files
            if isinstance(file_info, (list, tuple)) and len(file_info) >= 2:
                path = file_info[0]
            else:
                path = str(file_info)
            
            # Extract meaningful path components
            path_lower = path.lower()
            
            # Check for FE/BE indicators in paths
            if any(x in path_lower for x in ["ui", "frontend", "angular", "component", "html", "css"]):
                fe_paths.append(path.split("/")[-1])
                fe_score += 1
            if any(x in path_lower for x in ["api", "service", "backend", "server", "database", "controller"]):
                be_paths.append(path.split("/")[-1])
                be_score += 1
            
            # Extract file/folder name
            name = path.split("/")[-1] if "/" in path else path
            if name and not name.startswith("."):
                paths.append(name)
        
        if paths:
            parts.append(f"Works on: {', '.join(paths[:5])}")
        if fe_paths:
            parts.append(f"Frontend experience: {', '.join(fe_paths[:3])}")
        if be_paths:
            parts.append(f"Backend experience: {', '.join(be_paths[:3])}")
    
    # Role classification
    if fe_score > be_score * 2:
        role = "Frontend Developer"
    elif be_score > fe_score * 2:
        role = "Backend Developer"
    elif fe_score > 0 and be_score > 0:
        role = "Fullstack Developer"
    else:
        role = "Developer"
    parts.append(f"Role: {role}")
    
    # Experience level from commits
    commits = dev_data.get("commits", 0)
    loc_added = dev_data.get("loc_added", 0)
    wi_count = dev_data.get("wi_count", 0)
    
    if commits > 0:
        if commits > 50:
            experience = "highly active"
        elif commits > 20:
            experience = "active"
        elif commits > 5:
            experience = "moderately active"
        else:
            experience = "recently active"
        parts.append(f"Activity: {experience} ({commits} commits, {loc_added} lines added)")
    
    if wi_count > 0:
        parts.append(f"Work items completed: {wi_count}")
    
    # Add any explicit skills/tags if available
    skills = dev_data.get("skills", [])
    if skills:
        parts.append(f"Skills: {', '.join(skills)}")
    
    # Combine all parts
    return " | ".join(parts)


def load_developer_skills_from_file() -> List[Dict[str, Any]]:
    """Load developer skills from the JSON file."""
    skills_file = DATA_DIR / "developer_skills.json"
    if not skills_file.exists():
        logger.warning("developer_skills.json not found at %s", skills_file)
        return []
    
    try:
        return json.loads(skills_file.read_text())
    except Exception as e:
        logger.error("Failed to load developer_skills.json: %s", e)
        return []


def sync_developers_to_vector_db(force_rebuild: bool = False) -> Dict[str, Any]:
    """
    Synchronize developer skills to the vector database.
    
    This is the main entry point for keeping the developer knowledge base current.
    Called by the nightly scheduler task.
    
    Args:
        force_rebuild: If True, delete existing collection and rebuild from scratch
        
    Returns:
        Dict with sync statistics
    """
    from utilities.vector_db import get_vector_store
    
    start_time = datetime.now(timezone.utc)
    logger.info("Starting developer knowledge base sync (force_rebuild=%s)", force_rebuild)
    
    stats = {
        "started_at": start_time.isoformat(),
        "developers_processed": 0,
        "developers_added": 0,
        "developers_updated": 0,
        "errors": 0,
        "vector_store_type": None,
        "completed_at": None,
        "duration_seconds": None,
    }
    
    try:
        # Get vector store (Milvus > ChromaDB > JSON)
        store = get_vector_store(prefer_milvus=True, prefer_chroma=True)
        stats["vector_store_type"] = store.__class__.__name__
        logger.info("Using vector store: %s", stats["vector_store_type"])
        
        # Force rebuild if requested
        if force_rebuild:
            logger.info("Force rebuild: deleting existing collection")
            store.delete_collection(DEVELOPER_SKILLS_COLLECTION)
        
        # Load developer skills
        developers = load_developer_skills_from_file()
        if not developers:
            logger.warning("No developers found to sync")
            stats["completed_at"] = datetime.now(timezone.utc).isoformat()
            return stats
        
        logger.info("Processing %d developers", len(developers))
        stats["developers_processed"] = len(developers)
        
        # Build documents for vector store
        documents = []
        for dev in developers:
            try:
                email = dev.get("developer", "")
                if not email:
                    continue
                
                # Build rich text profile for embedding
                profile_text = build_developer_profile_text(dev)
                
                # Create document for vector store
                doc = {
                    "id": email.lower(),
                    "text": profile_text,
                    "developer": email,
                    "languages": ",".join(dev.get("languages", [])),
                    "commits": dev.get("commits", 0),
                    "loc_added": dev.get("loc_added", 0),
                    "wi_count": dev.get("wi_count", 0),
                    "raw_data": json.dumps(dev)[:10000],  # Store raw data for retrieval
                }
                documents.append(doc)
                
            except Exception as e:
                logger.error("Error processing developer %s: %s", dev.get("developer", "unknown"), e)
                stats["errors"] += 1
        
        # Add to vector store
        if documents:
            added = store.add_documents(documents, collection=DEVELOPER_SKILLS_COLLECTION)
            stats["developers_added"] = added
            logger.info("Added %d developers to vector store", added)
        
        # Get final count
        final_count = store.count(DEVELOPER_SKILLS_COLLECTION)
        logger.info("Total developers in knowledge base: %d", final_count)
        
    except Exception as e:
        logger.error("Developer KB sync failed: %s", e, exc_info=True)
        stats["errors"] += 1
    
    # Calculate duration
    end_time = datetime.now(timezone.utc)
    stats["completed_at"] = end_time.isoformat()
    stats["duration_seconds"] = (end_time - start_time).total_seconds()
    
    logger.info("Developer KB sync completed in %.2f seconds", stats["duration_seconds"])
    return stats


def find_similar_developers(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Find developers with skills similar to the query.
    
    This uses vector similarity to match developers based on:
    - Programming languages
    - Domain expertise (file paths)
    - Role (FE/BE/Fullstack)
    - Experience level
    
    Args:
        query: Natural language query describing needed skills
               e.g., "Angular frontend developer", "Java Spring API developer"
        top_k: Number of results to return
        
    Returns:
        List of matching developers with similarity scores
    """
    from utilities.vector_db import get_vector_store
    
    try:
        store = get_vector_store(prefer_milvus=True, prefer_chroma=True)
        results = store.query(query, collection=DEVELOPER_SKILLS_COLLECTION, top_k=top_k)
        
        # Enrich results with parsed metadata
        enriched = []
        for result in results:
            metadata = result.get("metadata", {})
            
            # Parse raw data if available
            raw_data = {}
            try:
                raw_json = metadata.get("raw_data", "{}")
                raw_data = json.loads(raw_json)
            except Exception:
                pass
            
            enriched.append({
                "developer": metadata.get("developer", result.get("id", "")),
                "similarity": result.get("similarity", 1 - result.get("distance", 1)),
                "profile_text": result.get("document", ""),
                "languages": metadata.get("languages", "").split(",") if metadata.get("languages") else [],
                "commits": int(metadata.get("commits", 0) or 0),
                "raw_data": raw_data,
            })
        
        return enriched
        
    except Exception as e:
        logger.error("Error finding similar developers: %s", e)
        return []


def get_developer_by_email(email: str) -> Optional[Dict[str, Any]]:
    """
    Get a specific developer's profile from the knowledge base.
    
    Args:
        email: Developer's email address
        
    Returns:
        Developer profile dict or None if not found
    """
    from utilities.vector_db import get_vector_store
    
    try:
        store = get_vector_store(prefer_milvus=True, prefer_chroma=True)
        all_devs = store.get_all(collection=DEVELOPER_SKILLS_COLLECTION)
        
        email_lower = email.lower()
        for dev in all_devs:
            if dev.get("id", "").lower() == email_lower:
                metadata = dev.get("metadata", {})
                raw_data = {}
                try:
                    raw_data = json.loads(metadata.get("raw_data", "{}"))
                except Exception:
                    pass
                
                return {
                    "developer": email,
                    "profile_text": dev.get("document", ""),
                    "metadata": metadata,
                    "raw_data": raw_data,
                }
        
        return None
        
    except Exception as e:
        logger.error("Error getting developer %s: %s", email, e)
        return None


def get_kb_stats() -> Dict[str, Any]:
    """Get statistics about the developer knowledge base."""
    from utilities.vector_db import get_vector_store
    
    try:
        store = get_vector_store(prefer_milvus=True, prefer_chroma=True)
        count = store.count(DEVELOPER_SKILLS_COLLECTION)
        
        return {
            "vector_store_type": store.__class__.__name__,
            "developer_count": count,
            "collection": DEVELOPER_SKILLS_COLLECTION,
        }
    except Exception as e:
        logger.error("Error getting KB stats: %s", e)
        return {
            "error": str(e),
            "developer_count": 0,
        }


# =============================================================================
# Scheduler Task Handler
# =============================================================================

async def developer_kb_sync_scheduled_task(config: Dict[str, Any] = None) -> None:
    """
    Scheduled task handler for nightly developer KB sync.
    
    This is called by the scheduler to update the developer knowledge base.
    No email notifications are sent - this is a silent background update.
    
    Args:
        config: Task configuration from config.yaml (optional)
    """
    config = config or {}
    
    logger.info("Starting scheduled developer KB sync task")
    logger.info("Config: %s", config)
    
    try:
        # Check if force rebuild is requested
        force_rebuild = config.get("options", {}).get("force_rebuild", False)
        
        # Run the sync
        stats = sync_developers_to_vector_db(force_rebuild=force_rebuild)
        
        logger.info("Scheduled developer KB sync completed: %s", stats)
        
        # Save stats to file for monitoring
        stats_file = DATA_DIR / "developer_kb_sync_stats.json"
        try:
            # Load existing stats history
            history = []
            if stats_file.exists():
                try:
                    history = json.loads(stats_file.read_text())
                except Exception:
                    pass
            
            # Keep last 30 days of history
            history.append(stats)
            history = history[-30:]
            
            stats_file.write_text(json.dumps(history, indent=2))
        except Exception as e:
            logger.warning("Could not save sync stats: %s", e)
        
    except Exception as e:
        logger.error("Developer KB sync task failed: %s", e, exc_info=True)
        raise


# =============================================================================
# CLI for manual testing
# =============================================================================

if __name__ == "__main__":
    import sys
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == "sync":
            force = "--force" in sys.argv
            print(f"Syncing developer KB (force_rebuild={force})...")
            stats = sync_developers_to_vector_db(force_rebuild=force)
            print(json.dumps(stats, indent=2))
            
        elif command == "stats":
            print("Developer KB Stats:")
            stats = get_kb_stats()
            print(json.dumps(stats, indent=2))
            
        elif command == "search":
            if len(sys.argv) < 3:
                print("Usage: python developer_knowledge_base.py search 'query'")
                sys.exit(1)
            query = sys.argv[2]
            print(f"Searching for: {query}")
            results = find_similar_developers(query, top_k=5)
            for i, dev in enumerate(results, 1):
                print(f"\n{i}. {dev['developer']} (similarity: {dev['similarity']:.2%})")
                print(f"   Languages: {', '.join(dev['languages'])}")
                print(f"   Profile: {dev['profile_text'][:200]}...")
        else:
            print(f"Unknown command: {command}")
            print("Available commands: sync, stats, search")
    else:
        print("Developer Knowledge Base CLI")
        print("Usage:")
        print("  python developer_knowledge_base.py sync [--force]  - Sync developers to vector DB")
        print("  python developer_knowledge_base.py stats           - Show KB statistics")
        print("  python developer_knowledge_base.py search 'query'  - Find similar developers")
