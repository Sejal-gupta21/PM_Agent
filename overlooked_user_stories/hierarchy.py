"""
Hierarchy Module - Fetch and Build Work Item Hierarchies

This module provides functions to fetch parent work items (Features and Epics)
and build hierarchical structure from Azure DevOps relations.

Architectural Rule: This module ONLY contains hierarchy-specific business logic.
It does NOT import from utilities/emailer.py or app/chat_ai.py (common files).
"""
from __future__ import annotations
import time
from typing import List, Dict, Any, Optional, Tuple
import requests
import logging

logger = logging.getLogger(__name__)

API_VERSION = "7.0"


def fetch_parent_workitems(org_url: str, parent_ids: List[int], pat: str, max_retries: int = 4) -> Dict[int, Dict[str, Any]]:
    """
    Fetch parent work items (Features, Epics) from Azure DevOps.
    
    Args:
        org_url: Azure DevOps organization URL
        parent_ids: List of parent work item IDs to fetch
        pat: Personal Access Token for authentication
        max_retries: Maximum number of retry attempts for failed requests
    
    Returns:
        Dictionary mapping work item ID to work item data
    """
    if not parent_ids:
        return {}
    
    result: Dict[int, Dict[str, Any]] = {}
    batch_size = 200
    failed_ids = []
    
    for i in range(0, len(parent_ids), batch_size):
        chunk = parent_ids[i : i + batch_size]
        ids_str = ",".join(map(str, chunk))
        url = f"{org_url}/_apis/wit/workitems?ids={ids_str}&$expand=all&api-version={API_VERSION}"
        
        batch_success = False
        # Retry logic with exponential backoff
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, auth=("", pat), timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("value", []):
                        item_id = item.get("id")
                        if item_id:
                            result[item_id] = item
                    batch_success = True
                    break  # Success, exit retry loop
                elif resp.status_code == 404:
                    # Batch failed due to invalid IDs - ADO doesn't return partial results
                    # We'll need to fetch individually
                    error_msg = resp.text[:300] if resp.text else "Unknown error"
                    logger.warning(f"  Warning: Batch fetch failed (404), will retry IDs individually: {error_msg}")
                    failed_ids.extend(chunk)
                    break  # Don't retry 404s for batch
                elif resp.status_code >= 500:
                    # Server error, retry with backoff
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.info(f"  Retry {attempt + 1}/{max_retries} after server error (status {resp.status_code}), waiting {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"  Failed to fetch parent work items after {max_retries} attempts: {resp.status_code} {resp.text[:200]}")
                else:
                    # Client error, don't retry
                    logger.warning(f"  Failed to fetch parent work items: {resp.status_code} {resp.text[:200]}")
                    break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"  Retry {attempt + 1}/{max_retries} after connection error: {e}, waiting {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"  Failed to fetch parent work items after {max_retries} attempts due to connection errors")
            except Exception as e:
                logger.exception(f"  Unexpected error fetching parent work items: {e}")
                break
    
    # If we had any failed IDs, try fetching them individually
    if failed_ids:
        logger.info(f"  Fetching {len(failed_ids)} parent work items individually...")
        for pid in failed_ids:
            try:
                url = f"{org_url}/_apis/wit/workitems?ids={pid}&$expand=all&api-version={API_VERSION}"
                resp = requests.get(url, auth=("", pat), timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("value", []):
                        item_id = item.get("id")
                        if item_id:
                            result[item_id] = item
                # Silently skip 404s for individual fetches
            except Exception:
                pass  # Skip errors for individual fetches
    
    return result


def build_hierarchy_map(rows: List[Dict[str, str]], workitems: List[Dict[str, Any]], org_url: str, pat: str) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, int], Dict[str, str]]:
    """
    Build hierarchical mapping from User Stories to Features to Epics.
    
    Args:
        rows: List of report row dictionaries (must contain "ID" key)
        workitems: List of work item dictionaries from ADO (with relations)
        org_url: Azure DevOps organization URL
        pat: Personal Access Token
    
    Returns:
        Tuple of (story_to_feature_map, feature_title_to_epic_map, story_to_feature_id, feature_id_to_title)
        - story_to_feature_map: Maps story ID to Feature title
        - feature_title_to_epic_map: Maps Feature title to Epic title
        - story_to_feature_id: Maps story ID to Feature ID (for lookups)
        - feature_id_to_title: Maps Feature ID to Feature title
    """
    story_to_feature: Dict[str, str] = {}  # story_id -> feature_title
    feature_title_to_epic: Dict[str, str] = {}   # feature_title -> epic_title
    
    # Extract parent relationships from work items
    story_to_feature_id: Dict[str, int] = {}  # story_id -> feature_id
    feature_to_epic_id: Dict[str, int] = {}   # feature_id -> epic_id
    
    for wi in workitems:
        wi_id = str(wi.get("id", ""))
        relations = wi.get("relations", [])
        
        if not relations:
            continue
        
        # Find parent relationship (System.LinkTypes.Hierarchy-Reverse)
        for rel in relations:
            rel_type = rel.get("rel", "")
            if rel_type == "System.LinkTypes.Hierarchy-Reverse":
                # This is the parent link
                url = rel.get("url", "")
                if url:
                    # Extract parent ID from URL (last segment)
                    try:
                        parent_id = int(url.rstrip('/').split('/')[-1])
                        wi_type = wi.get("fields", {}).get("System.WorkItemType", "")
                        
                        if wi_type in ("User Story", "Product Backlog Item"):
                            story_to_feature_id[wi_id] = parent_id
                        elif wi_type == "Feature":
                            feature_to_epic_id[wi_id] = parent_id
                    except (ValueError, IndexError):
                        pass
    
    # Collect all unique parent IDs we need to fetch
    all_parent_ids = set()
    all_parent_ids.update(story_to_feature_id.values())
    all_parent_ids.update(feature_to_epic_id.values())
    
    if not all_parent_ids:
        logger.info("  No parent relationships found in work items")
        return story_to_feature, feature_title_to_epic, story_to_feature_id, {}
    
    # Fetch all parent work items
    logger.info(f"  Fetching {len(all_parent_ids)} parent work items (Features/Epics)...")
    parent_workitems = fetch_parent_workitems(org_url, list(all_parent_ids), pat)
    
    # Build lookup from ID to title
    parent_id_to_title: Dict[int, str] = {}
    parent_id_to_type: Dict[int, str] = {}
    feature_id_to_title: Dict[str, str] = {}
    for pid, pwi in parent_workitems.items():
        fields = pwi.get("fields", {})
        title = fields.get("System.Title", f"Work Item {pid}")
        wi_type = fields.get("System.WorkItemType", "")
        parent_id_to_title[pid] = title
        parent_id_to_type[pid] = wi_type
        if wi_type == "Feature":
            feature_id_to_title[str(pid)] = title
    
    # Map stories to features (story_id -> feature_title)
    for story_id, feature_id in story_to_feature_id.items():
        feature_title = parent_id_to_title.get(feature_id, f"Feature {feature_id}")
        story_to_feature[story_id] = feature_title
        
        # Check if this feature has a parent epic
        if feature_id in feature_to_epic_id:
            epic_id = feature_to_epic_id[feature_id]
            epic_title = parent_id_to_title.get(epic_id, f"Epic {epic_id}")
            # Map feature_title -> epic_title (not feature_id -> epic_title)
            feature_title_to_epic[feature_title] = epic_title
    
    # Also map features that might be direct children of epics
    for feature_id_str, epic_id in feature_to_epic_id.items():
        epic_title = parent_id_to_title.get(epic_id, f"Epic {epic_id}")
        feature_title = parent_id_to_title.get(int(feature_id_str), f"Feature {feature_id_str}")
        feature_title_to_epic[feature_title] = epic_title
    
    logger.info(f"  Built hierarchy: {len(story_to_feature)} stories mapped to features, {len(feature_title_to_epic)} features mapped to epics")
    
    return story_to_feature, feature_title_to_epic, story_to_feature_id, feature_id_to_title


