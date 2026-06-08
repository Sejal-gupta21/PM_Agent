"""
LLM Embeddings Utilities

Provides text embedding and similarity functions using OpenAI API.
"""

import os
import logging
from typing import Dict, Any, List, Optional, Tuple
import math

logger = logging.getLogger("pm_agent.utilities.llm_embeddings")


def _cosine(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def build_embedding_text(entry: Dict[str, Any]) -> str:
    """
    Build text for embedding from a work item entry.
    
    Args:
        entry: Dict with title, desc_norm, repro_norm, tags, area, error, module
    
    Returns:
        Combined text string for embedding
    """
    parts = []
    if entry.get("title"):
        parts.append(str(entry["title"]))
    if entry.get("desc_norm"):
        parts.append(str(entry["desc_norm"]))
    if entry.get("repro_norm"):
        parts.append(str(entry["repro_norm"]))
    if entry.get("tags"):
        parts.append(str(entry["tags"]))
    if entry.get("area"):
        parts.append(str(entry["area"]))
    if entry.get("module"):
        parts.append(str(entry["module"]))
    if entry.get("error"):
        parts.append(str(entry["error"])[:500])  # Truncate error text
    if entry.get("text"):
        parts.append(str(entry["text"]))
    
    return " ".join(parts).strip()


def embed_texts_with_cache(
    items: Dict[str, Dict[str, Any]],
    cache: Optional[Dict[str, Dict[str, Any]]] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Embed texts using OpenAI API.
    
    Args:
        items: Dict mapping ID -> {"text": str}
        cache: Optional existing cache to update
    
    Returns:
        Dict mapping ID -> {"text": str, "embedding": List[float]}
    """
    result = cache.copy() if cache else {}
    texts_to_embed = []
    ids_to_embed = []
    
    for item_id, item in items.items():
        if item_id in result and result[item_id].get("embedding"):
            continue
        text = item.get("text") or build_embedding_text(item)
        if text:
            texts_to_embed.append(text)
            ids_to_embed.append(item_id)
            result[item_id] = {"text": text, "embedding": None}
    
    if not texts_to_embed:
        return result
    
    # Use OpenAI (new client API for openai>=1.0.0)
    from config import config as app_config
    openai_key = app_config.openai_api_key
    if openai_key and openai_key != "YOUR_OPENAI_KEY_HERE":
        try:
            from openai import OpenAI
            import httpx
            client = OpenAI(api_key=openai_key, timeout=httpx.Timeout(10.0, connect=5.0))
            
            # Batch embed (max 2048 texts per call for ada-002)
            batch_size = 100
            for i in range(0, len(texts_to_embed), batch_size):
                batch_texts = texts_to_embed[i:i + batch_size]
                batch_ids = ids_to_embed[i:i + batch_size]
                
                try:
                    response = client.embeddings.create(
                        model="text-embedding-ada-002",
                        input=batch_texts
                    )
                    for j, emb_data in enumerate(response.data):
                        embedding = emb_data.embedding
                        result[batch_ids[j]]["embedding"] = embedding
                except Exception as e:
                    logger.warning("OpenAI embedding batch failed: %s", e)
                    # Try one at a time
                    for j, text in enumerate(batch_texts):
                        try:
                            response = client.embeddings.create(
                                model="text-embedding-ada-002",
                                input=[text]
                            )
                            result[batch_ids[j]]["embedding"] = response.data[0].embedding
                        except Exception:
                            pass
            
            return result
        except Exception as e:
            logger.warning("OpenAI embeddings not available: %s", e)
    
    logger.warning("No embedding provider available (set OPENAI_API_KEY in config.yaml)")
    return result


def compute_best_matches(
    query_embedding: List[float],
    cache: Dict[str, Dict[str, Any]],
    top_k: int = 10,
    min_score: float = 0.0
) -> List[Tuple[str, float]]:
    """
    Find best matching items from cache based on embedding similarity.
    
    Args:
        query_embedding: Query embedding vector
        cache: Dict mapping ID -> {"embedding": List[float]}
        top_k: Number of top matches to return
        min_score: Minimum similarity score threshold
    
    Returns:
        List of (item_id, score) tuples sorted by score descending
    """
    if not query_embedding:
        return []
    
    scores = []
    for item_id, item in cache.items():
        emb = item.get("embedding")
        if not emb:
            continue
        score = _cosine(query_embedding, emb)
        if score >= min_score:
            scores.append((item_id, score))
    
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_k]
