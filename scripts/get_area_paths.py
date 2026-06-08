import os
import sys
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import config

ADO_ORG_URL = config.ado_org_url
PAT = config.ado_pat
PROJECT = 'FracPro-OPS'

if not PAT:
    raise SystemExit('ADO_PAT not set in config')

url = f"{ADO_ORG_URL}/{PROJECT}/_apis/wit/classificationnodes/areas?$depth=10&api-version=7.0"
print('Querying:', url)
resp = requests.get(url, auth=('', PAT), timeout=30)
if resp.status_code != 200:
    print('HTTP', resp.status_code, resp.text)
    raise SystemExit('Failed to fetch area nodes')

data = resp.json()

# Recursively traverse nodes to collect full paths
paths = []

def visit(node, prefix=''):
    name = node.get('name')
    full = f"{prefix}{name}" if prefix else name
    paths.append(full)
    for child in node.get('children', []):
        visit(child, full + '\\')

# Top-level nodes in 'value' or directly in 'children'
if 'value' in data:
    for node in data['value']:
        visit(node, '')
elif 'children' in data:
    for node in data['children']:
        visit(node, '')
else:
    print('No nodes found')

for p in paths:
    print(p)

print(f"Total area paths: {len(paths)}")
