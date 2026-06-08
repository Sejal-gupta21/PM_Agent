"""
Async ADO helper functions using aiohttp.

Provides async versions of WIQL and work item fetching.
"""

import os
import logging
import aiohttp
from typing import List, Dict, Any, Optional

logger = logging.getLogger("pm_agent.utilities.ado_async")

API_VERSION = "7.0"


def _get_pat():
    """Get PAT from environment."""
    from utilities.mcp.pat import get_pat
    return get_pat()


async def run_wiql_async(
    org_url: str,
    wiql: str,
    project: Optional[str] = None,
    team: Optional[str] = None,
    pat: Optional[str] = None
) -> List[int]:
    """
    Execute WIQL query asynchronously and return work item IDs.
    
    Args:
        org_url: Azure DevOps organization URL
        wiql: WIQL query string
        project: Optional project name (for scoped queries)
        team: Optional team name
        pat: Optional PAT (defaults to env)
    
    Returns:
        List of work item IDs
    """
    pat = pat or _get_pat()
    if not pat:
        logger.error("No PAT available for ADO request")
        return []
    
    if project and team:
        url = f"{org_url}/{project}/{team}/_apis/wit/wiql?api-version={API_VERSION}"
    elif project:
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version={API_VERSION}"
    else:
        url = f"{org_url}/_apis/wit/wiql?api-version={API_VERSION}"
    
    auth = aiohttp.BasicAuth("", pat)
    
    async with aiohttp.ClientSession(auth=auth) as session:
        try:
            async with session.post(
                url,
                json={"query": wiql},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error("WIQL query failed: %s %s", resp.status, text)
                    raise RuntimeError(f"WIQL request failed: {resp.status}")
                data = await resp.json()
                return [item["id"] for item in data.get("workItems", [])]
        except Exception as e:
            logger.exception("Error running WIQL: %s", e)
            raise


async def fetch_workitems_async(
    org_url: str,
    ids: List[int],
    pat: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Fetch work item details asynchronously.
    
    Args:
        org_url: Azure DevOps organization URL
        ids: List of work item IDs to fetch
        pat: Optional PAT (defaults to env)
    
    Returns:
        List of work item dicts with fields
    """
    if not ids:
        return []
    
    pat = pat or _get_pat()
    if not pat:
        logger.error("No PAT available for ADO request")
        return []
    
    auth = aiohttp.BasicAuth("", pat)
    batch = 200
    results: List[Dict[str, Any]] = []
    
    async with aiohttp.ClientSession(auth=auth) as session:
        for i in range(0, len(ids), batch):
            chunk = ids[i: i + batch]
            ids_chunk = ",".join(map(str, chunk))
            url = f"{org_url}/_apis/wit/workitems?ids={ids_chunk}&$expand=all&api-version={API_VERSION}"
            
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        logger.error("Fetch workitems failed: %s %s", resp.status, text)
                        continue
                    data = await resp.json()
                    results.extend(data.get("value", []))
            except Exception as e:
                logger.exception("Error fetching workitems chunk: %s", e)
                continue
    
    return results


async def get_workitem_async(
    org_url: str,
    wi_id: int,
    pat: Optional[str] = None
) -> Dict[str, Any]:
    """
    Fetch a single work item by ID.
    
    Args:
        org_url: Azure DevOps organization URL
        wi_id: Work item ID
        pat: Optional PAT (defaults to env)
    
    Returns:
        Work item dict with fields
    """
    pat = pat or _get_pat()
    if not pat:
        logger.error("No PAT available for ADO request")
        return {}


    async def fetch_comments_async(
        org_url: str,
        ids: List[int],
        pat: Optional[str] = None
    ) -> Dict[int, str]:
        """
        Fetch comments for a list of work items asynchronously.

        Returns a mapping of work item id -> concatenated comment text.
        """
        pat = pat or _get_pat()
        if not pat:
            logger.error("No PAT available for ADO request")
            return {}

        auth = aiohttp.BasicAuth("", pat)
        results: Dict[int, str] = {}

        async with aiohttp.ClientSession(auth=auth) as session:
            for wid in ids:
                url = f"{org_url}/_apis/wit/workItems/{wid}/comments?api-version={API_VERSION}"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            logger.debug("Comments fetch failed for %s: %s %s", wid, resp.status, text)
                            results[wid] = ""
                            continue
                        data = await resp.json()
                        comments = []
                        for c in data.get("comments", []):
                            txt = c.get("text") or c.get("content") or ""
                            if txt:
                                comments.append(str(txt))
                        results[wid] = "\n\n".join(comments)
                except Exception as e:
                    logger.exception("Error fetching comments for %s: %s", wid, e)
                    results[wid] = ""

        return results
    
    auth = aiohttp.BasicAuth("", pat)
    url = f"{org_url}/_apis/wit/workitems/{wi_id}?$expand=all&api-version={API_VERSION}"
    
    async with aiohttp.ClientSession(auth=auth) as session:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error("Get workitem failed: %s %s", resp.status, text)
                    return {}
                return await resp.json()
        except Exception as e:
            logger.exception("Error getting workitem %s: %s", wi_id, e)
            return {}


async def fetch_comments_async(
    org_url: str,
    ids: List[int],
    pat: Optional[str] = None
) -> Dict[int, str]:
    """
    Fetch comments for a list of work items asynchronously.

    Returns a mapping of work item id -> concatenated comment text.
    """
    pat = pat or _get_pat()
    if not pat:
        logger.error("No PAT available for ADO request")
        return {}

    auth = aiohttp.BasicAuth("", pat)
    results: Dict[int, str] = {}

    async with aiohttp.ClientSession(auth=auth) as session:
        for wid in ids:
            url = f"{org_url}/_apis/wit/workItems/{wid}/comments?api-version={API_VERSION}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        logger.debug("Comments fetch failed for %s: %s %s", wid, resp.status, text)
                        results[wid] = ""
                        continue
                    data = await resp.json()
                    comments = []
                    for c in data.get("comments", []):
                        txt = c.get("text") or c.get("content") or ""
                        if txt:
                            comments.append(str(txt))
                    results[wid] = "\n\n".join(comments)
            except Exception as e:
                logger.exception("Error fetching comments for %s: %s", wid, e)
                results[wid] = ""

    return results
