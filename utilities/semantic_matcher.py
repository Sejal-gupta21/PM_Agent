"""Semantic Matcher for skill intent recognition.

Uses embeddings (OpenAI if available) or TF-IDF fallback to match
user queries to registered skills based on semantic similarity.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"


def get_embedding_fn() -> Tuple[Callable[[List[str]], List[List[float]]], str]:
    """Get the best available embedding function.
    
    Returns:
        Tuple of (embed_function, embed_type_name)
    """
    from config import config as app_config
    openai_key = app_config.openai_api_key
    
    if openai_key:
        try:
            from openai import OpenAI
            import httpx
            model = app_config.openai_embedding_model
            client = OpenAI(api_key=openai_key, timeout=httpx.Timeout(10.0, connect=5.0))
            
            def embed_openai(texts: List[str]) -> List[List[float]]:
                embeddings = []
                for text in texts:
                    if len(text) > 8000:
                        text = text[:8000]
                    resp = client.embeddings.create(model=model, input=text)
                    embeddings.append(resp.data[0].embedding)
                return embeddings
            
            logger.info("Using OpenAI embeddings")
            return embed_openai, "openai"
        except Exception as e:
            logger.warning("OpenAI embedding init failed: %s", e)
    
    # Fallback: TF-IDF-like hashing (no external API needed)
    logger.info("Using TF-IDF hash fallback for embeddings")
    return _embed_tfidf_hash, "tfidf_hash"


def _embed_tfidf_hash(texts: List[str]) -> List[List[float]]:
    """TF-IDF-like hash-based embedding fallback.
    
    Creates a 256-dimensional vector based on word hashes.
    """
    embeddings = []
    for text in texts:
        words = _tokenize(text)
        vec = [0.0] * 256
        
        # Count word frequencies
        word_counts: Dict[str, int] = {}
        for word in words:
            word_counts[word] = word_counts.get(word, 0) + 1
        
        # Build vector from word hashes with TF weighting
        for word, count in word_counts.items():
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            tf = 1 + math.log(count) if count > 0 else 0
            for i in range(256):
                vec[i] += ((h >> i) & 1) * tf * 0.1
        
        # L2 normalize
        mag = math.sqrt(sum(x * x for x in vec)) or 1.0
        vec = [x / mag for x in vec]
        embeddings.append(vec)
    
    return embeddings


def _tokenize(text: str) -> List[str]:
    """Simple tokenization: lowercase, split on non-alphanumeric."""
    import re
    text = text.lower()
    words = re.findall(r'\b[a-z0-9]+\b', text)
    
    # Remove very short words and common stop words
    stop_words = {
        'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
        'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from', 'as',
        'into', 'through', 'during', 'before', 'after', 'above', 'below',
        'between', 'under', 'again', 'further', 'then', 'once', 'here',
        'there', 'when', 'where', 'why', 'how', 'all', 'each', 'few', 'more',
        'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own',
        'same', 'so', 'than', 'too', 'very', 'just', 'and', 'but', 'if', 'or',
        'because', 'until', 'while', 'it', 'its', 'this', 'that', 'these',
        'those', 'i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'you', 'your',
        'he', 'him', 'his', 'she', 'her', 'they', 'them', 'their', 'what',
        'which', 'who', 'whom', 'about'
    }
    
    return [w for w in words if len(w) > 1 and w not in stop_words]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _find_rationale(query: str, skill: Dict[str, Any]) -> str:
    """Generate a rationale for why the skill matched.
    
    Finds overlapping significant tokens between query and skill.
    """
    query_tokens = set(_tokenize(query))
    
    # Get skill text
    skill_text = " ".join([
        skill.get("display_name", ""),
        skill.get("description", ""),
        " ".join(skill.get("canonical_prompts", [])),
    ])
    skill_tokens = set(_tokenize(skill_text))
    
    # Find overlap
    overlap = query_tokens & skill_tokens
    
    if overlap:
        # Find which canonical prompt has the most overlap
        best_prompt = None
        best_overlap_count = 0
        for prompt in skill.get("canonical_prompts", []):
            prompt_tokens = set(_tokenize(prompt))
            count = len(query_tokens & prompt_tokens)
            if count > best_overlap_count:
                best_overlap_count = count
                best_prompt = prompt
        
        if best_prompt and best_overlap_count > 0:
            return f"Similar to: '{best_prompt}' (matching: {', '.join(sorted(overlap)[:5])})"
        return f"Matching tokens: {', '.join(sorted(overlap)[:5])}"
    
    return "Semantic similarity based on embedding"


def match_query_to_skills(
    query: str,
    top_k: int = 3,
    threshold: float = 0.0,
) -> List[Dict[str, Any]]:
    """Match a user query to registered skills using semantic similarity.
    
    Args:
        query: User's natural language query
        top_k: Maximum number of results to return
        threshold: Minimum similarity score (0-1)
        
    Returns:
        List of dicts with: skill_id, score, skill (full dict), rationale
    """
    from utilities.skill_registry import load_skills, get_skill_vectors, get_skill_text_for_embedding
    
    skills = load_skills()
    if not skills:
        logger.warning("No skills loaded")
        return []
    
    # Get skill vectors (cached)
    skill_vectors = get_skill_vectors()
    
    # Embed the query
    embed_fn, embed_type = get_embedding_fn()
    try:
        query_vec = embed_fn([query])[0]
    except Exception as e:
        logger.error("Failed to embed query: %s", e)
        return []
    
    # Compute similarities
    results = []
    for skill in skills:
        skill_id = skill.get("id")
        if not skill_id:
            continue
        
        skill_vec = skill_vectors.get(skill_id)
        if not skill_vec:
            # Compute on the fly if missing from cache
            text = get_skill_text_for_embedding(skill)
            try:
                skill_vec = embed_fn([text])[0]
            except Exception:
                continue
        
        score = cosine_similarity(query_vec, skill_vec)
        
        # Apply small heuristic boost for very specific phrases
        score = _apply_heuristic_boost(query, skill, score)
        
        if score >= threshold:
            rationale = _find_rationale(query, skill)
            results.append({
                "skill_id": skill_id,
                "score": round(score, 4),
                "skill": skill,
                "rationale": rationale,
            })
    
    # Sort by score descending
    results.sort(key=lambda x: (-x["score"], x.get("skill", {}).get("priority", 99)))
    
    return results[:top_k]


def _apply_heuristic_boost(query: str, skill: Dict[str, Any], score: float) -> float:
    """Apply small heuristic boost for guaranteed phrase matches.
    
    This is NOT the primary mechanism — just a small nudge for obvious matches.
    """
    query_lower = query.lower()
    skill_id = skill.get("id", "")
    
    # Sprint tracking boosters (max +0.1)
    if skill_id == "sprint_tracking":
        boost_phrases = [
            ("daily report", 0.1),
            ("progress report", 0.1),
            ("sprint progress", 0.08),
            ("iteration report", 0.1),
            ("offtrack", 0.1),
            ("off-track", 0.1),
            ("off track", 0.1),
            ("sprint status", 0.08),
            ("send report", 0.08),
            # New dynamic query boosters
            ("slowest", 0.12),
            ("moving the slowest", 0.15),
            ("stuck", 0.1),
            ("blocked", 0.12),
            ("blocker", 0.12),
            ("impediment", 0.1),
            ("stale", 0.1),
            ("not moving", 0.1),
            ("no progress", 0.1),
            ("current block", 0.15),
            ("what is blocked", 0.12),
            ("urgent", 0.08),
            ("needs attention", 0.08),
            ("delayed", 0.1),
            ("behind schedule", 0.1),
        ]
        for phrase, boost in boost_phrases:
            if phrase in query_lower:
                return min(1.0, score + boost)
    
    # Developer skills boosters
    elif skill_id == "developer_skills":
        boost_phrases = [
            ("tech stack", 0.1),
            ("skill matrix", 0.1),
            ("who knows", 0.08),
            ("developer knowledge", 0.18),
            ("developer knowledge base", 0.22),
            ("knowledge base", 0.12),
            ("developer kb", 0.18),
        ]
        for phrase, boost in boost_phrases:
            if phrase in query_lower:
                return min(1.0, score + boost)
    
    # Upcoming tasks boosters
    elif skill_id == "upcoming_tasks":
        boost_phrases = [
            ("sprint planning", 0.1),
            ("ready for planning", 0.1),
            ("upcoming tasks", 0.1),
            ("backlog", 0.05),
        ]
        for phrase, boost in boost_phrases:
            if phrase in query_lower:
                return min(1.0, score + boost)
    
    # Capacity forecast boosters
    elif skill_id == "get_capacity_forecast":
        boost_phrases = [
            ("capacity check", 0.15),
            ("check capacity", 0.15),
            ("capacity forecast", 0.15),
            ("team capacity", 0.12),
            ("developer capacity", 0.12),
            ("capacity status", 0.1),
            ("capacity warning", 0.12),
            ("who is overloaded", 0.15),
            ("who is underutilized", 0.15),
            ("overloaded", 0.1),
            ("underutilized", 0.1),
            ("workload", 0.08),
            ("utilization", 0.1),
            ("available capacity", 0.12),
            ("capacity report", 0.1),
        ]
        for phrase, boost in boost_phrases:
            if phrase in query_lower:
                return min(1.0, score + boost)
    
    # Overlooked stories boosters
    elif skill_id == "overlooked_stories":
        # NEGATIVE boosters first - prevent false positives
        if "show me all" in query_lower or "list all" in query_lower:
            if "overlooked" not in query_lower and "stale" not in query_lower:
                return max(0.0, score - 0.2)  # Strong penalty
        
        if "bug" in query_lower and "overlooked bug" not in query_lower:
            return max(0.0, score - 0.15)  # Penalty for bug-related queries
        
        # POSITIVE boosters
        boost_phrases = [
            ("overlooked stories", 0.15),
            ("overlooked user stories", 0.18),
            ("overlooked story", 0.15),
            ("stale stories", 0.12),
            ("stale user stories", 0.15),
            ("forgotten stories", 0.12),
            ("forgotten items", 0.1),
            ("neglected stories", 0.12),
            ("dormant stories", 0.1),
            ("inactive stories", 0.12),
            ("stories not updated", 0.1),
            ("no activity", 0.08),
            ("no recent activity", 0.12),
            ("untouched stories", 0.1),
            ("stories with no progress", 0.1),
            ("old stories", 0.08),
            ("stuck stories", 0.1),
            ("what stories are stale", 0.15),
            ("find overlooked", 0.12),
            ("find stale", 0.1),
            ("missed stories", 0.12),
            ("missed recently", 0.1),
        ]
        for phrase, boost in boost_phrases:
            if phrase in query_lower:
                return min(1.0, score + boost)
    
    # Billing deviation boosters - HIGH priority to ensure correct routing
    elif skill_id == "billing_deviation":
        # NEGATIVE boosters - prevent false positives with capacity forecast
        if "team" in query_lower and "breakdown" in query_lower:
            if "billing" not in query_lower and "deviation" not in query_lower:
                return max(0.0, score - 0.1)  # Penalty for team capacity queries
        
        # POSITIVE boosters - INCREASED values to ensure high confidence match
        boost_phrases = [
            ("billing deviation", 0.25),  # Primary phrase - high boost
            ("billing deviaiton", 0.25),  # Typo variant - same boost
            ("deviaiton report", 0.22),   # Typo variant
            ("deviation in billing", 0.22),  # Reversed phrase
            ("billing report", 0.18),
            ("billing status", 0.15),
            ("effort deviation", 0.18),
            ("effort variance", 0.15),
            ("billing hours", 0.15),
            ("actual vs target", 0.15),
            ("target hours", 0.12),
            ("billing off-track", 0.18),
            ("billing offtrack", 0.18),
            ("over budget", 0.12),
            ("under budget", 0.12),
            ("hours logged", 0.10),
            ("completed hours", 0.10),
            ("billing analysis", 0.15),
            ("effort tracking", 0.15),
            ("work hours", 0.10),
            ("actual hours", 0.12),
            ("hours deviation", 0.18),
            ("billing summary", 0.15),
            ("hours difference", 0.12),
            ("difference from target", 0.12),
            ("over-billing", 0.18),
            ("under-billing", 0.18),
            ("billing by module", 0.15),
            ("billing for current month", 0.20),  # Common query
            ("current month", 0.08),  # Small boost for time reference
        ]
        for phrase, boost in boost_phrases:
            if phrase in query_lower:
                return min(1.0, score + boost)
    
    # Bug Areas Highlight - CRITICAL: Prevent false positives for simple bug queries
    elif skill_id == "bug_areas_highlight":
        import re
        # NEGATIVE boosters - prevent matching simple "show bugs" queries
        # These should go to PM Agent for search_workitem calls, NOT to bug_areas_highlight
        is_simple_data_query = (
            re.search(r"(list|show|get|give|find|fetch|display)\s+(me\s+)?(all\s+)?(\w+\s+)?(bugs?|items?)", query_lower) is not None
            or re.search(r"(active|open|closed|resolved|new)\s+bugs?", query_lower) is not None
            or re.search(r"bugs?\s+(for|of|to|assigned)", query_lower) is not None
            or re.search(r"(all|every)\s+(the\s+)?bugs?", query_lower) is not None
        )
        
        # Only match bug_areas_highlight if user explicitly wants analysis/patterns
        has_analysis_keywords = any(kw in query_lower for kw in [
            "recurring", "pattern", "repeat", "highlight", "area",
            "hotspot", "trend", "analysis", "analyze", "similar"
        ])
        
        if is_simple_data_query and not has_analysis_keywords:
            return max(0.0, score - 0.4)  # Strong penalty to prevent matching
        
        # POSITIVE boosters for explicit requests
        boost_phrases = [
            ("recurring bugs", 0.15),
            ("bug patterns", 0.12),
            ("bug areas", 0.15),
            ("highlight bug", 0.12),
            ("bug hotspots", 0.15),
            ("bug trends", 0.12),
            ("analyze bugs", 0.1),
            ("bug analysis", 0.12),
        ]
        for phrase, boost in boost_phrases:
            if phrase in query_lower:
                return min(1.0, score + boost)
    
    # Feedback to Dev - CRITICAL: Prevent false positives for simple bug queries
    elif skill_id == "feedback_to_dev":
        import re
        # NEGATIVE boosters - prevent matching simple "show bugs" queries
        is_simple_data_query = (
            re.search(r"(list|show|get|give|find|fetch|display)\s+(me\s+)?(all\s+)?(\w+\s+)?(bugs?|items?)", query_lower) is not None
            or re.search(r"(active|open|closed|resolved|new)\s+bugs?", query_lower) is not None
            or re.search(r"bugs?\s+(for|of|to|assigned)", query_lower) is not None
            or re.search(r"(all|every)\s+(the\s+)?bugs?", query_lower) is not None
        )
        
        # Only match feedback_to_dev if user explicitly wants feedback/RCA/notification
        has_feedback_keywords = any(kw in query_lower for kw in [
            "feedback", "rca", "root cause", "notify", "notification",
            "send feedback", "developer feedback", "new bug notification"
        ])
        
        if is_simple_data_query and not has_feedback_keywords:
            return max(0.0, score - 0.4)  # Strong penalty to prevent matching
        
        # POSITIVE boosters for explicit requests
        boost_phrases = [
            ("feedback to dev", 0.18),
            ("developer feedback", 0.15),
            ("bug feedback", 0.12),
            ("rca feedback", 0.15),
            ("root cause", 0.12),
            ("notify developer", 0.12),
        ]
        for phrase, boost in boost_phrases:
            if phrase in query_lower:
                return min(1.0, score + boost)
    
    # Detect Recurring Bugs - same protection
    elif skill_id == "detect_recurring_bugs":
        import re
        is_simple_data_query = (
            re.search(r"(list|show|get|give|find|fetch|display)\s+(me\s+)?(all\s+)?(\w+\s+)?(bugs?|items?)", query_lower) is not None
            or re.search(r"(active|open|closed|resolved|new)\s+bugs?", query_lower) is not None
            or re.search(r"bugs?\s+(for|of|to|assigned)", query_lower) is not None
        )
        
        has_recurring_keywords = any(kw in query_lower for kw in [
            "recurring", "repeat", "repeated", "pattern", "same bug"
        ])
        
        if is_simple_data_query and not has_recurring_keywords:
            return max(0.0, score - 0.4)
    
    return score


def classify_intent(
    query: str,
    confident_threshold: float = 0.7,
    tentative_threshold: float = 0.4,
) -> Dict[str, Any]:
    """Classify user intent from query.
    
    Returns:
        Dict with:
        - matched: bool (whether a skill was confidently matched)
        - skill_id: str or None
        - score: float
        - confidence: "high" | "medium" | "low" | "none"
        - skill: full skill dict or None
        - rationale: explanation string
        - alternatives: list of other possible skills (if tentative)
    """
    results = match_query_to_skills(query, top_k=3, threshold=0.1)
    
    if not results:
        return {
            "matched": False,
            "skill_id": None,
            "score": 0.0,
            "confidence": "none",
            "skill": None,
            "rationale": "No matching skills found",
            "alternatives": [],
        }
    
    top = results[0]
    score = top["score"]
    
    if score >= confident_threshold:
        return {
            "matched": True,
            "skill_id": top["skill_id"],
            "score": score,
            "confidence": "high",
            "skill": top["skill"],
            "rationale": top["rationale"],
            "alternatives": [],
        }
    elif score >= tentative_threshold:
        return {
            "matched": True,
            "skill_id": top["skill_id"],
            "score": score,
            "confidence": "medium",
            "skill": top["skill"],
            "rationale": top["rationale"],
            "alternatives": results[1:] if len(results) > 1 else [],
        }
    else:
        return {
            "matched": False,
            "skill_id": None,
            "score": score,
            "confidence": "low",
            "skill": None,
            "rationale": f"Best match score {score:.2f} below threshold {tentative_threshold}",
            "alternatives": results,
        }