def enrich_rows_with_hierarchy(rows: List[Dict[str, str]], story_to_feature: Dict[str, str], feature_to_epic: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Enrich report rows with Epic and Feature titles.
    
    Args:
        rows: List of report row dictionaries
        story_to_feature: Mapping from story ID to Feature title
        feature_to_epic: Mapping from feature ID to Epic title
    
    Returns:
        List of enriched rows with "EpicTitle" and "FeatureTitle" fields
    """
    enriched = []
    
    for row in rows:
        story_id = row.get("ID", "")
        feature_title = story_to_feature.get(story_id, "Unassigned Feature")
        
        # To get epic, we need to find which feature this story belongs to,
        # then look up the epic for that feature. Since feature_to_epic uses feature_id,
        # we need a reverse mapping or store feature IDs. For simplicity, we'll use
        # feature title as key (this is a limitation, but works for most cases)
        epic_title = "Unassigned Epic"
        
        # Build a feature title to epic title mapping
        # This is a workaround since we don't have feature IDs in rows
        # We'll use the feature title to look up epic (requires storing feature IDs)
        # For now, we'll mark as "Unassigned Epic" if not found
        
        # Create enriched row with Epic and Feature columns prepended
        enriched_row = {
            "EpicTitle": epic_title,
            "FeatureTitle": feature_title,
            **row  # Include all original fields
        }
        enriched.append(enriched_row)
    
    return enriched


def extract_feature_id_from_story(story_id: str, workitems: List[Dict[str, Any]]) -> Optional[int]:
    """
    Extract Feature ID for a given story ID from work items.
    
    Args:
        story_id: Story work item ID
        workitems: List of work item dictionaries
    
    Returns:
        Feature ID if found, None otherwise
    """
    for wi in workitems:
        if str(wi.get("id")) == story_id:
            relations = wi.get("relations", [])
            for rel in relations:
                if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
                    url = rel.get("url", "")
                    if url:
                        try:
                            return int(url.rstrip('/').split('/')[-1])
                        except (ValueError, IndexError):
                            pass
    return None


def build_complete_hierarchy(rows: List[Dict[str, str]], workitems: List[Dict[str, Any]], org_url: str, pat: str) -> Tuple[List[Dict[str, str]], Dict[str, Dict[str, List[Dict[str, str]]]]]:
    """
    Build complete hierarchy structure for reporting.
    
    Args:
        rows: List of report row dictionaries
        workitems: List of work item dictionaries from ADO
        org_url: Azure DevOps organization URL
        pat: Personal Access Token
    
    Returns:
        Tuple of (enriched_rows, hierarchy_structure)
        - enriched_rows: Rows with Epic and Feature titles added
        - hierarchy_structure: Nested dict {epic_title: {feature_title: [rows]}}
    """
    # Build basic mappings
    story_to_feature, feature_title_to_epic, story_to_feature_id, feature_id_to_title = build_hierarchy_map(rows, workitems, org_url, pat)
    
    # Enrich rows with hierarchy information
    enriched_rows = []
    hierarchy: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
    
    for row in rows:
        story_id = row.get("ID", "")
        feature_title = story_to_feature.get(story_id, "Unassigned Feature")
        
        # Look up epic for this feature using feature title
        epic_title = feature_title_to_epic.get(feature_title, "Unassigned Epic")
        
        # Create enriched row
        enriched_row = {
            "EpicTitle": epic_title,
            "FeatureTitle": feature_title,
            **row
        }
        enriched_rows.append(enriched_row)
        
        # Build hierarchy structure for reporting
        if epic_title not in hierarchy:
            hierarchy[epic_title] = {}
        if feature_title not in hierarchy[epic_title]:
            hierarchy[epic_title][feature_title] = []
        hierarchy[epic_title][feature_title].append(enriched_row)
    
    return enriched_rows, hierarchy
