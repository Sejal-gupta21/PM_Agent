#!/usr/bin/env python3
"""List all Ready work items from XOPS Bugs Enhancement backlog not in any sprint."""
import os
import sys
from pathlib import Path
import requests
import json
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load configuration
from config import config

def fetch_ready_backlog_items():
    """Fetch Ready work items not in any sprint from XOPS Bugs Enhancement"""
    
    org = config.ado_org_url.rstrip("/")
    project = config.ado_project
    pat = config.ado_pat
    
    if not pat:
        print("❌ ADO_PAT not found in config")
        return []
    
    # WIQL query for Ready items in XOPS Bugs Enhancement not in any sprint
    wiql = {
        "query": (
            f"SELECT [System.Id], [System.Title], [System.State], [System.AreaPath], "
            f"[System.IterationPath], [System.WorkItemType], [System.Tags], "
            f"[Microsoft.VSTS.Common.StackRank] "
            f"FROM workitems "
            f"WHERE [System.TeamProject] = '{project}' "
            f"AND [System.AreaPath] UNDER 'FracPro-OPS\\Global Management\\WTT Development\\XOPS Bugs Enhancement' "
            f"AND [System.State] = 'Ready' "
            f"AND ( [System.IterationPath] = '' OR [System.IterationPath] = '{project}' ) "
            f"ORDER BY [Microsoft.VSTS.Common.StackRank] ASC"
        )
    }
    
    # Execute WIQL
    wiql_url = f"{org}/{project}/_apis/wit/wiql?api-version=7.0"
    response = requests.post(wiql_url, auth=("", pat), json=wiql, timeout=60)
    
    if response.status_code != 200:
        print(f"❌ WIQL query failed: {response.status_code}")
        print(response.text)
        return []
    
    work_item_refs = response.json().get("workItems", [])
    ids = [item["id"] for item in work_item_refs]
    
    if not ids:
        print("ℹ️ No Ready work items found in backlog (not assigned to any sprint)")
        return []
    
    # Batch fetch full details
    batch_url = f"{org}/_apis/wit/workitemsbatch?api-version=7.0"
    payload = {
        "ids": ids,
        "fields": [
            "System.Id",
            "System.Title",
            "System.State",
            "System.AreaPath",
            "System.IterationPath",
            "System.WorkItemType",
            "System.Tags",
            "System.AssignedTo",
            "System.CreatedDate",
            "System.ChangedDate",
            "Microsoft.VSTS.Common.StackRank",
            "Microsoft.VSTS.Scheduling.StoryPoints",
            "Microsoft.VSTS.Common.Priority"
        ]
    }
    
    batch_response = requests.post(batch_url, auth=("", pat), json=payload, timeout=60)
    
    if batch_response.status_code != 200:
        print(f"❌ Batch fetch failed: {batch_response.status_code}")
        return []
    
    work_items = batch_response.json().get("value", [])
    
    # Format results
    results = []
    for wi in work_items:
        fields = wi.get("fields", {})
        assigned_to = fields.get("System.AssignedTo")
        if isinstance(assigned_to, dict):
            assigned_to = assigned_to.get("displayName", "Unassigned")
        else:
            assigned_to = assigned_to or "Unassigned"
        
        results.append({
            "id": fields.get("System.Id"),
            "title": fields.get("System.Title"),
            "type": fields.get("System.WorkItemType"),
            "state": fields.get("System.State"),
            "areaPath": fields.get("System.AreaPath"),
            "iterationPath": fields.get("System.IterationPath", "Not in sprint"),
            "assignedTo": assigned_to,
            "tags": fields.get("System.Tags", ""),
            "stackRank": fields.get("Microsoft.VSTS.Common.StackRank"),
            "storyPoints": fields.get("Microsoft.VSTS.Scheduling.StoryPoints"),
            "priority": fields.get("Microsoft.VSTS.Common.Priority"),
            "createdDate": fields.get("System.CreatedDate"),
            "changedDate": fields.get("System.ChangedDate")
        })
    
    return results

if __name__ == "__main__":
    print("=" * 80)
    print("READY BACKLOG ITEMS - XOPS Bugs Enhancement")
    print("(Not assigned to any sprint)")
    print("=" * 80)
    
    items = fetch_ready_backlog_items()
    
    if items:
        print(f"\n✅ Found {len(items)} Ready work items not assigned to any sprint:\n")
        
        for i, item in enumerate(items, 1):
            print(f"{i}. WI-{item['id']}: {item['title']}")
            print(f"   Type: {item['type']}")
            print(f"   State: {item['state']}")
            print(f"   Assigned To: {item['assignedTo']}")
            print(f"   Priority: {item['priority']}")
            print(f"   Story Points: {item['storyPoints']}")
            print(f"   Stack Rank: {item['stackRank']}")
            if item['tags']:
                print(f"   Tags: {item['tags']}")
            print(f"   Area: {item['areaPath']}")
            print(f"   Iteration: {item['iterationPath']}")
            print()
        
        # Save to JSON
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = ROOT / "outputs" / f"backlog_ready_items_{timestamp}.json"
        output_file.parent.mkdir(exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        
        print("=" * 80)
        print(f"💾 Results saved to: {output_file}")
        print("=" * 80)
    else:
        print("\nℹ️ No Ready work items found in the backlog that are not assigned to a sprint")
