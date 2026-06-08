#!/usr/bin/env python3
"""Debug script to inspect ADO work item relations."""
import os
import sys
import base64
import requests
from pathlib import Path
from config import config

org = config.ado_org_url
project = config.ado_project
pat = config.ado_pat

print(f"Org: {org}")
print(f"Project: {project}")
print(f"PAT: {'***' + pat[-4:] if pat else 'NOT SET'}")

if not org or not pat:
    print("ERROR: ADO_ORG_URL and ADO_PAT must be set")
    sys.exit(1)

encoded = base64.b64encode(f':{pat}'.encode()).decode()
headers = {'Authorization': f'Basic {encoded}'}

# Query work items (WIQL uses $top parameter, not TOP in query)
wiql = 'SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project AND [System.ChangedDate] >= @today - 30 ORDER BY [System.ChangedDate] DESC'
url = f'{org}/{project}/_apis/wit/wiql?$top=100&api-version=7.1'
print(f"\nQuerying: {url}")

resp = requests.post(url, json={'query': wiql}, headers=headers, timeout=30)
print(f"Status: {resp.status_code}")

if resp.status_code != 200:
    print(f"Error: {resp.text}")
    sys.exit(1)

items = resp.json().get('workItems', [])
print(f"Found {len(items)} work items")

# Check first 20 for relations
found_with_relations = 0
found_with_commits = 0
all_rel_types = set()

for item in items[:100]:
    wid = item['id']
    wi_url = f'{org}/{project}/_apis/wit/workitems/{wid}?$expand=Relations&api-version=7.1'
    wi_resp = requests.get(wi_url, headers=headers, timeout=30)
    wi = wi_resp.json()
    rels = wi.get('relations', [])
    
    if rels:
        found_with_relations += 1
        
        for r in rels:
            rel_type = r.get('rel', '')
            url_val = r.get('url', '')
            all_rel_types.add(rel_type)
            
            # Check for commit links
            if 'ArtifactLink' in rel_type or 'commit' in url_val.lower() or 'vstfs' in url_val.lower():
                found_with_commits += 1
                print(f"\n=== WI {wid}: COMMIT FOUND ===")
                print(f"  Type: {rel_type}")
                print(f"  URL: {url_val}")
                attrs = r.get('attributes', {})
                print(f"  Attrs: {attrs}")

print(f"\n\n=== SUMMARY ===")
print(f"Total WIs checked: {min(len(items), 100)}")
print(f"WIs with any relations: {found_with_relations}")
print(f"Commit links found: {found_with_commits}")
print(f"\nAll relation types seen:")
for rt in sorted(all_rel_types):
    print(f"  - {rt}")
